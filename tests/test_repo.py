import pytest

from k8s_agent.repo import RepoError, Workspace
from tests.gitrepo import make_origin, push_commit

FILES = {
    "apps/x/base/kustomization.yaml": "resources:\n  - cm.yaml\n",
    "apps/x/base/cm.yaml": 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\ndata:\n  A: "1"\n',
    "apps/x/vps192/kustomization.yaml": "resources:\n  - ../base\n",
    "scripts/validate.sh": "#!/bin/sh\n",
}


@pytest.fixture
def ws(tmp_path):
    bare = make_origin(tmp_path, FILES)
    w = Workspace(tmp_path / "work" / "repo", str(bare))
    w.sync()
    return w, bare, tmp_path


def test_sync_clones_then_fetches_new_commits(ws):
    w, bare, tmp = ws
    first = w.head()
    second = push_commit(tmp, bare, "apps/x/base/cm.yaml", "changed\n", "fix(x): change")
    assert w.sync() == second != first
    assert (w.path / "apps/x/base/cm.yaml").read_text() == "changed\n"


def test_sync_discards_local_edits(ws):
    w, _, _ = ws
    (w.path / "apps/x/base/cm.yaml").write_text("dirty\n")
    (w.path / "apps/x/base/extra.yaml").write_text("new\n")
    w.sync()
    assert "dirty" not in (w.path / "apps/x/base/cm.yaml").read_text()
    assert not (w.path / "apps/x/base/extra.yaml").exists()


def test_read_file_numbers_lines_for_citations(ws):
    w, _, _ = ws
    out = w.read_file("apps/x/base/cm.yaml", start_line=5, max_lines=2)
    assert out["content"] == '5: data:\n6:   A: "1"'
    assert (out["start_line"], out["end_line"], out["total_lines"]) == (5, 6, 6)


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "apps/../../x", ".git/config", ""])
def test_read_file_rejects_escapes(ws, bad):
    w, _, _ = ws
    with pytest.raises(RepoError):
        w.read_file(bad)


def test_symlink_escape_is_rejected(ws, tmp_path):
    w, _, _ = ws
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (w.path / "apps/x/link.yaml").symlink_to(outside)
    with pytest.raises(RepoError, match="escapes"):
        w.read_file("apps/x/link.yaml")
    with pytest.raises(RepoError):
        w.write_file("apps/x/link.yaml", "x")


def test_log_lists_recent_commits_for_a_path(ws):
    w, bare, tmp = ws
    push_commit(tmp, bare, "apps/x/base/cm.yaml", "v2\n", "fix(x): bump")
    w.sync()
    [latest, seed] = w.log("apps/x/", limit=5)
    assert latest["subject"] == "fix(x): bump"
    assert seed["subject"] == "chore: seed"
    assert len(latest["sha"]) == 40


def test_write_diff_apply_round_trip(ws):
    w, _, _ = ws
    base = w.head()
    w.write_file("apps/x/base/cm.yaml", FILES["apps/x/base/cm.yaml"].replace('"1"', '"2"'))
    w.write_file("apps/x/vps192/patch.yaml", "a: b\n")
    diff = w.diff()
    assert '+  A: "2"' in diff
    assert "new file mode" in diff
    w.reset(base)
    assert w.diff() == ""
    w.apply(diff)
    assert sorted(w.staged_paths()) == ["apps/x/base/cm.yaml", "apps/x/vps192/patch.yaml"]


def test_write_outside_apps_is_refused(ws):
    w, _, _ = ws
    with pytest.raises(RepoError, match="outside apps"):
        w.write_file("scripts/validate.sh", "evil")


def test_commit_and_push_creates_branch_on_origin(ws):
    w, bare, tmp = ws
    w.write_file("apps/x/base/cm.yaml", "new\n")
    w.diff()
    sha = w.commit_and_push("agent/x-abc12345", "fix(x): test", "vps192-agent", "bot@example.com")
    from tests.gitrepo import git

    assert git(tmp, "--git-dir", str(bare), "rev-parse", "agent/x-abc12345").strip() == sha
    assert git(tmp, "--git-dir", str(bare), "log", "-1", "--format=%an <%ae>", sha).strip() == (
        "vps192-agent <bot@example.com>"
    )


def test_git_errors_raise_repo_error(tmp_path):
    w = Workspace(tmp_path / "repo", str(tmp_path / "does-not-exist.git"))
    with pytest.raises(RepoError, match="git clone failed"):
        w.sync()


def test_service_dir_maps_namespace_to_apps_directory(tmp_path):
    files = {
        "apps/stress-app-be/vps192/kustomization.yaml": "namespace: stress-api\nresources: []\n",
        "apps/openclaw/vps192/kustomization.yaml": "resources: []\n",
        "apps/auth/vps192/kustomization.yaml": "namespace: 'auth'\n",
    }
    w = Workspace(tmp_path / "repo", str(make_origin(tmp_path, files)))
    w.sync()
    assert w.service_dir("stress-api") == "apps/stress-app-be"
    assert w.service_dir("auth") == "apps/auth"
    assert w.service_dir("openclaw") == "apps/openclaw"
    assert w.service_dir("nope") is None
