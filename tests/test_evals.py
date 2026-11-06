import pytest

from evals.harness import run_scenario
from evals.run_eval import main
from evals.scoring import corpus, grounded, score
from tests import scenarios
from tests.graph_fakes import ScriptedModel, as_json, text


def test_six_scenarios_cover_the_runbook():
    assert sorted(scenarios.ALL) == ["5xx", "crashloop", "imagepull", "oomkilled", "other", "pending"]


def test_grounded_matches_real_facts_only():
    text_ = corpus(scenarios.crashloop())
    assert grounded("2026-11-05T09:58:12.004Z FATAL config: DATABASE_URL is not set", text_)
    assert grounded("Back-off restarting failed container dropitx-api", text_)
    assert not grounded("the disk on node srv9 is full", text_)


def test_score_hints():
    s = scenarios.crashloop()
    good = {
        "cause": "DATABASE_URL missing",
        "fix_kind": "config",
        "evidence": [
            {"source": "log", "detail": "FATAL config: DATABASE_URL is not set"},
            {"source": "x", "detail": "made up fact here"},
        ],
    }
    assert score(s, good) == {"cause_hint": True, "fix_kind_match": True, "grounded": 1, "evidence": 2}
    assert score(s, {"clarify": "?"})["cause_hint"] is False


def test_harness_runs_a_scenario_offline():
    s = scenarios.healthy()
    model = ScriptedModel(
        [
            as_json({"namespace": "openclaw", "workload": "openclaw", "kind": "Deployment", "symptom": "other"}),
            text("All replicas ready, no restarts."),
            as_json(
                {
                    "cause": "openclaw is healthy: 1/1 ready, no restarts.",
                    "confidence": "high",
                    "evidence": [{"source": "pods", "detail": "openclaw-7c9d8-abcde Running, 0 restarts"}],
                    "fix_kind": "none",
                }
            ),
        ]
    )
    row = run_scenario(s, model)
    assert row["result"]["fix_kind"] == "none"
    assert row["auto"]["cause_hint"] is True and row["auto"]["fix_kind_match"] is True


def test_cli_needs_the_api_key(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    assert main(["--no-prompt"]) == 2


def test_cli_rejects_unknown_scenarios():
    with pytest.raises(SystemExit):
        main(["--scenario", "nope"])
