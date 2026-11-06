import subprocess

import pytest

from k8s_agent import render
from k8s_agent.diffcheck import overlays_for
from k8s_agent.render import KustomizeRenderer, RenderError
from tests import scenarios

CM = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n"
REMOTE_ENTRIES = ["https://example.invalid/x", "git@github.com:o/r.git", "github.com/o/r/path?ref=v1"]
SECRET_KEYS = ("BOT_GH_TOKEN", "ZAI_API_KEY", "MCP_BEARER_TOKEN")


def _forbid_kustomize(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("kustomize must not run when a remote reference is present")

    monkeypatch.setattr(render.subprocess, "run", fail)


def _overlay(tmp_path, text, name="vps192"):
    d = tmp_path / "apps/x" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "kustomization.yaml").write_text(text)
    return f"apps/x/{name}"


@pytest.mark.parametrize("key", ["resources", "components", "bases"])
@pytest.mark.parametrize("entry", REMOTE_ENTRIES)
def test_remote_reference_is_refused_before_kustomize_runs(tmp_path, monkeypatch, key, entry):
    overlay = _overlay(tmp_path, f"{key}:\n  - {entry}\n")
    _forbid_kustomize(monkeypatch)
    with pytest.raises(RenderError, match="remote") as info:
        KustomizeRenderer().render(tmp_path, overlay)
    assert entry in info.value.detail


def test_remote_reference_in_flow_list_is_refused(tmp_path, monkeypatch):
    overlay = _overlay(tmp_path, "resources: [https://example.invalid/leak?d=x]\n")
    _forbid_kustomize(monkeypatch)
    with pytest.raises(RenderError, match="remote"):
        KustomizeRenderer().render(tmp_path, overlay)


def test_remote_reference_in_a_sibling_kustomization_is_refused(tmp_path, monkeypatch):
    _overlay(tmp_path, "resources:\n  - https://example.invalid/x\n", name="base")
    overlay = _overlay(tmp_path, "resources:\n  - ../base\n")
    _forbid_kustomize(monkeypatch)
    with pytest.raises(RenderError, match="remote"):
        KustomizeRenderer().render(tmp_path, overlay)


def test_local_references_still_render(tmp_path, kustomize):
    _overlay(tmp_path, "resources:\n  - cm.yaml\n", name="base")
    (tmp_path / "apps/x/base/cm.yaml").write_text(CM)
    overlay = _overlay(tmp_path, "resources:\n  - ../base\n")
    KustomizeRenderer(kustomize).render(tmp_path, overlay)


def test_kustomize_gets_a_minimal_environment(tmp_path, kustomize, monkeypatch):
    for key in SECRET_KEYS:
        monkeypatch.setenv(key, "tok")
    overlay = _overlay(tmp_path, "resources:\n  - cm.yaml\n")
    (tmp_path / "apps/x/vps192/cm.yaml").write_text(CM)
    seen: dict[str, str] = {}
    real_run = subprocess.run

    def spy(*args, **kwargs):
        seen.update(kwargs["env"])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(render.subprocess, "run", spy)
    KustomizeRenderer(kustomize).render(tmp_path, overlay)
    for key in SECRET_KEYS:
        assert key not in seen
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        assert seen[key] == "http://127.0.0.1:9"
    assert seen["NO_PROXY"] == "" and seen["no_proxy"] == ""
    assert seen["GIT_ALLOW_PROTOCOL"] == "file"
    assert seen["PATH"]
    assert set(seen) <= {
        "PATH",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "https_proxy",
        "http_proxy",
        "NO_PROXY",
        "no_proxy",
        "GIT_ALLOW_PROTOCOL",
    }


def test_valid_overlay_renders(tmp_path, kustomize):
    d = tmp_path / "apps/x/vps192"
    d.mkdir(parents=True)
    (d / "kustomization.yaml").write_text("resources:\n  - cm.yaml\n")
    (d / "cm.yaml").write_text("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")
    KustomizeRenderer(kustomize).render(tmp_path, "apps/x/vps192")


def test_broken_overlay_raises_with_stderr(tmp_path, kustomize):
    d = tmp_path / "apps/x/vps192"
    d.mkdir(parents=True)
    (d / "kustomization.yaml").write_text("resources:\n  - missing.yaml\n")
    with pytest.raises(RenderError) as info:
        KustomizeRenderer(kustomize).render(tmp_path, "apps/x/vps192")
    assert info.value.overlay == "apps/x/vps192"
    assert "missing.yaml" in info.value.detail


@pytest.mark.parametrize("name", sorted(scenarios.ALL))
def test_every_scenario_repo_renders(tmp_path, kustomize, name):
    scenario = scenarios.ALL[name]
    for rel, content in scenario.repo_files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    services = sorted({rel.split("/")[1] for rel in scenario.repo_files})
    overlays = [o for s in services for o in overlays_for(tmp_path, s)]
    assert overlays
    for overlay in overlays:
        KustomizeRenderer(kustomize).render(tmp_path, overlay)
