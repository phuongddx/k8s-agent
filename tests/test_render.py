import pytest

from k8s_agent.diffcheck import overlays_for
from k8s_agent.render import KustomizeRenderer, RenderError
from tests import scenarios


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
