"""Throwaway git repositories for tests. Hermetic: no global git config, so no signing."""

import os
import subprocess
from pathlib import Path

ENV = {
    "PATH": os.environ["PATH"],
    "HOME": os.environ.get("HOME", "/tmp"),
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "LC_ALL": "C",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        env=ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_origin(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a bare repo with one commit on main holding `files`; return the bare repo path."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet", "-b", "main")
    for rel, content in files.items():
        target = seed / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(seed, "add", "--all")
    git(seed, "commit", "--quiet", "-m", "chore: seed")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(bare))
    return bare


def push_commit(tmp_path: Path, bare: Path, rel: str, content: str, message: str) -> str:
    """Add a commit to origin/main from a separate clone; return its SHA."""
    other = tmp_path / f"other-{abs(hash((rel, message)))}"
    git(tmp_path, "clone", "--quiet", str(bare), str(other))
    target = other / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(other, "add", "--all")
    git(other, "commit", "--quiet", "-m", message)
    git(other, "push", "--quiet", "origin", "HEAD:main")
    return git(other, "rev-parse", "HEAD").strip()
