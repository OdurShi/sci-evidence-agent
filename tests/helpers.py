"""Test helpers: fixture loading and a scripted fake upstream."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

import httpx

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load ``tests/fixtures/<group>/<name>.json`` given as ``"group/name"``."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def response_from(fixture: dict[str, Any] | str) -> httpx.Response:
    """Build an HTTP response from a fixture (or its ``"group/name"``)."""
    doc = load_fixture(fixture) if isinstance(fixture, str) else fixture
    body = doc["body"]
    if isinstance(body, str):
        return httpx.Response(doc["status"], headers=doc.get("headers"), text=body)
    return httpx.Response(doc["status"], headers=doc.get("headers"), json=body)


Reply = httpx.Response | Exception | str | dict


class Upstream:
    """A scripted upstream. Routes are tried in the order they were added."""

    def __init__(self) -> None:
        self._routes: list[tuple[Callable[[str], bool], list[Reply]]] = []
        self.requests: list[httpx.Request] = []

    def add(self, match: str | Callable[[str], bool], *replies: Reply) -> Upstream:
        """Answer requests whose decoded URL contains ``match``.

        Several replies are served in order and the last one repeats. A reply
        is a fixture name, a fixture dict, a ready response or an exception
        to raise (e.g. ``httpx.ReadTimeout``).
        """
        matcher = (lambda url, m=match: m in url) if isinstance(match, str) else match
        self._routes.append((matcher, list(replies)))
        return self

    @property
    def urls(self) -> list[str]:
        return [unquote_plus(str(r.url)) for r in self.requests]

    def count(self, fragment: str) -> int:
        return sum(fragment in url for url in self.urls)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = unquote_plus(str(request.url))
        for matcher, replies in self._routes:
            if matcher(url):
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
                if isinstance(reply, Exception):
                    raise reply
                if isinstance(reply, httpx.Response):
                    # A response object can only be consumed once; rebuild it.
                    return httpx.Response(
                        reply.status_code, headers=reply.headers, content=reply.content
                    )
                return response_from(reply)
        raise AssertionError(f"unexpected request in test: {url}")


class Sleeper:
    """Stands in for time.sleep and remembers the requested delays."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
