import dataclasses
import logging
import threading
import time

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from k8s_agent import graph as graph_module
from k8s_agent import server
from k8s_agent.graph import DeadlineExceeded, build_graph, run_diagnosis
from k8s_agent.logs import configure_logging
from k8s_agent.server import BearerAuth, build_app, build_service, build_uvicorn_config, main
from k8s_agent.service import BUSY, TIMEOUT, AgentService
from k8s_agent.settings import Budgets, load_settings
from tests import scenarios
from tests.graph_fakes import ScriptedModel, as_json, scenario_deps, text
from tests.test_graph import CRASH_INTAKE

TOKEN = "s" * 40
BASE = "http://localhost:8080"


class StubService:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.questions = []

    def diagnose(self, question):
        self.questions.append(question)
        time.sleep(self.delay)
        return {"cause": "c", "confidence": "high", "evidence": [], "fix_kind": "none"}

    def open_pr(self, proposal_id):
        return {"error": "Proposal not found, ask again."}


def http(app, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE, headers=headers)


@pytest.mark.anyio
async def test_healthz_is_open():
    async with http(build_app(StubService(), TOKEN, 150)) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.anyio
@pytest.mark.parametrize("token", [None, "wrong", TOKEN[:-1]])
async def test_mcp_without_the_right_token_is_401(token):
    async with http(build_app(StubService(), TOKEN, 150), token) as c:
        r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


async def _call(app, tool, args):
    async with app.state.mcp.session_manager.run(), http(app, TOKEN) as c:
        async with Client(streamable_http_client(f"{BASE}/mcp", http_client=c)) as client:
            tools = sorted(t.name for t in (await client.list_tools()).tools)
            result = await client.call_tool(tool, args)
    return tools, result


@pytest.mark.anyio
async def test_authorized_client_lists_and_calls_tools():
    service = StubService()
    tools, result = await _call(build_app(service, TOKEN, 150), "diagnose", {"question": "why?"})
    assert tools == ["diagnose", "open_pr"]
    assert result.structured_content == {"cause": "c", "confidence": "high", "evidence": [], "fix_kind": "none"}
    assert service.questions == ["why?"]


@pytest.mark.anyio
async def test_open_pr_tool_passes_errors_through():
    _, result = await _call(build_app(StubService(), TOKEN, 150), "open_pr", {"proposal_id": "x"})
    assert result.structured_content == {"error": "Proposal not found, ask again."}


@pytest.mark.anyio
async def test_diagnose_hard_time_limit():
    _, result = await _call(build_app(StubService(delay=1.0), TOKEN, 0.2), "diagnose", {"question": "slow"})
    assert result.structured_content == {"error": TIMEOUT}


def test_main_exits_2_when_settings_are_invalid(monkeypatch):
    for name in ("MCP_BEARER_TOKEN", "ZAI_API_KEY", "BOT_GH_TOKEN", "BOT_LOGIN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as info:
        main()
    assert info.value.code == 2


# --- R4: bearer edge cases -------------------------------------------------------------------------


async def _post_with(headers, path="/mcp"):
    app = build_app(StubService(), TOKEN, 150)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url=BASE, headers=headers) as c:
        return await c.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


@pytest.mark.anyio
@pytest.mark.parametrize(
    "header",
    [
        f"bearer {TOKEN}",  # the scheme is matched exactly: only OpenClaw's "Bearer" is accepted
        f"BEARER {TOKEN}",
        f"Bearer {TOKEN} ",
        f" Bearer {TOKEN}",
        f"Bearer  {TOKEN}",
        f"Bearer {TOKEN}\t",
        TOKEN,
        "Bearer",
    ],
)
async def test_mcp_rejects_malformed_authorization_headers(header):
    r = await _post_with({"Authorization": header})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.anyio
@pytest.mark.parametrize("path", [f"/mcp?access_token={TOKEN}", f"/mcp?token={TOKEN}&Authorization=Bearer%20{TOKEN}"])
async def test_token_in_the_query_string_is_not_accepted(path):
    r = await _post_with({}, path=path)
    assert r.status_code == 401


@pytest.mark.anyio
async def test_paths_next_to_mcp_are_gated_too():
    assert (await _post_with({}, path="/mcp/")).status_code == 401
    assert (await _post_with({}, path="/mcpx")).status_code == 401


# --- R1: uvicorn must not replace our logging ------------------------------------------------------

ENV = {
    "MCP_BEARER_TOKEN": TOKEN,
    "ZAI_API_KEY": "z",
    "BOT_GH_TOKEN": "g",
    "BOT_LOGIN": "bot",
    "PORT": "9191",
}


def test_uvicorn_config_leaves_logging_to_configure_logging():
    configure_logging()
    config = build_uvicorn_config(object(), 9191)
    assert config.log_config is None
    assert config.access_log is False
    assert config.port == 9191
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_main_starts_uvicorn_without_its_own_log_config(monkeypatch):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    started = []
    monkeypatch.setattr(server, "build_service", lambda settings: StubService())
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **kw: started.append(self.config))
    main()
    [config] = started
    assert config.log_config is None and config.port == 9191 and config.host == "0.0.0.0"  # noqa: S104


# --- R2 and R3: wiring of build_service -------------------------------------------------------------


