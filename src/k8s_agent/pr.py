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
BRANCH_EXISTS = "A branch for this fix already exists; ask again for a fresh proposal."
FOOTER = "Opened by k8s-agent; owner review required."
IN_FLIGHT = "This fix is already being opened."
NOT_PLAIN_TEXT = "the diff is not a plain text change (symlink, file mode or binary content)"
NO_OVERLAY = "no kustomize overlay for apps/{service}"

_PLAIN_MODE = "100644"
_MODE_HEADER = re.compile(r"^(?:old mode|new mode|(?:new|deleted) file mode) (\d+)$")
_INDEX_HEADER = re.compile(r"^index \S+\.\.\S+ (\d+)$")
_QUESTION_MAX, _CAUSE_MAX, _SOURCE_MAX, _DETAIL_MAX, _EVIDENCE_MAX = 1000, 2000, 200, 500, 10
_ALLOWED_STAGED_MODES = frozenset({"000000", "100644"})
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


def _clip(value: object, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fenced(value: str) -> str:
    """The value in a code block whose fence is longer than any backtick run inside it."""
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{value}\n{fence}"


def pr_body(p: Proposal) -> str:
    """Bounded and inert: model-written text sits in code blocks, so mentions, HTML and comments do nothing."""
    parts = [
        f"**Question:**\n\n{_fenced(_clip(p.question, _QUESTION_MAX))}",
        f"**Cause:**\n\n{_fenced(_clip(p.cause, _CAUSE_MAX))}",
        "**Evidence:**",
    ]
    for number, item in enumerate(p.evidence[:_EVIDENCE_MAX], start=1):
        line = f"{_clip(item.get('source', '?'), _SOURCE_MAX)}: {_clip(item.get('detail', ''), _DETAIL_MAX)}"
        parts.append(f"Evidence {number}\n\n{_fenced(line)}")
    parts.append(f"Proposal `{p.id}`, based on `{p.base_sha}`.")
    parts.append(FOOTER)
    return "\n\n".join(parts)


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
            # The text check cannot see a rename or copy of a symlink, or an edit of one, so read what git staged.
            if any({src, dst} - _ALLOWED_STAGED_MODES for src, dst, _ in self._ws.staged_entries()):
                raise PathNotAllowed(NOT_PLAIN_TEXT)
            overlays = overlays_for(self._ws.path, service)
            if not overlays:
                raise RepoError(NO_OVERLAY.format(service=service))
            for overlay in overlays:
                self._renderer.render(self._ws.path, overlay)
            branch = f"agent/{service}-{p.id[:8]}"
            if self._ws.remote_branch_exists(branch):
                return {"error": BRANCH_EXISTS}
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
