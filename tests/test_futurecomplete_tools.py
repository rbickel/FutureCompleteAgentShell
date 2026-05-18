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


def test_get_trial_subscription_sanitizes_subscription_key(agent_module, fake_context, monkeypatch):
    def fake_create_trial(identity, user_email=None):
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
    assert "super-secret" not in json.dumps(result)


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
            "location": "https://inait-saas-apim-jjyzmt7v.azure-api.net/v1/sessions/forecast-123/status",
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
    assert captured["payload"]["config"]["operation_arguments"]["targets"] == ["AAPL"]
    assert "AAPL" in captured["payload"]["data"]
    assert result["job"]["id"] == "forecast-123"
    assert "2015-08-03" not in json.dumps(result["job"]["request"])


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
    assert args["prediction_stride"] == 4
    assert args["backtest_size"] == 20


def test_submit_backtest_validates_stride_and_window(agent_module, fake_context, active_trial_subscription, sample_dataset_path):
    stride_result = tool_json(
        agent_module.submit_backtest,
        fake_context,
        target_columns="AAPL",
        horizon=5,
        prediction_stride=2,
        prediction_intervals="0.8,0.95",
        dataset_reference=str(sample_dataset_path),
    )
    assert stride_result["ok"] is False
    assert "multiple of horizon" in stride_result["error"]

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


def test_list_jobs_returns_remembered_jobs(agent_module, fake_context):
    agent_module._jobs_by_session[fake_context.session.session_id] = [
        {"id": "job-1", "type": "backtest", "status": "running"}
    ]

    result = tool_json(agent_module.list_jobs, fake_context)

    assert result == {"ok": True, "jobs": [{"id": "job-1", "type": "backtest", "status": "running"}]}