class Recorder:
    """Replaces the real collaborators of build_service with recording stand-ins."""

    def __init__(self, monkeypatch, tmp_path, budgets=None):
        self.kube_kwargs = {}
        self.loki_http = None
        self.github_http = None
        self.deps = None
        self.graph_deadlines = []
        env = {**ENV, "REPO_DIR": str(tmp_path / "repo")}
        self.settings = load_settings(env)
        if budgets is not None:
            self.settings = dataclasses.replace(self.settings, budgets=budgets)
        recorder = self

        class FakeKube:
            @classmethod
            def in_cluster(cls, **kwargs):
                recorder.kube_kwargs = kwargs
                return object()

        class FakeLoki(server.LokiClient):
            def __init__(self, base_url, http, **kwargs):
                recorder.loki_http = http
                super().__init__(base_url, http, **kwargs)

        class FakeGitHub(server.GitHubClient):
            def __init__(self, repo, token, http, **kwargs):
                recorder.github_http = http
                super().__init__(repo, token, http, **kwargs)

        class FakeDeps(server.Deps):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                recorder.deps = self

        class SlowGraph:
            """Works until the deadline it was given, like the real graph, and then gives up."""

            def invoke(self, state):
                recorder.graph_deadlines.append(state["deadline"] - time.monotonic())
                time.sleep(max(state["deadline"] - time.monotonic(), 0))
                raise DeadlineExceeded()

        monkeypatch.setattr(server, "HttpKubeApi", FakeKube)
        monkeypatch.setattr(server, "LokiClient", FakeLoki)
        monkeypatch.setattr(server, "GitHubClient", FakeGitHub)
        monkeypatch.setattr(server, "Deps", FakeDeps)
        monkeypatch.setattr(server, "build_graph", lambda deps: SlowGraph())


def test_every_http_client_has_explicit_timeouts(monkeypatch, tmp_path):
    rec = Recorder(monkeypatch, tmp_path)
    build_service(rec.settings)
    kube = httpx2.Timeout(rec.kube_kwargs["timeout"])
    for timeout, read_cap in ((kube, 30), (rec.loki_http.timeout, 30), (rec.github_http.timeout, 20)):
        assert timeout.connect is not None and timeout.connect <= 5
        assert timeout.read is not None and timeout.read <= read_cap
        assert timeout.write is not None and timeout.pool is not None


def test_outbound_clients_ignore_proxy_environment(monkeypatch, tmp_path):
    rec = Recorder(monkeypatch, tmp_path)
    build_service(rec.settings)
    assert rec.github_http._trust_env is False
    assert rec.loki_http._trust_env is False
    assert rec.deps.model._client._client._trust_env is False


def test_graph_gets_a_deadline_before_the_outer_limit(monkeypatch, tmp_path):
    rec = Recorder(monkeypatch, tmp_path)
    build_service(rec.settings)
    assert rec.deps.budgets.graph_seconds == rec.settings.budgets.graph_seconds - server.GRAPH_MARGIN_SECONDS
    assert rec.deps.budgets.investigate_seconds == rec.settings.budgets.investigate_seconds
    assert rec.settings.budgets.graph_seconds == 150  # the settings (and the outer limit) are untouched


def test_graph_caps_model_calls_with_the_configured_model_timeout(monkeypatch, tmp_path):
    rec = Recorder(monkeypatch, tmp_path)
    rec.settings = dataclasses.replace(rec.settings, model_timeout_seconds=25)
    build_service(rec.settings)
    assert rec.deps.model_timeout == 25


@pytest.mark.anyio
async def test_slow_diagnosis_times_out_and_frees_the_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "GRAPH_MARGIN_SECONDS", 0.5)
    budgets = Budgets(graph_seconds=1.2, investigate_seconds=0.2)
    rec = Recorder(monkeypatch, tmp_path, budgets)
    service = build_service(rec.settings)
    app = build_app(service, TOKEN, budgets.graph_seconds)
    _, result = await _call(app, "diagnose", {"question": "slow"})
    assert result.structured_content == {"error": TIMEOUT}
    assert 0.5 < rec.graph_deadlines[0] < 0.8  # the graph was told 0.7 s, not the outer 1.2 s
    # The worker already finished, so the next call is not BUSY.
    assert service.open_pr("nope") != {"error": BUSY}


@pytest.mark.anyio
async def test_real_graph_stops_before_a_second_model_call_and_frees_the_lock(monkeypatch, tmp_path):
    """The inner deadline (0.5 s) passes during the first model call (0.7 s). The graph must not start the
    re-ask, so the worker returns at about 0.7 s, before the outer limit (1.0 s). The 5 s model reserve is
    switched off: this test is about a scripted call that ignores its timeout."""
    monkeypatch.setattr(graph_module, "MODEL_RESERVE_SECONDS", 0)
    sleeps = []

    def slow_call():
        sleeps.append(time.monotonic())
        time.sleep(0.7)

    model = ScriptedModel([text("not json"), as_json(CRASH_INTAKE)], on_call=slow_call)
    budgets = Budgets(graph_seconds=0.5, investigate_seconds=0.2)
    deps = scenario_deps(tmp_path, scenarios.crashloop(), model, budgets=budgets)
    graph = build_graph(deps)
    worker_done = threading.Event()

    def diagnose_fn(question):
        try:
            return run_diagnosis(graph, deps, question)
        finally:
            worker_done.set()

    service = AgentService(diagnose_fn, lambda pid: {"error": "not found"})
    started = time.monotonic()
    _, result = await _call(build_app(service, TOKEN, 1.0), "diagnose", {"question": "why is dropitx-api down?"})
    assert result.structured_content == {"error": TIMEOUT}
    assert len(sleeps) == 1
    assert worker_done.wait(0.5)  # returned within about one call of the deadline
    assert time.monotonic() - started < 3
    assert service.open_pr("nope") != {"error": BUSY}


# --- BearerAuth refuses weak tokens ----------------------------------------------------------------


@pytest.mark.parametrize("token", ["", "x" * 31])
def test_bearer_auth_refuses_empty_or_short_tokens(token):
    with pytest.raises(ValueError, match="at least 32"):
        BearerAuth(lambda *a: None, token)
    BearerAuth(lambda *a: None, "x" * 32)
