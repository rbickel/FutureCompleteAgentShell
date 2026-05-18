import os
import sys
import traceback
import asyncio
import json
import tempfile
import urllib.error
import urllib.request
import re
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4
from dotenv import load_dotenv
from pydantic import Field

from microsoft_agents.hosting.core import (
    AgentApplication,
    TurnState,
    TurnContext,
    MemoryStorage,
)
from microsoft_agents.activity import (
    load_configuration_from_env,
    ActivityTypes,
)
from microsoft_agents.hosting.aiohttp import CloudAdapter
from microsoft_agents.authentication.msal import MsalConnectionManager

from agent_framework import (
    AgentSession,
    FunctionInvocationContext,
    MCPStreamableHTTPTool,
    function_middleware,
    tool,
)
from agent_framework.openai import OpenAIChatClient

from config import Config

MAX_DATASET_BYTES = 200 * 1024 * 1024
MAX_SAMPLE_ROWS = 5
SUPPORTED_DATASET_SUFFIXES = {".csv", ".xlsx", ".parquet"}
TRIAL_LIMITATION_TEXT = "Trial subscriptions only allow Backtest jobs through /v1/backtest. They do not allow Forecast jobs through /v1/prediction or Benchmark jobs through /v1/benchmark."
TERMINAL_JOB_STATUSES = {"completed", "complete", "succeeded", "success", "failed", "error", "cancelled", "canceled"}
DEBUG_TRIGGER_PATTERN = re.compile(r"\bdebug\b", re.IGNORECASE)


