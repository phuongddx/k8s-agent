"""Read-only access to the Kubernetes API over HTTPS with the pod's ServiceAccount.

The `kubernetes` package is not used: v37 replaced its models and transport, and the agent
only needs GET requests returning raw JSON. The ServiceAccount token is re-read on every
request because bound tokens rotate."""

import logging
import os
import ssl
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import httpx2

from k8s_agent.logs import log_event

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
Params = Mapping[str, str | int | bool]
REFUSED_CHARS = ("#", "?", "%")


class KubeError(Exception):
    def __init__(self, status: int, path: str, message: str) -> None:
        super().__init__(f"{status} {path}: {message}")
        self.status = status
        self.path = path
        self.message = message


class KubeApi(Protocol):
    def get_json(self, path: str, params: Params | None = None) -> dict: ...

    def get_text(self, path: str, params: Params | None = None) -> str: ...


def _encode(params: Params | None) -> dict[str, str]:
    if not params:
        return {}
    return {k: (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in params.items()}


def _refuse(path: str) -> None:
    """Refuse any path that could escape its endpoint or name a Secret, before any I/O."""
    segments = {s.lower() for s in path.split("/")}
    if ".." in path or any(c in path for c in REFUSED_CHARS) or "secrets" in segments:
        raise KubeError(400, path, "refused path")


def _error_message(response: httpx2.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    return str(body.get("message", "")) if isinstance(body, dict) else ""


class HttpKubeApi:
    def __init__(self, base_url: str, token_file: Path, http: httpx2.Client) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_file = token_file
        self._http = http

    @classmethod
    def in_cluster(
        cls, sa_dir: Path = SA_DIR, env: Mapping[str, str] = os.environ, timeout: float = 15.0
    ) -> "HttpKubeApi":
        host = env["KUBERNETES_SERVICE_HOST"]
        port = env.get("KUBERNETES_SERVICE_PORT", "443")
        if ":" in host:
            host = f"[{host}]"
        context = ssl.create_default_context(cafile=str(sa_dir / "ca.crt"))
        # trust_env=False: a proxy variable must never receive the bearer token.
        http = httpx2.Client(verify=context, timeout=timeout, trust_env=False)
        return cls(f"https://{host}:{port}", sa_dir / "token", http)

    def _get(self, path: str, params: Params | None, accept: str) -> httpx2.Response:
        _refuse(path)
        try:
            token = self._token_file.read_text().strip()
        except OSError as exc:
            raise KubeError(500, path, "ServiceAccount token unreadable") from exc
        try:
            response = self._http.get(
                self._base_url + path,
                params=_encode(params),
                headers={"Authorization": f"Bearer {token}", "Accept": accept},
            )
        except (httpx2.HTTPError, OSError, ValueError, AttributeError) as exc:
            raise KubeError(503, path, f"API request failed: {type(exc).__name__}") from exc
        if response.status_code >= 300:
            if response.status_code == 403:
                log_event("kube.forbidden", logging.WARNING, path=path, status=403)
            raise KubeError(response.status_code, path, _error_message(response) or response.reason_phrase)
        return response

    def get_json(self, path: str, params: Params | None = None) -> dict:
        response = self._get(path, params, "application/json")
        try:
            body = response.json()
        except ValueError as exc:
            raise KubeError(502, path, "response was not JSON") from exc
        if not isinstance(body, dict):
            raise KubeError(502, path, "response was not a JSON object")
        return body

    def get_text(self, path: str, params: Params | None = None) -> str:
        return self._get(path, params, "text/plain").text
