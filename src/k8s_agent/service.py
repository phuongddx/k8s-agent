"""One diagnosis or PR at a time, mapped errors, and one log line per request."""

import logging
import threading
import time
from collections.abc import Callable

from k8s_agent.graph import DeadlineExceeded
from k8s_agent.kube import KubeError
from k8s_agent.llm import ModelUnavailable
from k8s_agent.logs import log_event

BUSY = "Busy with another diagnosis."
MODEL_DOWN = "Model unavailable, try again shortly."
TIMEOUT = "The diagnosis hit its time limit. Ask a narrower question."
EMPTY = "Ask a question about a workload on vps192."
MAX_QUESTION_CHARS = 2000
TOO_LONG = f"Question too long (max {MAX_QUESTION_CHARS} characters)."


class AgentService:
    def __init__(self, diagnose_fn: Callable[[str], tuple[dict, dict]], open_pr_fn: Callable[[str], dict]) -> None:
        self._diagnose_fn = diagnose_fn
        self._open_pr_fn = open_pr_fn
        self._lock = threading.Lock()

    def diagnose(self, question: str) -> dict:
        question = question.strip()
        if not question:
            return {"error": EMPTY}
        if len(question) > MAX_QUESTION_CHARS:
            return {"error": TOO_LONG}  # before the lock, the graph and any log line
        if not self._lock.acquire(blocking=False):
            log_event("diagnose.busy", question=question)
            return {"error": BUSY}
        started = time.monotonic()
        stats: dict = {}
        outcome = "ok"
        try:
            result, stats = self._diagnose_fn(question)
            if "clarify" in result:
                outcome = "clarify"
        except ModelUnavailable as exc:
            outcome, result = "model_unavailable", {"error": MODEL_DOWN}
            stats = {"error_type": str(exc)}
        except DeadlineExceeded:
            outcome, result = "timeout", {"error": TIMEOUT}
        except KubeError as exc:
            outcome, result = "kube_error", {"error": f"Kubernetes API error {exc.status}: {exc.message}"}
        except Exception as exc:  # the MCP caller gets a message, the log gets the type
            logging.getLogger("k8s_agent").exception("diagnose.crashed")
            outcome, result = "crashed", {"error": f"Internal error ({type(exc).__name__})."}
        finally:
            self._lock.release()
        log_event(
            "diagnose.done",
            question=question,
            outcome=outcome,
            duration_ms=round((time.monotonic() - started) * 1000),
            **stats,
        )
        return result

    def open_pr(self, proposal_id: str) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"error": BUSY}
        try:
            return self._open_pr_fn(proposal_id.strip())
        finally:
            self._lock.release()
