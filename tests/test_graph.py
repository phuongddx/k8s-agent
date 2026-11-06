import pytest

from k8s_agent.graph import DeadlineExceeded, build_graph, run_diagnosis
from k8s_agent.llm import ModelUnavailable
from k8s_agent.render import KustomizeRenderer
from k8s_agent.repo import Workspace
from k8s_agent.settings import Budgets
from tests import scenarios
from tests.graph_fakes import FakeClock, FakeRenderer, ScriptedModel, as_json, scenario_deps, text, tool_calls

CRASH_INTAKE = {
    "namespace": "dropitx-api",
    "workload": "dropitx-api",
    "kind": "Deployment",
    "symptom": "crashloop",
    "clarify": None,
}
CRASH_DIAG = {
    "cause": "DATABASE_URL is missing from the dropitx-api ConfigMap, so the app exits with code 1.",
    "confidence": "high",
    "evidence": [
        {"source": "log", "detail": "2026-11-05T09:58:12.004Z FATAL config: DATABASE_URL is not set"},
        {"source": "manifest apps/dropitx-api/vps192/configmap.yaml:5", "detail": "no DATABASE_URL key"},
    ],
    "fix_kind": "config",
    "fix_summary": "Add DATABASE_URL to the ConfigMap",
}
FIXED_CM = (
    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dropitx-api-config\ndata:\n"
    '  PORT: "8080"\n  LOG_LEVEL: info\n'
    "  DATABASE_URL: postgres://dropitx-postgres.dropitx-api.svc:5432/dropitx\n"
)
FIX_PLAN = {
    "summary": "add DATABASE_URL",
    "edits": [{"path": "apps/dropitx-api/vps192/configmap.yaml", "content": FIXED_CM}],
}


def make_deps(tmp_path, scenario, model, renderer=None, budgets=None, clock=None, loki_up=False):
    return scenario_deps(tmp_path, scenario, model, renderer=renderer, budgets=budgets, clock=clock, loki_up=loki_up)


def run(deps, question="why is dropitx-api crashlooping?"):
    return run_diagnosis(build_graph(deps), deps, question)


def test_clarify_path_stops_after_intake(tmp_path):
    model = ScriptedModel([as_json({"clarify": "Which app do you mean?"})])
    result, stats = run(make_deps(tmp_path, scenarios.crashloop(), model), "is it broken?")
    assert result == {"clarify": "Which app do you mean?"}
    assert len(model.sent) == 1
    assert stats["prompt_tokens"] == 100


def test_unknown_workload_asks_for_clarification(tmp_path):
    model = ScriptedModel([as_json({**CRASH_INTAKE, "workload": "nope"})])
    result, _ = run(make_deps(tmp_path, scenarios.crashloop(), model))
    assert result["clarify"].startswith("I could not find dropitx-api/nope.")
    assert "dropitx-api/dropitx-api" in result["clarify"]


def test_intake_sees_inventory_as_untrusted_data(tmp_path):
    model = ScriptedModel([as_json({"clarify": "?"})])
    run(make_deps(tmp_path, scenarios.crashloop(), model))
    user = model.sent[0][1]["content"]
    assert '<untrusted-data source="workload inventory">' in user
    assert "dropitx-api" in user


