# k8s-agent

A read-only Kubernetes troubleshooting agent for the vps192 K3s cluster. OpenClaw calls it over
MCP (streamable HTTP at `/mcp`, bearer token) with two tools:

- `diagnose(question)` returns a cause, a confidence, cited evidence and a fix kind. For config
  causes it can also return a diff under `apps/**` of `phuongddx/vps192-k8s-config`.
- `open_pr(proposal_id)` opens that diff as a pull request from the bot account. The owner merges.

The agent never writes to the cluster and never reads Secrets. Design: [docs/spec.md](docs/spec.md).
Implementation plan: [docs/plan.md](docs/plan.md).

## Develop

```bash
uv sync
uv run pytest          # kustomize on PATH runs the render tests; they are skipped without it
uv run ruff check . && uv run ruff format --check .
```

## Configuration

| Env | Default | Notes |
|---|---|---|
| `MCP_BEARER_TOKEN` | required | at least 32 characters |
| `ZAI_API_KEY` | required | |
| `BOT_GH_TOKEN` | required | classic PAT of the bot account, `repo` scope, no `workflow` scope |
| `BOT_LOGIN` | required | commit author of fix PRs |
| `BOT_EMAIL` | `<BOT_LOGIN>@users.noreply.github.com` | |
| `MODEL` | `glm-5.3` | |
| `MODEL_BASE_URL` | `https://api.z.ai/api/coding/paas/v4` | |
| `REPO` | `phuongddx/vps192-k8s-config` | |
| `REPO_DIR` | `/work/repo` | emptyDir |
| `LOKI_URL` | `http://loki.logging.svc:3100` | |
| `MAX_TOOL_CALLS`, `INVESTIGATE_SECONDS`, `GRAPH_SECONDS` | 12, 90, 150 | `GRAPH_SECONDS` < 180 |
| `LOG_MAX_LINES`, `LOG_MAX_BYTES` | 200, 32768 | per log fetch |
| `LOKI_DEFAULT_HOURS`, `LOKI_MAX_HOURS` | 1, 168 | |
| `PROPOSAL_TTL_HOURS`, `MODEL_TIMEOUT_SECONDS`, `PORT` | 24, 40, 8080 | |

## Manual eval

Six hand-built scenarios (`tests/scenarios.py`) run against the real model, by hand, never in CI:

```bash
ZAI_API_KEY=... uv run python -m evals.run_eval
```
