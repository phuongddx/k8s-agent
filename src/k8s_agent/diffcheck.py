"""The `apps/**` allowlist for every path the agent writes, diffs, applies or commits."""

import re
from pathlib import Path, PurePosixPath

_DIFF_GIT = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HEADER_PREFIXES = ("rename from ", "rename to ", "copy from ", "copy to ")


SELF_SERVICE = "k8s-agent"
OWN_MANIFESTS = f"the agent may not change its own manifests (apps/{SELF_SERVICE}/)"


class PathNotAllowed(ValueError):
    """A path is outside `apps/<service>/`, is the agent's own service, or the change spans several services."""


def is_own_manifest(path: str) -> bool:
    """True for apps/k8s-agent/**: the agent's own manifests and RBAC, which it must never change."""
    parts = PurePosixPath(path).parts
    return len(parts) >= 2 and parts[0] == "apps" and parts[1].lower() == SELF_SERVICE


def is_allowed(path: str) -> bool:
    if not path or path.startswith(("/", '"')) or "\\" in path:
        return False
    parts = PurePosixPath(path).parts
    ok = len(parts) >= 3 and parts[0] == "apps" and all(p not in ("..", ".", ".git") for p in parts)
    return ok and not is_own_manifest(path)


def paths_in_diff(diff: str) -> list[str]:
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            match = _DIFF_GIT.match(line)
            if not match:
                paths.add(line[len("diff --git ") :])
                continue
            paths.update(match.groups())
        elif line.startswith(_HEADER_PREFIXES):
            paths.add(line.split(" ", 2)[2])
        elif line.startswith(("--- ", "+++ ")):
            path = line[4:].split("\t")[0]
            if path == "/dev/null":
                continue
            paths.add(path[2:] if path.startswith(("a/", "b/")) else path)
    return sorted(paths)


def check_paths(paths: list[str]) -> str:
    """Return the single service the paths belong to, or raise PathNotAllowed."""
    if not paths:
        raise PathNotAllowed("the change touches no files")
    own = [p for p in paths if is_own_manifest(p)]
    if own:
        raise PathNotAllowed(f"{OWN_MANIFESTS}: {', '.join(sorted(own))}")
    bad = [p for p in paths if not is_allowed(p)]
    if bad:
        raise PathNotAllowed(f"paths outside apps/**: {', '.join(sorted(bad))}")
    services = sorted({PurePosixPath(p).parts[1] for p in paths})
    if len(services) != 1:
        raise PathNotAllowed(f"the change spans several services: {', '.join(services)}")
    return services[0]


def overlays_for(repo_root: Path, service: str) -> list[str]:
    """Overlays to render for a change under apps/<service>/. A base change renders every overlay."""
    service_dir = repo_root / "apps" / service
    overlays = (
        sorted(
            f"apps/{service}/{d.name}"
            for d in service_dir.iterdir()
            if d.is_dir() and d.name != "base" and (d / "kustomization.yaml").is_file()
        )
        if service_dir.is_dir()
        else []
    )
    if not overlays and (service_dir / "base" / "kustomization.yaml").is_file():
        overlays = [f"apps/{service}/base"]
    return overlays