class FutureCompleteApiError(RuntimeError):
    def __init__(self, message: str, http_status: int, response_body: str, method: str, url: str, debug: dict[str, Any] | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.response_body = response_body
        self.method = method
        self.url = url
        self.debug = debug

    @property
    def problem(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.response_body) if self.response_body else {}
        except json.JSONDecodeError:
            parsed = {"detail": self.response_body}
        return parsed if isinstance(parsed, dict) else {"detail": str(parsed)}

load_dotenv()

# Load configuration
config = Config(os.environ)
agents_sdk_config = load_configuration_from_env(dict(os.environ))

system_prompt = (
    Path(__file__).parent / "agent.md"
).read_text(encoding="utf-8")
#create a tool that has access to the context


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _debug_enabled(session_id: str | None) -> bool:
    return bool(session_id and session_id in _debug_sessions)


def _sanitize_headers(headers: Any) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in dict(headers or {}).items():
        sanitized[str(key)] = str(value)
    return sanitized


def _sanitize_body_for_debug(body: Any) -> Any:
    if isinstance(body, dict):
        sanitized: dict[str, Any] = {}
        for key, value in body.items():
            if key == "data" and isinstance(value, dict):
                if {"index", "columns", "data"}.issubset(value.keys()):
                    columns = value.get("columns") if isinstance(value.get("columns"), list) else []
                    rows = value.get("data") if isinstance(value.get("data"), list) else []
                    sanitized[key] = {
                        "omitted": "dataset payload omitted from debug output",
                        "format": "pandas_split",
                        "columns": columns,
                        "column_count": len(columns),
                        "row_count": len(rows),
                    }
                    continue
                sanitized[key] = {
                    "omitted": "dataset payload omitted from debug output",
                    "format": "column_mapping",
                    "columns": list(value.keys()),
                    "column_count": len(value),
                }
            else:
                sanitized[key] = _sanitize_body_for_debug(value)
        return sanitized
    if isinstance(body, list):
        return [_sanitize_body_for_debug(item) for item in body]
    return body


def _parse_json_or_text(raw_body: str) -> Any:
    if not raw_body:
        return None
    try:
        return json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body


def _debug_exchange(
    method: str,
    url: str,
    request_headers: dict[str, str],
    request_body: Any,
    response_status: int | None,
    response_headers: Any,
    response_body: Any,
) -> dict[str, Any]:
    return {
        "request": {
            "method": method,
            "url": url,
            "headers": _sanitize_headers(request_headers),
            "body": _sanitize_body_for_debug(request_body),
        },
        "response": {
            "status": response_status,
            "headers": _sanitize_headers(response_headers),
            "body": _sanitize_body_for_debug(response_body),
        },
    }


def _futurecomplete_api_error(
    prefix: str,
    error: urllib.error.HTTPError,
    method: str,
    url: str,
    request_headers: dict[str, str] | None = None,
    request_body: Any = None,
    include_debug: bool = False,
) -> FutureCompleteApiError:
    error_body = error.read().decode("utf-8", errors="replace")
    problem = {}
    try:
        parsed = json.loads(error_body) if error_body else {}
        if isinstance(parsed, dict):
            problem = parsed
    except json.JSONDecodeError:
        problem = {}
    code = problem.get("code") or problem.get("statusCode")
    detail = problem.get("detail") or problem.get("message") or problem.get("title") or error.reason
    message = f"{prefix} returned HTTP {error.code}"
    if code:
        message += f" ({code})"
    if detail:
        message += f": {detail}"
    debug = None
    if include_debug:
        debug = _debug_exchange(
            method,
            url,
            request_headers or {},
            request_body,
            error.code,
            dict(error.headers.items()) if error.headers else {},
            _parse_json_or_text(error_body),
        )
    return FutureCompleteApiError(message, error.code, error_body, method, url, debug=debug)


def _futurecomplete_error_payload(error: FutureCompleteApiError) -> dict[str, Any]:
    problem = error.problem
    payload = {
        "ok": False,
        "error": str(error),
        "http_status": error.http_status,
        "error_code": problem.get("code") or problem.get("statusCode"),
        "error_title": problem.get("title"),
        "error_detail": problem.get("detail") or problem.get("message"),
        "error_context": problem.get("context"),
        "errors": problem.get("errors"),
    }
    if error.debug:
        payload["debug"] = error.debug
    return payload


def _get_session_id(context: FunctionInvocationContext) -> str | None:
    session = context.session
    return session.session_id if session is not None else None


def _get_session_identity(context: FunctionInvocationContext) -> dict[str, str | None]:
    identity = context.metadata.get("user_identity") if context.metadata else None
    return identity or {}


def _dashboard_link(session_id: str) -> str:
    return f"{config.futurecomplete_dashboard_url.rstrip('/')}/jobs/{session_id}"


def _looks_like_email(value: str | None) -> bool:
    return bool(value and "@" in value and "." in value.rsplit("@", 1)[-1])


def _extract_user_email(identity: dict[str, str | None]) -> str | None:
    candidates = [
        identity.get("email"),
        identity.get("user_principal_name"),
        identity.get("user_id"),
        identity.get("user_name"),
    ]
    for candidate in candidates:
        if _looks_like_email(candidate):
            return candidate
    return None


def _extract_subscription_key(body: dict[str, Any]) -> str | None:
    subscription = body.get("apim_subscription") or body.get("subscription") or {}
    return (
        subscription.get("primary_key")
        or subscription.get("primaryKey")
        or subscription.get("secondary_key")
        or subscription.get("secondaryKey")
        or body.get("api_key")
        or body.get("apiKey")
        or body.get("key")
    )


def _subscription_cache_key(identity: dict[str, str | None], user_email: str | None = None) -> str:
    return identity.get("aad_object_id") or user_email or _extract_user_email(identity) or identity.get("user_id") or "anonymous"


def _get_session_subscription(session_id: str | None, identity: dict[str, str | None]) -> dict[str, Any] | None:
    if session_id:
        cache_key = _session_subscription_keys.get(session_id)
        if cache_key:
            subscription = _subscriptions_by_user.get(cache_key)
            if subscription:
                return subscription
    return _subscriptions_by_user.get(_subscription_cache_key(identity))


def _create_trial_subscription(identity: dict[str, str | None], user_email: str | None = None, session_id: str | None = None) -> dict[str, Any]:
    resolved_email = user_email if _looks_like_email(user_email) else _extract_user_email(identity)
    if not resolved_email:
        raise RuntimeError("A work email is required to create a self-service trial subscription.")
    if not user_email or not config.futurecomplete_trial_users_url:
        user_email = resolved_email

    request_body = {
        "user_email": resolved_email,
        "plan_id": config.futurecomplete_trial_plan_id,
    }
    request_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    request = urllib.request.Request(
        config.futurecomplete_trial_users_url,
        data=json.dumps(request_body).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw_body = response.read().decode("utf-8")
            body = json.loads(raw_body) if raw_body else {}
            debug = None
            if _debug_enabled(session_id):
                debug = _debug_exchange(
                    "POST",
                    config.futurecomplete_trial_users_url,
                    request_headers,
                    request_body,
                    response.status,
                    dict(response.headers.items()),
                    body,
                )
    except urllib.error.HTTPError as error:
        raise _futurecomplete_api_error(
            "FutureComplete trial license request",
            error,
            "POST",
            config.futurecomplete_trial_users_url,
            request_headers,
            request_body,
            include_debug=_debug_enabled(session_id),
        ) from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"FutureComplete trial license request failed: {error}") from error

    subscription_key = _extract_subscription_key(body)
    if not subscription_key:
        raise RuntimeError("FutureComplete trial license response did not include an APIM subscription key.")
    subscription: dict[str, Any] = {
        "cache_key": _subscription_cache_key(identity, resolved_email),
        "user_email": resolved_email,
        "plan_id": str((body.get("user") or {}).get("plan_id") or config.futurecomplete_trial_plan_id),
        "subscription_key": subscription_key,
        "source": "self-service-trial",
        "created_at": _now_iso(),
        "limitations": TRIAL_LIMITATION_TEXT,
    }
    if debug:
        subscription["debug"] = debug
    return subscription


def _futurecomplete_headers(identity: dict[str, str | None], session_id: str | None, required_capability: str) -> dict[str, str]:
    subscription = _get_session_subscription(session_id, identity)
    if not subscription:
        raise RuntimeError(
            "No FutureComplete subscription is active in this session. Ask the user to provide an existing license "
            "or explicitly approve a self-service trial subscription. Trial subscriptions only support Backtest."
        )
    if subscription.get("plan_id") == "trial" and required_capability != "backtest":
        raise RuntimeError(TRIAL_LIMITATION_TEXT)
    return {
        "Ocp-Apim-Subscription-Key": str(subscription["subscription_key"]),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _post_futurecomplete(path: str, payload: dict[str, Any], identity: dict[str, str | None], session_id: str | None, required_capability: str) -> dict[str, Any]:
    url = f"{config.futurecomplete_api_base_url.rstrip('/')}{path}"
    headers = _futurecomplete_headers(identity, session_id, required_capability)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw_body = response.read().decode("utf-8")
            parsed_body = _parse_json_or_text(raw_body)
            body = parsed_body if isinstance(parsed_body, dict) else {"body": parsed_body}
            location = response.headers.get("Location")
            if location:
                body["location"] = location
            if _debug_enabled(session_id):
                body["_debug"] = _debug_exchange(
                    "POST",
                    url,
                    headers,
                    payload,
                    response.status,
                    dict(response.headers.items()),
                    parsed_body,
                )
            return body
    except urllib.error.HTTPError as error:
        raise _futurecomplete_api_error("FutureComplete API", error, "POST", url, headers, payload, include_debug=_debug_enabled(session_id)) from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"FutureComplete API request failed: {error}") from error


def _post_futurecomplete_without_body(path: str, identity: dict[str, str | None], session_id: str | None, required_capability: str) -> dict[str, Any]:
    url = f"{config.futurecomplete_api_base_url.rstrip('/')}{path}"
    headers = _futurecomplete_headers(identity, session_id, required_capability)
    headers.pop("Content-Type", None)
    request = urllib.request.Request(url, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw_body = response.read().decode("utf-8")
            parsed_body = _parse_json_or_text(raw_body)
            body = parsed_body if isinstance(parsed_body, dict) else {"body": parsed_body}
            if _debug_enabled(session_id):
                body["_debug"] = _debug_exchange(
                    "POST",
                    url,
                    headers,
                    None,
                    response.status,
                    dict(response.headers.items()),
                    parsed_body,
                )
            return body
    except urllib.error.HTTPError as error:
        raise _futurecomplete_api_error("FutureComplete API", error, "POST", url, headers, None, include_debug=_debug_enabled(session_id)) from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"FutureComplete API request failed: {error}") from error


def _get_futurecomplete(path: str, identity: dict[str, str | None], session_id: str | None, required_capability: str) -> dict[str, Any]:
    url = f"{config.futurecomplete_api_base_url.rstrip('/')}{path}"
    headers = _futurecomplete_headers(identity, session_id, required_capability)
    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw_body = response.read().decode("utf-8")
            parsed_body = _parse_json_or_text(raw_body)
            body = parsed_body if isinstance(parsed_body, dict) else {"body": parsed_body}
            if _debug_enabled(session_id):
                body["_debug"] = _debug_exchange(
                    "GET",
                    url,
                    headers,
                    None,
                    response.status,
                    dict(response.headers.items()),
                    parsed_body,
                )
            return body
    except urllib.error.HTTPError as error:
        raise _futurecomplete_api_error("FutureComplete status API", error, "GET", url, headers, None, include_debug=_debug_enabled(session_id)) from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"FutureComplete status request failed: {error}") from error


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _normalize_prediction_intervals(value: str | None) -> str | None:
    if value is None:
        return None
    intervals: list[str] = []
    for item in str(value).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            numeric = float(item)
        except ValueError:
            intervals.append(item)
            continue
        if 0 < numeric <= 1:
            numeric *= 100
        intervals.append(str(int(numeric)) if numeric.is_integer() else str(numeric))
    return ",".join(intervals) if intervals else None


