import hashlib
import json
import logging
import os
from contextlib import contextmanager
from urllib.parse import urlparse
from typing import Any, Iterator

LOGGER_NAME = "futurecomplete.agent"

logger = logging.getLogger(LOGGER_NAME)
logger.setLevel(logging.INFO)

try:
    from azure.monitor.opentelemetry import configure_azure_monitor
except Exception:  # pragma: no cover - optional dependency in local tests
    configure_azure_monitor = None

try:
    from agent_framework.observability import create_resource, enable_instrumentation
except Exception:  # pragma: no cover - optional dependency in local tests
    create_resource = None
    enable_instrumentation = None

try:
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, Status, StatusCode, TraceFlags, set_span_in_context
except Exception:  # pragma: no cover - optional dependency in local tests
    trace = None
    NonRecordingSpan = None
    SpanKind = None
    SpanContext = None
    Status = None
    StatusCode = None
    TraceFlags = None
    set_span_in_context = None


_TELEMETRY_ENABLED = False
_tracer = trace.get_tracer(LOGGER_NAME) if trace is not None else None


def configure_telemetry() -> bool:
    global _TELEMETRY_ENABLED, _tracer
    if _TELEMETRY_ENABLED:
        return True
    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not connection_string:
        return False
    if configure_azure_monitor is None:
        logger.warning("APPLICATIONINSIGHTS_CONNECTION_STRING is set, but azure-monitor-opentelemetry is not installed.")
        return False
    os.environ.setdefault("AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING", "true")
    os.environ.setdefault("ENABLE_INSTRUMENTATION", "true")
    os.environ.setdefault("OTEL_SERVICE_NAME", os.environ.get("APPLICATIONINSIGHTS_ROLE_NAME") or "FutureCompleteAgentShell")
    options: dict[str, Any] = {
        "connection_string": connection_string,
        "logger_name": LOGGER_NAME,
        "enable_live_metrics": True,
    }
    if create_resource is not None:
        options["resource"] = create_resource(service_name=os.environ["OTEL_SERVICE_NAME"])
    configure_azure_monitor(**options)
    if enable_instrumentation is not None:
        enable_instrumentation(enable_sensitive_data=_env_flag("ENABLE_SENSITIVE_DATA"))
    else:
        logger.warning("Agent Framework observability is not installed; agent spans will not be emitted.")
    _TELEMETRY_ENABLED = True
    if trace is not None:
        _tracer = trace.get_tracer(LOGGER_NAME)
    return True


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def conversation_trace_id(conversation_id: str | None) -> int:
    source = conversation_id or "unknown-conversation"
    digest = hashlib.sha256(source.encode("utf-8")).digest()[:16]
    trace_id = int.from_bytes(digest, byteorder="big")
    return trace_id or 1


