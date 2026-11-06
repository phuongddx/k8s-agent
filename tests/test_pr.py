import json
import threading

import httpx2
import pytest

from k8s_agent.pr import IN_FLIGHT, MAIN_CHANGED, NOT_FOUND, GitHubClient, PrOpener, pr_body
from k8s_agent.proposals import ProposalStore
from k8s_agent.repo import Workspace
from tests.gitrepo import git, make_origin, push_commit
from tests.graph_fakes import FakeRenderer

FILES = {
    "apps/x/base/kustomization.yaml": "resources:\n  - cm.yaml\n",
    "apps/x/base/cm.yaml": 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\ndata:\n  A: "1"\n',
    "apps/x/vps192/kustomization.yaml": "resources:\n  - ../base\n",
    "scripts/validate.sh": "#!/bin/sh\n",
}


class GitHubStub:
    def __init__(self, status=201):
        self.requests = []
        self.status = status

    def handler(self, request):
        self.requests.append(request)
        if self.status != 201:
            return httpx2.Response(self.status, json={"message": "Validation Failed"})
        return httpx2.Response(201, json={"html_url": "https://github.com/phuongddx/vps192-k8s-config/pull/99"})


@pytest.fixture
def env(tmp_path):
    bare = make_origin(tmp_path, FILES)
    ws = Workspace(tmp_path / "repo", str(bare))
    base = ws.sync()
    ws.write_file("apps/x/base/cm.yaml", FILES["apps/x/base/cm.yaml"].replace('"1"', '"2"'))
    diff = ws.diff()
    ws.reset(base)
    store = ProposalStore()
    stub = GitHubStub()
    gh = GitHubClient(
        "phuongddx/vps192-k8s-config", "ghp_test", httpx2.Client(transport=httpx2.MockTransport(stub.handler))
    )
    renderer = FakeRenderer()
    opener = PrOpener(ws, renderer, store, gh, "vps192-agent", "vps192-agent@users.noreply.github.com")
    proposal = store.add(
        diff=diff,
        base_sha=base,
        service="x",
        summary="raise A to 2",
        question="why is x down?",
        cause="A is 1",
        evidence=[{"source": "events", "detail": "BackOff at 10:00"}],
    )
    return {
        "tmp": tmp_path,
        "bare": bare,
        "ws": ws,
        "store": store,
        "stub": stub,
        "opener": opener,
        "proposal": proposal,
        "renderer": renderer,
    }


def test_success_pushes_branch_and_opens_pr(env):
    p = env["proposal"]
    assert env["opener"].open_pr(p.id) == {"pr_url": "https://github.com/phuongddx/vps192-k8s-config/pull/99"}
    branch = f"agent/x-{p.id[:8]}"
    bare, tmp = env["bare"], env["tmp"]
    assert git(tmp, "--git-dir", str(bare), "log", "-1", "--format=%s|%an", branch).strip() == (
        "fix(x): raise A to 2|vps192-agent"
    )
    changed = git(tmp, "--git-dir", str(bare), "diff", "--name-only", "main", branch).split()
    assert changed == ["apps/x/base/cm.yaml"]
    [request] = env["stub"].requests
    assert request.url.path == "/repos/phuongddx/vps192-k8s-config/pulls"
    assert request.headers["authorization"] == "Bearer ghp_test"
    body = json.loads(request.content)
    assert body["head"] == branch and body["base"] == "main" and body["title"] == "fix(x): raise A to 2"
    assert body["body"].endswith("Opened by k8s-agent; owner review required.")
    assert env["renderer"].rendered == ["apps/x/vps192"]
    assert env["store"].get(p.id) is None
    assert env["ws"].head() == git(tmp, "--git-dir", str(bare), "rev-parse", "main").strip()


def test_missing_or_expired_proposal(env):
    assert env["opener"].open_pr("does-not-exist") == {"error": NOT_FOUND}


def test_main_moved_means_ask_again(env):
    push_commit(env["tmp"], env["bare"], "apps/x/base/other.yaml", "a: b\n", "chore: other change")
    assert env["opener"].open_pr(env["proposal"].id) == {"error": MAIN_CHANGED}
    assert env["stub"].requests == []


