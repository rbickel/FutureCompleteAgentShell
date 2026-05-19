import logging
import sys


def test_conversation_trace_id_is_deterministic(repo_root):
    sys.path.insert(0, str(repo_root / "src"))
    try:
        import telemetry

        first = telemetry.conversation_trace_id("conversation-123")
        second = telemetry.conversation_trace_id("conversation-123")
        other = telemetry.conversation_trace_id("conversation-456")
    finally:
        if str(repo_root / "src") in sys.path:
            sys.path.remove(str(repo_root / "src"))

    assert first == second
    assert first != other
    assert first > 0


def test_log_conversation_event_adds_correlation_dimensions(repo_root, caplog):
    sys.path.insert(0, str(repo_root / "src"))
    try:
        import telemetry

        with caplog.at_level(logging.INFO, logger=telemetry.LOGGER_NAME):
            telemetry.log_conversation_event(
                "conversation.inbound",
                "conversation-123",
                "session-123",
                direction="inbound",
                text="hello",
                attributes={"channel_id": "msteams"},
            )
    finally:
        if str(repo_root / "src") in sys.path:
            sys.path.remove(str(repo_root / "src"))

    record = next(record for record in caplog.records if record.name == telemetry.LOGGER_NAME)
    dimensions = record.custom_dimensions
    assert dimensions["correlation_id"] == "conversation-123"
    assert dimensions["conversation_id"] == "conversation-123"
    assert dimensions["session_id"] == "session-123"
    assert dimensions["direction"] == "inbound"
    assert dimensions["text"] == "hello"
    assert dimensions["channel_id"] == "msteams"


def test_http_dependency_attributes_include_default_https_port(repo_root):
    sys.path.insert(0, str(repo_root / "src"))
    try:
        import telemetry

        attributes = telemetry.http_dependency_attributes("GET", "https://api.forecasting.inait.ai/v1/sessions/123/status")
    finally:
        if str(repo_root / "src") in sys.path:
            sys.path.remove(str(repo_root / "src"))

    assert attributes["dependency.type"] == "HTTP"
    assert attributes["http.request.method"] == "GET"
    assert attributes["server.address"] == "api.forecasting.inait.ai"
    assert attributes["server.port"] == 443
    assert attributes["url.path"] == "/v1/sessions/123/status"