@contextmanager
def conversation_span(
    name: str,
    conversation_id: str | None,
    session_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    span_attributes = _base_dimensions(conversation_id, session_id, attributes)
    if _tracer is None or SpanContext is None or NonRecordingSpan is None or set_span_in_context is None or TraceFlags is None:
        yield None
        return

    parent_context = set_span_in_context(
        NonRecordingSpan(
            SpanContext(
                trace_id=conversation_trace_id(conversation_id),
                span_id=1,
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
        )
    )
    with _tracer.start_as_current_span(name, context=parent_context, attributes=span_attributes) as span:
        yield span


@contextmanager
def dependency_span(
    name: str,
    conversation_id: str | None,
    session_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    span_attributes = _base_dimensions(conversation_id, session_id, attributes)
    if _tracer is None or SpanKind is None:
        yield None
        return

    context = None
    if trace is not None and SpanContext is not None and NonRecordingSpan is not None and set_span_in_context is not None and TraceFlags is not None:
        try:
            current_span = trace.get_current_span()
            if current_span is None or not current_span.is_recording():
                context = set_span_in_context(
                    NonRecordingSpan(
                        SpanContext(
                            trace_id=conversation_trace_id(conversation_id),
                            span_id=1,
                            is_remote=True,
                            trace_flags=TraceFlags(TraceFlags.SAMPLED),
                        )
                    )
                )
        except Exception:
            context = None

    kwargs = {"kind": SpanKind.CLIENT, "attributes": span_attributes}
    if context is not None:
        kwargs["context"] = context

    with _tracer.start_as_current_span(name, **kwargs) as span:
        try:
            yield span
        except Exception as error:
            if span is not None:
                span.record_exception(error)
                if Status is not None and StatusCode is not None:
                    span.set_status(Status(StatusCode.ERROR, str(error)))
            raise


def http_dependency_attributes(method: str, url: str, attributes: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed = urlparse(url)
    target = parsed.netloc or parsed.hostname or "unknown-target"
    result: dict[str, Any] = {
        "dependency.type": "HTTP",
        "http.method": method,
        "http.request.method": method,
        "http.url": url,
        "url.full": url,
        "url.scheme": parsed.scheme or "https",
        "http.host": target,
        "server.address": parsed.hostname or target,
        "server.port": parsed.port or (443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None),
        "net.peer.name": parsed.hostname or target,
        "network.protocol.name": parsed.scheme or "https",
    }
    if parsed.path:
        result["url.path"] = parsed.path
    if attributes:
        result.update(attributes)
    return result


def set_http_span_result(span: Any, status_code: int | None):
    if span is None or status_code is None:
        return
    span.set_attribute("http.status_code", status_code)
    span.set_attribute("http.response.status_code", status_code)
    if status_code >= 400 and Status is not None and StatusCode is not None:
        span.set_status(Status(StatusCode.ERROR, f"HTTP {status_code}"))


def log_conversation_event(
    event_name: str,
    conversation_id: str | None,
    session_id: str | None = None,
    direction: str | None = None,
    text: str | None = None,
    attributes: dict[str, Any] | None = None,
):
    dimensions = _base_dimensions(conversation_id, session_id, attributes)
    dimensions["event_name"] = event_name
    if direction:
        dimensions["direction"] = direction
    if text is not None:
        dimensions["text"] = text
        dimensions["text_length"] = len(text)

    logger.info(
        "%s %s",
        event_name,
        json.dumps(_compact_dimensions(dimensions), sort_keys=True, default=str),
        extra={"custom_dimensions": dimensions},
    )

    _record_span_event(event_name, conversation_id, session_id, dimensions)


def _record_span_event(event_name: str, conversation_id: str | None, session_id: str | None, dimensions: dict[str, Any]):
    if trace is None or _tracer is None:
        return

    try:
        current_span = trace.get_current_span()
        if current_span is not None and current_span.is_recording():
            current_span.add_event(event_name, dimensions)
            return

        with conversation_span(f"{event_name}.event", conversation_id, session_id, dimensions) as span:
            if span is not None:
                span.add_event(event_name, dimensions)
    except Exception:
        logger.debug("Failed to record span event for %s", event_name, exc_info=True)


def _base_dimensions(conversation_id: str | None, session_id: str | None, attributes: dict[str, Any] | None = None) -> dict[str, Any]:
    dimensions: dict[str, Any] = {
        "correlation_id": conversation_id or "unknown-conversation",
        "conversation_id": conversation_id or "unknown-conversation",
        "session_id": session_id or "unknown-session",
        "app_role": os.environ.get("APPLICATIONINSIGHTS_ROLE_NAME") or "FutureCompleteAgentShell",
    }
    if attributes:
        for key, value in attributes.items():
            dimensions[str(key)] = _telemetry_value(value)
    return dimensions


def _telemetry_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple, set)):
        return json.dumps(list(value), sort_keys=True, default=str)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def _compact_dimensions(dimensions: dict[str, Any]) -> dict[str, Any]:
    compact = dict(dimensions)
    text = compact.get("text")
    if isinstance(text, str) and len(text) > 500:
        compact["text"] = text[:500] + "..."
    return compact
