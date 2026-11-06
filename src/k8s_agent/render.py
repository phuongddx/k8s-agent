"""`kustomize build` as the render check for proposed changes."""

import subprocess
from pathlib import Path
from typing import Protocol


class RenderError(Exception):
    def __init__(self, overlay: str, detail: str) -> None:
        super().__init__(f"kustomize build {overlay} failed: {detail}")
        self.overlay = overlay
        self.detail = detail


class Renderer(Protocol):
    def render(self, repo_dir: Path, overlay: str) -> None: ...


class KustomizeRenderer:
    def __init__(self, binary: str = "kustomize", timeout: float = 60) -> None:
        self._binary = binary
        self._timeout = timeout

    def render(self, repo_dir: Path, overlay: str) -> None:
        try:
            result = subprocess.run(
                [self._binary, "build", overlay],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RenderError(overlay, f"timed out after {self._timeout} s") from exc
        if result.returncode != 0:
            raise RenderError(overlay, result.stderr.strip()[-2000:])
