"""Record and replay HTTP exchanges, so an evaluation can be repeated offline.

``RecordingTransport`` wraps the real transport and stores every exchange;
``ReplayTransport`` answers from such a recording without touching the
network. Replaying the cassette of a live evaluation reproduces every metric
except latency, which makes the evaluation deterministic and independent of
upstream changes.

What is stored: method, URL (secret parameters removed), status, a small set
of response headers and the body of JSON responses. Bodies of non-JSON
responses (HTML error pages, which can echo the client address) are dropped.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx

from sci_evidence_agent.http import redact_url

KEPT_HEADERS = (
    "content-type",
    "retry-after",
    "x-total-results",
    "x-uniprot-release",
    "x-uniprot-release-date",
    "x-throttling-control",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
)


def _key(request: httpx.Request) -> str:
    return f"{request.method} {redact_url(str(request.url))}"


class Cassette:
    """An ordered recording of HTTP exchanges, grouped by request."""

    def __init__(self, entries: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.entries: dict[str, list[dict[str, Any]]] = entries or {}
        self._cursor: dict[str, int] = {}

    def __len__(self) -> int:
        return sum(len(v) for v in self.entries.values())

    def add(self, key: str, entry: dict[str, Any]) -> None:
        self.entries.setdefault(key, []).append(entry)

    def next(self, key: str) -> dict[str, Any] | None:
        """Next recorded response for a request; the last one repeats when exhausted."""
        recorded = self.entries.get(key)
        if not recorded:
            return None
        index = min(self._cursor.get(key, 0), len(recorded) - 1)
        self._cursor[key] = index + 1
        return recorded[index]

    def save(self, path: str | Path) -> None:
        payload = json.dumps({"version": 1, "entries": self.entries}, ensure_ascii=False)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(payload)

    @classmethod
    def load(cls, path: str | Path) -> Cassette:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return cls(json.load(handle)["entries"])


class RecordingTransport(httpx.BaseTransport):
    """Forward requests to ``inner`` and record the exchanges."""

    def __init__(self, cassette: Cassette, inner: httpx.BaseTransport | None = None) -> None:
        self.cassette = cassette
        self.inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        upstream = self.inner.handle_request(request)
        upstream.read()  # httpx undoes any content encoding here
        text = upstream.text
        upstream.close()
        # Only the kept headers are passed on; in particular no Content-Encoding,
        # because the body handed to the caller is already decoded.
        headers = {k: v for k, v in upstream.headers.items() if k.lower() in KEPT_HEADERS}
        is_json = "json" in headers.get("content-type", "").lower()
        self.cassette.add(
            _key(request),
            {"status": upstream.status_code, "headers": headers, "body": text if is_json else None},
        )
        return httpx.Response(
            upstream.status_code, headers=headers, content=text.encode("utf-8"), request=request
        )

    def close(self) -> None:
        self.inner.close()


class ReplayTransport(httpx.BaseTransport):
    """Answer requests from a cassette; unknown requests fail like a dead network."""

    def __init__(self, cassette: Cassette) -> None:
        self.cassette = cassette

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        entry = self.cassette.next(_key(request))
        if entry is None:
            raise httpx.ConnectError(f"not in cassette: {_key(request)}", request=request)
        body = entry["body"]
        content = body.encode("utf-8") if body is not None else b"<non-JSON body was not recorded>"
        return httpx.Response(
            entry["status"], headers=entry["headers"], content=content, request=request
        )
