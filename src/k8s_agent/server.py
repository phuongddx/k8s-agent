"""The MCP server: tools `diagnose` and `open_pr` over streamable HTTP at /mcp, bearer-token
protected, plus an unauthenticated GET /healthz for the readiness probe."""

import hmac
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

import anyio
import httpx2
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from k8s_agent.graph import Deps, build_graph, run_diagnosis
from k8s_agent.kube import HttpKubeApi
from k8s_agent.llm import OpenAIChatModel
from k8s_agent.logs import configure_logging, log_event
from k8s_agent.pr import GitHubClient, PrOpener
from k8s_agent.proposals import ProposalStore
from k8s_agent.render import KustomizeRenderer
from k8s_agent.repo import Workspace
from k8s_agent.service import TIMEOUT, AgentService
from k8s_agent.settings import Settings, SettingsError, load_settings
from k8s_agent.tools.cluster import ClusterTools
from k8s_agent.tools.loki import LokiClient
from k8s_agent.tools.toolbox import Toolbox

OPEN_PR_SECONDS = 120
ALLOWED_HOSTS = [
    "k8s-agent.k8s-agent.svc",
    "k8s-agent.k8s-agent.svc:*",
    "k8s-agent.k8s-agent.svc.cluster.local",
    "k8s-agent.k8s-agent.svc.cluster.local:*",
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
]
INSTRUCTIONS = (
    "Read-only troubleshooting for workloads on the vps192 K3s cluster. Call diagnose with the owner's "
    "question. Call open_pr only when the owner asks for a PR in the current chat."
)


class Service(Protocol):
    def diagnose(self, question: str) -> dict: ...

    def open_pr(self, proposal_id: str) -> dict: ...


class BearerAuth:
    """Rejects requests under /mcp without `Authorization: Bearer <token>`. Constant-time compare."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self._expected):
                body = json.dumps({"error": "unauthorized"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self._app(scope, receive, send)


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_app(service: Service, bearer_token: str, graph_seconds: float) -> Starlette:
    mcp = MCPServer("k8s-agent", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def diagnose(question: str) -> dict[str, Any]:
        """Diagnose a problem with a vps192 workload. Returns cause, confidence (high|medium|low), evidence
        [{source, detail}], fix_kind (config|app-code|capacity|none|unknown), and optionally fix_summary,
        proposal_id and diff. Returns only `clarify` when the question is ambiguous, or `error`."""
        try:
            with anyio.fail_after(graph_seconds):
                return await anyio.to_thread.run_sync(service.diagnose, question, abandon_on_cancel=True)
        except TimeoutError:
            log_event("diagnose.timeout", question=question)
            return {"error": TIMEOUT}

    @mcp.tool()
    async def open_pr(proposal_id: str) -> dict[str, Any]:
        """Open a pull request for a fix proposal returned by diagnose. Only call this when the owner asked
        for a PR in the current chat. Returns {pr_url} or {error}."""
        try:
            with anyio.fail_after(OPEN_PR_SECONDS):
                return await anyio.to_thread.run_sync(service.open_pr, proposal_id, abandon_on_cancel=True)
        except TimeoutError:
            return {"error": "Opening the PR timed out."}

    mcp_app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=ALLOWED_HOSTS),
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[Route("/healthz", healthz, methods=["GET"]), Mount("/", app=mcp_app)],
        middleware=[Middleware(BearerAuth, token=bearer_token)],
        lifespan=lifespan,
    )
    app.state.mcp = mcp
    return app


def build_service(settings: Settings) -> AgentService:
    b = settings.budgets
    workspace = Workspace(
        Path(settings.repo_dir), settings.repo_url, token=settings.bot_gh_token, max_bytes=b.log_max_bytes
    )
    cluster = ClusterTools(HttpKubeApi.in_cluster(), log_max_lines=b.log_max_lines, log_max_bytes=b.log_max_bytes)
    loki = LokiClient(
        settings.loki_url,
        httpx2.Client(timeout=15),
        default_hours=b.loki_default_hours,
        max_hours=b.loki_max_hours,
        max_lines=b.log_max_lines,
        max_bytes=b.log_max_bytes,
    )
    store = ProposalStore(ttl_seconds=settings.proposal_ttl_hours * 3600)
    renderer = KustomizeRenderer()
    model = OpenAIChatModel(
        settings.model_base_url, settings.zai_api_key, settings.model, timeout=settings.model_timeout_seconds
    )
    deps = Deps(
        model=model,
        toolbox=Toolbox(cluster, loki, workspace, max_bytes=b.log_max_bytes),
        cluster=cluster,
        workspace=workspace,
        renderer=renderer,
        store=store,
        budgets=b,
    )
    graph = build_graph(deps)
    github = GitHubClient(settings.repo, settings.bot_gh_token, httpx2.Client(timeout=30))
    opener = PrOpener(workspace, renderer, store, github, settings.bot_login, settings.bot_email)
    return AgentService(lambda question: run_diagnosis(graph, deps, question), opener.open_pr)


def main() -> None:
    configure_logging()
    try:
        settings = load_settings(os.environ)
    except SettingsError as exc:
        log_event("settings.invalid", logging.ERROR, error_type=str(exc))
        sys.exit(2)
    app = build_app(build_service(settings), settings.mcp_bearer_token, settings.budgets.graph_seconds)
    log_event("server.start", status="listening")
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_config=None, access_log=False)  # noqa: S104


if __name__ == "__main__":
    main()
