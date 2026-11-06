"""Run one recorded scenario through the real graph with any ChatModel."""

import tempfile
from pathlib import Path

from evals.scoring import score
from k8s_agent.graph import build_graph, run_diagnosis
from k8s_agent.llm import ChatModel
from k8s_agent.render import Renderer
from tests.graph_fakes import scenario_deps
from tests.scenarios import Scenario


def run_scenario(scenario: Scenario, model: ChatModel, renderer: Renderer | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"k8s-agent-eval-{scenario.name}-") as tmp:
        deps = scenario_deps(Path(tmp), scenario, model, renderer=renderer)
        result, stats = run_diagnosis(build_graph(deps), deps, scenario.question)
    return {
        "scenario": scenario.name,
        "question": scenario.question,
        "result": result,
        "stats": stats,
        "auto": score(scenario, result),
    }
