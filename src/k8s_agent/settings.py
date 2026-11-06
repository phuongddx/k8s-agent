"""Environment settings. Every value the agent reads from its environment is parsed here."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
OPENCLAW_MCP_TIMEOUT_SECONDS = 180


class SettingsError(ValueError):
    """Raised when the environment is missing a required value or holds an invalid one."""


@dataclass(frozen=True)
class Budgets:
    max_tool_calls: int = 12
    investigate_seconds: float = 90
    graph_seconds: float = 150
    log_max_lines: int = 200
    log_max_bytes: int = 32768
    loki_default_hours: int = 1
    loki_max_hours: int = 168


@dataclass(frozen=True)
class Settings:
    mcp_bearer_token: str
    zai_api_key: str
    bot_gh_token: str
    bot_login: str
    bot_email: str
    model: str
    model_base_url: str
    model_timeout_seconds: float
    repo: str
    repo_dir: str
    loki_url: str
    proposal_ttl_hours: int
    port: int
    budgets: Budgets

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.repo}.git"


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise SettingsError(f"{name} is required")
    return value


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise SettingsError(f"{name} must be positive, got {value}")
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    token = _required(env, "MCP_BEARER_TOKEN")
    if len(token) < 32:
        raise SettingsError("MCP_BEARER_TOKEN must be at least 32 characters")
    repo = env.get("REPO", "").strip() or "phuongddx/vps192-k8s-config"
    if not REPO_PATTERN.match(repo):
        raise SettingsError(f"REPO must look like owner/name, got {repo!r}")
    bot_login = _required(env, "BOT_LOGIN")
    budgets = Budgets(
        max_tool_calls=_int(env, "MAX_TOOL_CALLS", 12),
        investigate_seconds=_int(env, "INVESTIGATE_SECONDS", 90),
        graph_seconds=_int(env, "GRAPH_SECONDS", 150),
        log_max_lines=_int(env, "LOG_MAX_LINES", 200),
        log_max_bytes=_int(env, "LOG_MAX_BYTES", 32768),
        loki_default_hours=_int(env, "LOKI_DEFAULT_HOURS", 1),
        loki_max_hours=_int(env, "LOKI_MAX_HOURS", 168),
    )
    if budgets.graph_seconds >= OPENCLAW_MCP_TIMEOUT_SECONDS:
        raise SettingsError(f"GRAPH_SECONDS must be below OpenClaw's {OPENCLAW_MCP_TIMEOUT_SECONDS} s MCP timeout")
    if budgets.investigate_seconds >= budgets.graph_seconds:
        raise SettingsError("INVESTIGATE_SECONDS must be below GRAPH_SECONDS")
    if budgets.loki_default_hours > budgets.loki_max_hours:
        raise SettingsError("LOKI_DEFAULT_HOURS must not exceed LOKI_MAX_HOURS")
    return Settings(
        mcp_bearer_token=token,
        zai_api_key=_required(env, "ZAI_API_KEY"),
        bot_gh_token=_required(env, "BOT_GH_TOKEN"),
        bot_login=bot_login,
        bot_email=env.get("BOT_EMAIL", "").strip() or f"{bot_login}@users.noreply.github.com",
        model=env.get("MODEL", "").strip() or "glm-5.3",
        model_base_url=env.get("MODEL_BASE_URL", "").strip() or "https://api.z.ai/api/coding/paas/v4",
        model_timeout_seconds=_int(env, "MODEL_TIMEOUT_SECONDS", 40),
        repo=repo,
        repo_dir=env.get("REPO_DIR", "").strip() or "/work/repo",
        loki_url=(env.get("LOKI_URL", "").strip() or "http://loki.logging.svc:3100").rstrip("/"),
        proposal_ttl_hours=_int(env, "PROPOSAL_TTL_HOURS", 24),
        port=_int(env, "PORT", 8080),
        budgets=budgets,
    )
