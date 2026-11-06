"""`kustomize build` as the render check for proposed changes.

Rendering must not reach the network or see secrets. Remote references are refused before kustomize
runs, and kustomize gets a fresh environment whose proxies point at a closed local port."""

import os
import re
import subprocess
from pathlib import Path
from typing import Protocol

_KUSTOMIZATION_NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
_LIST_KEYS = ("resources", "components", "bases", "generators", "transformers", "validators", "crds")
_KEY = re.compile(r"^['\"]?([A-Za-z]+)['\"]?\s*:(.*)$")
_ITEM = re.compile(r"^\s*-\s*(.*)$")
_HOST_REF = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}/")
_CLOSED_PROXY = "http://127.0.0.1:9"


class RenderError(Exception):
    def __init__(self, overlay: str, detail: str) -> None:
        super().__init__(f"kustomize build {overlay} failed: {detail}")
        self.overlay = overlay
        self.detail = detail


class Renderer(Protocol):
    def render(self, repo_dir: Path, overlay: str) -> None: ...


def _unquote(value: str) -> str:
    return value.strip().strip("'\"").strip()


def _list_entries(text: str) -> list[str]:
    """Entries of the list-valued keys in one kustomization file.

    Line-based on purpose: no YAML parser is a direct dependency of this project. Unterminated
    flow lists are rejected rather than guessed at."""
    entries: list[str] = []
    key: str | None = None
    for raw in text.splitlines():
        line = re.sub(r"\s+#.*$", "", raw).rstrip()
        if not line.strip():
            continue
        if not line[0].isspace() and not line.startswith("-"):
            match = _KEY.match(line)
            key = match.group(1) if match else None
            value = match.group(2).strip() if match else ""
            if key in _LIST_KEYS and value.startswith("["):
                if not value.endswith("]"):
                    raise ValueError(f"unsupported multi-line flow list under {key}")
                entries.extend(_unquote(item) for item in value[1:-1].split(",") if item.strip())
            continue
        item = _ITEM.match(line)
        if item and key in _LIST_KEYS:
            entries.append(_unquote(item.group(1)))
    return entries


def _is_remote(entry: str) -> bool:
    return "://" in entry or "::" in entry or entry.startswith("git@") or bool(_HOST_REF.match(entry))


def _refuse_remote_references(repo_dir: Path, overlay: str) -> None:
    apps = repo_dir / "apps"
    if apps.is_symlink():
        raise RenderError(overlay, "apps is a symlink")
    for dirpath, _dirnames, filenames in os.walk(apps, followlinks=False):
        for name in filenames:
            if name not in _KUSTOMIZATION_NAMES:
                continue
            path = Path(dirpath, name)
            rel = path.relative_to(repo_dir).as_posix()
            if path.is_symlink():
                raise RenderError(overlay, f"{rel} is a symlink")
            try:
                text = path.read_text(errors="replace")
            except OSError as exc:
                raise RenderError(overlay, f"cannot read {rel}: {exc.strerror}") from exc
            try:
                entries = _list_entries(text)
            except ValueError as exc:
                raise RenderError(overlay, f"{rel}: {exc}") from exc
            for entry in entries:
                if _is_remote(entry):
                    raise RenderError(overlay, f"remote kustomize references are not allowed: {entry} (in {rel})")


class KustomizeRenderer:
    def __init__(self, binary: str = "kustomize", timeout: float = 60) -> None:
        self._binary = binary
        self._timeout = timeout

    def render(self, repo_dir: Path, overlay: str) -> None:
        _refuse_remote_references(repo_dir, overlay)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(repo_dir.parent),
            "HTTPS_PROXY": _CLOSED_PROXY,
            "HTTP_PROXY": _CLOSED_PROXY,
            "https_proxy": _CLOSED_PROXY,
            "http_proxy": _CLOSED_PROXY,
            "NO_PROXY": "",
            "no_proxy": "",
            "GIT_ALLOW_PROTOCOL": "file",
        }
        try:
            result = subprocess.run(
                [self._binary, "build", overlay],
                cwd=repo_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RenderError(overlay, f"timed out after {self._timeout} s") from exc
        if result.returncode != 0:
            raise RenderError(overlay, result.stderr.strip()[-2000:])
