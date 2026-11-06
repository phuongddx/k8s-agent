import time

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from k8s_agent.server import build_app, main
from k8s_agent.service import TIMEOUT

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
