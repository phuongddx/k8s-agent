"""open_pr: turn a stored proposal into a branch and a pull request, as the bot account."""

import re
import threading

import httpx2

from k8s_agent.diffcheck import PathNotAllowed, check_paths, overlays_for, paths_in_diff
from k8s_agent.logs import log_event
from k8s_agent.proposals import Proposal, ProposalStore
from k8s_agent.render import Renderer, RenderError
from k8s_agent.repo import RepoError, Workspace

NOT_FOUND = "Proposal not found, ask again."
MAIN_CHANGED = "main changed, ask again."
IN_FLIGHT = "This fix is already being opened."
NOT_PLAIN_TEXT = "the diff is not a plain text change (symlink, file mode or binary content)"
NO_OVERLAY = "no kustomize overlay for apps/{service}"

_PLAIN_MODE = "100644"
_MODE_HEADER = re.compile(r"^(?:old mode|new mode|(?:new|deleted) file mode) (\d+)$")
_INDEX_HEADER = re.compile(r"^index \S+\.\.\S+ (\d+)$")
# Hunk bodies start with one of these; every other line of a git diff is a header.
_BODY_PREFIXES = (" ", "+", "-", "\\")


class GitHubError(Exception):
    pass


class GitHubClient:
    def __init__(self, repo: str, token: str, http: httpx2.Client, api_url: str = "https://api.github.com") -> None:
        self._repo = repo
        self._token = token
        self._http = http
        self._api_url = api_url.rstrip("/")

    def redact(self, text: str) -> str:
        return text.replace(self._token, "[redacted]") if self._token else text

    def create_pull(self, head: str, base: str, title: str, body: str) -> str:
        try:
            response = self._http.post(
                f"{self._api_url}/repos/{self._repo}/pulls",
                json={"head": head, "base": base, "title": title, "body": body},
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except httpx2.HTTPError as exc:
            # The exception text can echo request details, so only its type is reported.
            raise GitHubError(f"GitHub request failed: {type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code != 201:
            message = payload.get("message", "") if isinstance(payload, dict) else response.text[:200]
            raise GitHubError(self.redact(f"GitHub returned {response.status_code}: {message}"))
        url = payload.get("html_url") if isinstance(payload, dict) else None
        if not isinstance(url, str) or not url:
            raise GitHubError("GitHub returned 201 without a pull request URL")
        return url


def diff_is_plain_text(diff: str) -> bool:
    """False for a diff that adds or changes a symlink, changes a file mode, or is binary.

    Only header lines are read: hunk bodies always start with a space, +, - or backslash."""
    for line in diff.splitlines():
        if line.startswith(_BODY_PREFIXES) or not line:
            continue
        if line.startswith(("GIT binary patch", "Binary files ")):
            return False
        if "mode 120000" in line:
            return False
        match = _MODE_HEADER.match(line) or _INDEX_HEADER.match(line)
        if match and (line.startswith(("old mode", "new mode")) or match.group(1) != _PLAIN_MODE):
            return False
    return True


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
        self._guard = threading.Lock()
        self._in_flight: set[str] = set()
        # One clone backs every call, so two different proposals must not share the work tree.
        self._tree = threading.Lock()

    def open_pr(self, proposal_id: str) -> dict:
        """Open the PR for a stored proposal. Never raises: failures come back as {"error": ...}.

        A second call for a proposal that is already being opened returns IN_FLIGHT, so one
        proposal produces at most one push and one PR."""
        with self._guard:
            if proposal_id in self._in_flight:
                return {"error": IN_FLIGHT}
            self._in_flight.add(proposal_id)
        try:
            with self._tree:
                return self._open(proposal_id)
        except Exception as exc:
            log_event("open_pr.failed", proposal_id=proposal_id, error_type=type(exc).__name__)
            return {"error": f"Could not open the PR: {type(exc).__name__}"}
        finally:
            with self._guard:
                self._in_flight.discard(proposal_id)

    def _open(self, proposal_id: str) -> dict:
        p = self._store.get(proposal_id)
        if p is None:
            return {"error": NOT_FOUND}
        try:
            if not diff_is_plain_text(p.diff):
                raise PathNotAllowed(NOT_PLAIN_TEXT)
            check_paths(paths_in_diff(p.diff))
            if self._ws.fetch_origin_main() != p.base_sha:
                return {"error": MAIN_CHANGED}
            self._ws.reset(p.base_sha)
            self._ws.apply(p.diff)
            service = check_paths(sorted(set(self._ws.staged_paths()) | set(paths_in_diff(p.diff))))
            overlays = overlays_for(self._ws.path, service)
            if not overlays:
                raise RepoError(NO_OVERLAY.format(service=service))
            for overlay in overlays:
                self._renderer.render(self._ws.path, overlay)
            branch = f"agent/{service}-{p.id[:8]}"
            title = f"fix({service}): {p.summary}"
            self._ws.commit_and_push(branch, title, self._bot_login, self._bot_email)
            url = self._github.create_pull(head=branch, base="main", title=title, body=pr_body(p))
        except (RepoError, PathNotAllowed, RenderError, GitHubError) as exc:
            log_event("open_pr.failed", proposal_id=proposal_id, error_type=type(exc).__name__)
            return {"error": f"Could not open the PR: {self._github.redact(str(exc))}"}
        finally:
            try:
                self._ws.reset("origin/main")
            except RepoError:
                pass
        self._store.remove(p.id)
        log_event("open_pr.done", proposal_id=proposal_id, pr_url=url)
        return {"pr_url": url}
