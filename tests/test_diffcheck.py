import pytest

from k8s_agent.diffcheck import PathNotAllowed, check_paths, is_allowed, overlays_for, paths_in_diff

MODIFY = """diff --git a/apps/llm-gateway/base/deployment.yaml b/apps/llm-gateway/base/deployment.yaml
index 1111111..2222222 100644
--- a/apps/llm-gateway/base/deployment.yaml
+++ b/apps/llm-gateway/base/deployment.yaml
@@ -1 +1 @@
-memory: 128Mi
+memory: 256Mi
"""

NEW_FILE = """diff --git a/apps/x/vps192/new.yaml b/apps/x/vps192/new.yaml
new file mode 100644
--- /dev/null
+++ b/apps/x/vps192/new.yaml
@@ -0,0 +1 @@
+a: b
"""

RENAME_OUT = """diff --git a/apps/x/base/a.yaml b/scripts/a.yaml
similarity index 100%
rename from apps/x/base/a.yaml
rename to scripts/a.yaml
"""


@pytest.mark.parametrize(
    "path,ok",
    [
        ("apps/x/base/a.yaml", True),
        ("apps/x/a.yaml", True),
        ("apps/x", False),
        ("scripts/validate.sh", False),
        (".github/workflows/deploy.yml", False),
        ("apps/../scripts/x", False),
        ("apps/x/../../scripts/y", False),
        ("/apps/x/a.yaml", False),
        ('"apps/x/a b.yaml"', False),
        ("apps/x/.git/config", False),
    ],
)
def test_is_allowed(path, ok):
    assert is_allowed(path) is ok


def test_paths_in_diff_reads_headers_and_skips_dev_null():
    assert paths_in_diff(MODIFY) == ["apps/llm-gateway/base/deployment.yaml"]
    assert paths_in_diff(NEW_FILE) == ["apps/x/vps192/new.yaml"]


def test_rename_out_of_apps_is_caught():
    paths = paths_in_diff(RENAME_OUT)
    assert "scripts/a.yaml" in paths
    with pytest.raises(PathNotAllowed, match="scripts/a.yaml"):
        check_paths(paths)


def test_check_paths_returns_the_single_service():
    assert check_paths(["apps/x/base/a.yaml", "apps/x/vps192/b.yaml"]) == "x"
    with pytest.raises(PathNotAllowed, match="several services"):
        check_paths(["apps/x/a.yaml", "apps/y/b.yaml"])
    with pytest.raises(PathNotAllowed, match="no files"):
        check_paths([])


def test_overlays_for_renders_every_overlay_not_base(tmp_path):
    for d in ("base", "vps192"):
        (tmp_path / "apps/x" / d).mkdir(parents=True)
        (tmp_path / "apps/x" / d / "kustomization.yaml").write_text("resources: []\n")
    assert overlays_for(tmp_path, "x") == ["apps/x/vps192"]
    (tmp_path / "apps/y/base").mkdir(parents=True)
    (tmp_path / "apps/y/base/kustomization.yaml").write_text("resources: []\n")
    assert overlays_for(tmp_path, "y") == ["apps/y/base"]
    assert overlays_for(tmp_path, "missing") == []