def test_paths_are_rechecked_before_commit(env):
    ws, store = env["ws"], env["store"]
    ws.write_file("apps/x/base/cm.yaml", "changed\n")
    evil = ws.diff().replace("apps/x/base/cm.yaml", "scripts/validate.sh")
    ws.reset("origin/main")
    p = store.add(diff=evil, base_sha=ws.head(), service="x", summary="s", question="q", cause="c", evidence=[])
    out = env["opener"].open_pr(p.id)
    assert out["error"].startswith("Could not open the PR")
    assert env["stub"].requests == []


def test_render_failure_blocks_the_pr(env):
    env["renderer"].fail_times = 1
    out = env["opener"].open_pr(env["proposal"].id)
    assert "kustomize build" in out["error"]
    assert env["stub"].requests == []


def test_github_error_is_reported(env):
    env["stub"].status = 422
    out = env["opener"].open_pr(env["proposal"].id)
    assert out == {"error": "Could not open the PR: GitHub returned 422: Validation Failed"}


def test_pr_body_lists_question_cause_evidence(env):
    body = pr_body(env["proposal"])
    assert "**Question:** why is x down?" in body
    assert "**Cause:** A is 1" in body
    assert "- **events**: BackOff at 10:00" in body


# --- controller rulings: R1 symlink/mode/binary, R2 duplicate guard, R3 render parity, R4 never raise ---

SYMLINK_DIFF = (
    "diff --git a/apps/x/base/link.yaml b/apps/x/base/link.yaml\n"
    "new file mode 120000\n"
    "index 0000000..1111111\n"
    "--- /dev/null\n"
    "+++ b/apps/x/base/link.yaml\n"
    "@@ -0,0 +1 @@\n"
    "+../../../scripts/validate.sh\n"
    "\\ No newline at end of file\n"
)
MODE_CHANGE_DIFF = "diff --git a/apps/x/base/cm.yaml b/apps/x/base/cm.yaml\nold mode 100644\nnew mode 100755\n"
EXEC_NEW_FILE_DIFF = (
    "diff --git a/apps/x/base/run.sh b/apps/x/base/run.sh\n"
    "new file mode 100755\n"
    "index 0000000..1111111\n"
    "--- /dev/null\n"
    "+++ b/apps/x/base/run.sh\n"
    "@@ -0,0 +1 @@\n"
    "+echo hi\n"
)
SYMLINK_INDEX_DIFF = (
    "diff --git a/apps/x/base/cm.yaml b/apps/x/base/cm.yaml\n"
    "index 1111111..2222222 120000\n"
    "--- a/apps/x/base/cm.yaml\n"
    "+++ b/apps/x/base/cm.yaml\n"
    "@@ -1 +1 @@\n"
    "-a\n"
    "+b\n"
)
BINARY_PATCH_DIFF = (
    "diff --git a/apps/x/base/blob.bin b/apps/x/base/blob.bin\n"
    "new file mode 100644\n"
    "index 0000000..1111111\n"
    "GIT binary patch\n"
    "literal 2\n"
    "Jc${NMa0000E\n"
)
BINARY_FILES_DIFF = (
    "diff --git a/apps/x/base/blob.bin b/apps/x/base/blob.bin\n"
    "index 1111111..2222222 100644\n"
    "Binary files a/apps/x/base/blob.bin and b/apps/x/base/blob.bin differ\n"
)


@pytest.mark.parametrize(
    "diff",
    [SYMLINK_DIFF, MODE_CHANGE_DIFF, EXEC_NEW_FILE_DIFF, SYMLINK_INDEX_DIFF, BINARY_PATCH_DIFF, BINARY_FILES_DIFF],
    ids=["new-symlink", "mode-change", "exec-new-file", "index-symlink-mode", "binary-patch", "binary-files"],
)
def test_symlink_mode_and_binary_diffs_are_refused(env, diff):
    p = env["store"].add(
        diff=diff, base_sha=env["ws"].head(), service="x", summary="s", question="q", cause="c", evidence=[]
    )
    out = env["opener"].open_pr(p.id)
    assert out["error"].startswith("Could not open the PR:")
    assert "not a plain text change" in out["error"]
    assert env["stub"].requests == []
    assert env["renderer"].rendered == []
    heads = git(env["tmp"], "--git-dir", str(env["bare"]), "branch", "--list", "agent/*").strip()
    assert heads == ""


