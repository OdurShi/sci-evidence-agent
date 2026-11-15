"""HTTP layer: retries, backoff, Retry-After, rate limiting, secret redaction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from sci_evidence_agent import http as http_layer
from sci_evidence_agent.http import (
    HttpClient,
    RateLimiter,
    RetryPolicy,
    UpstreamError,
    body_excerpt,
    parse_retry_after,
    redact_url,
)

URL = "https://example.org/api"
OK = httpx.Response(200, json={"ok": True})


def client() -> HttpClient:
    return http_layer.get_client()


# -- retries and backoff ---------------------------------------------------------


def test_success_needs_one_request_and_no_sleep(upstream, sleeper):
    upstream.add("example.org", OK)
    resp = client().get_json("Test", URL, {"a": 1})
    assert resp.status == 200 and resp.data == {"ok": True}
    assert len(upstream.requests) == 1
    assert sleeper.delays == []


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_status_is_retried_with_exponential_backoff(upstream, sleeper, status):
    upstream.add("example.org", httpx.Response(status, text="busy"), OK)
    resp = client().get_json("Test", URL)
    assert resp.status == 200
    assert len(upstream.requests) == 2
    assert sleeper.delays == [0.8]


def test_attempts_are_bounded_and_backoff_doubles(upstream, sleeper):
    upstream.add("example.org", httpx.Response(500, text="boom"))
    with pytest.raises(UpstreamError, match="HTTP 500 after 3 attempts"):
        client().get_json("Test", URL)
    assert len(upstream.requests) == 3
    assert sleeper.delays == [0.8, 1.6]  # no sleep after the last attempt


def test_backoff_is_capped():
    policy = RetryPolicy(backoff_base=0.8, backoff_cap=10.0)
    assert [policy.backoff(i) for i in range(6)] == [0.8, 1.6, 3.2, 6.4, 10.0, 10.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried(upstream, sleeper, status):
    upstream.add("example.org", httpx.Response(status, json={"message": "bad"}))
    with pytest.raises(UpstreamError, match=f"HTTP {status}"):
        client().get_json("Test", URL)
    assert len(upstream.requests) == 1
    assert sleeper.delays == []


def test_404_can_be_a_valid_answer(upstream):
    upstream.add("example.org", httpx.Response(404, json={"Fault": {}}))
    resp = client().get_json("Test", URL, ok_404=True)
    assert resp.status == 404 and resp.data is None


def test_short_retry_after_is_honoured(upstream, sleeper):
    upstream.add("example.org", httpx.Response(429, headers={"Retry-After": "7"}), OK)
    assert client().get_json("Test", URL).status == 200
    assert sleeper.delays == [7.0]


def test_retry_after_as_http_date_is_honoured(upstream, sleeper):
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    upstream.add(
        "example.org",
        httpx.Response(503, headers={"Retry-After": format_datetime(when, usegmt=True)}),
        OK,
    )
    assert client().get_json("Test", URL).status == 200
    assert len(sleeper.delays) == 1 and 25 <= sleeper.delays[0] <= 30


def test_unparsable_retry_after_falls_back_to_backoff(upstream, sleeper):
    upstream.add("example.org", httpx.Response(503, headers={"Retry-After": "soon"}), OK)
    assert client().get_json("Test", URL).status == 200
    assert sleeper.delays == [0.8]


def test_long_retry_after_fails_fast_without_retrying(upstream, sleeper):
    upstream.add("example.org", "synthetic/wikidata_429_blocked")
    with pytest.raises(UpstreamError) as info:
        client().get_json("Test", URL)
    assert "1000s" in str(info.value) and "about 17 min" in str(info.value)
    assert len(upstream.requests) == 1  # retrying early could extend the block
    assert sleeper.delays == []


def test_timeout_is_retried(upstream, sleeper):
    upstream.add("example.org", httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), OK)
    assert client().get_json("Test", URL).status == 200
    assert len(upstream.requests) == 3
    assert sleeper.delays == [0.8, 1.6]


def test_timeout_gives_up_after_max_attempts(upstream, sleeper):
    upstream.add("example.org", httpx.ReadTimeout("slow"))
    with pytest.raises(UpstreamError, match="timed out after 20s"):
        client().get_json("Test", URL)
    assert len(upstream.requests) == 3


def test_transport_error_is_retried(upstream, sleeper):
    upstream.add("example.org", httpx.ConnectError("refused"), OK)
    assert client().get_json("Test", URL).status == 200
    assert sleeper.delays == [0.8]


def test_policy_can_disable_retries(upstream, sleeper):
    policy = RetryPolicy(retry_statuses=frozenset({429}), retry_timeouts=False)
    upstream.add("gateway", "synthetic/wikidata_504")
    upstream.add("timeout", httpx.ReadTimeout("slow"))
    with pytest.raises(UpstreamError, match="HTTP 504"):
        client().get_json("Test", "https://example.org/gateway", policy=policy)
    with pytest.raises(UpstreamError, match="timed out"):
        client().get_json("Test", "https://example.org/timeout", policy=policy)
    assert len(upstream.requests) == 2
    assert sleeper.delays == []


def test_non_json_body_is_an_error_not_a_crash(upstream):
    upstream.add("example.org", httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(UpstreamError, match="not valid JSON"):
        client().get_json("Test", URL)


def test_prototype_compatible_helper_returns_a_tuple(upstream):
    upstream.add("example.org", httpx.Response(200, json=[1], headers={"X-Total-Results": "9"}))
    status, data, url, headers = http_layer.request_json("Test", URL, {"q": "x"})
    assert (status, data) == (200, [1])
    assert url == "https://example.org/api?q=x"
    assert headers["x-total-results"] == "9"  # header names are lower-cased


def test_user_agent_is_generic_and_sent(upstream):
    upstream.add("example.org", OK)
    client().get_json("Test", URL)
    agent = upstream.requests[0].headers["user-agent"]
    assert agent.startswith("sci-evidence-agent/")
    assert "@" not in agent  # no contact address by default


# -- parsing helpers ---------------------------------------------------------------


def test_parse_retry_after_variants():
    now = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(" 1.5 ") == 1.5
    assert parse_retry_after("-3") == 0.0
    assert parse_retry_after("Sat, 14 Nov 2026 12:00:40 GMT", now) == 40.0
    assert parse_retry_after("Sat, 14 Nov 2026 11:00:00 GMT", now) == 0.0  # in the past
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("tomorrow") is None


def test_secrets_are_removed_from_urls(upstream):
    url = "https://x.org/e?tool=t&api_key=SECRET&db=pubmed&email=a%40b.org&term=a+b"
    assert redact_url(url) == "https://x.org/e?tool=t&db=pubmed&term=a+b"
    assert redact_url("https://x.org/e") == "https://x.org/e"

    upstream.add("example.org", OK)
    resp = client().get_json("Test", URL, {"api_key": "SECRET", "email": "a@b.org", "q": "z"})
    assert "SECRET" not in resp.url and "a%40b.org" not in resp.url
    assert resp.url == "https://example.org/api?q=z"
    # The secret was still sent to the service; it is only kept out of what is stored.
    assert "api_key=SECRET" in str(upstream.requests[0].url)


def test_error_excerpt_drops_markup_and_client_address():
    page = httpx.Response(
        429,
        headers={"content-type": "text/html"},
        text="<html><style>p{}</style><script>var a=1;</script><p>Too many requests</p>"
        "<p>IP address: 192.0.2.44 and 2001:db8:85a3:8d3:1319:8a2e:370:7348</p></html>",
    )
    excerpt = body_excerpt(page, limit=200)
    assert excerpt.startswith("Too many requests")
    assert "192.0.2.44" not in excerpt and "2001:db8" not in excerpt
    assert "<" not in excerpt and "var a" not in excerpt


# -- rate limiting -------------------------------------------------------------------


class FakeTime:
    """A clock that only moves when someone sleeps."""

    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 6))
        self.now += seconds


def limiter(intervals, fake: FakeTime) -> RateLimiter:
    return RateLimiter(intervals, default_interval=0.1, clock=fake.clock, sleep=fake.sleep)


def test_first_request_in_a_bucket_does_not_wait():
    fake = FakeTime()
    assert limiter({"A": 5.0}, fake).acquire("A") == 0.0
    assert fake.slept == []


def test_requests_in_one_bucket_keep_the_minimum_interval():
    fake = FakeTime()
    lim = limiter({"A": 5.0}, fake)
    lim.acquire("A")
    fake.now += 1.5  # 1.5 s of work between the two requests
    assert lim.acquire("A") == pytest.approx(3.5)
    assert fake.slept == [3.5]
    fake.now += 6.0  # longer than the interval: no wait
    assert lim.acquire("A") == 0.0
    assert fake.slept == [3.5]


def test_buckets_are_independent():
    fake = FakeTime()
    lim = limiter({"A": 5.0, "B": 2.0}, fake)
    lim.acquire("A")
    assert lim.acquire("B") == 0.0  # a different service is not delayed
    assert lim.acquire("B") == pytest.approx(2.0)
    assert lim.acquire("unknown") == 0.0
    assert lim.acquire("unknown") == pytest.approx(0.1)  # default interval


def test_interval_can_depend_on_the_environment(monkeypatch):
    lim = RateLimiter()  # production table
    assert lim.interval("PubMed") == 0.60
    monkeypatch.setenv("NCBI_API_KEY", "k")
    assert lim.interval("PubMed") == 0.11  # read at call time, not at import time
    assert lim.interval("PubChem") == 0.22
    assert lim.interval("Wikidata-SPARQL") == 61.0
    monkeypatch.setenv("SCI_EVIDENCE_WDQS_INTERVAL", "90")
    assert lim.interval("Wikidata-SPARQL") == 90.0


def test_client_spaces_requests_through_the_limiter():
    fake = FakeTime()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    http = HttpClient(
        client=httpx.Client(transport=transport),
        limiter=limiter({"Svc": 0.5}, fake),
        sleep=fake.sleep,
    )
    for _ in range(3):
        http.get_json("Svc", URL)
    assert fake.slept == [0.5, 0.5]
    # A separate bucket for the same source name is honoured as well.
    http.get_json("Svc", URL, bucket="Other")
    assert fake.slept == [0.5, 0.5]


def test_each_retry_passes_through_the_limiter():
    fake = FakeTime()
    replies = iter([httpx.Response(503), httpx.Response(200, json={})])
    transport = httpx.MockTransport(lambda request: next(replies))
    http = HttpClient(
        client=httpx.Client(transport=transport),
        limiter=limiter({"Svc": 5.0}, fake),
        sleep=fake.sleep,
    )
    http.get_json("Svc", URL)
    # 0.8 s backoff, then the limiter tops the gap up to the 5 s interval.
    assert fake.slept == [0.8, 4.2]
