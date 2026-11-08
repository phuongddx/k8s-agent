import json
import logging
import threading

from k8s_agent.graph import DeadlineExceeded
from k8s_agent.kube import KubeError
from k8s_agent.llm import ModelUnavailable
from k8s_agent.logs import JsonFormatter
from k8s_agent.service import BUSY, MAX_QUESTION_CHARS, MODEL_DOWN, TIMEOUT, TOO_LONG, AgentService

STATS = {
    "namespace": "x",
    "workload": "x",
    "symptom": "crashloop",
    "tools": ["pod_logs"],
    "calls": 1,
    "budget_hit": False,
    "time_hit": True,
    "prompt_tokens": 500,
    "completion_tokens": 90,
}


def _raise(exc):
    def fn(_):
        raise exc

    return fn


def test_diagnose_returns_result_and_logs_stats(caplog):
    caplog.set_level(logging.INFO, logger="k8s_agent")
    svc = AgentService(lambda q: ({"cause": "c"}, STATS), lambda pid: {})
    assert svc.diagnose("why?") == {"cause": "c"}
    [entry] = [json.loads(JsonFormatter().format(r)) for r in caplog.records]
    assert entry["event"] == "diagnose.done"
    assert entry["question"] == "why?"
    assert entry["tools"] == ["pod_logs"]
    assert entry["prompt_tokens"] == 500 and entry["outcome"] == "ok" and "duration_ms" in entry
    assert entry["budget_hit"] is False and entry["time_hit"] is True


def test_error_mapping():
    assert AgentService(_raise(ModelUnavailable("APITimeoutError")), None).diagnose("q") == {"error": MODEL_DOWN}
    assert AgentService(_raise(DeadlineExceeded()), None).diagnose("q") == {"error": TIMEOUT}
    assert AgentService(_raise(KubeError(503, "/x", "down")), None).diagnose("q") == {
        "error": "Kubernetes API error 503: down"
    }
    assert AgentService(_raise(RuntimeError("boom")), None).diagnose("q") == {"error": "Internal error (RuntimeError)."}
    assert "error" in AgentService(None, None).diagnose("   ")


def test_second_request_while_busy_gets_busy():
    started, release = threading.Event(), threading.Event()

    def slow(q):
        started.set()
        release.wait(5)
        return {"cause": "c"}, STATS

    svc = AgentService(slow, lambda pid: {"pr_url": "u"})
    results = []
    t = threading.Thread(target=lambda: results.append(svc.diagnose("first")))
    t.start()
    started.wait(5)
    assert svc.diagnose("second") == {"error": BUSY}
    assert svc.open_pr("abc") == {"error": BUSY}
    release.set()
    t.join(5)
    assert results == [{"cause": "c"}]
    assert svc.diagnose("third") == {"cause": "c"}
    assert svc.open_pr(" abc ") == {"pr_url": "u"}


def test_lock_is_released_after_errors():
    svc = AgentService(_raise(ModelUnavailable("x")), lambda pid: {"pr_url": "u"})
    svc.diagnose("q")
    assert svc.open_pr("p") == {"pr_url": "u"}


# --- question size ---------------------------------------------------------------------------------


def test_question_over_the_limit_is_rejected_without_work_or_logging(caplog):
    caplog.set_level(logging.DEBUG)
    calls = []
    svc = AgentService(lambda q: calls.append(q) or ({"cause": "c"}, STATS), lambda pid: {"pr_url": "u"})
    long_question = "SECRET" + "x" * (MAX_QUESTION_CHARS - 5)  # 2001 characters
    assert len(long_question) == MAX_QUESTION_CHARS + 1
    assert svc.diagnose(f"  {long_question}\n") == {"error": TOO_LONG}
    assert TOO_LONG == "Question too long (max 2000 characters)."
    assert calls == []
    assert "SECRET" not in caplog.text and not [
        r for r in caplog.records if "SECRET" in json.dumps(vars(r), default=str)
    ]
    assert svc.open_pr("p") == {"pr_url": "u"}  # the lock was never taken


def test_question_at_the_limit_is_accepted():
    svc = AgentService(lambda q: ({"cause": q[:1]}, STATS), lambda pid: {})
    assert svc.diagnose("y" * MAX_QUESTION_CHARS) == {"cause": "y"}
    assert svc.diagnose(" " * 5000 + "y" * MAX_QUESTION_CHARS + " " * 5000) == {"cause": "y"}


def test_every_question_log_is_cut_to_200_characters(caplog):
    caplog.set_level(logging.INFO, logger="k8s_agent")
    started, release = threading.Event(), threading.Event()

    def slow(q):
        started.set()
        release.wait(5)
        return {"cause": "c"}, STATS

    svc = AgentService(slow, lambda pid: {})
    question = "q" * 500
    t = threading.Thread(target=lambda: svc.diagnose(question))
    t.start()
    started.wait(5)
    svc.diagnose(question)  # busy
    release.set()
    t.join(5)
    entries = [json.loads(JsonFormatter().format(r)) for r in caplog.records]
    assert {e["event"] for e in entries} == {"diagnose.busy", "diagnose.done"}
    assert all(e["question"] == "q" * 200 for e in entries)