def test_full_path_without_diff_for_app_code(tmp_path):
    diag = {**CRASH_DIAG, "fix_kind": "app-code", "fix_summary": None}
    model = ScriptedModel(
        [
            as_json(CRASH_INTAKE),
            tool_calls(("pod_logs", {"namespace": "dropitx-api", "pod": "dropitx-api-6d5f7-x2k9p", "previous": True})),
            text("The previous container logged DATABASE_URL is not set."),
            as_json(diag),
        ]
    )
    deps = make_deps(tmp_path, scenarios.crashloop(), model)
    result, stats = run(deps)
    assert result == {k: v for k, v in diag.items() if v is not None}
    assert stats["tools"] == ["pod_logs"]
    assert stats["prompt_tokens"] == 400 and stats["completion_tokens"] == 80
    triage_msg = model.sent[1][1]["content"]
    assert '<untrusted-data source="triage">' in triage_msg
    assert "CrashLoopBackOff" in triage_msg and "chore: seed" in triage_msg
    tool_msg = model.sent[2][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_0"
    assert tool_msg["content"].startswith('<untrusted-data source="pod_logs">')
    assert "DATABASE_URL is not set" in tool_msg["content"]


def test_tool_call_budget_caps_confidence(tmp_path):
    five = tool_calls(*[("top_nodes", {})] * 5)
    model = ScriptedModel([as_json(CRASH_INTAKE), five, five, five, as_json({**CRASH_DIAG, "fix_kind": "unknown"})])
    result, stats = run(make_deps(tmp_path, scenarios.crashloop(), model))
    assert stats["calls"] == 12
    assert stats["budget_hit"] is True
    assert result["confidence"] == "low"
    assert result["cause"].startswith("Budget hit (12 tool calls or 90 s)")
    diagnose_request = model.sent[4]
    refused = [m for m in diagnose_request if m["role"] == "tool" and "budget exhausted" in m["content"]]
    assert len(refused) == 3


def test_investigation_stops_once_the_twelfth_call_is_used(tmp_path):
    four = tool_calls(*[("top_nodes", {})] * 4)
    model = ScriptedModel([as_json(CRASH_INTAKE), four, four, four, as_json({**CRASH_DIAG, "fix_kind": "unknown"})])
    result, stats = run(make_deps(tmp_path, scenarios.crashloop(), model))
    assert stats["calls"] == 12 and stats["budget_hit"] is True
    assert len(model.sent) == 5
    assert model.replies == []


def test_time_budget_stops_investigation(tmp_path):
    clock = FakeClock()

    def advance():
        clock.now += 50

    model = ScriptedModel(
        [
            as_json(CRASH_INTAKE),
            tool_calls(("top_nodes", {})),
            tool_calls(("top_nodes", {})),
            as_json(CRASH_DIAG | {"fix_kind": "unknown"}),
        ],
        on_call=advance,
    )
    deps = make_deps(
        tmp_path, scenarios.crashloop(), model, clock=clock, budgets=Budgets(investigate_seconds=90, graph_seconds=170)
    )
    result, stats = run(deps)
    assert stats["budget_hit"] is True
    assert stats["calls"] == 1
    assert result["confidence"] == "low"


def test_overall_deadline_raises(tmp_path):
    clock = FakeClock()

    def advance():
        clock.now += 200

    model = ScriptedModel([as_json(CRASH_INTAKE)], on_call=advance)
    with pytest.raises(DeadlineExceeded):
        run(make_deps(tmp_path, scenarios.crashloop(), model, clock=clock))


def test_invalid_json_is_reasked_once_then_falls_back(tmp_path):
    model = ScriptedModel([as_json(CRASH_INTAKE), text("done"), text("not json"), text("still not json")])
    result, _ = run(make_deps(tmp_path, scenarios.crashloop(), model))
    assert result["fix_kind"] == "unknown" and result["confidence"] == "low"
    assert "not valid JSON" in model.sent[3][-1]["content"]


def test_loki_unavailable_is_noted_in_evidence(tmp_path):
    model = ScriptedModel(
        [
            as_json(CRASH_INTAKE),
            tool_calls(("loki_query", {"query": '{app="traefik"}'})),
            text("Loki down."),
            as_json({**CRASH_DIAG, "fix_kind": "unknown"}),
        ]
    )
    result, _ = run(make_deps(tmp_path, scenarios.crashloop(), model))
    assert {"source": "loki", "detail": "Loki was unavailable; Kubernetes logs only."} in result["evidence"]


def test_model_unavailable_propagates(tmp_path):
    model = ScriptedModel([ModelUnavailable("RateLimitError")])
    with pytest.raises(ModelUnavailable):
        run(make_deps(tmp_path, scenarios.crashloop(), model))


def test_config_fix_produces_proposal_and_clean_clone(tmp_path):
    renderer = FakeRenderer()
    model = ScriptedModel([as_json(CRASH_INTAKE), text("found it"), as_json(CRASH_DIAG), as_json(FIX_PLAN)])
    deps = make_deps(tmp_path, scenarios.crashloop(), model, renderer=renderer)
    result, _ = run(deps)
    assert result["fix_summary"] == "add DATABASE_URL"
    assert "+  DATABASE_URL: postgres://" in result["diff"]
    assert renderer.rendered == ["apps/dropitx-api/vps192"]
    proposal = deps.store.get(result["proposal_id"])
    assert proposal.base_sha == deps.workspace.head()
    assert proposal.service == "dropitx-api"
    assert proposal.diff == result["diff"]
    assert "DATABASE_URL" not in (deps.workspace.path / "apps/dropitx-api/vps192/configmap.yaml").read_text()
    assert '<untrusted-data source="repository files' in model.sent[3][1]["content"]


def test_config_fix_renders_with_real_kustomize(tmp_path, kustomize):
    model = ScriptedModel([as_json(CRASH_INTAKE), text("found it"), as_json(CRASH_DIAG), as_json(FIX_PLAN)])
    deps = make_deps(tmp_path, scenarios.crashloop(), model, renderer=KustomizeRenderer(kustomize))
    result, _ = run(deps)
    assert "proposal_id" in result


def test_path_outside_apps_is_rejected_without_diff(tmp_path):
    plan = {"summary": "x", "edits": [{"path": "scripts/validate.sh", "content": "exit 0\n"}]}
    model = ScriptedModel([as_json(CRASH_INTAKE), text("x"), as_json(CRASH_DIAG), as_json(plan)])
    deps = make_deps(tmp_path, scenarios.crashloop(), model)
    result, _ = run(deps)
    assert "diff" not in result and "proposal_id" not in result
    assert "outside apps" in result["fix_summary"]


def test_render_failure_gets_one_repair_attempt(tmp_path):
    renderer = FakeRenderer(fail_times=1)
    model = ScriptedModel([as_json(CRASH_INTAKE), text("x"), as_json(CRASH_DIAG), as_json(FIX_PLAN), as_json(FIX_PLAN)])
    result, _ = run(make_deps(tmp_path, scenarios.crashloop(), model, renderer=renderer))
    assert "proposal_id" in result
    assert "kustomize build" in model.sent[4][-1]["content"]


def test_render_failing_twice_returns_diagnosis_without_diff(tmp_path):
    renderer = FakeRenderer(fail_times=2)
    model = ScriptedModel([as_json(CRASH_INTAKE), text("x"), as_json(CRASH_DIAG), as_json(FIX_PLAN), as_json(FIX_PLAN)])
    result, _ = run(make_deps(tmp_path, scenarios.crashloop(), model, renderer=renderer))
    assert "diff" not in result
    assert result["cause"] == CRASH_DIAG["cause"]
    assert "kustomize build failed" in result["fix_summary"]


def test_repo_unavailable_skips_propose_and_notes_it(tmp_path):
    model = ScriptedModel([as_json(CRASH_INTAKE), text("x"), as_json(CRASH_DIAG)])
    deps = make_deps(tmp_path, scenarios.crashloop(), model)
    deps.workspace = Workspace(tmp_path / "other", str(tmp_path / "missing.git"))
    result, _ = run(deps)
    assert "proposal_id" not in result
    assert any(e["source"] == "repo" for e in result["evidence"])


def test_fix_lands_in_the_apps_dir_of_the_namespace(tmp_path):
    s = scenarios.imagepull()
    good = s.repo_files["apps/stress-app-be/vps192/kustomization.yaml"].replace("abc1234", "9" * 7 + "a" * 33)
    model = ScriptedModel(
        [
            as_json(
                {"namespace": "stress-api", "workload": "stress-app-be", "kind": "Deployment", "symptom": "imagepull"}
            ),
            text("tag abc1234 does not exist"),
            as_json(
                {
                    "cause": "newTag abc1234 does not exist: manifest unknown",
                    "confidence": "high",
                    "evidence": [{"source": "event", "detail": "Failed to pull image: manifest unknown"}],
                    "fix_kind": "config",
                }
            ),
            as_json(
                {
                    "summary": "pin an existing image tag",
                    "edits": [{"path": "apps/stress-app-be/vps192/kustomization.yaml", "content": good}],
                }
            ),
        ]
    )
    deps = make_deps(tmp_path, s, model)
    result, stats = run(deps, s.question)
    assert stats["namespace"] == "stress-api"
    assert "apps/stress-app-be/vps192/kustomization.yaml" in result["diff"]
    assert "chore: seed" in model.sent[1][1]["content"]
