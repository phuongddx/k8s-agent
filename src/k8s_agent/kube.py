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
        http = httpx2.Client(verify=context, timeout=timeout)
        return cls(f"https://{host}:{port}", sa_dir / "token", http)

    def _get(self, path: str, params: Params | None, accept: str) -> httpx2.Response:
        token = self._token_file.read_text().strip()
        response = self._http.get(
            self._base_url + path,
            params=_encode(params),
            headers={"Authorization": f"Bearer {token}", "Accept": accept},
        )
        if response.status_code >= 400:
            try:
                message = str(response.json().get("message", ""))
            except ValueError:
                message = response.text[:300]
            if response.status_code == 403:
                log_event("kube.forbidden", logging.WARNING, path=path, status=403)
            raise KubeError(response.status_code, path, message or response.reason_phrase)
        return response

    def get_json(self, path: str, params: Params | None = None) -> dict:
        return self._get(path, params, "application/json").json()

    def get_text(self, path: str, params: Params | None = None) -> str:
        return self._get(path, params, "text/plain").text
