import json

import pytest

from evals import run_eval
from evals.harness import run_scenario
from evals.run_eval import main
from evals.scoring import corpus, grounded, score
from k8s_agent.graph import DeadlineExceeded
from tests import scenarios
from tests.graph_fakes import FakeRenderer, ScriptedModel, as_json, text


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


def test_corpus_has_only_this_scenarios_facts():
    text_ = corpus(scenarios.crashloop()).lower()
    assert "restartcount" not in text_
    assert "ghcr.io/openclaw" not in text_
    assert "dropitx-api" in text_


def test_grounding_is_word_bounded_and_ignores_schema_names():
    text_ = corpus(scenarios.crashloop())
    assert not grounded("restartCount is suspiciously high", text_)
    assert grounded("Back-off restarting failed container dropitx-api", text_)
    assert grounded("Back-off restarting failed container dropitx-api.", text_)


def test_exit_code_and_memory_limit_are_grounded_as_whole_words():
    text_ = corpus(scenarios.oomkilled())
    assert grounded("exit code 137", text_)
    assert grounded("the memory limit is 128Mi", text_)
    assert not grounded("the memory limit is 28Mi", text_)


def test_made_up_sentence_with_a_real_keyword_is_not_grounded():
    assert not grounded("CrashLoopBackOff caused by a corrupted sqlite file", corpus(scenarios.crashloop()))


def test_cause_hint_matches_whole_words_only():
    s = scenarios.healthy()
    assert score(s, {"cause": "openclaw is unhealthy", "fix_kind": "none", "evidence": []})["cause_hint"] is False
    assert score(s, {"cause": "openclaw is healthy", "fix_kind": "none", "evidence": []})["cause_hint"] is True


class _DeadlineModel:
    """Raises DeadlineExceeded on the crashloop question; answers every other question from `inner`."""

    def __init__(self, inner):
        self.inner = inner

    def complete(self, messages, tools=None):
        if any("crashlooping" in (m.get("content") or "") for m in messages):
            raise DeadlineExceeded()
        return self.inner.complete(messages, tools)


def _healthy_replies():
    return [
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


def test_main_contains_a_deadline_per_scenario_and_exits_1(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ZAI_API_KEY", "dummy-test-key")
    monkeypatch.setattr(run_eval, "ALL", {"crashloop": scenarios.crashloop(), "other": scenarios.healthy()})
    monkeypatch.setattr(run_eval, "RESULTS", tmp_path)
    monkeypatch.setattr(run_eval, "OpenAIChatModel", lambda *a, **k: _DeadlineModel(ScriptedModel(_healthy_replies())))
    assert main(["--no-prompt"]) == 1
    (path,) = tmp_path.glob("*.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0] == {"scenario": "crashloop", "error": "DeadlineExceeded"}
    assert rows[1]["scenario"] == "other" and rows[1]["auto"]["cause_hint"] is True
    out = capsys.readouterr().out
    assert "scenario   cause" in out and f"wrote {path}" in out


def test_main_exits_0_when_no_scenario_errored(monkeypatch, tmp_path):
    monkeypatch.setenv("ZAI_API_KEY", "dummy-test-key")
    monkeypatch.setattr(run_eval, "ALL", {"other": scenarios.healthy()})
    monkeypatch.setattr(run_eval, "RESULTS", tmp_path)
    monkeypatch.setattr(run_eval, "OpenAIChatModel", lambda *a, **k: ScriptedModel(_healthy_replies()))
    assert main(["--no-prompt"]) == 0


def test_ask_treats_eof_as_unanswered(monkeypatch):
    def no_stdin(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_stdin)
    assert run_eval.ask("Right cause?") is None


def test_missing_kustomize_warns_once_and_uses_fake_renderer(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ZAI_API_KEY", "dummy-test-key")
    monkeypatch.setattr(run_eval, "ALL", {"other": scenarios.healthy()})
    monkeypatch.setattr(run_eval, "RESULTS", tmp_path)
    monkeypatch.setattr(run_eval, "OpenAIChatModel", lambda *a, **k: ScriptedModel([]))
    monkeypatch.setattr(run_eval.shutil, "which", lambda _name: None)
    seen = {}

    def fake_run(scenario, model, renderer=None):
        seen["renderer"] = renderer
        return {"scenario": scenario.name, "auto": {"cause_hint": True, "fix_kind_match": True}}

    monkeypatch.setattr(run_eval, "run_scenario", fake_run)
    assert main(["--no-prompt"]) == 0
    assert isinstance(seen["renderer"], FakeRenderer)
    assert capsys.readouterr().err.count("kustomize") == 1
