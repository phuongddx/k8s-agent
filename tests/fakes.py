"""Test doubles shared by the test suite and the manual eval harness."""

import copy

from k8s_agent.kube import KubeError, Params


class FakeKube:
    """A KubeApi backed by a snapshot dict of API path -> JSON object (or log text).

    Log text for `previous=true` lives under the key `<path>?previous=true`."""

    def __init__(self, snapshot: dict[str, object], forbidden: frozenset[str] = frozenset()) -> None:
        self.snapshot = snapshot
        self.forbidden = forbidden
        self.calls: list[str] = []
        self.params_seen: list[tuple[str, dict]] = []

    def _lookup(self, path: str, params: Params | None) -> object:
        self.calls.append(path)
        self.params_seen.append((path, dict(params or {})))
        if path in self.forbidden:
            raise KubeError(403, path, "forbidden")
        key = path
        if params and params.get("previous") is True:
            key = f"{path}?previous=true"
        if key not in self.snapshot:
            raise KubeError(404, path, "not found")
        return copy.deepcopy(self.snapshot[key])

    def get_json(self, path: str, params: Params | None = None) -> dict:
        value = self._lookup(path, params)
        assert isinstance(value, dict), f"{path} holds text, not JSON"
        return value

    def get_text(self, path: str, params: Params | None = None) -> str:
        value = self._lookup(path, params)
        assert isinstance(value, str), f"{path} holds JSON, not text"
        return value