def _find_attachment(session_id: str | None, file_reference: str | None) -> dict[str, Any] | None:
    if not session_id:
        return None
    attachments = _session_attachments.get(session_id, [])
    if not attachments:
        return None
    if not file_reference:
        return attachments[-1]
    normalized = file_reference.lower()
    for attachment in reversed(attachments):
        candidates = [
            attachment.get("id"),
            attachment.get("name"),
            attachment.get("content_url"),
        ]
        if any(candidate and normalized in str(candidate).lower() for candidate in candidates):
            return attachment
    return None


def _download_file(url: str, suffix: str) -> Path:
    request = urllib.request.Request(url, headers={"User-Agent": "FutureCompleteAgent/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_DATASET_BYTES:
            raise ValueError("Dataset exceeds the 200 MB limit.")
        data = response.read(MAX_DATASET_BYTES + 1)
    if len(data) > MAX_DATASET_BYTES:
        raise ValueError("Dataset exceeds the 200 MB limit.")
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    with handle:
        handle.write(data)
    return Path(handle.name)


def _resolve_dataset_path(context: FunctionInvocationContext, file_reference: str | None) -> tuple[Path, str]:
    if file_reference:
        raw_candidate = Path(file_reference).expanduser()
        search_candidates = [
            raw_candidate,
            Path.cwd() / raw_candidate,
            Path(__file__).resolve().parent / raw_candidate,
            Path(__file__).resolve().parent.parent / raw_candidate,
        ]
        for candidate in search_candidates:
            if candidate.exists():
                return candidate.resolve(), str(candidate)

    session_id = _get_session_id(context)
    attachment = _find_attachment(session_id, file_reference)
    if attachment is not None:
        name = attachment.get("name") or "dataset"
        suffix = Path(name).suffix.lower()
        content_url = attachment.get("content_url")
        if not content_url:
            raise ValueError("The selected attachment does not expose a downloadable content URL.")
        return _download_file(content_url, suffix), name

    if file_reference and file_reference.startswith(("http://", "https://")):
        suffix = Path(file_reference.split("?", 1)[0]).suffix.lower()
        return _download_file(file_reference, suffix), file_reference

    raise ValueError("No dataset file was found. Upload a CSV, XLSX, or Parquet file, or provide a local path or URL.")


def _read_dataset_frame(path: Path, row_limit: int | None = None):
    try:
        import pandas as pd
    except ImportError as error:
        raise RuntimeError("Dataset handling requires pandas. Install dependencies from src/requirements.txt.") from error

    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path, nrows=row_limit)
        if len(frame.columns) > 0:
            first_column = frame.columns[0]
            first_column_name = str(first_column)
            first_column_values = frame[first_column]
            parsed_index = pd.to_datetime(
                first_column_values,
                errors="coerce",
                dayfirst=first_column_values.astype(str).str.match(r"^\d{1,2}\.\d{1,2}\.\d{4}").mean() >= 0.8,
            )
            should_use_as_index = first_column_name.startswith("Unnamed:") or first_column_name.lower() in {"date", "timestamp", "time", "ds"}
        else:
            should_use_as_index = False

        if should_use_as_index:
            index_name = "date" if first_column_name.startswith("Unnamed:") else first_column_name
            if parsed_index.notna().mean() >= 0.8:
                frame = frame.drop(columns=[first_column])
                frame.index = parsed_index.dt.strftime("%Y-%m-%d")
                frame.index.name = index_name
            else:
                frame = frame.rename(columns={first_column: index_name})
    elif suffix == ".xlsx":
        frame = pd.read_excel(path, nrows=row_limit)
    else:
        frame = pd.read_parquet(path)
        if row_limit is not None:
            frame = frame.head(row_limit)
    return frame


def _inspect_dataset_file(path: Path, source_name: str) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"Dataset file does not exist: {path}")
    if path.stat().st_size > MAX_DATASET_BYTES:
        raise ValueError("Dataset exceeds the 200 MB limit.")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_DATASET_SUFFIXES:
        raise ValueError("Unsupported dataset type. Use .csv, .xlsx, or .parquet.")

    if suffix == ".csv":
        frame = _read_dataset_frame(path, row_limit=100)
        row_count = sum(1 for _ in path.open("rb")) - 1
    elif suffix == ".xlsx":
        frame = _read_dataset_frame(path, row_limit=100)
        row_count = None
    else:
        frame = _read_dataset_frame(path)
        row_count = len(frame)
        frame = frame.head(100)

    columns = [str(column) for column in frame.columns]
    dtypes = {str(column): str(dtype) for column, dtype in frame.dtypes.items()}
    numeric_columns = [column for column in columns if str(frame[column].dtype).startswith(("int", "float"))]
    datetime_like_columns = [column for column in columns if "datetime" in str(frame[column].dtype)]
    suggested_target_columns = [column for column in numeric_columns if "__" not in column] or numeric_columns
    sample_preview_json = frame.head(MAX_SAMPLE_ROWS).astype(object).where(frame.notna(), None).to_json(orient="records", date_format="iso") or "[]"
    sample_preview = json.loads(sample_preview_json)

    return {
        "source": source_name,
        "file_type": suffix.removeprefix("."),
        "row_count": row_count,
        "sampled_rows": len(frame),
        "sample_preview_rows": min(len(frame), MAX_SAMPLE_ROWS),
        "sample_preview": sample_preview,
        "columns": columns,
        "dtypes": dtypes,
        "index_name": frame.index.name,
        "numeric_columns": numeric_columns,
        "datetime_like_columns": datetime_like_columns,
        "suggested_target_columns": suggested_target_columns,
        "default_target_column": suggested_target_columns[0] if suggested_target_columns else (columns[0] if columns else None),
    }


