"""LogQL range queries against Loki (`auth_enabled: false`, 7-day retention on vps192)."""

import time
from datetime import UTC, datetime

import httpx2


class LokiUnavailable(Exception):
    """Loki could not be reached or returned a server error."""


class LokiQueryError(ValueError):
    """Loki rejected the query (HTTP 4xx), usually bad LogQL."""


def _iso(ns: str) -> str:
    return datetime.fromtimestamp(int(ns) / 1e9, UTC).isoformat(timespec="milliseconds")


class LokiClient:
    def __init__(
        self,
        base_url: str,
        http: httpx2.Client,
        default_hours: int = 1,
        max_hours: int = 168,
        max_lines: int = 200,
        max_bytes: int = 32768,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._default_hours = default_hours
        self._max_hours = max_hours
        self._max_lines = max_lines
        self._max_bytes = max_bytes

    def query(self, query: str, hours: int | None = None, limit: int | None = None, now: float | None = None) -> dict:
        try:
            hours = None if hours is None else int(hours)
            limit = None if limit is None else int(limit)
        except (ValueError, TypeError, OverflowError) as exc:
            raise LokiQueryError("hours and limit must be integers") from exc
        requested = hours or self._default_hours
        window = max(1, min(requested, self._max_hours))
        lines_limit = max(1, min(limit or self._max_lines, self._max_lines))
        end = int((now if now is not None else time.time()) * 1e9)
        start = end - window * 3600 * 10**9
        try:
            response = self._http.get(
                f"{self._base_url}/loki/api/v1/query_range",
                params={
                    "query": query,
                    "start": str(start),
                    "end": str(end),
                    "limit": str(lines_limit),
                    "direction": "backward",
                },
            )
        except httpx2.TransportError as exc:
            raise LokiUnavailable(f"Loki unreachable: {type(exc).__name__}") from exc
        code = response.status_code
        if 200 <= code < 300:
            pass
        elif code == 429 or code >= 500 or code < 400:
            raise LokiUnavailable(f"Loki returned HTTP {code}")
        else:
            raise LokiQueryError(f"Loki rejected the query: {response.text[:300]}")
        try:
            data = response.json()["data"]
            result_type = data["resultType"]
        except (ValueError, TypeError, AttributeError, KeyError, IndexError) as exc:
            raise LokiUnavailable("Loki returned an unparseable response") from exc
        if result_type != "streams":
            raise LokiQueryError(f"only log queries are supported (resultType={result_type})")
        try:
            entries = []
            for stream in data.get("result", []):
                labels = stream.get("stream", {})
                for value in stream.get("values", []):
                    entries.append((value[0], labels, value[1]))
            entries.sort(key=lambda e: int(e[0]))
            entries = entries[-lines_limit:]
            out = [{"ts": _iso(ts), "labels": labels, "line": line} for ts, labels, line in entries]
        except (ValueError, TypeError, AttributeError, KeyError, IndexError) as exc:
            raise LokiUnavailable("Loki returned an unparseable response") from exc
        truncated = False
        while out and len(str(out).encode()) > self._max_bytes:
            out = out[1:]
            truncated = True
        return {
            "query": query,
            "window_hours": window,
            "clamped": window != requested,
            "lines": len(out),
            "truncated": truncated,
            "entries": out,
        }
