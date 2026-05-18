import asyncio
import json

import pytest


def tool_json(tool, *args, **kwargs):
    return json.loads(tool.func(*args, **kwargs))


def test_check_license_status_reports_trial_limitations(agent_module, fake_context):
    result = tool_json(agent_module.check_license_status, fake_context)

    assert result["ok"] is True
    assert result["has_subscription"] is False
    assert result["trial_available"] is True
    assert "/v1/backtest" in result["trial_limitations"]


def test_get_trial_subscription_requires_explicit_acceptance(agent_module, fake_context):
    result = tool_json(agent_module.get_trial_subscription, fake_context, accepted_trial_limitations=False)

    assert result["ok"] is False
    assert "explicitly accept" in result["error"]


def test_get_trial_subscription_returns_subscription_key(agent_module, fake_context, monkeypatch):
    def fake_create_trial(identity, user_email=None, session_id=None):
        return {
            "cache_key": "user-123",
            "user_email": "user@example.com",
            "plan_id": "trial",
            "subscription_key": "super-secret",
            "source": "self-service-trial",
            "limitations": agent_module.TRIAL_LIMITATION_TEXT,
        }

    monkeypatch.setattr(agent_module, "_create_trial_subscription", fake_create_trial)

    result = tool_json(agent_module.get_trial_subscription, fake_context, accepted_trial_limitations=True)

    assert result["ok"] is True
    assert result["subscription"]["plan_id"] == "trial"
    assert result["subscription"]["allowed_workflows"] == ["backtest"]
    assert result["subscription"]["subscription_key"] == "super-secret"


def test_debug_mode_records_sanitized_request_response_exchange(agent_module, fake_context, active_full_subscription, monkeypatch):
    class FakeResponse:
        status = 202
        headers = {
            "Location": "https://api.forecasting.inait.ai/v1/sessions/debug-123/status",
            "X-Request-Id": "request-123",
        }

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"status": "running", "response": {"session_id": "debug-123"}}).encode("utf-8")

    def fake_urlopen(request, timeout):
        return FakeResponse()

    monkeypatch.setattr(agent_module.urllib.request, "urlopen", fake_urlopen)
    agent_module._debug_sessions.add(fake_context.session.session_id)
    payload = agent_module._public_operation_payload(
        "forecast",
        {"AAPL": {"2015-08-03": 1.0}, "MSFT": {"2015-08-03": 2.0}},
        agent_module._operation_arguments("forecast", "AAPL", 5, "0.8,0.95", None, False),
        background=True,
    )

    response = agent_module._post_futurecomplete(
        "/v1/prediction",
        payload,
        fake_context.metadata["user_identity"],
        fake_context.session.session_id,
        "forecast",
    )

    debug = response["_debug"]
    assert debug["request"]["method"] == "POST"
    assert debug["request"]["headers"]["Ocp-Apim-Subscription-Key"] == "paid-subscription-key"
    assert debug["request"]["body"]["data"]["omitted"] == "dataset payload omitted from debug output"
    assert debug["request"]["body"]["data"]["columns"] == ["AAPL", "MSFT"]
    assert "2015-08-03" not in json.dumps(debug["request"]["body"]["data"])
    assert debug["response"]["status"] == 202
    assert debug["response"]["headers"]["X-Request-Id"] == "request-123"
    assert debug["response"]["body"]["response"]["session_id"] == "debug-123"


def test_llm_prompt_debug_payload_includes_instructions_and_input(agent_module):
    payload = agent_module._llm_prompt_debug_payload("submit this backtest debug")

    assert "FutureComplete Agent System Prompt" in payload["llm_prompts"]["instructions"]
    assert payload["llm_prompts"]["input"] == "submit this backtest debug"


def test_submit_backtest_includes_debug_block_from_api_call(agent_module, fake_context, active_trial_subscription, sample_dataset_path, monkeypatch):
    def fake_post(path, payload, identity, session_id, required_capability):
        return {
            "status": "running",
            "response": {"operation_type": "backtest", "session_id": "debug-backtest-123", "data": {}},
            "_debug": {
                "request": {"method": "POST", "url": "https://example.test/v1/backtest", "headers": {}, "body": {}},
                "response": {"status": 202, "headers": {}, "body": {"status": "running"}},
            },
        }

    monkeypatch.setattr(agent_module, "_post_futurecomplete", fake_post)

    result = tool_json(
        agent_module.submit_backtest,
        fake_context,
        target_columns="AAPL",
        horizon=4,
        prediction_stride=1,
        prediction_intervals="0.8,0.95",
        backtest_size=20,
        dataset_reference=str(sample_dataset_path),
    )

    assert result["ok"] is True
    assert result["debug"]["request"]["method"] == "POST"
    assert "_debug" not in json.dumps(result["job"])


