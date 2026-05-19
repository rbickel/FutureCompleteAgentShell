import logging
import sys


def _import_telemetry(repo_root):
    sys.path.insert(0, str(repo_root / "src"))
    try:
        import telemetry
    finally:
        if str(repo_root / "src") in sys.path:
            sys.path.remove(str(repo_root / "src"))
    return telemetry


def test_configure_telemetry_enables_agent_framework_observability(repo_root, monkeypatch):
    telemetry = _import_telemetry(repo_root)
    calls = []

    def fake_configure_azure_monitor(**kwargs):
        calls.append(("azure_monitor", kwargs))

    def fake_create_resource(**kwargs):
        calls.append(("create_resource", kwargs))
        return "resource"

    def fake_enable_instrumentation(**kwargs):
        calls.append(("enable_instrumentation", kwargs))

    monkeypatch.setattr(telemetry, "_TELEMETRY_ENABLED", False)
    monkeypatch.setattr(telemetry, "configure_azure_monitor", fake_configure_azure_monitor)
    monkeypatch.setattr(telemetry, "create_resource", fake_create_resource)
    monkeypatch.setattr(telemetry, "enable_instrumentation", fake_enable_instrumentation)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=test")
    monkeypatch.setenv("APPLICATIONINSIGHTS_ROLE_NAME", "FutureCompleteAgentShell-test")
    monkeypatch.setenv("ENABLE_SENSITIVE_DATA", "true")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)

    assert telemetry.configure_telemetry() is True

    assert calls[0] == ("create_resource", {"service_name": "FutureCompleteAgentShell-test"})
    assert calls[1][0] == "azure_monitor"
    assert calls[1][1]["connection_string"] == "InstrumentationKey=test"
    assert calls[1][1]["resource"] == "resource"
    assert calls[1][1]["enable_live_metrics"] is True
    assert calls[2] == ("enable_instrumentation", {"enable_sensitive_data": True})
    assert telemetry.os.environ["ENABLE_INSTRUMENTATION"] == "true"
    assert telemetry.os.environ["OTEL_SERVICE_NAME"] == "FutureCompleteAgentShell-test"


def test_conversation_trace_id_is_deterministic(repo_root):
    telemetry = _import_telemetry(repo_root)
    first = telemetry.conversation_trace_id("conversation-123")
    second = telemetry.conversation_trace_id("conversation-123")
    other = telemetry.conversation_trace_id("conversation-456")

    assert first == second
    assert first != other
    assert first > 0


def test_log_conversation_event_adds_correlation_dimensions(repo_root, caplog):
    telemetry = _import_telemetry(repo_root)
    with caplog.at_level(logging.INFO, logger=telemetry.LOGGER_NAME):
        telemetry.log_conversation_event(
            "conversation.inbound",
            "conversation-123",
            "session-123",
            direction="inbound",
            text="hello",
            attributes={"channel_id": "msteams"},
        )

    record = next(record for record in caplog.records if record.name == telemetry.LOGGER_NAME)
    dimensions = record.custom_dimensions
    assert dimensions["correlation_id"] == "conversation-123"
    assert dimensions["conversation_id"] == "conversation-123"
    assert dimensions["session_id"] == "session-123"
    assert dimensions["direction"] == "inbound"
    assert dimensions["text"] == "hello"
    assert dimensions["channel_id"] == "msteams"


def test_http_dependency_attributes_include_default_https_port(repo_root):
    telemetry = _import_telemetry(repo_root)
    attributes = telemetry.http_dependency_attributes("GET", "https://api.forecasting.inait.ai/v1/sessions/123/status")

    assert attributes["dependency.type"] == "HTTP"
    assert attributes["http.request.method"] == "GET"
    assert attributes["server.address"] == "api.forecasting.inait.ai"
    assert attributes["server.port"] == 443
    assert attributes["url.path"] == "/v1/sessions/123/status"
