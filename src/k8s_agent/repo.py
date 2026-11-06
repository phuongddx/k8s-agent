"""The clone of REPO in REPO_DIR. Every git call is hermetic: no global or system git config,
no prompts, and the token reaches git only through a credential helper that reads it from
the environment, so it never lands in argv, the remote URL, or .git/config."""

import os
import re
import subprocess
from pathlib import Path

from k8s_agent.diffcheck import is_allowed

_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get && printf \'username=x-access-token\\npassword=%s\\n\' "$K8S_AGENT_GIT_TOKEN"; }; f'
)


class RepoError(Exception):
    """A git operation failed or a path was rejected."""


class Workspace:
    def __init__(self, path: Path, remote_url: str, token: str | None = None, max_bytes: int = 32768) -> None:
        self.path = path
        self._remote_url = remote_url
        self._token = token
        self._max_bytes = max_bytes

    def _git(self, *args: str, cwd: Path | None = None, stdin: str | None = None) -> str:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(self.path.parent)),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LITERAL_PATHSPECS": "1",
            "LC_ALL": "C",
        }
        config: list[str] = []
        if self._token:
            env["K8S_AGENT_GIT_TOKEN"] = self._token
            config = ["-c", "credential.helper=", "-c", f"credential.helper={_CREDENTIAL_HELPER}"]
        try:
            result = subprocess.run(
                ["git", *config, *args],
                cwd=cwd or self.path,
                env=env,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RepoError(f"git {args[0]} timed out") from exc
        if result.returncode != 0:
            last = (result.stderr.strip().splitlines() or ["no output"])[-1]
            raise RepoError(f"git {args[0]} failed: {last}")
        return result.stdout

    def sync(self) -> str:
        """Clone or fetch, then put the work tree at a clean origin/main. Returns that SHA."""
        if not (self.path / ".git").is_dir():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._git("clone", "--quiet", self._remote_url, str(self.path), cwd=self.path.parent)
        else:
            self._git("fetch", "--quiet", "--prune", "origin")
        self.reset("origin/main")
        return self.head()

    def fetch_origin_main(self) -> str:
        self._git("fetch", "--quiet", "--prune", "origin")
        return self._git("rev-parse", "origin/main").strip()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def reset(self, ref: str = "HEAD") -> None:
        self._git("checkout", "--quiet", "--force", "--detach", ref)
        self._git("reset", "--quiet", "--hard", ref)
        self._git("clean", "-ffdxq")

    def resolve(self, rel: str) -> Path:
        if not rel or rel.startswith("/") or "\\" in rel:
            raise RepoError(f"invalid path {rel!r}")
        try:
            root = self.path.resolve()
            target = (root / rel).resolve()
        except (ValueError, OSError) as exc:
            raise RepoError(f"invalid path {rel!r}") from exc
        if target == root or not target.is_relative_to(root):
            raise RepoError(f"path {rel!r} escapes the repository")
        if ".git" in target.relative_to(root).parts:
            raise RepoError(f"path {rel!r} is inside .git")
        return target

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 200) -> dict:
        target = self.resolve(path)
        if not target.is_file():
            raise RepoError(f"{path} is not a file")
        try:
            lines = target.read_text(errors="replace").splitlines()
        except OSError as exc:
            raise RepoError(f"cannot read {path}: {exc.strerror}") from exc
        start = max(1, start_line)
        chosen = lines[start - 1 : start - 1 + max(1, max_lines)]
        numbered = [f"{start + i}: {line}" for i, line in enumerate(chosen)]
        while numbered and len("\n".join(numbered).encode()) > self._max_bytes:
            numbered.pop()
        return {
            "path": path,
            "start_line": start,
            "end_line": start + len(numbered) - 1,
            "total_lines": len(lines),
            "content": "\n".join(numbered),
        }

    def list_files(self, rel_dir: str) -> list[str]:
        self.resolve(rel_dir)
        return [p for p in self._git("ls-files", "--", rel_dir).splitlines() if p]

    def service_dir(self, namespace: str) -> str | None:
        """The `apps/<dir>` whose kustomization sets `namespace: <namespace>` (dir and namespace can differ)."""
        apps = self.path / "apps"
        if not apps.is_dir():
            return None
        pattern = re.compile(rf"^namespace:\s*['\"]?{re.escape(namespace)}['\"]?\s*$", re.MULTILINE)
        for service in sorted(d for d in apps.iterdir() if d.is_dir()):
            for kustomization in sorted(service.glob("*/kustomization.yaml")):
                if pattern.search(kustomization.read_text(errors="replace")):
                    return f"apps/{service.name}"
        return f"apps/{namespace}" if (apps / namespace).is_dir() else None

    def log(self, path: str, limit: int = 5) -> list[dict]:
        self.resolve(path)
        out = self._git("log", f"-n{min(max(1, limit), 20)}", "--format=%H%x1f%an%x1f%aI%x1f%s", "--", path)
        commits = []
        for line in out.splitlines():
            sha, author, date, subject = line.split("\x1f", 3)
            commits.append({"sha": sha, "author": author, "date": date, "subject": subject})
        return commits

    def write_file(self, rel: str, content: str) -> None:
        if not is_allowed(rel):
            raise RepoError(f"{rel} is outside apps/**")
        target = self.resolve(rel)
        # The allowlist must hold for the resolved location: a symlinked parent can move the write out of apps/.
        resolved_rel = target.relative_to(self.path.resolve()).as_posix()
        if not is_allowed(resolved_rel):
            raise RepoError(f"{rel} is outside apps/**")
        # resolve() follows links, so only the leaf itself can still be a symlink here.
        if (self.path / rel).is_symlink():
            raise RepoError(f"{rel} is a symlink")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        except (ValueError, OSError) as exc:
            raise RepoError(f"cannot write {rel}: {getattr(exc, 'strerror', None) or exc}") from exc

    def diff(self) -> str:
        """Unified diff of the work tree against HEAD, new files included. Leaves the index staged."""
        self._git("add", "--all")
        return self._git("diff", "--cached", "--no-renames", "--no-color", "--no-ext-diff", "HEAD")

    def staged_paths(self) -> list[str]:
        return [p for p in self._git("diff", "--cached", "--no-renames", "--name-only", "HEAD").splitlines() if p]

    def staged_entries(self) -> list[tuple[str, str, str]]:
        """(source mode, destination mode, path) of every staged change against HEAD, read from git's raw diff."""
        entries = []
        for line in self._git("diff", "--cached", "--raw", "--no-renames", "HEAD").splitlines():
            meta, _, path = line.partition("\t")
            fields = meta.lstrip(":").split()
            entries.append((fields[0], fields[1], path) if len(fields) >= 2 else ("?", "?", path))
        return entries

    def remote_branch_exists(self, branch: str) -> bool:
        return bool(self._git("ls-remote", "--heads", "origin", f"refs/heads/{branch}").strip())

    def apply(self, diff: str) -> None:
        self._git("apply", "--index", "--whitespace=nowarn", "-", stdin=diff)

    def commit_and_push(self, branch: str, message: str, author_name: str, author_email: str) -> str:
        self._git(
            "-c", f"user.name={author_name}", "-c", f"user.email={author_email}", "commit", "--quiet", "-m", message
        )
        self._git("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        return self.head()