def test_new_regular_file_still_opens_a_pr(env):
    ws = env["ws"]
    ws.write_file("apps/x/base/extra.yaml", "a: b\n")
    diff = ws.diff()
    ws.reset("origin/main")
    p = env["store"].add(
        diff=diff, base_sha=ws.head(), service="x", summary="add", question="q", cause="c", evidence=[]
    )
    assert "pr_url" in env["opener"].open_pr(p.id)


class BlockingRenderer(FakeRenderer):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def render(self, repo_dir, overlay):
        self.entered.set()
        assert self.release.wait(10)
        super().render(repo_dir, overlay)


def test_duplicate_open_pr_for_the_same_proposal_opens_one_pr(env):
    renderer = BlockingRenderer()
    opener = PrOpener(env["ws"], renderer, env["store"], env["opener"]._github, "vps192-agent", "bot@example.com")
    p = env["proposal"]
    first: list[dict] = []
    thread = threading.Thread(target=lambda: first.append(opener.open_pr(p.id)))
    thread.start()
    assert renderer.entered.wait(10)
    assert opener.open_pr(p.id) == {"error": IN_FLIGHT}
    renderer.release.set()
    thread.join(10)
    assert first == [{"pr_url": "https://github.com/phuongddx/vps192-k8s-config/pull/99"}]
    assert len(env["stub"].requests) == 1
    assert opener.open_pr(p.id) == {"error": NOT_FOUND}


def test_failed_open_pr_can_be_retried(env):
    env["renderer"].fail_times = 1
    assert "kustomize build" in env["opener"].open_pr(env["proposal"].id)["error"]
    assert "pr_url" in env["opener"].open_pr(env["proposal"].id)


def test_no_overlay_for_the_service_is_refused(env):
    ws = env["ws"]
    ws.write_file("apps/y/base/cm.yaml", "a: b\n")
    diff = ws.diff()
    ws.reset("origin/main")
    p = env["store"].add(diff=diff, base_sha=ws.head(), service="y", summary="s", question="q", cause="c", evidence=[])
    out = env["opener"].open_pr(p.id)
    assert out == {"error": "Could not open the PR: no kustomize overlay for apps/y"}
    assert env["stub"].requests == []


class RaisingRenderer:
    def render(self, repo_dir, overlay):
        raise OSError("kustomize binary missing")


def test_unexpected_renderer_error_is_returned_not_raised(env):
    opener = PrOpener(env["ws"], RaisingRenderer(), env["store"], env["opener"]._github, "b", "b@example.com")
    out = opener.open_pr(env["proposal"].id)
    assert out == {"error": "Could not open the PR: OSError"}
    assert env["stub"].requests == []
    assert "pr_url" in env["opener"].open_pr(env["proposal"].id)


def _github_with(handler):
    return GitHubClient(
        "phuongddx/vps192-k8s-config", "ghp_secret", httpx2.Client(transport=httpx2.MockTransport(handler))
    )


def test_network_error_does_not_leak_the_token(env):
    def handler(request):
        raise httpx2.ConnectError("connect failed for Bearer ghp_secret")

    opener = PrOpener(env["ws"], env["renderer"], env["store"], _github_with(handler), "b", "b@example.com")
    out = opener.open_pr(env["proposal"].id)
    assert out["error"].startswith("Could not open the PR:")
    assert "ghp_secret" not in out["error"]


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(201, json=["not", "a", "dict"]),
        httpx2.Response(201, json={"no_url": True}),
        httpx2.Response(201, text="<html>"),
        httpx2.Response(500, text="boom ghp_secret"),
        httpx2.Response(422, json=["x"]),
    ],
    ids=["list-body", "missing-url", "html-body", "500-text-with-token", "422-list"],
)
def test_malformed_github_responses_are_errors(env, response):
    opener = PrOpener(
        env["ws"], env["renderer"], env["store"], _github_with(lambda request: response), "b", "b@example.com"
    )
    out = opener.open_pr(env["proposal"].id)
    assert out["error"].startswith("Could not open the PR:")
    assert "ghp_secret" not in out["error"]