def test_trial_subscription_allows_backtest_and_blocks_forecast_and_benchmark(agent_module, fake_context, active_trial_subscription):
    identity = fake_context.metadata["user_identity"]

    headers = agent_module._futurecomplete_headers(identity, fake_context.session.session_id, "backtest")
    assert headers["Ocp-Apim-Subscription-Key"] == "trial-subscription-key"

    with pytest.raises(RuntimeError, match="Forecast"):
        agent_module._futurecomplete_headers(identity, fake_context.session.session_id, "forecast")

    with pytest.raises(RuntimeError, match="Benchmark"):
        agent_module._futurecomplete_headers(identity, fake_context.session.session_id, "benchmark")


def test_submit_forecast_posts_public_prediction_payload(agent_module, fake_context, active_full_subscription, sample_dataset_path, monkeypatch):
    captured = {}

    def fake_post(path, payload, identity, session_id, required_capability):
        captured.update(
            path=path,
            payload=payload,
            identity=identity,
            session_id=session_id,
            required_capability=required_capability,
        )
        return {
            "status": "running",
            "location": "https://api.forecasting.inait.ai/v1/sessions/forecast-123/status",
            "response": {"operation_type": "forecast", "data": {}, "resource_id": "response:forecast-123"},
        }

    monkeypatch.setattr(agent_module, "_post_futurecomplete", fake_post)

    result = tool_json(
        agent_module.submit_forecast,
        fake_context,
        target_columns="AAPL",
        horizon=5,
        prediction_intervals="0.8,0.95",
        feature_columns="MSFT",
        run_explain=True,
        dataset_reference=str(sample_dataset_path),
    )

    assert result["ok"] is True
    assert captured["path"] == "/v1/prediction"
    assert captured["required_capability"] == "forecast"
    assert captured["payload"]["config"]["operation"] == "forecast"
    assert captured["payload"]["config"]["operation_arguments"]["targets"] == "AAPL"
    assert "AAPL" in captured["payload"]["data"]["columns"]
    assert result["job"]["id"] == "forecast-123"
    assert "2015-08-03" not in json.dumps(result["job"]["request"])


def test_submit_backtest_returns_structured_api_error(agent_module, fake_context, active_trial_subscription, sample_dataset_path, monkeypatch):
    def fake_post(path, payload, identity, session_id, required_capability):
        raise agent_module.FutureCompleteApiError(
            "FutureComplete API returned HTTP 422 (VALIDATION_ERROR): bad target",
            422,
            json.dumps(
                {
                    "title": "Validation error",
                    "status": 422,
                    "detail": "bad target",
                    "code": "VALIDATION_ERROR",
                    "errors": [{"loc": ["targets"], "msg": "unknown column"}],
                }
            ),
            "POST",
            "https://api.forecasting.inait.ai/v1/backtest",
        )

    monkeypatch.setattr(agent_module, "_post_futurecomplete", fake_post)

    result = tool_json(
        agent_module.submit_backtest,
        fake_context,
        target_columns="NOT_A_COLUMN",
        horizon=4,
        prediction_stride=1,
        prediction_intervals="0.8,0.95",
        backtest_size=20,
        dataset_reference=str(sample_dataset_path),
    )

    assert result["ok"] is False
    assert result["http_status"] == 422
    assert result["error_code"] == "VALIDATION_ERROR"
    assert result["error_detail"] == "bad target"
    assert result["errors"][0]["loc"] == ["targets"]


def test_structured_api_error_supports_apim_message_shape(agent_module):
    error = agent_module.FutureCompleteApiError(
        "FutureComplete API returned HTTP 401",
        401,
        json.dumps({"statusCode": 401, "message": "Access denied due to missing subscription key."}),
        "POST",
        "https://api.forecasting.inait.ai/v1/backtest",
    )

    payload = agent_module._futurecomplete_error_payload(error)

    assert payload["http_status"] == 401
    assert payload["error_code"] == 401
    assert payload["error_detail"] == "Access denied due to missing subscription key."


