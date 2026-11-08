"""Doubles for graph-level tests and the manual eval: a scripted model, a fake renderer and clock,
a mocked Loki, and `scenario_deps`, which wires a whole Deps for one scenario."""

import copy
import json
from datetime import datetime
from pathlib import Path

import httpx2

from k8s_agent.graph import Deps
from k8s_agent.llm import ModelReply, ModelTimeout, ToolCall
from k8s_agent.proposals import ProposalStore
from k8s_agent.render import RenderError
from k8s_agent.repo import Workspace
from k8s_agent.settings import Budgets
from k8s_agent.tools.cluster import ClusterTools
from k8s_agent.tools.loki import LokiClient
from k8s_agent.tools.toolbox import Toolbox
from tests.fakes import FakeKube
from tests.gitrepo import make_origin
from tests.scenarios import Scenario


class ScriptedModel:
    """A ChatModel that returns scripted replies in order and records what it was sent.

    A scripted Exception is raised instead of returned. `on_call` runs before each reply,
    which lets time-budget tests advance a fake clock."""

    def __init__(self, replies, on_call=None) -> None:
        self.replies = list(replies)
        self.sent: list[list[dict]] = []
        self.tools_offered: list[list[dict] | None] = []
        self.on_call = on_call

    def complete(self, messages, tools=None, timeout=None, max_retries=None):
        self.sent.append(copy.deepcopy(messages))
        self.tools_offered.append(tools)
        if self.on_call:
            self.on_call()
        if not self.replies:
            raise AssertionError("ScriptedModel ran out of replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class TimedModel:
    """A ChatModel that lives on a FakeClock. Each scripted reply takes `duration` seconds. A call whose
    per-call `timeout` is shorter than that raises a retryable ModelTimeout after exactly `timeout` seconds,
    as the real adapter does. A scripted Exception is raised after `duration`. Records (clock at call,
    timeout, max_retries) for every call. The fake never retries on its own: that is the graph's job, and
    `max_retries` is recorded so tests can assert the graph switched the SDK retry off."""

    def __init__(self, clock, script) -> None:
        self.clock = clock
        self.script = list(script)
        self.calls: list[tuple[float, float | None, int | None]] = []

    def complete(self, messages, tools=None, timeout=None, max_retries=None):
        self.calls.append((self.clock.now, timeout, max_retries))
        duration, reply = self.script.pop(0)
        if timeout is not None and duration > timeout:
            self.clock.now += timeout
            raise ModelTimeout("APITimeoutError", retryable=True)
        self.clock.now += duration
        if isinstance(reply, Exception):
            raise reply
        return reply


def text(content: str, tokens: tuple[int, int] = (100, 20)) -> ModelReply:
    return ModelReply(content=content, prompt_tokens=tokens[0], completion_tokens=tokens[1])


def as_json(obj: dict, tokens: tuple[int, int] = (100, 20)) -> ModelReply:
    return text(json.dumps(obj), tokens)


def tool_calls(*calls: tuple[str, dict], tokens: tuple[int, int] = (100, 20)) -> ModelReply:
    return ModelReply(
        content=None,
        tool_calls=[
            ToolCall(id=f"call_{i}", name=name, arguments=args, raw_arguments=json.dumps(args))
            for i, (name, args) in enumerate(calls)
        ],
        prompt_tokens=tokens[0],
        completion_tokens=tokens[1],
    )


class FakeRenderer:
    """A Renderer that records overlays and fails the first `fail_times` calls."""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.rendered: list[str] = []

    def render(self, repo_dir: Path, overlay: str) -> None:
        self.rendered.append(overlay)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RenderError(overlay, "accumulating resources: missing.yaml: no such file")


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def loki_transport(lines: list[tuple[str, str]], up: bool = True) -> httpx2.MockTransport:
    """Answers every Loki query with `lines` as one Traefik stream, or 503 when down."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if not up:
            return httpx2.Response(503, text="loki down")
        values = [
            [str(int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1e9)), line] for ts, line in lines
        ]
        result = [{"stream": {"namespace": "kube-system", "container": "traefik"}, "values": values}] if values else []
        return httpx2.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": result}})

    return httpx2.MockTransport(handler)


def scenario_deps(work_dir: Path, scenario: Scenario, model, renderer=None, budgets=None, clock=None, loki_up=True):
    """Wire a Deps for a scenario: fake cluster, mocked Loki, a local git origin holding the repo files."""
    ws = Workspace(work_dir / "repo", str(make_origin(work_dir, scenario.repo_files)))
    loki = LokiClient(
        "http://loki.logging.svc:3100", httpx2.Client(transport=loki_transport(scenario.loki_lines, loki_up))
    )
    cluster = ClusterTools(FakeKube(scenario.snapshot))
    deps = Deps(
        model=model,
        toolbox=Toolbox(cluster, loki, ws),
        cluster=cluster,
        workspace=ws,
        renderer=renderer or FakeRenderer(),
        store=ProposalStore(),
        budgets=budgets or Budgets(),
    )
    if clock is not None:
        deps.clock = clock
    return deps
