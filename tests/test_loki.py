import httpx2
import pytest

from k8s_agent.tools.loki import LokiClient, LokiQueryError, LokiUnavailable

NOW = 1_762_336_800.0  # 2025-11-05T10:00:00Z


def _client(handler, **kw):
    return LokiClient("http://loki.logging.svc:3100/", httpx2.Client(transport=httpx2.MockTransport(handler)), **kw)


def _streams(*values):
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{"stream": {"namespace": "kube-system", "app": "traefik"}, "values": list(values)}],
        },
    }


def test_query_range_params_and_flattening():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx2.Response(200, json=_streams(["1762336790000000000", "b"], ["1762336780000000000", "a"]))

    out = _client(handler).query('{app="traefik"} |= "502"', now=NOW)
    assert seen["path"] == "/loki/api/v1/query_range"
    p = seen["params"]
    assert p["query"] == '{app="traefik"} |= "502"'
    assert p["direction"] == "backward"
    assert p["limit"] == "200"
    assert int(p["end"]) - int(p["start"]) == 3600 * 10**9
    assert [e["line"] for e in out["entries"]] == ["a", "b"]
    assert out["entries"][0]["ts"] == "2025-11-05T09:59:40.000+00:00"
    assert out["window_hours"] == 1 and out["clamped"] is False


def test_window_is_clamped_to_168_hours_and_limit_to_200():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx2.Response(200, json=_streams())

    out = _client(handler).query('{app="x"}', hours=500, limit=1000, now=NOW)
    assert int(seen["end"]) - int(seen["start"]) == 168 * 3600 * 10**9
    assert seen["limit"] == "200"
    assert out["clamped"] is True


def test_output_is_capped_to_max_bytes():
    values = [[str(1762336000000000000 + i), "y" * 400] for i in range(200)]
    out = _client(lambda r: httpx2.Response(200, json=_streams(*values))).query('{a="b"}', now=NOW)
    assert out["truncated"] is True
    assert len(str(out["entries"]).encode()) <= 32768


def test_unreachable_and_5xx_raise_unavailable():
    def boom(request):
        raise httpx2.ConnectError("refused")

    with pytest.raises(LokiUnavailable):
        _client(boom).query('{a="b"}')
    with pytest.raises(LokiUnavailable):
        _client(lambda r: httpx2.Response(503, text="down")).query('{a="b"}')


def test_bad_logql_is_a_query_error():
    with pytest.raises(LokiQueryError, match="parse error"):
        _client(lambda r: httpx2.Response(400, text="parse error at line 1")).query("{")


def test_html_or_empty_200_is_unavailable():
    with pytest.raises(LokiUnavailable):
        _client(lambda r: httpx2.Response(200, text="<html>")).query('{a="b"}', now=NOW)


def test_non_object_200_body_is_unavailable():
    with pytest.raises(LokiUnavailable):
        _client(lambda r: httpx2.Response(200, json=[])).query('{a="b"}', now=NOW)


def test_redirect_is_unavailable():
    with pytest.raises(LokiUnavailable):
        _client(lambda r: httpx2.Response(302, headers={"location": "/elsewhere"})).query('{a="b"}', now=NOW)


def test_three_element_values_keep_timestamp_and_line():
    body = _streams(["1762336780000000000", "a", {"k": "v"}])
    out = _client(lambda r: httpx2.Response(200, json=body)).query('{a="b"}', now=NOW)
    assert out["entries"][0]["ts"] == "2025-11-05T09:59:40.000+00:00"
    assert out["entries"][0]["line"] == "a"


def test_non_numeric_timestamp_is_unavailable():
    body = _streams(["abc", "a"])
    with pytest.raises(LokiUnavailable):
        _client(lambda r: httpx2.Response(200, json=body)).query('{a="b"}', now=NOW)


def test_matrix_result_is_rejected_as_not_a_log_query():
    body = {
        "status": "success",
        "data": {"resultType": "matrix", "result": [{"metric": {}, "values": [[1762336790, "3"]]}]},
    }
    with pytest.raises(LokiQueryError, match="only log queries"):
        _client(lambda r: httpx2.Response(200, json=body)).query('count_over_time({a="b"}[5m])', now=NOW)


def test_vector_result_is_rejected_as_not_a_log_query():
    body = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1762336790, "3"]}]},
    }
    with pytest.raises(LokiQueryError, match="only log queries"):
        _client(lambda r: httpx2.Response(200, json=body)).query('rate({a="b"}[5m])', now=NOW)


def test_non_integer_hours_is_a_query_error():
    with pytest.raises(LokiQueryError, match="hours and limit must be integers"):
        _client(lambda r: httpx2.Response(200, json=_streams())).query('{a="b"}', hours="x", now=NOW)


def test_fractional_hours_send_integer_start():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx2.Response(200, json=_streams())

    _client(handler).query('{a="b"}', hours=1.5, now=NOW)
    assert seen["start"].isdigit()
    assert int(seen["end"]) - int(seen["start"]) == 3600 * 10**9


def test_429_is_unavailable():
    with pytest.raises(LokiUnavailable):
        _client(lambda r: httpx2.Response(429, text="slow down")).query('{a="b"}', now=NOW)