def test_submit_backtest_posts_public_backtest_payload(agent_module, fake_context, active_trial_subscription, sample_dataset_path, monkeypatch):
    captured = {}

    def fake_post(path, payload, identity, session_id, required_capability):
        captured.update(path=path, payload=payload, required_capability=required_capability)
        return {
            "status": "running",
            "response": {"operation_type": "backtest", "session_id": "backtest-123", "data": {}},
        }

    monkeypatch.setattr(agent_module, "_post_futurecomplete", fake_post)

    result = tool_json(
        agent_module.submit_backtest,
        fake_context,
        target_columns="AAPL",
        horizon=4,
        prediction_stride=4,
        prediction_intervals="0.8,0.95",
        feature_columns="MSFT",
        run_explain=False,
        backtest_size=20,
        dataset_reference=str(sample_dataset_path),
    )

    assert result["ok"] is True
    assert captured["path"] == "/v1/backtest"
    assert captured["required_capability"] == "backtest"
    args = captured["payload"]["config"]["operation_arguments"]
    assert args["operation_type"] == "backtest"
    assert args["prediction_interval_levels"] == "80,95"
    assert args["prediction_stride"] == 4
    assert args["backtest_size"] == 20


def test_submit_backtest_allows_stride_one_and_validates_window(agent_module, fake_context, active_trial_subscription, sample_dataset_path, monkeypatch):
    captured = {}

    def fake_post(path, payload, identity, session_id, required_capability):
        captured.update(path=path, payload=payload, required_capability=required_capability)
        return {
            "status": "running",
            "response": {"operation_type": "backtest", "session_id": "stride-one-123", "data": {}},
        }

    monkeypatch.setattr(agent_module, "_post_futurecomplete", fake_post)

    stride_result = tool_json(
        agent_module.submit_backtest,
        fake_context,
        target_columns="AAPL",
        horizon=5,
        prediction_stride=1,
        prediction_intervals="0.8,0.95",
        backtest_size=20,
        dataset_reference=str(sample_dataset_path),
    )
    assert stride_result["ok"] is True
    assert captured["payload"]["config"]["operation_arguments"]["prediction_stride"] == 1
    assert captured["payload"]["config"]["operation_arguments"]["prediction_interval_levels"] == "80,95"

    window_result = tool_json(
        agent_module.submit_backtest,
        fake_context,
        target_columns="AAPL",
        horizon=5,
        prediction_stride=5,
        prediction_intervals="0.8,0.95",
        backtest_size=20,
        backtest_start_date="2016-01-01",
        backtest_end_date="2016-02-01",
        dataset_reference=str(sample_dataset_path),
    )
    assert window_result["ok"] is False
    assert "either backtest_size or a start/end date range" in window_result["error"]


def test_submit_benchmark_posts_public_benchmark_payload(agent_module, fake_context, active_full_subscription, sample_dataset_path, monkeypatch):
    captured = {}

    def fake_post(path, payload, identity, session_id, required_capability):
        captured.update(path=path, payload=payload, required_capability=required_capability)
        return {
            "status": "running",
            "response": {"operation_type": "benchmark", "session_id": "benchmark-123", "data": {}},
        }

    monkeypatch.setattr(agent_module, "_post_futurecomplete", fake_post)

    result = tool_json(
        agent_module.submit_benchmark,
        fake_context,
        target_columns="AAPL",
        horizon=4,
        prediction_stride=4,
        prediction_intervals="0.8,0.95",
        feature_columns="MSFT",
        run_explain=False,
        backtest_size=20,
        dataset_reference=str(sample_dataset_path),
    )

    assert result["ok"] is True
    assert captured["path"] == "/v1/benchmark"
    assert captured["required_capability"] == "benchmark"
    args = captured["payload"]["config"]["operation_arguments"]
    assert args["operation_type"] == "benchmark"
    assert args["backtest_config"]["operation_type"] == "backtest"


def test_cancel_job_calls_session_cancel_endpoint(agent_module, fake_context, active_trial_subscription, monkeypatch):
    agent_module._jobs_by_session[fake_context.session.session_id] = [
        {"id": "backtest-123", "type": "backtest", "status": "running"}
    ]
    captured = {}

    def fake_cancel(path, identity, session_id, required_capability):
        captured.update(path=path, session_id=session_id, required_capability=required_capability)
        return {"status": "success", "message": "Cancellation requested", "session_id": "backtest-123"}

    monkeypatch.setattr(agent_module, "_post_futurecomplete_without_body", fake_cancel)

    result = tool_json(agent_module.cancel_job, fake_context, session_id="backtest-123")

    assert result["ok"] is True
    assert captured["path"] == "/v1/sessions/backtest-123/cancel"
    assert captured["required_capability"] == "backtest"
    assert agent_module._jobs_by_session[fake_context.session.session_id][0]["status"] == "cancelled"


