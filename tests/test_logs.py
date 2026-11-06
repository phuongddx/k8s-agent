import json
import logging

from k8s_agent.logs import JsonFormatter, configure_logging, log_event


def _capture(caplog):
    return [json.loads(JsonFormatter().format(r)) for r in caplog.records]


def test_log_event_writes_json_with_allowed_fields(caplog):
    caplog.set_level(logging.INFO, logger="k8s_agent")
    log_event("tool.call", tool="pod_logs", duration_ms=12, prompt_tokens=100)
    [entry] = _capture(caplog)
    assert entry["event"] == "tool.call"
    assert entry["tool"] == "pod_logs"
    assert entry["duration_ms"] == 12
    assert entry["level"] == "info"
    assert "ts" in entry


def test_unknown_fields_are_dropped_by_name_only(caplog):
    caplog.set_level(logging.INFO, logger="k8s_agent")
    log_event("x", prompt="secret prompt text", output="tool body", token="ghp_x", tool="events")
    [entry] = _capture(caplog)
    assert entry["dropped_fields"] == ["output", "prompt", "token"]
    assert entry["tool"] == "events"
    text = json.dumps(entry)
    assert "secret prompt text" not in text
    assert "tool body" not in text
    assert "ghp_x" not in text


def test_configure_logging_quiets_third_party_loggers():
    configure_logging()
    for name in ("httpx", "httpx2", "httpcore", "uvicorn", "uvicorn.access", "uvicorn.error", "mcp", "openai"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
