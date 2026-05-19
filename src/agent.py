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
    Activity,
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
from telemetry import configure_telemetry, conversation_span, dependency_span, http_dependency_attributes, log_conversation_event, set_http_span_result

MAX_DATASET_BYTES = 200 * 1024 * 1024
MAX_SAMPLE_ROWS = 5
MAX_RESULT_TABLE_ROWS = 20
MAX_RESULT_TABLE_COLUMNS = 12
SUPPORTED_DATASET_SUFFIXES = {".csv", ".xlsx", ".parquet"}
TRIAL_LIMITATION_TEXT = "Trial subscriptions only allow Backtest jobs through /v1/backtest. They do not allow Forecast jobs through /v1/prediction or Benchmark jobs through /v1/benchmark."
TERMINAL_JOB_STATUSES = {"completed", "complete", "succeeded", "success", "failed", "error", "cancelled", "canceled"}
SUCCESS_JOB_STATUSES = {"completed", "complete", "succeeded", "success"}
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
configure_telemetry()

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
    conversation_id = _conversation_id_from_session(session_id)
    with dependency_span(
        _http_dependency_name("POST", config.futurecomplete_trial_users_url),
        conversation_id,
        session_id,
        http_dependency_attributes(
            "POST",
            config.futurecomplete_trial_users_url,
            {"futurecomplete.operation": "trial_subscription", "futurecomplete.plan_id": config.futurecomplete_trial_plan_id},
        ),
    ) as span:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                set_http_span_result(span, response.status)
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
            set_http_span_result(span, error.code)
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


def _conversation_id_from_session(session_id: str | None) -> str | None:
    if not session_id:
        return None
    if ":" not in session_id:
        return session_id
    return session_id.rsplit(":", 1)[0]


def _http_dependency_name(method: str, url: str) -> str:
    parsed = urlparse(url)
    target = parsed.netloc or parsed.hostname or "unknown-target"
    return f"{method} {target}{parsed.path or '/'}"


