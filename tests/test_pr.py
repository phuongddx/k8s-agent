import json

import httpx2
import pytest

from k8s_agent.pr import MAIN_CHANGED, NOT_FOUND, GitHubClient, PrOpener, pr_body
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
