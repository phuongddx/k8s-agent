"""open_pr: turn a stored proposal into a branch and a pull request, as the bot account."""

import httpx2

from k8s_agent.diffcheck import PathNotAllowed, check_paths, overlays_for, paths_in_diff
from k8s_agent.logs import log_event
from k8s_agent.proposals import Proposal, ProposalStore
from k8s_agent.render import Renderer, RenderError
from k8s_agent.repo import RepoError, Workspace

NOT_FOUND = "Proposal not found, ask again."
MAIN_CHANGED = "main changed, ask again."


class GitHubError(Exception):
    pass


class GitHubClient:
    def __init__(self, repo: str, token: str, http: httpx2.Client, api_url: str = "https://api.github.com") -> None:
        self._repo = repo
        self._token = token
        self._http = http
        self._api_url = api_url.rstrip("/")

    def create_pull(self, head: str, base: str, title: str, body: str) -> str:
        response = self._http.post(
            f"{self._api_url}/repos/{self._repo}/pulls",
            json={"head": head, "base": base, "title": title, "body": body},
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if response.status_code != 201:
            try:
                message = response.json().get("message", "")
            except ValueError:
                message = response.text[:200]
            raise GitHubError(f"GitHub returned {response.status_code}: {message}")
        return response.json()["html_url"]


def pr_body(p: Proposal) -> str:
    evidence = "\n".join(f"- **{e.get('source', '?')}**: {e.get('detail', '')}" for e in p.evidence[:8])
    return (
        f"**Question:** {p.question}\n\n**Cause:** {p.cause}\n\n**Evidence:**\n{evidence}\n\n"
        f"Proposal `{p.id}`, based on `{p.base_sha}`.\n\n"
        "Opened by k8s-agent; owner review required."
    )


class PrOpener:
    def __init__(
        self,
        workspace: Workspace,
        renderer: Renderer,
        store: ProposalStore,
        github: GitHubClient,
        bot_login: str,
        bot_email: str,
    ) -> None:
        self._ws = workspace
        self._renderer = renderer
        self._store = store
        self._github = github
        self._bot_login = bot_login
        self._bot_email = bot_email

    def open_pr(self, proposal_id: str) -> dict:
        p = self._store.get(proposal_id)
        if p is None:
            return {"error": NOT_FOUND}
        try:
            if self._ws.fetch_origin_main() != p.base_sha:
                return {"error": MAIN_CHANGED}
            self._ws.reset(p.base_sha)
            self._ws.apply(p.diff)
            service = check_paths(sorted(set(self._ws.staged_paths()) | set(paths_in_diff(p.diff))))
            for overlay in overlays_for(self._ws.path, service):
                self._renderer.render(self._ws.path, overlay)
            branch = f"agent/{service}-{p.id[:8]}"
            title = f"fix({service}): {p.summary}"
            self._ws.commit_and_push(branch, title, self._bot_login, self._bot_email)
            url = self._github.create_pull(head=branch, base="main", title=title, body=pr_body(p))
        except (RepoError, PathNotAllowed, RenderError, GitHubError) as exc:
            log_event("open_pr.failed", proposal_id=proposal_id, error_type=type(exc).__name__)
            return {"error": f"Could not open the PR: {exc}"}
        finally:
            try:
                self._ws.reset("origin/main")
            except RepoError:
                pass
        self._store.remove(p.id)
        log_event("open_pr.done", proposal_id=proposal_id, pr_url=url)
        return {"pr_url": url}
