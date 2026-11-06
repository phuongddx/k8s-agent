import json
import os
import re
import threading

import httpx2
import pytest

from k8s_agent.pr import (
    BRANCH_EXISTS,
    FOOTER,
    IN_FLIGHT,
    MAIN_CHANGED,
    NOT_FOUND,
    GitHubClient,
    PrOpener,
    diff_is_plain_text,
    pr_body,
)
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
    assert "**Question:**\n\n```\nwhy is x down?\n```" in body
    assert "**Cause:**\n\n```\nA is 1\n```" in body
    assert "```\nevents: BackOff at 10:00\n```" in body
    assert body.endswith(FOOTER)


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


# --- fix round 1: R5 staged modes after apply, R6 bounded and neutralised PR body, R7 existing branch ---


def outside_fences(body: str) -> str:
    """The body with every fenced code block removed (a fence closes on a line equal to its opener)."""
    kept, fence = [], None
    for line in body.splitlines():
        if fence is None:
            if re.fullmatch(r"`{3,}", line):
                fence = line
            else:
                kept.append(line)
        elif line == fence:
            fence = None
    return "\n".join(kept)


def make_proposal(env, **overrides):
    fields = dict(
        diff=env["proposal"].diff,
        base_sha=env["proposal"].base_sha,
        service="x",
        summary="s",
        question="q",
        cause="c",
        evidence=[],
    )
    fields.update(overrides)
    return env["store"].add(**fields)


@pytest.fixture
def link_env(env):
    """env, with a symlink apps/x/link (to `target`) committed on origin/main and the clone synced to it."""
    other = env["tmp"] / "linkmaker"
    git(env["tmp"], "clone", "--quiet", str(env["bare"]), str(other))
    os.symlink("target", other / "apps/x/link")
    git(other, "add", "--all")
    git(other, "commit", "--quiet", "-m", "chore: add a symlink")
    git(other, "push", "--quiet", "origin", "HEAD:main")
    env["base"] = env["ws"].sync()
    return env


RENAME_TO_SYMLINK_DIFF = (
    "diff --git a/apps/x/link b/apps/x/link2\nsimilarity index 100%\nrename from apps/x/link\nrename to apps/x/link2\n"
)
COPY_SYMLINK_DIFF = (
    "diff --git a/apps/x/link b/apps/x/link2\nsimilarity index 100%\ncopy from apps/x/link\ncopy to apps/x/link2\n"
)
MODIFY_SYMLINK_DIFF = (
    "diff --git a/apps/x/link b/apps/x/link\n"
    "index 1111111..2222222\n"
    "--- a/apps/x/link\n"
    "+++ b/apps/x/link\n"
    "@@ -1 +1 @@\n"
    "-target\n"
    "\\ No newline at end of file\n"
    "+other\n"
    "\\ No newline at end of file\n"
)


@pytest.mark.parametrize(
    "diff", [RENAME_TO_SYMLINK_DIFF, COPY_SYMLINK_DIFF, MODIFY_SYMLINK_DIFF], ids=["rename", "copy", "modify"]
)
def test_headerless_symlink_diffs_are_refused_after_apply(link_env, diff):
    env = link_env
    assert diff_is_plain_text(diff)  # the text pass cannot see these, only the staged modes can
    p = make_proposal(env, diff=diff, base_sha=env["base"])
    out = env["opener"].open_pr(p.id)
    assert out["error"].startswith("Could not open the PR:") and "not a plain text change" in out["error"]
    assert env["stub"].requests == []
    assert git(env["tmp"], "--git-dir", str(env["bare"]), "branch", "--list", "agent/*").strip() == ""
    assert git(env["ws"].path, "status", "--porcelain").strip() == ""
    assert env["ws"].head() == env["base"]


def test_staged_entries_report_modes(link_env):
    ws = link_env["ws"]
    ws.apply(MODIFY_SYMLINK_DIFF)
    assert [(old, new, path) for old, new, path in ws.staged_entries()] == [("120000", "120000", "apps/x/link")]
    ws.reset("origin/main")


class StagedPathsLie(Workspace):
    def staged_paths(self):
        return ["scripts/validate.sh"]


def test_post_apply_check_uses_the_staged_paths(env):
    # No hand-written diff gets past the header check with different staged paths (git apply stages exactly the
    # paths its headers name), so the staged list is stubbed to differ and the post-apply check must refuse it.
    ws = StagedPathsLie(env["ws"].path, str(env["bare"]))
    opener = PrOpener(ws, env["renderer"], env["store"], env["opener"]._github, "b", "b@example.com")
    out = opener.open_pr(env["proposal"].id)
    assert out["error"].startswith("Could not open the PR: paths outside apps/**")
    assert env["stub"].requests == []
    assert env["renderer"].rendered == []


def test_pr_body_is_bounded_and_neutralised(env):
    cause = "@octocat <!-- ```` " + "`" * 50 + "x" * 100_000
    evidence = [{"source": "s" * 1000, "detail": f"@evil <b>{i}</b> " + "d" * 5000} for i in range(30)]
    p = make_proposal(env, cause=cause, question="@octocat " + "q" * 5000, evidence=evidence)
    assert env["opener"].open_pr(p.id) == {"pr_url": "https://github.com/phuongddx/vps192-k8s-config/pull/99"}
    body = json.loads(env["stub"].requests[0].content)["body"]
    assert len(body) < 60_000
    assert body.endswith(FOOTER)
    assert "@octocat" in body and "<!--" in body
    outside = outside_fences(body)
    for hazard in ("@octocat", "@evil", "<!--", "<b>"):
        assert hazard not in outside
    assert FOOTER in outside
    longest = max(len(run) for run in re.findall(r"`+", body.split("**Cause:**")[1].split("**Evidence")[0]))
    cause_fence = re.search(r"\*\*Cause:\*\*\n\n(`+)\n", body).group(1)
    assert len(cause_fence) == longest and len(cause_fence) > 50
    assert body.count("**Evidence") == 1 and len(re.findall(r"^Evidence \d+$", body, re.M)) == 10


def test_pr_body_truncates_with_an_ellipsis(env):
    p = make_proposal(env, question="q" * 1500, cause="c" * 2500, evidence=[{"source": "s" * 300, "detail": "d" * 600}])
    body = pr_body(p)
    assert "q" * 999 + "…" in body and "q" * 1000 not in body
    assert "c" * 1999 + "…" in body and "c" * 2000 not in body
    assert "s" * 199 + "…" in body and "d" * 499 + "…" in body


def test_existing_remote_branch_gives_a_readable_error(env):
    p = env["proposal"]
    git(env["tmp"], "--git-dir", str(env["bare"]), "branch", f"agent/x-{p.id[:8]}", "main")
    assert env["opener"].open_pr(p.id) == {"error": BRANCH_EXISTS}
    assert env["stub"].requests == []
    assert env["store"].get(p.id) is not None
