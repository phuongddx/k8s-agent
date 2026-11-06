import pytest

from k8s_agent.settings import Budgets, SettingsError, load_settings

BASE = {
    "MCP_BEARER_TOKEN": "t" * 32,
    "ZAI_API_KEY": "zai-key",
    "BOT_GH_TOKEN": "ghp_example",
    "BOT_LOGIN": "vps192-agent",
}


def test_defaults_match_the_spec():
    s = load_settings(BASE)
    assert s.model == "glm-5.3"
    assert s.model_base_url == "https://api.z.ai/api/coding/paas/v4"
    assert s.repo == "phuongddx/vps192-k8s-config"
    assert s.repo_url == "https://github.com/phuongddx/vps192-k8s-config.git"
    assert s.loki_url == "http://loki.logging.svc:3100"
    assert s.repo_dir == "/work/repo"
    assert s.port == 8080
    assert s.proposal_ttl_hours == 24
    assert s.bot_email == "vps192-agent@users.noreply.github.com"
    assert s.budgets == Budgets()
    assert (s.budgets.max_tool_calls, s.budgets.investigate_seconds, s.budgets.graph_seconds) == (12, 90, 150)
    assert (s.budgets.log_max_lines, s.budgets.log_max_bytes) == (200, 32768)
    assert (s.budgets.loki_default_hours, s.budgets.loki_max_hours) == (1, 168)


@pytest.mark.parametrize("name", ["MCP_BEARER_TOKEN", "ZAI_API_KEY", "BOT_GH_TOKEN", "BOT_LOGIN"])
def test_required_values(name):
    env = {k: v for k, v in BASE.items() if k != name}
    with pytest.raises(SettingsError, match=name):
        load_settings(env)


def test_short_bearer_token_is_rejected():
    with pytest.raises(SettingsError, match="at least 32"):
        load_settings({**BASE, "MCP_BEARER_TOKEN": "short"})


def test_graph_budget_must_stay_below_openclaw_timeout():
    with pytest.raises(SettingsError, match="180"):
        load_settings({**BASE, "GRAPH_SECONDS": "180"})


def test_investigate_budget_must_be_below_graph_budget():
    with pytest.raises(SettingsError, match="INVESTIGATE_SECONDS"):
        load_settings({**BASE, "INVESTIGATE_SECONDS": "150"})


@pytest.mark.parametrize("value", ["abc", "0", "-3"])
def test_integers_are_validated(value):
    with pytest.raises(SettingsError, match="MAX_TOOL_CALLS"):
        load_settings({**BASE, "MAX_TOOL_CALLS": value})


def test_repo_must_be_owner_slash_name():
    with pytest.raises(SettingsError, match="REPO"):
        load_settings({**BASE, "REPO": "https://github.com/x/y"})


def test_repr_hides_secrets():
    s = load_settings(BASE)
    text = repr(s)
    for secret in (BASE["MCP_BEARER_TOKEN"], BASE["ZAI_API_KEY"], BASE["BOT_GH_TOKEN"]):
        assert secret not in text


def test_overrides_and_trailing_slash():
    s = load_settings({**BASE, "LOKI_URL": "http://loki:3100/", "MAX_TOOL_CALLS": "5", "BOT_EMAIL": "1+b@x.y"})
    assert s.loki_url == "http://loki:3100"
    assert s.budgets.max_tool_calls == 5
    assert s.bot_email == "1+b@x.y"
