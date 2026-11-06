import json
import logging
import ssl

import httpx2
import pytest

from k8s_agent import kube
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
        return httpx2.Response(403, json={"message": "pods is forbidden"})

    api, _ = _api(tmp_path, handler)
    with pytest.raises(KubeError):
        api.get_json("/api/v1/namespaces/a/pods/p")
    [entry] = [json.loads(JsonFormatter().format(r)) for r in caplog.records]
    assert entry["event"] == "kube.forbidden"
    assert entry["level"] == "warning"
    assert entry["path"] == "/api/v1/namespaces/a/pods/p"


REFUSED_PATHS = [
    "/api/v1/namespaces/a/configmaps/../secrets/db",
    "/api/v1/namespaces/a/configmaps/x#frag",
    "/api/v1/namespaces/a/pods?labelSelector=x",
    "/api/v1/namespaces/a/pods%2fsecrets",
    "/api/v1/namespaces/a/secrets/x",
    "/api/v1/secrets",
]


@pytest.mark.parametrize("path", REFUSED_PATHS)
def test_refuses_unsafe_paths_before_any_request(tmp_path, path):
    sent = []

    def handler(request):
        sent.append(request.url.path)
        return httpx2.Response(200, json={})

    api, _ = _api(tmp_path, handler)
    with pytest.raises(KubeError, match="refused path"):
        api.get_json(path)
    with pytest.raises(KubeError, match="refused path"):
        api.get_text(path)
    assert sent == []


def test_transport_failure_becomes_kube_error(tmp_path):
    def handler(request):
        raise httpx2.ConnectError("connection refused")

    api, _ = _api(tmp_path, handler)
    with pytest.raises(KubeError) as info:
        api.get_json("/api/v1/nodes")
    assert info.value.status == 503
    assert info.value.message == "API request failed: ConnectError"


def test_non_json_success_body_becomes_kube_error(tmp_path):
    api, _ = _api(tmp_path, lambda request: httpx2.Response(200, text="<html>proxy page</html>"))
    with pytest.raises(KubeError):
        api.get_json("/api/v1/nodes")


def test_non_object_success_body_becomes_kube_error(tmp_path):
    api, _ = _api(tmp_path, lambda request: httpx2.Response(200, json=[1, 2]))
    with pytest.raises(KubeError):
        api.get_json("/api/v1/nodes")


def test_error_body_that_is_a_list_becomes_kube_error(tmp_path):
    api, _ = _api(tmp_path, lambda request: httpx2.Response(500, json=[{"message": "x"}]))
    with pytest.raises(KubeError) as info:
        api.get_json("/api/v1/nodes")
    assert info.value.status == 500


def test_redirect_becomes_kube_error(tmp_path):
    api, _ = _api(tmp_path, lambda request: httpx2.Response(302, headers={"location": "/elsewhere"}, text=""))
    with pytest.raises(KubeError) as info:
        api.get_json("/api/v1/nodes")
    assert info.value.status == 302


def test_missing_token_file_becomes_kube_error(tmp_path):
    http = httpx2.Client(transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={})))
    api = HttpKubeApi("https://10.43.0.1:443", tmp_path / "no-such-token", http)
    with pytest.raises(KubeError) as info:
        api.get_json("/api/v1/nodes")
    assert info.value.status == 500


def test_in_cluster_client_ignores_proxy_environment(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("tok-1\n")
    real_context = ssl.create_default_context
    seen = {}

    def recording_context(cafile=None, **kwargs):
        seen["cafile"] = cafile
        return real_context()

    monkeypatch.setattr(kube.ssl, "create_default_context", recording_context)
    api = HttpKubeApi.in_cluster(sa_dir=tmp_path, env={"KUBERNETES_SERVICE_HOST": "10.43.0.1"})
    assert seen["cafile"] == str(tmp_path / "ca.crt")
    assert api._http.trust_env is False