def _post_futurecomplete(path: str, payload: dict[str, Any], identity: dict[str, str | None], session_id: str | None, required_capability: str) -> dict[str, Any]:
    url = f"{config.futurecomplete_api_base_url.rstrip('/')}{path}"
    headers = _futurecomplete_headers(identity, session_id, required_capability)
    conversation_id = _conversation_id_from_session(session_id)
    log_conversation_event(
        "futurecomplete.api.request",
        conversation_id,
        session_id,
        direction="outbound",
        attributes={"method": "POST", "path": path, "required_capability": required_capability},
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with dependency_span(
        _http_dependency_name("POST", url),
        conversation_id,
        session_id,
        http_dependency_attributes("POST", url, {"futurecomplete.path": path, "futurecomplete.capability": required_capability}),
    ) as span:
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                set_http_span_result(span, response.status)
                raw_body = response.read().decode("utf-8")
                parsed_body = _parse_json_or_text(raw_body)
                body = parsed_body if isinstance(parsed_body, dict) else {"body": parsed_body}
                location = response.headers.get("Location")
                if location:
                    body["location"] = location
                    if span is not None:
                        span.set_attribute("http.response.header.location", location)
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
                log_conversation_event(
                    "futurecomplete.api.response",
                    conversation_id,
                    session_id,
                    direction="inbound",
                    attributes={"method": "POST", "path": path, "http_status": response.status, "required_capability": required_capability},
                )
                return body
        except urllib.error.HTTPError as error:
            set_http_span_result(span, error.code)
            log_conversation_event(
                "futurecomplete.api.error",
                conversation_id,
                session_id,
                direction="inbound",
                attributes={"method": "POST", "path": path, "http_status": error.code, "required_capability": required_capability},
            )
            raise _futurecomplete_api_error("FutureComplete API", error, "POST", url, headers, payload, include_debug=_debug_enabled(session_id)) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(f"FutureComplete API request failed: {error}") from error


def _post_futurecomplete_without_body(path: str, identity: dict[str, str | None], session_id: str | None, required_capability: str) -> dict[str, Any]:
    url = f"{config.futurecomplete_api_base_url.rstrip('/')}{path}"
    headers = _futurecomplete_headers(identity, session_id, required_capability)
    conversation_id = _conversation_id_from_session(session_id)
    log_conversation_event(
        "futurecomplete.api.request",
        conversation_id,
        session_id,
        direction="outbound",
        attributes={"method": "POST", "path": path, "required_capability": required_capability},
    )
    headers.pop("Content-Type", None)
    request = urllib.request.Request(url, headers=headers, method="POST")
    with dependency_span(
        _http_dependency_name("POST", url),
        conversation_id,
        session_id,
        http_dependency_attributes("POST", url, {"futurecomplete.path": path, "futurecomplete.capability": required_capability}),
    ) as span:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                set_http_span_result(span, response.status)
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
                log_conversation_event(
                    "futurecomplete.api.response",
                    conversation_id,
                    session_id,
                    direction="inbound",
                    attributes={"method": "POST", "path": path, "http_status": response.status, "required_capability": required_capability},
                )
                return body
        except urllib.error.HTTPError as error:
            set_http_span_result(span, error.code)
            log_conversation_event(
                "futurecomplete.api.error",
                conversation_id,
                session_id,
                direction="inbound",
                attributes={"method": "POST", "path": path, "http_status": error.code, "required_capability": required_capability},
            )
            raise _futurecomplete_api_error("FutureComplete API", error, "POST", url, headers, None, include_debug=_debug_enabled(session_id)) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(f"FutureComplete API request failed: {error}") from error


def _get_futurecomplete(path: str, identity: dict[str, str | None], session_id: str | None, required_capability: str) -> dict[str, Any]:
    url = f"{config.futurecomplete_api_base_url.rstrip('/')}{path}"
    headers = _futurecomplete_headers(identity, session_id, required_capability)
    conversation_id = _conversation_id_from_session(session_id)
    log_conversation_event(
        "futurecomplete.api.request",
        conversation_id,
        session_id,
        direction="outbound",
        attributes={"method": "GET", "path": path, "required_capability": required_capability},
    )
    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )
    with dependency_span(
        _http_dependency_name("GET", url),
        conversation_id,
        session_id,
        http_dependency_attributes("GET", url, {"futurecomplete.path": path, "futurecomplete.capability": required_capability}),
    ) as span:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                set_http_span_result(span, response.status)
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
                log_conversation_event(
                    "futurecomplete.api.response",
                    conversation_id,
                    session_id,
                    direction="inbound",
                    attributes={"method": "GET", "path": path, "http_status": response.status, "required_capability": required_capability},
                )
                return body
        except urllib.error.HTTPError as error:
            set_http_span_result(span, error.code)
            log_conversation_event(
                "futurecomplete.api.error",
                conversation_id,
                session_id,
                direction="inbound",
                attributes={"method": "GET", "path": path, "http_status": error.code, "required_capability": required_capability},
            )
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


def _is_split_table(value: Any) -> bool:
    return isinstance(value, dict) and {"index", "columns", "data"}.issubset(value.keys()) and isinstance(value.get("columns"), list) and isinstance(value.get("data"), list)


def _split_table_rows(value: dict[str, Any], max_rows: int = MAX_RESULT_TABLE_ROWS, max_columns: int = MAX_RESULT_TABLE_COLUMNS) -> list[dict[str, Any]]:
    columns = [str(column) for column in value.get("columns", [])][:max_columns]
    raw_indexes = value.get("index")
    indexes = raw_indexes if isinstance(raw_indexes, list) else []
    rows = []
    for row_index, row_values in enumerate(value.get("data", [])[:max_rows]):
        if not isinstance(row_values, list):
            continue
        row: dict[str, Any] = {}
        if row_index < len(indexes):
            row["index"] = indexes[row_index]
        for column, cell in zip(columns, row_values[:max_columns]):
            row[column] = cell
        rows.append(row)
    return rows


def _summarize_split_table(value: dict[str, Any]) -> dict[str, Any]:
    columns = [str(column) for column in value.get("columns", [])]
    data = value.get("data", [])
    return {
        "type": "table",
        "row_count": len(data),
        "column_count": len(columns),
        "columns": columns[:MAX_RESULT_TABLE_COLUMNS],
        "rows": _split_table_rows(value),
        "truncated": len(data) > MAX_RESULT_TABLE_ROWS or len(columns) > MAX_RESULT_TABLE_COLUMNS,
    }


