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
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
DEFAULT_SAMPLE_DATASET = REPO_ROOT / "dataset_GKYZ_2016_AAPL_MSFT_trimmed.csv"


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
    os.environ.setdefault("FUTURECOMPLETE_API_BASE_URL", "https://inait-saas-apim-jjyzmt7v.azure-api.net")
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


def _install_mock_trial(agent_module):
    def fake_create_trial(identity, user_email=None):
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
            "location": f"https://inait-saas-apim-jjyzmt7v.azure-api.net/v1/sessions/{job_id}/status",
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

    agent_module._post_futurecomplete = fake_post
    agent_module._post_futurecomplete_without_body = fake_cancel


def _assert(condition: bool, message: str):
    if not condition:
        raise EvalFailure(message)


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
    _assert("eval-trial-key" not in json.dumps(trial), "trial key leaked into tool result")

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


def run_offline_evals(dataset_path: Path = DEFAULT_SAMPLE_DATASET) -> list[EvalResult]:
    if not dataset_path.exists():
        raise FileNotFoundError(f"Sample dataset not found: {dataset_path}")
    agent_module = _load_agent_module()
    scenarios: list[Callable[[Any, Path], EvalResult]] = [
        eval_trial_backtest_flow,
        eval_full_license_forecast_and_benchmark_flow,
        eval_cancel_running_job_flow,
    ]
    results: list[EvalResult] = []
    for scenario in scenarios:
        try:
            results.append(scenario(agent_module, dataset_path))
        except Exception as error:
            results.append(EvalResult(name=scenario.__name__.removeprefix("eval_"), passed=False, error=str(error)))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FutureComplete MAF agent e2e evaluations.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_SAMPLE_DATASET, help="Sample CSV/XLSX/Parquet dataset path.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text summary.")
    args = parser.parse_args()

    results = run_offline_evals(args.dataset)
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
