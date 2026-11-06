import json
import logging
import threading

from k8s_agent.graph import DeadlineExceeded
from k8s_agent.kube import KubeError
from k8s_agent.llm import ModelUnavailable
from k8s_agent.logs import JsonFormatter
from k8s_agent.service import BUSY, MODEL_DOWN, TIMEOUT, AgentService

STATS = {
    "namespace": "x",
    "workload": "x",
    "symptom": "crashloop",
    "tools": ["pod_logs"],
    "calls": 1,
    "budget_hit": False,
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