def _load_dataset_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"Dataset file does not exist: {path}")
    if path.stat().st_size > MAX_DATASET_BYTES:
        raise ValueError("Dataset exceeds the 200 MB limit.")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_DATASET_SUFFIXES:
        raise ValueError("Unsupported dataset type. Use .csv, .xlsx, or .parquet.")

    frame = _read_dataset_frame(path)

    dataset_json = frame.to_json(orient="split", date_format="iso") or "{}"
    return json.loads(dataset_json)


def _operation_arguments(
    operation_type: str,
    target_columns: str,
    horizon: int,
    prediction_intervals: str,
    feature_columns: str | None,
    run_explain: bool,
    prediction_stride: int = 1,
    include_genai_summary: bool = False,
    backtest_size: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    arguments = {
        "operation_type": operation_type,
        "forecasting_horizon": horizon,
        "targets": target_columns,
        "features": feature_columns,
        "prediction_interval_levels": _normalize_prediction_intervals(prediction_intervals),
        "prediction_stride": prediction_stride,
        "end_date": end_date,
        "run_explain": run_explain,
        "include_genai_summary": include_genai_summary,
    }
    if operation_type == "backtest":
        arguments["backtest_size"] = backtest_size
        arguments["start_date"] = start_date
    return {key: value for key, value in arguments.items() if value is not None}


def _public_operation_payload(
    operation_type: str,
    data: dict[str, Any],
    operation_arguments: dict[str, Any],
    background: bool,
) -> dict[str, Any]:
    return {
        "data": data,
        "config": {
            "operation": operation_type,
            "operation_arguments": operation_arguments,
        },
        "background": background,
    }


def _summarize_public_payload(payload: dict[str, Any], source_name: str) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("columns"), list):
        data_columns = data["columns"]
        data_format = "pandas_split"
    elif isinstance(data, dict):
        data_columns = list(data.keys())
        data_format = "column_mapping"
    else:
        data_columns = []
        data_format = None
    return {
        "data_source": source_name,
        "data_format": data_format,
        "data_columns": data_columns,
        "config": payload.get("config"),
        "background": payload.get("background"),
    }


def _response_payload(response: dict[str, Any]) -> dict[str, Any]:
    nested = response.get("response")
    return nested if isinstance(nested, dict) else response


def _response_session_id(response: dict[str, Any]) -> str | None:
    payload = _response_payload(response)
    direct_session_id = (
        payload.get("session_id")
        or payload.get("sessionId")
        or response.get("session_id")
        or response.get("sessionId")
        or response.get("job_id")
        or response.get("jobId")
        or response.get("id")
    )
    if direct_session_id:
        return str(direct_session_id)
    location = response.get("location") or response.get("Location")
    if not location:
        return None
    path_parts = [part for part in urlparse(str(location)).path.split("/") if part]
    if not path_parts:
        return None
    if "sessions" in path_parts:
        session_index = path_parts.index("sessions") + 1
        if session_index < len(path_parts):
            return path_parts[session_index]
    return path_parts[-2] if len(path_parts) >= 2 and path_parts[-1] in {"status", "result", "cancel"} else path_parts[-1]


def _summarize_api_response(response: dict[str, Any]) -> dict[str, Any]:
    payload = _response_payload(response)
    summary = {
        "status": response.get("status"),
        "location": response.get("location") or response.get("Location"),
        "error_details": response.get("error_details"),
        "response": {
            "operation_type": payload.get("operation_type"),
            "session_id": payload.get("session_id"),
            "resource_id": payload.get("resource_id"),
            "dataset_resource_id": payload.get("dataset_resource_id"),
        },
    }
    data = payload.get("data")
    if isinstance(data, dict):
        summary["response"]["data_keys"] = list(data.keys())
        summary["response"]["data_inline"] = bool(data)
    return summary


def _take_debug(response: dict[str, Any]) -> dict[str, Any] | None:
    debug = response.pop("_debug", None)
    return debug if isinstance(debug, dict) else None


def _add_debug(payload: dict[str, Any], debug: dict[str, Any] | None) -> dict[str, Any]:
    if debug:
        payload["debug"] = debug
    return payload