def test_get_job_status_checks_latest_remembered_job(agent_module, fake_context, active_trial_subscription, monkeypatch):
    agent_module._jobs_by_session[fake_context.session.session_id] = [
        {"id": "backtest-123", "type": "backtest", "status": "queued", "dashboard_url": "https://futurecomplete.inait.ai/jobs/backtest-123"}
    ]
    captured = {}

    def fake_get(path, identity, session_id, required_capability):
        captured.update(path=path, session_id=session_id, required_capability=required_capability)
        return {"status": "completed", "response": {"operation_type": "backtest", "session_id": "backtest-123", "data": {}}}

    monkeypatch.setattr(agent_module, "_get_futurecomplete", fake_get)

    result = tool_json(agent_module.get_job_status, fake_context)

    assert result["ok"] is True
    assert result["session_id"] == "backtest-123"
    assert result["status"] == "completed"
    assert result["is_terminal"] is True
    assert captured["path"] == "/v1/sessions/backtest-123/status"
    assert captured["required_capability"] == "backtest"
    assert agent_module._jobs_by_session[fake_context.session.session_id][0]["status"] == "completed"


def test_get_job_status_checks_explicit_session_id(agent_module, fake_context, active_trial_subscription, monkeypatch):
    captured = {}

    def fake_get(path, identity, session_id, required_capability):
        captured.update(path=path, session_id=session_id, required_capability=required_capability)
        return {"status": "queued", "response": {"operation_type": "backtest", "session_id": "external-456", "data": {}}}

    monkeypatch.setattr(agent_module, "_get_futurecomplete", fake_get)

    result = tool_json(agent_module.get_job_status, fake_context, session_id="external-456")

    assert result["ok"] is True
    assert result["session_id"] == "external-456"
    assert result["status"] == "queued"
    assert result["is_terminal"] is False
    assert captured["path"] == "/v1/sessions/external-456/status"
    assert captured["required_capability"] == "backtest"


def test_polling_posts_result_summary_when_job_completes(agent_module, fake_context, monkeypatch):
    sent_messages = []

    class FakeTurnContext:
        async def send_activity(self, message):
            sent_messages.append(message)

    def fake_get(path, identity, session_id, required_capability):
        if path.endswith("/status"):
            return {"status": "completed", "response": {"operation_type": "backtest", "session_id": "backtest-123"}}
        if path.endswith("/result"):
            return {
                "status": "completed",
                "response": {
                    "operation_type": "backtest",
                    "session_id": "backtest-123",
                    "resource_id": "result:backtest-123",
                    "data": {
                        "metrics": {"mae": 1.25, "rmse": 2.5},
                        "predictions": [{"date": "2016-01-01", "value": 123.45}],
                    },
                },
            }
        raise AssertionError(f"Unexpected path: {path}")

    monkeypatch.setattr(agent_module, "_get_futurecomplete", fake_get)
    agent_module.config.futurecomplete_poll_interval_seconds = 0
    agent_module.config.futurecomplete_poll_max_attempts = 1
    job = {
        "id": "backtest-123",
        "type": "backtest",
        "status": "running",
        "dashboard_url": "https://futurecomplete.inait.ai/jobs/backtest-123",
    }

    asyncio.run(agent_module._poll_job_and_notify(FakeTurnContext(), fake_context.session.session_id, job))

    assert len(sent_messages) == 1
    assert "is completed" in sent_messages[0]
    assert "Result summary:" in sent_messages[0]
    assert "mae" in sent_messages[0]
    assert "predictions" in sent_messages[0]
    assert "Open the dashboard" in sent_messages[0]
    assert job["result_summary"]["data"]["metrics"]["mae"] == 1.25
    assert job["result_summary"]["data"]["predictions"]["type"] == "array"


def test_list_jobs_returns_remembered_jobs(agent_module, fake_context):
    agent_module._jobs_by_session[fake_context.session.session_id] = [
        {"id": "job-1", "type": "backtest", "status": "running"}
    ]

    result = tool_json(agent_module.list_jobs, fake_context)

    assert result == {"ok": True, "jobs": [{"id": "job-1", "type": "backtest", "status": "running"}]}