def _summarize_result_value(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if _is_split_table(value):
        return _summarize_split_table(value)
    if depth >= 2:
        if isinstance(value, dict):
            return {"type": "object", "keys": list(value.keys())[:10], "key_count": len(value)}
        if isinstance(value, list):
            return {"type": "array", "count": len(value)}
        return str(value)
    if isinstance(value, dict):
        return {key: _summarize_result_value(nested_value, depth + 1) for key, nested_value in list(value.items())[:10]}
    if isinstance(value, list):
        summary: dict[str, Any] = {"type": "array", "count": len(value)}
        if value:
            first = value[0]
            if _is_split_table(first):
                summary["tables"] = [_summarize_split_table(item) for item in value[:3] if _is_split_table(item)]
            elif isinstance(first, dict):
                summary["item_keys"] = list(first.keys())[:10]
                summary["sample"] = [_summarize_result_value(item, depth + 1) for item in value[:3]]
            elif len(value) <= 5 and all(isinstance(item, (bool, int, float, str)) or item is None for item in value):
                summary["items"] = value
            else:
                summary["sample"] = [_summarize_result_value(item, depth + 1) for item in value[:3]]
        return summary
    return str(value)


def _summarize_result_for_chat(response: dict[str, Any]) -> dict[str, Any]:
    payload = _response_payload(response)
    summary = {
        "operation_type": payload.get("operation_type"),
        "session_id": payload.get("session_id") or response.get("session_id"),
        "resource_id": payload.get("resource_id"),
        "dataset_resource_id": payload.get("dataset_resource_id"),
    }
    data = payload.get("data")
    if isinstance(data, dict):
        summary["data"] = _summarize_result_value(data)
    elif data is not None:
        summary["data"] = _summarize_result_value(data)
    return {key: value for key, value in summary.items() if value is not None}


def _format_result_summary_for_chat(summary: dict[str, Any]) -> str:
    lines = []
    if summary.get("operation_type"):
        lines.append(f"Operation: {summary['operation_type']}")
    if summary.get("session_id"):
        lines.append(f"Session: {summary['session_id']}")
    if summary.get("resource_id"):
        lines.append(f"Resource: {summary['resource_id']}")
    data = summary.get("data")
    if isinstance(data, dict) and data:
        lines.append("Result summary:")
        preferred_keys = [key for key in ("scores", "metrics", "predictions", "explain") if key in data]
        remaining_keys = [key for key in data.keys() if key not in preferred_keys]
        for key in (preferred_keys + remaining_keys)[:8]:
            value = data[key]
            lines.extend(_format_result_data_item(str(key), value))
    elif data is not None:
        lines.append(f"Result summary: {json.dumps(data, sort_keys=True)[:500]}")
    return "\n".join(lines) if lines else "The result is available."


def _format_result_data_item(name: str, value: Any) -> list[str]:
    if isinstance(value, dict) and value.get("type") == "table":
        return _format_table_summary(name, value)
    if isinstance(value, dict) and value.get("type") == "array" and value.get("tables"):
        lines = [f"- {name}: {value.get('count')} table payload(s)"]
        for index, table in enumerate(value.get("tables", []), start=1):
            lines.extend(_format_table_summary(f"{name} table {index}", table))
        return lines
    return [f"- {name}: {json.dumps(value, sort_keys=True)[:1000]}"]


def _format_table_summary(name: str, table: dict[str, Any]) -> list[str]:
    lines = [f"- {name}: {table.get('row_count', 0)} rows x {table.get('column_count', 0)} columns"]
    raw_rows = table.get("rows")
    rows = raw_rows if isinstance(raw_rows, list) else []
    if rows:
        lines.extend(_format_markdown_table(rows))
    if table.get("truncated"):
        lines.append(f"  Showing first {len(rows)} rows and up to {MAX_RESULT_TABLE_COLUMNS} columns.")
    return lines


def _format_markdown_table(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for column in row.keys():
            if column not in columns:
                columns.append(column)
    columns = columns[:MAX_RESULT_TABLE_COLUMNS + 1]
    if not columns:
        return []
    lines = ["  | " + " | ".join(columns) + " |", "  | " + " | ".join(["---"] * len(columns)) + " |"]
    for row in rows:
        cells = [str(row.get(column, "")).replace("\n", " ")[:120] for column in columns]
        lines.append("  | " + " | ".join(cells) + " |")
    return lines


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
        "polling": {
            "enabled": status not in TERMINAL_JOB_STATUSES,
            "starts_after_response": True,
            "interval_seconds": config.futurecomplete_poll_interval_seconds,
            "max_attempts": config.futurecomplete_poll_max_attempts,
            "on_success": "fetch /v1/sessions/{session_id}/result and post a concise result summary plus dashboard link back into this chat",
        },
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


def _agent_app_id() -> str | None:
    return os.environ.get("BOT_ID") or os.environ.get("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID")


async def _send_job_notification(context: TurnContext, session_id: str, job: dict[str, Any], message: str):
    errors: list[str] = []
    conversation_id = job.get("conversation_id") or session_id
    log_conversation_event(
        "conversation.proactive_notification.prepare",
        str(conversation_id),
        session_id,
        direction="outbound",
        text=message,
        attributes={"job_id": job.get("id"), "job_type": job.get("type"), "status": job.get("status")},
    )
    reference = _conversation_references.get(session_id)
    agent_app_id = _agent_app_id()
    if reference is not None and agent_app_id:
        try:
            continuation_activity = Activity(type=ActivityTypes.message)
            continuation_activity.apply_conversation_reference(reference)

            async def callback(turn_context: TurnContext):
                await turn_context.send_activity(message)

            await adapter.continue_conversation(agent_app_id, continuation_activity, callback)
            job["last_notification"] = {"sent_at": _now_iso(), "method": "continue_conversation"}
            log_conversation_event(
                "conversation.proactive_notification.sent",
                str(conversation_id),
                session_id,
                direction="outbound",
                attributes={"job_id": job.get("id"), "method": "continue_conversation"},
            )
            return
        except Exception as error:
            errors.append(f"continue_conversation failed: {error}")
    try:
        await context.send_activity(message)
        job["last_notification"] = {"sent_at": _now_iso(), "method": "turn_context"}
        log_conversation_event(
            "conversation.proactive_notification.sent",
            str(conversation_id),
            session_id,
            direction="outbound",
            attributes={"job_id": job.get("id"), "method": "turn_context"},
        )
        return
    except Exception as error:
        errors.append(f"turn_context failed: {error}")
    job["last_notify_error"] = "; ".join(errors) or "notification failed"
    job["updated_at"] = _now_iso()
    log_conversation_event(
        "conversation.proactive_notification.failed",
        str(conversation_id),
        session_id,
        direction="outbound",
        attributes={"job_id": job.get("id"), "errors": errors},
    )


async def _poll_job_and_notify(context: TurnContext, session_id: str, job: dict[str, Any]):
    identity = _session_users.get(session_id, {})
    capability = _job_capability(job.get("type"))
    job_id = str(job["id"])
    if isinstance(job.get("polling"), dict):
        job["polling"]["started"] = True
        job["polling"].setdefault("started_at", _now_iso())
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
                label = "completed" if job["status"] in SUCCESS_JOB_STATUSES else job["status"]
                message = f"Your FutureComplete {job.get('type')} job {job_id} is {label}."
                if job["status"] in SUCCESS_JOB_STATUSES:
                    try:
                        result_response = await asyncio.to_thread(
                            _get_futurecomplete,
                            _job_result_path(job_id),
                            identity,
                            session_id,
                            capability,
                        )
                        result_summary = _summarize_result_for_chat(result_response)
                        job["result_response"] = _summarize_api_response(result_response)
                        job["result_summary"] = result_summary
                        job["updated_at"] = _now_iso()
                        message += "\n\n" + _format_result_summary_for_chat(result_summary)
                    except Exception as error:
                        job["last_result_error"] = str(error)
                        job["updated_at"] = _now_iso()
                        message += f"\n\nI could not retrieve the result payload automatically: {error}"
                message += f"\n\nOpen the dashboard for plots and CSV download: {job['dashboard_url']}"
                await _send_job_notification(context, session_id, job, message)
                return
        except Exception as error:
            job["last_poll_error"] = str(error)
            job["updated_at"] = _now_iso()


def _schedule_job_polling(context: TurnContext, session_id: str, job: dict[str, Any]):
    if job.get("polling_started") or job.get("status") in TERMINAL_JOB_STATUSES:
        return
    job["polling_started"] = True
    if isinstance(job.get("polling"), dict):
        job["polling"]["started"] = True
        job["polling"]["started_at"] = _now_iso()
    task = asyncio.create_task(_poll_job_and_notify(context, session_id, job))
    _polling_tasks.add(task)
    task.add_done_callback(_polling_tasks.discard)


def _find_remembered_job(current_session_id: str | None, job_session_id: str | None) -> dict[str, Any] | None:
    jobs = _jobs_by_session.get(current_session_id or "", [])
    if not jobs:
        return None
    if not job_session_id:
        return jobs[-1]
    return next((job for job in jobs if str(job.get("id")) == job_session_id), None)


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
def get_job_status(
    context: FunctionInvocationContext,
    session_id: Annotated[str | None, Field(description="Optional FutureComplete session ID/job ID. If omitted, checks the most recent remembered job in this conversation.")] = None,
) -> Annotated[str, Field(description="Check a FutureComplete job status by session ID, or the latest remembered job if no session ID is provided.")]:
    """Fetch the current status for a FutureComplete background job."""
    try:
        current_session_id = _get_session_id(context)
        identity = _get_session_identity(context)
        matching_job = _find_remembered_job(current_session_id, session_id)
        target_session_id = session_id or (str(matching_job["id"]) if matching_job else None)
        if not target_session_id:
            raise ValueError("No FutureComplete job is remembered in this conversation. Ask the user for the FutureComplete session ID from the dashboard URL.")

        capability = _job_capability(matching_job.get("type") if matching_job else "backtest")
        response = _get_futurecomplete(_job_status_path(target_session_id), identity, current_session_id, capability)
        debug = _take_debug(response)
        status = _extract_job_status(response) or str(response.get("status") or "unknown").lower()
        normalized_status = "completed" if status in {"complete", "succeeded", "success"} else status
        if matching_job is not None:
            matching_job["status"] = normalized_status
            matching_job["last_status_response"] = _summarize_api_response(response)
            matching_job["updated_at"] = _now_iso()

        return _json(
            _add_debug(
                {
                    "ok": True,
                    "session_id": target_session_id,
                    "status": normalized_status,
                    "is_terminal": normalized_status in TERMINAL_JOB_STATUSES,
                    "dashboard_url": _dashboard_link(target_session_id),
                    "response": _summarize_api_response(response),
                },
                debug,
            )
        )
    except FutureCompleteApiError as error:
        return _json(_futurecomplete_error_payload(error))
    except Exception as error:
        return _json({"ok": False, "error": str(error)})


@tool(approval_mode="never_require")
def get_job_result(
    context: FunctionInvocationContext,
    session_id: Annotated[str | None, Field(description="Optional FutureComplete session ID/job ID. If omitted, fetches results for the most recent remembered job in this conversation.")] = None,
) -> Annotated[str, Field(description="Fetch and summarize FutureComplete job results by session ID, or the latest remembered job if no session ID is provided.")]:
    """Fetch the result payload for a completed FutureComplete job."""
    try:
        current_session_id = _get_session_id(context)
        identity = _get_session_identity(context)
        matching_job = _find_remembered_job(current_session_id, session_id)
        target_session_id = session_id or (str(matching_job["id"]) if matching_job else None)
        if not target_session_id:
            raise ValueError("No FutureComplete job is remembered in this conversation. Ask the user for the FutureComplete session ID from the dashboard URL.")

        capability = _job_capability(matching_job.get("type") if matching_job else "backtest")
        response = _get_futurecomplete(_job_result_path(target_session_id), identity, current_session_id, capability)
        debug = _take_debug(response)
        result_summary = _summarize_result_for_chat(response)
        chat_summary = _format_result_summary_for_chat(result_summary)
        if matching_job is not None:
            matching_job["result_response"] = _summarize_api_response(response)
            matching_job["result_summary"] = result_summary
            matching_job["updated_at"] = _now_iso()

        return _json(
            _add_debug(
                {
                    "ok": True,
                    "session_id": target_session_id,
                    "dashboard_url": _dashboard_link(target_session_id),
                    "result_summary": result_summary,
                    "chat_summary": chat_summary,
                    "response": _summarize_api_response(response),
                },
                debug,
            )
        )
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
    id="futurecomplete-agent",
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
        get_job_status,
        get_job_result,
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
_conversation_references: dict[str, Any] = {}
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
            _conversation_references.pop(previous_session.session_id, None)
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
    try:
        _conversation_references[session.session_id] = context.activity.get_conversation_reference()
    except Exception as error:
        print(f"[session {session.session_id}] could not capture conversation reference: {error}", file=sys.stderr)
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

    with conversation_span(
        "conversation.turn",
        conversation_id,
        session.session_id,
        {
            "channel_id": user_identity.get("channel_id"),
            "user_id": user_identity.get("user_id"),
            "aad_object_id": user_identity.get("aad_object_id"),
            "message_length": len(incoming_text),
            "attachment_count": len(captured_attachments),
            "debug_requested": debug_requested,
            "reset_requested": should_reset,
        },
    ):
        log_conversation_event(
            "conversation.inbound",
            conversation_id,
            session.session_id,
            direction="inbound",
            text=incoming_text,
            attributes={"attachment_count": len(captured_attachments)},
        )
        log_conversation_event(
            "maf.request",
            conversation_id,
            session.session_id,
            direction="internal",
            text=agent_input,
        )

        before_job_ids = {job["id"] for job in _jobs_by_session.get(session.session_id, [])}
        azure_openai_host = urlparse(config.azure_openai_endpoint).hostname or config.azure_openai_endpoint
        with dependency_span(
            "Azure OpenAI FutureCompleteAgent run",
            conversation_id,
            session.session_id,
            {
                "dependency.type": "Azure OpenAI",
                "gen_ai.system": "azure_openai",
                "gen_ai.operation.name": "responses",
                "gen_ai.request.model": config.azure_openai_deployment_name,
                "gen_ai.request.endpoint": config.azure_openai_endpoint,
                "server.address": azure_openai_host,
                "maf.agent.name": "FutureCompleteAgent",
                "maf.session_id": session.session_id,
                "llm.input_length": len(agent_input),
            },
        ) as llm_span:
            response = await maf_agent.run(agent_input, session=session)
            if llm_span is not None:
                llm_span.set_attribute("llm.output_length", len(response.text or ""))
        for job in _jobs_by_session.get(session.session_id, []):
            if job["id"] not in before_job_ids:
                job["conversation_id"] = conversation_id
                log_conversation_event(
                    "futurecomplete.job.created",
                    conversation_id,
                    session.session_id,
                    attributes={"job_id": job.get("id"), "job_type": job.get("type"), "status": job.get("status")},
                )
                _schedule_job_polling(context, session.session_id, job)

        response_text = response.text
        if _debug_enabled(session.session_id):
            response_text += "\n\nDebug LLM prompt payload:\n```json\n" + _json(_llm_prompt_debug_payload(agent_input)) + "\n```"

        log_conversation_event(
            "conversation.outbound",
            conversation_id,
            session.session_id,
            direction="outbound",
            text=response_text,
        )

        await context.send_activity(response_text)

@agent_app.error
async def on_error(context: TurnContext, error: Exception):
    # This check writes out errors to console log .vs. app insights.
    # NOTE: In production environment, you should consider logging this to Azure
    #       application insights.
    print(f"\n [on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()
    conversation_id = getattr(getattr(context.activity, "conversation", None), "id", None)
    log_conversation_event(
        "conversation.error",
        conversation_id,
        attributes={"error": str(error), "error_type": type(error).__name__},
    )

    # Send a message to the user
    await context.send_activity("The agent encountered an error or bug.")
