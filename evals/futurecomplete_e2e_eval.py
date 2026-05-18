"""Offline e2e evaluations for the FutureComplete single MAF agent flow.

The default runner validates the agent/tool contract without calling Azure OpenAI
or the production FutureComplete API. It uses the real sample dataset and mocked
backend responses so it can run in CI and during local development.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
DEFAULT_SAMPLE_DATASET = REPO_ROOT / "dataset_GKYZ_2016_AAPL_MSFT_trimmed.csv"
DEFAULT_API_BASE_URL = "https://api.forecasting.inait.ai"


@dataclass
class EvalResult:
    name: str
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class EvalFailure(AssertionError):
    pass


def _load_agent_module():
    os.environ.setdefault("AZURE_OPENAI_API_KEY", "eval-key")
    os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT_NAME", "eval-deployment")
    os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    os.environ.setdefault("FUTURECOMPLETE_API_BASE_URL", "https://api.forecasting.inait.ai")
    os.environ.setdefault("FUTURECOMPLETE_DASHBOARD_URL", "https://futurecomplete.inait.ai")
    os.environ.setdefault("FUTURECOMPLETE_TRIAL_USERS_URL", "https://api.forecasting.inait.ai/users/dev/users")
    os.environ.setdefault("FUTURECOMPLETE_TRIAL_PLAN_ID", "trial")
    os.environ.setdefault("FUTURECOMPLETE_JOB_STATUS_PATH_TEMPLATE", "/v1/sessions/{session_id}/status")
    os.environ.setdefault("FUTURECOMPLETE_JOB_RESULT_PATH_TEMPLATE", "/v1/sessions/{session_id}/result")
    os.environ.setdefault("FUTURECOMPLETE_JOB_CANCEL_PATH_TEMPLATE", "/v1/sessions/{session_id}/cancel")

    sys.path.insert(0, str(SRC_DIR))
    return importlib.import_module("agent")


def _tool_json(tool, *args, **kwargs) -> dict[str, Any]:
    return json.loads(tool.func(*args, **kwargs))


def _fake_context(agent_module, session_id: str = "eval-conversation:session"):
    identity = {
        "aad_object_id": "eval-user",
        "email": "eval.user@example.com",
        "user_name": "Eval User",
        "user_id": "eval.user@example.com",
    }
    agent_module._session_users[session_id] = identity
    return SimpleNamespace(session=SimpleNamespace(session_id=session_id), metadata={"user_identity": identity})


def _reset_agent_state(agent_module):
    agent_module._session_users.clear()
    agent_module._session_attachments.clear()
    agent_module._dataset_schemas_by_session.clear()
    agent_module._jobs_by_session.clear()
    agent_module._subscriptions_by_user.clear()
    agent_module._session_subscription_keys.clear()
    agent_module._debug_sessions.clear()


def _install_mock_trial(agent_module):
    def fake_create_trial(identity, user_email=None, session_id=None):
        return {
            "cache_key": "eval-user",
            "user_email": user_email or "eval.user@example.com",
            "plan_id": "trial",
            "subscription_key": "eval-trial-key",
            "source": "eval-self-service-trial",
            "limitations": agent_module.TRIAL_LIMITATION_TEXT,
        }

    agent_module._create_trial_subscription = fake_create_trial


def _install_mock_backend(agent_module, calls: list[dict[str, Any]]):
    def fake_post(path, payload, identity, session_id, required_capability):
        job_id = f"{required_capability}-eval-001"
        calls.append(
            {
                "method": "POST",
                "path": path,
                "payload": payload,
                "session_id": session_id,
                "required_capability": required_capability,
            }
        )
        return {
            "status": "running",
            "location": f"https://api.forecasting.inait.ai/v1/sessions/{job_id}/status",
            "response": {
                "operation_type": required_capability,
                "data": {},
                "resource_id": f"response:{job_id}",
                "dataset_resource_id": f"data:{job_id}",
            },
        }

    def fake_cancel(path, identity, session_id, required_capability):
        calls.append(
            {
                "method": "POST",
                "path": path,
                "session_id": session_id,
                "required_capability": required_capability,
            }
        )
        cancelled_session = path.split("/sessions/", 1)[1].split("/", 1)[0]
        return {"status": "success", "message": "Cancellation requested", "session_id": cancelled_session}

    def fake_get(path, identity, session_id, required_capability):
        calls.append(
            {
                "method": "GET",
                "path": path,
                "session_id": session_id,
                "required_capability": required_capability,
            }
        )
        requested_session = path.split("/sessions/", 1)[1].split("/", 1)[0]
        if path.endswith("/status"):
            return {"status": "completed", "response": {"operation_type": required_capability, "session_id": requested_session}}
        if path.endswith("/result"):
            return {
                "status": "completed",
                "response": {
                    "operation_type": required_capability,
                    "session_id": requested_session,
                    "resource_id": f"response:{requested_session}",
                    "dataset_resource_id": f"data:{requested_session}",
                    "data": {
                        "predictions": [{"index": [0], "columns": ["AAPL"], "data": [[123.45]]}],
                        "scores": {"index": ["mae"], "columns": ["value"], "data": [[1.25]]},
                        "explain": None,
                    },
                },
            }
        raise EvalFailure(f"unexpected GET path: {path}")

    agent_module._post_futurecomplete = fake_post
    agent_module._post_futurecomplete_without_body = fake_cancel
    agent_module._get_futurecomplete = fake_get


def _assert(condition: bool, message: str):
    if not condition:
        raise EvalFailure(message)


def _read_json_response(response) -> dict[str, Any]:
    raw_body = response.read().decode("utf-8")
    return json.loads(raw_body) if raw_body else {}


def _live_request(method: str, url: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any], dict[str, str]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, _read_json_response(response), dict(response.headers)
    except urllib.error.HTTPError as error:
        raw_body = error.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            body = {"detail": raw_body}
        return error.code, body, dict(error.headers)


def eval_trial_backtest_flow(agent_module, dataset_path: Path) -> EvalResult:
    calls: list[dict[str, Any]] = []
    _reset_agent_state(agent_module)
    _install_mock_trial(agent_module)
    _install_mock_backend(agent_module, calls)
    context = _fake_context(agent_module)

    inspected = _tool_json(agent_module.inspect_dataset, context, file_reference=str(dataset_path))
    _assert(inspected["ok"], "dataset inspection failed")
    _assert("AAPL" in inspected["dataset"]["columns"], "AAPL column was not discovered")

    license_before = _tool_json(agent_module.check_license_status, context)
    _assert(license_before["has_subscription"] is False, "license status should start empty")

    trial = _tool_json(agent_module.get_trial_subscription, context, accepted_trial_limitations=True)
    _assert(trial["ok"], "trial provisioning failed")
    _assert(trial["subscription"]["allowed_workflows"] == ["backtest"], "trial should only allow backtest")
    _assert(trial["subscription"]["subscription_key"] == "eval-trial-key", "trial key should be returned to the user")

    backtest = _tool_json(
        agent_module.submit_backtest,
        context,
        target_columns="AAPL",
        horizon=4,
        prediction_stride=4,
        prediction_intervals="0.8,0.95",
        feature_columns="MSFT",
        run_explain=False,
        backtest_size=20,
        dataset_reference=str(dataset_path),
    )
    _assert(backtest["ok"], "backtest submission failed")
    _assert(calls[-1]["path"] == "/v1/backtest", "backtest did not use public /v1/backtest")
    _assert(calls[-1]["payload"]["config"]["operation"] == "backtest", "backtest payload operation mismatch")
    _assert("2015-08-03" not in json.dumps(backtest["job"]["request"]), "raw dataset leaked into job summary")

    return EvalResult(
        name="trial_backtest_flow",
        passed=True,
        details={"job_id": backtest["job"]["id"], "tool_calls": [call["path"] for call in calls]},
    )


def eval_full_license_forecast_and_benchmark_flow(agent_module, dataset_path: Path) -> EvalResult:
    calls: list[dict[str, Any]] = []
    _reset_agent_state(agent_module)
    _install_mock_backend(agent_module, calls)
    context = _fake_context(agent_module)
    agent_module._subscriptions_by_user["eval-user"] = {
        "cache_key": "eval-user",
        "user_email": "eval.user@example.com",
        "plan_id": "paid",
        "subscription_key": "eval-paid-key",
        "source": "eval",
    }
    agent_module._session_subscription_keys[context.session.session_id] = "eval-user"

    forecast = _tool_json(
        agent_module.submit_forecast,
        context,
        target_columns="AAPL",
        horizon=5,
        prediction_intervals="0.8,0.95",
        feature_columns="MSFT",
        run_explain=True,
        dataset_reference=str(dataset_path),
    )
    _assert(forecast["ok"], "forecast submission failed")

    benchmark = _tool_json(
        agent_module.submit_benchmark,
        context,
        target_columns="AAPL",
        horizon=4,
        prediction_stride=4,
        prediction_intervals="0.8,0.95",
        feature_columns="MSFT",
        run_explain=False,
        backtest_size=20,
        dataset_reference=str(dataset_path),
    )
    _assert(benchmark["ok"], "benchmark submission failed")

    paths = [call["path"] for call in calls]
    _assert(paths == ["/v1/prediction", "/v1/benchmark"], f"unexpected backend paths: {paths}")
    _assert(calls[1]["payload"]["config"]["operation_arguments"]["backtest_config"]["operation_type"] == "backtest", "benchmark missing nested backtest config")

    return EvalResult(
        name="full_license_forecast_and_benchmark_flow",
        passed=True,
        details={"job_ids": [forecast["job"]["id"], benchmark["job"]["id"]], "tool_calls": paths},
    )


def eval_cancel_running_job_flow(agent_module, dataset_path: Path) -> EvalResult:
    calls: list[dict[str, Any]] = []
    _reset_agent_state(agent_module)
    _install_mock_trial(agent_module)
    _install_mock_backend(agent_module, calls)
    context = _fake_context(agent_module)
    _tool_json(agent_module.get_trial_subscription, context, accepted_trial_limitations=True)
    submitted = _tool_json(
        agent_module.submit_backtest,
        context,
        target_columns="AAPL",
        horizon=4,
        prediction_stride=4,
        prediction_intervals="0.8,0.95",
        feature_columns="MSFT",
        backtest_size=20,
        dataset_reference=str(dataset_path),
    )
    job_id = submitted["job"]["id"]

    cancelled = _tool_json(agent_module.cancel_job, context, session_id=job_id)
    _assert(cancelled["ok"], "cancel job failed")
    _assert(calls[-1]["path"] == f"/v1/sessions/{job_id}/cancel", "cancel endpoint mismatch")

    return EvalResult(name="cancel_running_job_flow", passed=True, details={"job_id": job_id, "cancel_path": calls[-1]["path"]})


def eval_result_retrieval_flow(agent_module, dataset_path: Path) -> EvalResult:
    calls: list[dict[str, Any]] = []
    _reset_agent_state(agent_module)
    _install_mock_trial(agent_module)
    _install_mock_backend(agent_module, calls)
    context = _fake_context(agent_module)
    _tool_json(agent_module.get_trial_subscription, context, accepted_trial_limitations=True)
    submitted = _tool_json(
        agent_module.submit_backtest,
        context,
        target_columns="AAPL",
        horizon=4,
        prediction_stride=4,
        prediction_intervals="0.8,0.95",
        feature_columns="MSFT",
        backtest_size=20,
        dataset_reference=str(dataset_path),
    )
    job_id = submitted["job"]["id"]

    result = _tool_json(agent_module.get_job_result, context, session_id=job_id)
    _assert(result["ok"], "result retrieval failed")
    _assert(calls[-1]["path"] == f"/v1/sessions/{job_id}/result", "result endpoint mismatch")
    _assert("predictions" in result["result_summary"]["data"], "predictions missing from result summary")
    _assert("scores" in result["result_summary"]["data"], "scores missing from result summary")
    _assert("Result summary:" in result["chat_summary"], "chat summary missing result section")

    return EvalResult(
        name="result_retrieval_flow",
        passed=True,
        details={
            "job_id": job_id,
            "result_path": calls[-1]["path"],
            "result_data_keys": list(result["result_summary"]["data"].keys()),
        },
    )


def eval_live_api_health(base_url: str) -> EvalResult:
    status, body, _headers = _live_request("GET", f"{base_url.rstrip('/')}/health")
    _assert(status in {200, 401, 403}, f"expected /health to return 200, 401, or 403, got {status}: {body}")
    return EvalResult(
        name="live_api_health",
        passed=True,
        details={
            "http_status": status,
            "auth_required": status in {401, 403},
            "message": body.get("message") or body.get("detail") or body.get("title"),
        },
    )


def eval_live_api_unauthenticated_backtest_error(base_url: str) -> EvalResult:
    payload = {
        "data": None,
        "config": {
            "operation": "backtest",
            "operation_arguments": {
                "operation_type": "backtest",
                "forecasting_horizon": 1,
                "targets": ["AAPL"],
                "prediction_interval_levels": "0.8,0.95",
                "prediction_stride": 1,
                "backtest_size": 5,
            },
        },
        "background": True,
    }
    status, body, _headers = _live_request("POST", f"{base_url.rstrip('/')}/v1/backtest", payload)
    _assert(status in {401, 403}, f"expected unauthenticated /v1/backtest to return 401 or 403, got {status}: {body}")
    return EvalResult(
        name="live_api_unauthenticated_backtest_error",
        passed=True,
        details={
            "http_status": status,
            "error_code": body.get("code") or body.get("statusCode"),
            "error_title": body.get("title"),
            "error_detail": body.get("detail") or body.get("message"),
        },
    )


def run_offline_evals(dataset_path: Path = DEFAULT_SAMPLE_DATASET) -> list[EvalResult]:
    if not dataset_path.exists():
        raise FileNotFoundError(f"Sample dataset not found: {dataset_path}")
    agent_module = _load_agent_module()
    scenarios: list[Callable[[Any, Path], EvalResult]] = [
        eval_trial_backtest_flow,
        eval_full_license_forecast_and_benchmark_flow,
        eval_cancel_running_job_flow,
        eval_result_retrieval_flow,
    ]
    results: list[EvalResult] = []
    for scenario in scenarios:
        try:
            results.append(scenario(agent_module, dataset_path))
        except Exception as error:
            scenario_name = getattr(scenario, "__name__", scenario.__class__.__name__).removeprefix("eval_")
            results.append(EvalResult(name=scenario_name, passed=False, error=str(error)))
    return results


def run_live_api_evals(base_url: str = DEFAULT_API_BASE_URL) -> list[EvalResult]:
    scenarios: list[Callable[[str], EvalResult]] = [
        eval_live_api_health,
        eval_live_api_unauthenticated_backtest_error,
    ]
    results: list[EvalResult] = []
    for scenario in scenarios:
        try:
            results.append(scenario(base_url))
        except Exception as error:
            scenario_name = getattr(scenario, "__name__", scenario.__class__.__name__).removeprefix("eval_")
            results.append(EvalResult(name=scenario_name, passed=False, error=str(error)))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FutureComplete MAF agent e2e evaluations.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_SAMPLE_DATASET, help="Sample CSV/XLSX/Parquet dataset path.")
    parser.add_argument("--include-live-api", action="store_true", help="Also call the public FutureComplete API health and unauthenticated error endpoints.")
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL, help="FutureComplete public API base URL for live checks.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text summary.")
    args = parser.parse_args()

    results = run_offline_evals(args.dataset)
    if args.include_live_api:
        results.extend(run_live_api_evals(args.api_base_url))
    payload = {
        "passed": all(result.passed for result in results),
        "results": [result.__dict__ for result in results],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for result in results:
            status = "PASS" if result.passed else "FAIL"
            print(f"{status} {result.name}")
            if result.error:
                print(f"  {result.error}")
        print("PASS overall" if payload["passed"] else "FAIL overall")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
