import pytest

from k8s_agent.kube import KubeError
from k8s_agent.tools.cluster import ClusterTools, cpu_m, mem_mi
from tests import scenarios
from tests.fakes import FakeKube


def tools_for(scenario, **kw):
    return ClusterTools(FakeKube(scenario.snapshot), **kw)


def test_quantities():
    assert mem_mi("131072Ki") == 128
    assert mem_mi("1Gi") == 1024
    assert mem_mi("268435456") == 256
    assert cpu_m("250m") == 250
    assert cpu_m("1") == 1000
    assert cpu_m("12000000n") == 12


def test_list_workloads_covers_all_namespaces_and_filters():
    t = tools_for(scenarios.crashloop())
    names = [(w["namespace"], w["name"], w["kind"]) for w in t.list_workloads()]
    assert names == [("dropitx-api", "dropitx-api", "Deployment"), ("openclaw", "openclaw", "Deployment")]
    assert [w["name"] for w in t.list_workloads("openclaw")] == ["openclaw"]


def test_crashloop_pod_shows_waiting_reason_and_last_exit_code():
    t = tools_for(scenarios.crashloop())
    [pod] = t.list_pods("dropitx-api", "dropitx-api")
    [c] = pod["containers"]
    assert c["reason"] == "CrashLoopBackOff"
    assert c["last_termination"] == {"reason": "Error", "exit_code": 1, "finished_at": "2026-11-05T09:58:12Z"}
    assert pod["restarts"] == 14


def test_previous_logs_are_fetched_with_timestamps():
    t = tools_for(scenarios.crashloop())
    out = t.pod_logs("dropitx-api", "dropitx-api-6d5f7-x2k9p", previous=True)
    assert "DATABASE_URL is not set" in out["log"]
    assert out["previous"] is True


def test_logs_are_capped_by_lines_and_bytes():
    s = scenarios.crashloop()
    path = "/api/v1/namespaces/dropitx-api/pods/dropitx-api-6d5f7-x2k9p/log"
    s.snapshot[path] = "\n".join(f"2026-11-05T09:00:{i % 60:02d}Z line {i} " + "x" * 300 for i in range(500))
    out = ClusterTools(FakeKube(s.snapshot)).pod_logs("dropitx-api", "dropitx-api-6d5f7-x2k9p")
    assert out["lines"] <= 200
    assert len(out["log"].encode()) <= 32768
    assert out["truncated"] is True
    assert out["log"].endswith("x" * 300)
    small = ClusterTools(FakeKube(s.snapshot), log_max_lines=5).pod_logs(
        "dropitx-api", "dropitx-api-6d5f7-x2k9p", lines=50
    )
    assert small["lines"] == 5


def test_oomkilled_shows_limit_usage_and_node_headroom():
    t = tools_for(scenarios.oomkilled())
    [pod] = t.list_pods("llm-gateway", "llm-gateway")
    assert pod["containers"][0]["last_termination"]["reason"] == "OOMKilled"
    assert pod["containers"][0]["last_termination"]["exit_code"] == 137
    assert t.describe_workload("llm-gateway", "llm-gateway")["containers"][0]["limits"] == {"memory": "128Mi"}
    assert t.top_pods("llm-gateway") == [{"pod": "llm-gateway-5b8c4-q7w2e", "cpu_m": 20, "memory_mi": 121}]
    [node] = t.top_nodes()
    assert node == {
        "node": "srv1980192",
        "cpu_m": 900,
        "memory_mi": 4900,
        "allocatable_memory_mi": 7935,
        "memory_percent": 62,
    }


def test_imagepull_message_and_events():
    t = tools_for(scenarios.imagepull())
    [pod] = t.list_pods("stress-api")
    assert "manifest unknown" in pod["containers"][0]["message"]
    [ev] = t.events("stress-api", involved="stress-app-be")
    assert ev["reason"] == "Failed"
    assert ev["object"] == "Pod/stress-app-be-7f6d9-m3n4b"


def test_pending_shows_scheduling_problem_and_stuck_rollout():
    t = tools_for(scenarios.pending())
    [pod] = t.list_pods("dropitx-fe", "dropitx-fe")
    assert pod["phase"] == "Pending"
    assert pod["problems"] == [
        {
            "type": "PodScheduled",
            "reason": "Unschedulable",
            "message": "0/1 nodes are available: 1 Insufficient memory.",
        }
    ]
    status = t.rollout_status("dropitx-fe", "dropitx-fe")
    assert status["complete"] is False
    assert status["replicasets"][0]["revision"] == "2"
    assert status["conditions"][0]["reason"] == "ProgressDeadlineExceeded"


def test_5xx_route_to_unready_endpoints():
    t = tools_for(scenarios.gateway_5xx())
    [route] = t.get_ingressroutes()
    assert route["routes"][0] == {"match": "Host(`sso.dropitx.site`)", "services": ["keycloak:8080"], "middlewares": []}
    assert t.get_ingressroutes("auth") == [route]
    ep = t.get_endpoints("auth", "keycloak")
    assert ep["ready"] == []
    assert ep["not_ready"] == [{"ip": "10.42.0.31", "pod": "keycloak-59d7c-p0o9i", "ports": [8080]}]


def test_events_keep_the_last_twenty_in_time_order():
    s = scenarios.healthy()
    s.snapshot["/api/v1/namespaces/openclaw/events"] = {
        "items": [
            scenarios.event("openclaw", "Pod", "p", "R", f"m{i}", f"2026-11-05T10:{i:02d}:00Z") for i in range(30)
        ][::-1]
    }
    out = ClusterTools(FakeKube(s.snapshot)).events("openclaw", limit=50)
    assert len(out) == 20
    assert out[0]["message"] == "m10"
    assert out[-1]["message"] == "m29"


def test_configmap_and_missing_objects():
    t = tools_for(scenarios.crashloop())
    assert t.get_configmap("dropitx-api", "dropitx-api-config")["data"] == {"PORT": "8080", "LOG_LEVEL": "info"}
    with pytest.raises(KubeError) as info:
        t.get_configmap("dropitx-api", "nope")
    assert info.value.status == 404
    with pytest.raises(KubeError):
        t.describe_workload("dropitx-api", "nope")
    with pytest.raises(ValueError):
        t.describe_workload("dropitx-api", "dropitx-api", kind="Job")


def test_registry_exposes_the_twelve_cluster_tools():
    assert sorted(tools_for(scenarios.healthy()).registry()) == sorted(
        [
            "list_workloads",
            "describe_workload",
            "list_pods",
            "describe_pod",
            "pod_logs",
            "events",
            "top_pods",
            "top_nodes",
            "rollout_status",
            "get_ingressroutes",
            "get_endpoints",
            "get_configmap",
        ]
    )
