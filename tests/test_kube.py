import json
import logging

import httpx2
import pytest

from k8s_agent.kube import HttpKubeApi, KubeError
from k8s_agent.logs import JsonFormatter


def _api(tmp_path, handler, token="tok-1"):
    token_file = tmp_path / "token"
    token_file.write_text(token + "\n")
    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    return HttpKubeApi("https://10.43.0.1:443", token_file, http), token_file


def test_get_json_sends_bearer_and_params(tmp_path):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx2.Response(200, json={"items": []})

    api, _ = _api(tmp_path, handler)
    assert api.get_json("/api/v1/namespaces/a/pods", {"labelSelector": "app=x", "previous": True}) == {"items": []}
    assert seen["auth"] == "Bearer tok-1"
    assert seen["path"] == "/api/v1/namespaces/a/pods"
    assert seen["params"] == {"labelSelector": "app=x", "previous": "true"}


def test_token_is_reread_on_every_request(tmp_path):
    tokens = []

    def handler(request):
        tokens.append(request.headers["authorization"])
        return httpx2.Response(200, text="log line")

    api, token_file = _api(tmp_path, handler)
    api.get_text("/x")
    token_file.write_text("tok-2")
    api.get_text("/x")
    assert tokens == ["Bearer tok-1", "Bearer tok-2"]


def test_errors_raise_kube_error_with_api_message(tmp_path):
    def handler(request):
        return httpx2.Response(404, json={"kind": "Status", "message": 'pods "p" not found'})

    api, _ = _api(tmp_path, handler)
    with pytest.raises(KubeError) as info:
        api.get_json("/api/v1/namespaces/a/pods/p")
    assert info.value.status == 404
    assert info.value.message == 'pods "p" not found'


def test_forbidden_is_logged_as_warning(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="k8s_agent")

    def handler(request):
        return httpx2.Response(403, json={"message": "secrets is forbidden"})

    api, _ = _api(tmp_path, handler)
    with pytest.raises(KubeError):
        api.get_json("/api/v1/namespaces/a/secrets/s")
    [entry] = [json.loads(JsonFormatter().format(r)) for r in caplog.records]
    assert entry["event"] == "kube.forbidden"
    assert entry["level"] == "warning"
    assert entry["path"] == "/api/v1/namespaces/a/secrets/s"
