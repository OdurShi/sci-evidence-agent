"""Shared pytest fixtures: a temporary store and a scripted fake upstream.

No test in the default run touches the network. HTTP is served by
``httpx.MockTransport`` from the JSON files in ``tests/fixtures`` (recorded
from the live APIs, trimmed) and from a few hand-written failure responses.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from helpers import Sleeper, Upstream
from sci_evidence_agent import http as http_layer
from sci_evidence_agent import storage


@pytest.fixture(autouse=True)
def store(tmp_path: Path) -> Iterator[storage.Store]:
    """A fresh provenance store in a temporary directory, for every test.

    Autouse, so that no test can create ``./data`` in the repository.
    """
    yield storage.configure(data_dir=tmp_path / "data")
    storage.set_store(None)


@pytest.fixture
def sleeper() -> Sleeper:
    return Sleeper()


@pytest.fixture
def upstream(store: storage.Store, sleeper: Sleeper) -> Iterator[Upstream]:
    """Install a fake upstream as the package's HTTP client (no waiting, no network)."""
    fake = Upstream()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler), follow_redirects=True)
    limiter = http_layer.RateLimiter(intervals={}, default_interval=0.0, sleep=sleeper)
    http_layer.set_client(http_layer.HttpClient(client=client, limiter=limiter, sleep=sleeper))
    yield fake
    client.close()


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"test tried to reach the network: {request.url}")


@pytest.fixture(autouse=True)
def _isolation(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[None]:
    """No ambient credentials, and no real network unless a test is marked ``live``."""
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "LLM_API_KEY",
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "NCBI_API_KEY",
        "NCBI_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    if request.node.get_closest_marker("live") is None:
        blocked = httpx.Client(transport=httpx.MockTransport(_refuse))
        http_layer.set_client(http_layer.HttpClient(client=blocked, sleep=lambda _s: None))
    yield
    http_layer.set_client(None)