def _remember_job(context: FunctionInvocationContext, job_type: str, request_summary: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    session_id = _get_session_id(context) or "unknown-session"
    subscription = _get_session_subscription(session_id, _get_session_identity(context))
    job_session_id = _response_session_id(response) or str(uuid4())
    status = str(response.get("status") or "running").lower()
    job = {
        "id": str(job_session_id),
        "type": job_type,
        "status": status,
        "created_at": _now_iso(),
        "dashboard_url": _dashboard_link(str(job_session_id)),
        "polling_started": False,
        "subscription_plan_id": subscription.get("plan_id") if subscription else None,
        "request": request_summary,
        "response": _summarize_api_response(response),
    }
    _jobs_by_session.setdefault(session_id, []).append(job)
    return job


def _extract_job_status(response: dict[str, Any]) -> str | None:
    for key in ("status", "state", "job_status", "jobStatus"):
        value = response.get(key)
        if value:
            return str(value).lower()
    job = response.get("job")
    if isinstance(job, dict):
        return _extract_job_status(job)
    return None


def _job_status_path(job_id: str) -> str:
    return config.futurecomplete_job_status_path_template.format(session_id=job_id, job_id=job_id)


def _job_result_path(job_id: str) -> str:
    return config.futurecomplete_job_result_path_template.format(session_id=job_id, job_id=job_id)


def _job_cancel_path(job_id: str) -> str:
    return config.futurecomplete_job_cancel_path_template.format(session_id=job_id, job_id=job_id)


def _job_capability(job_type: str | None) -> str:
    if job_type == "backtest":
        return "backtest"
    if job_type == "benchmark":
        return "benchmark"
    return "forecast"


async def _poll_job_and_notify(context: TurnContext, session_id: str, job: dict[str, Any]):
    identity = _session_users.get(session_id, {})
    capability = _job_capability(job.get("type"))
    job_id = str(job["id"])
    for _ in range(config.futurecomplete_poll_max_attempts):
        await asyncio.sleep(config.futurecomplete_poll_interval_seconds)
        try:
            response = await asyncio.to_thread(_get_futurecomplete, _job_status_path(job_id), identity, session_id, capability)
            status = _extract_job_status(response)
            if status:
                job["status"] = "completed" if status in {"complete", "succeeded", "success"} else status
            job["last_status_response"] = _summarize_api_response(response)
            job["updated_at"] = _now_iso()
            if status in TERMINAL_JOB_STATUSES:
                label = "completed" if job["status"] in {"complete", "succeeded", "success"} else job["status"]
                await context.send_activity(
                    f"Your FutureComplete {job.get('type')} job {job_id} is {label}. Inspect results: {job['dashboard_url']}"
                )
                return
        except Exception as error:
            job["last_poll_error"] = str(error)
            job["updated_at"] = _now_iso()


def _schedule_job_polling(context: TurnContext, session_id: str, job: dict[str, Any]):
    if job.get("polling_started") or job.get("status") in TERMINAL_JOB_STATUSES:
        return
    job["polling_started"] = True
    task = asyncio.create_task(_poll_job_and_notify(context, session_id, job))
    _polling_tasks.add(task)
    task.add_done_callback(_polling_tasks.discard)


@tool(approval_mode="never_require")
def get_day_of_week(context: FunctionInvocationContext) -> Annotated[str, Field(description="Today's day of the week (e.g. 'Monday').")]:
    """Return the current day of the week from the host OS clock."""
    identity = _get_session_identity(context)
    caller = identity.get("user_name") or identity.get("user_id") or "unknown user"
    return datetime.now().strftime("%A") + f" (called by {caller})"


@tool(approval_mode="never_require")
def inspect_dataset(
    context: FunctionInvocationContext,
    file_reference: Annotated[str | None, Field(description="Optional file name, URL, or local path for the uploaded CSV, XLSX, or Parquet dataset.")] = None,
) -> Annotated[str, Field(description="Dataset columns, data types, row count, and default target column.")]:
    """Inspect an uploaded or referenced dataset and return schema metadata."""
    try:
        path, source_name = _resolve_dataset_path(context, file_reference)
        result = _inspect_dataset_file(path, source_name)
        session_id = _get_session_id(context)
        if session_id:
            _dataset_schemas_by_session[session_id] = result
        return _json({"ok": True, "dataset": result})
    except Exception as error:
        return _json({"ok": False, "error": str(error)})


@tool(approval_mode="never_require")
def check_license_status(context: FunctionInvocationContext) -> Annotated[str, Field(description="Check whether this conversation already has a FutureComplete subscription available.")]:
    """Return whether the current session has a remembered FutureComplete subscription."""
    session_id = _get_session_id(context)
    identity = _get_session_identity(context)
    subscription = _get_session_subscription(session_id, identity)
    if not subscription:
        return _json(
            {
                "ok": True,
                "has_subscription": False,
                "trial_available": True,
                "trial_limitations": TRIAL_LIMITATION_TEXT,
            }
        )
    return _json(
        {
            "ok": True,
            "has_subscription": True,
            "plan_id": subscription.get("plan_id"),
            "source": subscription.get("source"),
            "user_email": subscription.get("user_email"),
            "allowed_workflows": ["backtest"] if subscription.get("plan_id") == "trial" else ["forecast", "backtest", "benchmark"],
            "trial_limitations": TRIAL_LIMITATION_TEXT if subscription.get("plan_id") == "trial" else None,
        }
    )


@tool(approval_mode="never_require")
def get_trial_subscription(
    context: FunctionInvocationContext,
    accepted_trial_limitations: Annotated[bool, Field(description="True only after the user explicitly accepts that trial subscriptions allow Backtest only and not Forecast.")],
    user_email: Annotated[str | None, Field(description="Optional work email to use if Teams identity did not provide one.")] = None,
) -> Annotated[str, Field(description="Create or look up a self-service trial subscription for the current user after explicit user approval.")]:
    """Provision or look up a self-service trial subscription after explicit user approval."""
    try:
        if not accepted_trial_limitations:
            raise ValueError(TRIAL_LIMITATION_TEXT + " Ask the user to explicitly accept these limitations before calling this tool.")
        session_id = _get_session_id(context)
        identity = _get_session_identity(context)
        subscription = _create_trial_subscription(identity, user_email, session_id=session_id)
        cache_key = str(subscription["cache_key"])
        _subscriptions_by_user[cache_key] = subscription
        if session_id:
            _session_subscription_keys[session_id] = cache_key
        result = {
            "ok": True,
            "subscription": {
                "plan_id": subscription["plan_id"],
                "source": subscription["source"],
                "user_email": subscription["user_email"],
                "subscription_key": subscription["subscription_key"],
                "allowed_workflows": ["backtest"],
                "limitations": TRIAL_LIMITATION_TEXT,
            },
        }
        return _json(_add_debug(result, subscription.get("debug")))
    except FutureCompleteApiError as error:
        payload = _futurecomplete_error_payload(error)
        payload["trial_limitations"] = TRIAL_LIMITATION_TEXT
        return _json(payload)
    except Exception as error:
        return _json({"ok": False, "error": str(error), "trial_limitations": TRIAL_LIMITATION_TEXT})


@tool(approval_mode="never_require")
def submit_forecast(
    context: FunctionInvocationContext,
    target_columns: Annotated[str, Field(description="Comma-separated target columns for the forecast.")],
    horizon: Annotated[int, Field(description="Forecast horizon.")],
    prediction_intervals: Annotated[str, Field(description="Comma-separated prediction interval confidence bands, such as 0.8,0.95.")],
    feature_columns: Annotated[str | None, Field(description="Optional comma-separated feature or driver columns.")] = None,
    run_explain: Annotated[bool, Field(description="Whether to generate an explainability report.")] = False,
    dataset_reference: Annotated[str | None, Field(description="Optional uploaded file name, URL, or backend dataset reference.")] = None,
) -> Annotated[str, Field(description="Submit a FutureComplete forecast job and return status plus dashboard link.")]:
    """Submit a forecast job to FutureComplete's prediction endpoint."""
    try:
        path, source_name = _resolve_dataset_path(context, dataset_reference)
        data = _load_dataset_data(path)
        arguments = _operation_arguments("forecast", target_columns, horizon, prediction_intervals, feature_columns, run_explain)
        payload = _public_operation_payload("forecast", data, arguments, background=True)
        request_summary = _summarize_public_payload(payload, source_name)
        session_id = _get_session_id(context)
        response = _post_futurecomplete("/v1/prediction", payload, _get_session_identity(context), session_id, "forecast")
        debug = _take_debug(response)
        job = _remember_job(context, "forecast", request_summary, response)
        return _json(_add_debug({"ok": True, "job": job}, debug))
    except FutureCompleteApiError as error:
        return _json(_futurecomplete_error_payload(error))
    except Exception as error:
        return _json({"ok": False, "error": str(error)})


@tool(approval_mode="never_require")
def submit_backtest(
    context: FunctionInvocationContext,
    target_columns: Annotated[str, Field(description="Comma-separated target columns for the backtest.")],
    horizon: Annotated[int, Field(description="Forecast horizon used inside the backtest.")],
    prediction_stride: Annotated[int, Field(description="Backtest refresh cadence. Must be a positive integer; 1 is valid.")],
    prediction_intervals: Annotated[str, Field(description="Comma-separated prediction interval confidence bands, such as 0.8,0.95.")],
    feature_columns: Annotated[str | None, Field(description="Optional comma-separated feature or driver columns.")] = None,
    run_explain: Annotated[bool, Field(description="Whether to generate an explainability report.")] = False,
    backtest_size: Annotated[int | None, Field(description="Optional backtest window size. Mutually exclusive with start/end dates.")] = None,
    backtest_start_date: Annotated[str | None, Field(description="Optional backtest start date.")] = None,
    backtest_end_date: Annotated[str | None, Field(description="Optional backtest end date.")] = None,
    dataset_reference: Annotated[str | None, Field(description="Optional uploaded file name, URL, or backend dataset reference.")] = None,
) -> Annotated[str, Field(description="Submit a FutureComplete backtest job and return status plus dashboard link.")]:
    """Submit a backtest job to FutureComplete's public backtest endpoint."""
    try:
        if prediction_stride < 1:
            raise ValueError("prediction_stride must be a positive integer.")
        has_size = backtest_size is not None
        has_dates = bool(backtest_start_date or backtest_end_date)
        if has_size and has_dates:
            raise ValueError("Use either backtest_size or a start/end date range, not both.")
        if has_dates and not (backtest_start_date and backtest_end_date):
            raise ValueError("Provide both backtest_start_date and backtest_end_date.")

        path, source_name = _resolve_dataset_path(context, dataset_reference)
        data = _load_dataset_data(path)
        arguments = _operation_arguments(
            "backtest",
            target_columns,
            horizon,
            prediction_intervals,
            feature_columns,
            run_explain,
            prediction_stride=prediction_stride,
            backtest_size=backtest_size,
            start_date=backtest_start_date,
            end_date=backtest_end_date,
        )
        payload = _public_operation_payload("backtest", data, arguments, background=True)
        request_summary = _summarize_public_payload(payload, source_name)
        session_id = _get_session_id(context)
        response = _post_futurecomplete("/v1/backtest", payload, _get_session_identity(context), session_id, "backtest")
        debug = _take_debug(response)
        job = _remember_job(context, "backtest", request_summary, response)
        return _json(_add_debug({"ok": True, "job": job}, debug))
    except FutureCompleteApiError as error:
        return _json(_futurecomplete_error_payload(error))
    except Exception as error:
        return _json({"ok": False, "error": str(error)})


@tool(approval_mode="never_require")
def submit_benchmark(
    context: FunctionInvocationContext,
    target_columns: Annotated[str, Field(description="Comma-separated target columns for the benchmark backtest configuration.")],
    horizon: Annotated[int, Field(description="Forecast horizon used inside the benchmark backtest configuration.")],
    prediction_stride: Annotated[int, Field(description="Backtest refresh cadence. Must be a positive integer; 1 is valid.")],
    prediction_intervals: Annotated[str, Field(description="Comma-separated prediction interval confidence bands, such as 0.8,0.95.")],
    feature_columns: Annotated[str | None, Field(description="Optional comma-separated feature or driver columns.")] = None,
    run_explain: Annotated[bool, Field(description="Whether to generate an explainability report.")] = False,
    backtest_size: Annotated[int | None, Field(description="Optional backtest window size. Mutually exclusive with start/end dates.")] = None,
    backtest_start_date: Annotated[str | None, Field(description="Optional backtest start date.")] = None,
    backtest_end_date: Annotated[str | None, Field(description="Optional backtest end date.")] = None,
    dataset_reference: Annotated[str | None, Field(description="Optional uploaded file name, URL, or backend dataset reference.")] = None,
) -> Annotated[str, Field(description="Submit a FutureComplete benchmark job for multi-model comparison.")]:
    """Submit a benchmark job to FutureComplete's public benchmark endpoint."""
    try:
        if prediction_stride < 1:
            raise ValueError("prediction_stride must be a positive integer.")
        has_size = backtest_size is not None
        has_dates = bool(backtest_start_date or backtest_end_date)
        if has_size and has_dates:
            raise ValueError("Use either backtest_size or a start/end date range, not both.")
        if has_dates and not (backtest_start_date and backtest_end_date):
            raise ValueError("Provide both backtest_start_date and backtest_end_date.")

        path, source_name = _resolve_dataset_path(context, dataset_reference)
        data = _load_dataset_data(path)
        backtest_config = _operation_arguments(
            "backtest",
            target_columns,
            horizon,
            prediction_intervals,
            feature_columns,
            run_explain,
            prediction_stride=prediction_stride,
            backtest_size=backtest_size,
            start_date=backtest_start_date,
            end_date=backtest_end_date,
        )
        arguments = {
            "operation_type": "benchmark",
            "backtest_config": backtest_config,
        }
        payload = _public_operation_payload("benchmark", data, arguments, background=True)
        request_summary = _summarize_public_payload(payload, source_name)
        session_id = _get_session_id(context)
        response = _post_futurecomplete("/v1/benchmark", payload, _get_session_identity(context), session_id, "benchmark")
        debug = _take_debug(response)
        job = _remember_job(context, "benchmark", request_summary, response)
        return _json(_add_debug({"ok": True, "job": job}, debug))
    except FutureCompleteApiError as error:
        return _json(_futurecomplete_error_payload(error))
    except Exception as error:
        return _json({"ok": False, "error": str(error)})


@tool(approval_mode="never_require")
def cancel_job(
    context: FunctionInvocationContext,
    session_id: Annotated[str, Field(description="FutureComplete session ID/job ID to cancel.")],
) -> Annotated[str, Field(description="Cancel a running FutureComplete job by session ID.")]:
    """Cancel a running FutureComplete background job."""
    try:
        current_session_id = _get_session_id(context)
        identity = _get_session_identity(context)
        jobs = _jobs_by_session.get(current_session_id or "", [])
        matching_job = next((job for job in jobs if str(job.get("id")) == session_id), None)
        capability = "backtest"
        response = _post_futurecomplete_without_body(_job_cancel_path(session_id), identity, current_session_id, capability)
        debug = _take_debug(response)
        if matching_job is not None:
            matching_job["status"] = "cancelled" if response.get("status") == "success" else str(response.get("status") or "cancel_requested")
            matching_job["cancel_response"] = response
            matching_job["updated_at"] = _now_iso()
        return _json(_add_debug({"ok": True, "session_id": session_id, "response": response}, debug))
    except FutureCompleteApiError as error:
        return _json(_futurecomplete_error_payload(error))
    except Exception as error:
        return _json({"ok": False, "error": str(error)})


@tool(approval_mode="never_require")
def list_jobs(context: FunctionInvocationContext) -> Annotated[str, Field(description="List FutureComplete jobs remembered in this conversation.")]:
    """Return locally remembered jobs for this MAF session."""
    session_id = _get_session_id(context)
    jobs = _jobs_by_session.get(session_id or "", [])
    return _json({"ok": True, "jobs": jobs})

mcp_server = MCPStreamableHTTPTool(
    name="Microsoft Learn MCP",
    url="https://learn.microsoft.com/api/mcp",
)

@function_middleware
async def inject_user_identity(context: FunctionInvocationContext, call_next):
    """Make the caller's identity available to every tool invocation.

    Tools can read it via `context.metadata["user_identity"]` (when invoked
    through the framework) or by importing `get_current_user(session_id)`.
    """
    session = context.session
    if session is not None:
        identity = _session_users.get(session.session_id)
        if identity is not None:
            # `metadata` is a Mapping on the dataclass; create a fresh dict
            # that merges any existing entries with our identity payload.
            merged = dict(context.metadata or {})
            merged["user_identity"] = identity
            context.metadata = merged
    await call_next()

def get_current_user(session_id: str) -> dict[str, str | None] | None:
    """Helper for tools that don't receive FunctionInvocationContext."""
    return _session_users.get(session_id)

# Build a Microsoft Agent Framework agent backed by Azure OpenAI's
# Responses API. `OpenAIChatClient` targets `/responses` (not chat
# completions) and stores conversations server-side by default
# (`STORES_BY_DEFAULT = True`). Each turn only sends the new input and
# is chained via `previous_response_id` / `conversation_id`, which the
# AgentSession tracks for you.
chat_client = OpenAIChatClient(
    model=config.azure_openai_deployment_name,
    api_key=config.azure_openai_api_key,
    azure_endpoint=config.azure_openai_endpoint,
    api_version="preview",
)

maf_agent = chat_client.as_agent(
    name="FutureCompleteAgent",
    instructions=system_prompt,
    tools=[
        get_day_of_week, 
        inspect_dataset, 
        check_license_status,
        get_trial_subscription,
        submit_forecast, 
        submit_backtest, 
        submit_benchmark,
        cancel_job,
        list_jobs, 
        mcp_server
    ],
    middleware=[inject_user_identity],
)

# Keep one MAF AgentSession per Bot Framework conversation so multi-turn
# context is preserved across messages.
_sessions: dict[str, AgentSession] = {}

# Per-session user identity captured from the channel activity. Keyed by
# session_id so tools/middleware can look up "who is calling" without
# touching TurnContext.
_session_users: dict[str, dict[str, str | None]] = {}
_session_attachments: dict[str, list[dict[str, Any]]] = {}
_dataset_schemas_by_session: dict[str, dict[str, Any]] = {}
_jobs_by_session: dict[str, list[dict[str, Any]]] = {}
_subscriptions_by_user: dict[str, dict[str, Any]] = {}
_session_subscription_keys: dict[str, str] = {}
_polling_tasks: set[asyncio.Task] = set()
_debug_sessions: set[str] = set()

RESET_COMMANDS = {
    "/clear",
    "clear",
    "clear chat",
    "reset",
    "reset chat",
    "restart",
    "start over",
}


def _new_session(conversation_id: str) -> AgentSession:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return AgentSession(session_id=f"{conversation_id}:{timestamp}")


def _extract_user_identity(context: TurnContext) -> dict[str, str | None]:
    """Pull the current user's identity from the inbound activity.

    The values available depend on the channel:
      * Teams: aad_object_id + tenant_id are populated; email/UPN requires
        a Graph call or TeamsInfo.get_member().
      * Playground / emulator: aad_object_id is usually None.
    """
    activity = context.activity
    from_property = getattr(activity, "from_property", None)
    conversation = getattr(activity, "conversation", None)
    claims = context.identity  # bot/channel claims, not the user's

    return {
        "user_id": getattr(from_property, "id", None),
        "user_name": getattr(from_property, "name", None),
        "email": getattr(from_property, "email", None) or getattr(from_property, "mail", None),
        "user_principal_name": getattr(from_property, "user_principal_name", None) or getattr(from_property, "userPrincipalName", None),
        "aad_object_id": getattr(from_property, "aad_object_id", None),
        "tenant_id": getattr(conversation, "tenant_id", None),
        "channel_id": getattr(activity, "channel_id", None),
        "caller_app_id": claims.get_app_id() if claims else None,
    }


def _capture_attachments(context: TurnContext, session_id: str) -> list[dict[str, Any]]:
    activity = context.activity
    attachments = getattr(activity, "attachments", None) or []
    captured: list[dict[str, Any]] = []
    for attachment in attachments:
        name = getattr(attachment, "name", None)
        content_url = getattr(attachment, "content_url", None)
        content_type = getattr(attachment, "content_type", None)
        captured.append(
            {
                "id": str(uuid4()),
                "name": name,
                "content_url": content_url,
                "content_type": content_type,
                "received_at": _now_iso(),
            }
        )
    if captured:
        _session_attachments.setdefault(session_id, []).extend(captured)
    return captured


def _attachment_prompt(captured: list[dict[str, Any]]) -> str:
    if not captured:
        return ""
    attachment_lines = [
        f"- {attachment.get('name') or 'unnamed file'} ({attachment.get('content_type') or 'unknown content type'})"
        for attachment in captured
    ]
    return "\n\nThe user uploaded these files, which are available to the inspect_dataset tool:\n" + "\n".join(attachment_lines)


def _llm_prompt_debug_payload(agent_input: str) -> dict[str, Any]:
    return {
        "llm_prompts": {
            "instructions": system_prompt,
            "input": agent_input,
        }
    }

# Define storage and application
storage = MemoryStorage()
connection_manager = MsalConnectionManager(**agents_sdk_config)
adapter = CloudAdapter(connection_manager=connection_manager)

agent_app = AgentApplication[TurnState](
    storage=storage, 
    adapter=adapter, 
    **agents_sdk_config
)

# @agent_app.conversation_update("membersAdded")
# async def on_members_added(context: TurnContext, _state: TurnState):
#     await context.send_activity("Hi there! I'm an agent to chat with you.")

# Listen for ANY message to be received. MUST BE AFTER ANY OTHER MESSAGE HANDLERS
@agent_app.activity(ActivityTypes.message)
async def on_message(context: TurnContext, state: TurnState):
    # Delegate the conversational turn to the Microsoft Agent Framework agent.
    conversation_id = context.activity.conversation.id
    incoming_text = (context.activity.text or "").strip()
    should_reset = incoming_text.lower() in RESET_COMMANDS

    if should_reset:
        previous_session = _sessions.pop(conversation_id, None)
        if previous_session is not None:
            _session_users.pop(previous_session.session_id, None)
            _session_attachments.pop(previous_session.session_id, None)
            _dataset_schemas_by_session.pop(previous_session.session_id, None)
            _jobs_by_session.pop(previous_session.session_id, None)
            _session_subscription_keys.pop(previous_session.session_id, None)
            _debug_sessions.discard(previous_session.session_id)

    session = _sessions.get(conversation_id)
    if session is None:
        session = _new_session(conversation_id)
        _sessions[conversation_id] = session
        
    # Capture/refresh the caller's identity for this session and stash it
    # in the parallel _session_users store so tools/middleware can look up
    # "who is calling" by session_id.
    user_identity = _extract_user_identity(context)
    _session_users[session.session_id] = user_identity
    captured_attachments = _capture_attachments(context, session.session_id)
    debug_requested = bool(DEBUG_TRIGGER_PATTERN.search(incoming_text))
    if debug_requested:
        _debug_sessions.add(session.session_id)
    print(f"[session {session.session_id}] user identity: {user_identity}", file=sys.stderr)

    if should_reset:
        agent_input = "The user cleared the conversation. Acknowledge the reset and restart the FutureComplete welcome flow."
    else:
        agent_input = incoming_text + _attachment_prompt(captured_attachments)
        if debug_requested:
            agent_input += (
                "\n\nHost note: Debug mode is enabled for this session. If the user is confirming a prepared "
                "FutureComplete API call, proceed with the confirmed call and include any `debug` block returned by "
                "the tool. Debug output must not include uploaded dataset contents or subscription keys."
            )

    before_job_ids = {job["id"] for job in _jobs_by_session.get(session.session_id, [])}
    response = await maf_agent.run(agent_input, session=session)
    for job in _jobs_by_session.get(session.session_id, []):
        if job["id"] not in before_job_ids:
            _schedule_job_polling(context, session.session_id, job)

    response_text = response.text
    if _debug_enabled(session.session_id):
        response_text += "\n\nDebug LLM prompt payload:\n```json\n" + _json(_llm_prompt_debug_payload(agent_input)) + "\n```"

    await context.send_activity(response_text)

@agent_app.error
async def on_error(context: TurnContext, error: Exception):
    # This check writes out errors to console log .vs. app insights.
    # NOTE: In production environment, you should consider logging this to Azure
    #       application insights.
    print(f"\n [on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()

    # Send a message to the user
    await context.send_activity("The agent encountered an error or bug.")
