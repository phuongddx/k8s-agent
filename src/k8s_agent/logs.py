"""Structured JSON logs on stdout. Only allowlisted field names are written, so prompts,
tool output bodies and tokens cannot reach the log by accident."""

import json
import logging
import sys
from datetime import UTC, datetime

LOGGER_NAME = "k8s_agent"

ALLOWED_FIELDS = frozenset(
    {
        "question",
        "namespace",
        "workload",
        "symptom",
        "tool",
        "tools",
        "duration_ms",
        "prompt_tokens",
        "completion_tokens",
        "calls",
        "budget_hit",
        "time_hit",
        "outcome",
        "status",
        "path",
        "proposal_id",
        "pr_url",
        "overlay",
        "attempt",
        "error_type",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Library INFO records carry URLs and bodies that bypass the log_event allowlist.
    for name in ("httpx", "httpx2", "httpcore", "uvicorn", "uvicorn.access", "uvicorn.error", "mcp", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)


MAX_LOGGED_QUESTION_CHARS = 200


def log_event(event: str, level: int = logging.INFO, **fields: object) -> None:
    kept = {k: v for k, v in fields.items() if k in ALLOWED_FIELDS}
    if isinstance(kept.get("question"), str):
        kept["question"] = kept["question"][:MAX_LOGGED_QUESTION_CHARS]
    dropped = sorted(set(fields) - ALLOWED_FIELDS)
    if dropped:
        kept["dropped_fields"] = dropped
    logging.getLogger(LOGGER_NAME).log(level, event, extra={"fields": kept})
