"""HTTP layer shared by all connectors: timeout, rate limiting, bounded retries.

A connector only decides how to build a request and how to map the response
to a :class:`~sci_evidence_agent.schemas.ToolResult`. Everything about being a
polite and robust client lives here:

* a minimum interval between two requests to the same service
  (:class:`RateLimiter`), with one independent bucket per service;
* a bounded number of attempts with exponential backoff
  (:class:`RetryPolicy`), honouring ``Retry-After`` when it is short and
  failing fast when the server asks for a long pause;
* secrets (``api_key``, ``email``) are removed from every URL that leaves
  this module, so they cannot reach results, logs or the raw archive.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from sci_evidence_agent import config

TIMEOUT = 20.0  # seconds per request
MAX_RETRIES = 3  # total attempts per request, including the first
BACKOFF_BASE = 0.8  # first backoff delay in seconds; doubles on every attempt

#: Query parameters that must never appear in stored or displayed URLs.
SECRET_PARAMS = frozenset({"api_key", "email"})

Interval = float | Callable[[], float]


def default_intervals() -> dict[str, Interval]:
    """Minimum seconds between two requests, per rate-limit bucket.

    * PubChem allows 5 requests/second; 0.22 s leaves a margin.
    * NCBI E-utilities allow 3 requests/second without a key and 10 with
      ``NCBI_API_KEY``; one literature search issues two requests. At the
      prototype's 0.40 s the ``X-RateLimit-Remaining`` header read 0 on most
      requests of an evaluation run, so the keyless interval is 0.60 s.
    * The Wikidata Query Service answered a second request sent 8 s after the
      first with HTTP 429 and ``Retry-After: 1000`` on 2026-11-14 (the edge
      reported a limit of one request per minute), hence 61 s by default.
    """
    return {
        "PubChem": 0.22,
        "ChEMBL": 0.10,
        "UniProt": 0.10,
        "PubMed": lambda: 0.11 if os.environ.get("NCBI_API_KEY") else 0.60,
        "Wikidata-API": 1.0,
        "Wikidata-SPARQL": lambda: _env_float("SCI_EVIDENCE_WDQS_INTERVAL", 61.0),
    }


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


class UpstreamError(Exception):
    """An upstream request failed for good (attempts exhausted or not retryable)."""


@dataclass(frozen=True)
class RetryPolicy:
    """When and how often a request is retried."""

    max_attempts: int = MAX_RETRIES
    backoff_base: float = BACKOFF_BASE
    backoff_cap: float = 10.0  # upper bound for one computed backoff delay
    retry_after_limit: float = 60.0  # a longer Retry-After means "stop", not "wait"
    retry_statuses: frozenset[int] | None = None  # None: HTTP 429 and every 5xx
    retry_timeouts: bool = True
    retry_transport_errors: bool = True

    def should_retry_status(self, status: int) -> bool:
        if self.retry_statuses is None:
            return status == 429 or status >= 500
        return status in self.retry_statuses

    def backoff(self, attempt: int) -> float:
        """Delay before the retry that follows attempt number ``attempt`` (0-based)."""
        return min(self.backoff_base * (2**attempt), self.backoff_cap)


class RateLimiter:
    """Guarantees a minimum interval between two requests in the same bucket.

    Buckets are independent: waiting for one service never delays another.
    ``clock`` and ``sleep`` are injectable so the behaviour can be tested
    without real waiting.
    """

    def __init__(
        self,
        intervals: Mapping[str, Interval] | None = None,
        default_interval: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._intervals = dict(default_intervals() if intervals is None else intervals)
        self._default = default_interval
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def interval(self, bucket: str) -> float:
        value = self._intervals.get(bucket, self._default)
        return float(value() if callable(value) else value)

    def acquire(self, bucket: str) -> float:
        """Block until a request in ``bucket`` is allowed. Returns the time waited."""
        with self._guard:
            lock = self._locks.setdefault(bucket, threading.Lock())
        with lock:
            waited = 0.0
            last = self._last.get(bucket)
            if last is not None:
                wait = self.interval(bucket) - (self._clock() - last)
                if wait > 0:
                    self._sleep(wait)
                    waited = wait
            self._last[bucket] = self._clock()
            return waited


@dataclass(frozen=True)
class HttpResponse:
    """Outcome of :meth:`HttpClient.get_json`."""

    status: int
    data: Any  # parsed JSON, or None for an accepted 404
    url: str  # final URL with secrets removed
    headers: dict[str, str]  # lower-cased header names

    def as_tuple(self) -> tuple[int, Any, str, dict[str, str]]:
        return self.status, self.data, self.url, self.headers


def redact_url(url: str, secrets: frozenset[str] = SECRET_PARAMS) -> str:
    """Remove secret query parameters from a URL."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in secrets]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds requested by a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(float(text), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max((when - current).total_seconds(), 0.0)


_TAG_BLOCK_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}\b")


def body_excerpt(response: httpx.Response, limit: int = 160) -> str:
    """Short, single-line excerpt of a response body for error messages.

    HTML error pages are reduced to their text, and anything that looks like
    an IP address is masked (some error pages echo the client address).
    """
    text = response.text
    if "json" not in response.headers.get("content-type", "").lower():
        text = _TAG_RE.sub(" ", _TAG_BLOCK_RE.sub(" ", text))
    text = _IP_RE.sub("[address removed]", " ".join(text.split()))
    return text[:limit]


class HttpClient:
    """GET-and-parse-JSON client with rate limiting and bounded retries."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        policy: RetryPolicy | None = None,
        timeout: float = TIMEOUT,
        user_agent: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(follow_redirects=True)
        self.limiter = limiter or RateLimiter(sleep=sleep)
        self.policy = policy or RetryPolicy()
        self.timeout = timeout
        self._user_agent = user_agent
        self._sleep = sleep

    @property
    def user_agent(self) -> str:
        return self._user_agent or config.user_agent()

    def close(self) -> None:
        self._client.close()

    def get_json(
        self,
        source: str,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        ok_404: bool = False,
        accept: str = "application/json",
        timeout: float | None = None,
        policy: RetryPolicy | None = None,
        bucket: str | None = None,
    ) -> HttpResponse:
        """Send one GET request and parse the JSON body.

        Args:
            source: Display name of the database, used in error messages.
            url: Request URL.
            params: Query parameters.
            ok_404: Treat HTTP 404 as a valid answer (``data`` is None).
            accept: ``Accept`` header.
            timeout: Per-request timeout; defaults to the client timeout.
            policy: Retry policy; defaults to the client policy.
            bucket: Rate-limit bucket; defaults to ``source``.

        Raises:
            UpstreamError: the request failed and will not be retried further.
        """
        policy = policy or self.policy
        timeout = self.timeout if timeout is None else timeout
        bucket = bucket or source
        attempts = max(policy.max_attempts, 1)

        for attempt in range(attempts):
            last_attempt = attempt == attempts - 1
            self.limiter.acquire(bucket)
            try:
                resp = self._client.get(
                    url,
                    params=params,
                    timeout=timeout,
                    headers={"User-Agent": self.user_agent, "Accept": accept},
                    follow_redirects=True,
                )
            except httpx.TimeoutException as exc:
                if policy.retry_timeouts and not last_attempt:
                    self._sleep(policy.backoff(attempt))
                    continue
                raise UpstreamError(
                    f"{source}: request timed out after {timeout:g}s "
                    f"({attempt + 1} attempt{'s' if attempt else ''})"
                ) from exc
            except httpx.TransportError as exc:
                if policy.retry_transport_errors and not last_attempt:
                    self._sleep(policy.backoff(attempt))
                    continue
                raise UpstreamError(
                    f"{source}: network error after {attempt + 1} "
                    f"attempt{'s' if attempt else ''}: {type(exc).__name__}"
                ) from exc
            except httpx.HTTPError as exc:
                raise UpstreamError(f"{source}: HTTP client error: {type(exc).__name__}") from exc

            final_url = redact_url(str(resp.url))
            headers = {k.lower(): v for k, v in resp.headers.items()}
            status = resp.status_code

            if status == 404 and ok_404:
                return HttpResponse(404, None, final_url, headers)

            if policy.should_retry_status(status):
                requested = parse_retry_after(headers.get("retry-after"))
                if requested is not None and requested > policy.retry_after_limit:
                    # The service imposed a long block. Waiting is pointless in an
                    # interactive setting and retrying early can extend the block.
                    raise UpstreamError(
                        f"{source}: HTTP {status}, the service asked to wait "
                        f"{requested:.0f}s (about {requested / 60:.0f} min) before retrying. "
                        "This is a usage limit, not a malformed query."
                    )
                if not last_attempt:
                    # A short Retry-After is honoured as given; otherwise back off.
                    self._sleep(policy.backoff(attempt) if requested is None else requested)
                    continue
                raise UpstreamError(
                    f"{source}: HTTP {status} after {attempts} attempts: {body_excerpt(resp)}"
                )

            if status != 200:
                raise UpstreamError(f"{source}: HTTP {status}: {body_excerpt(resp)}")

            try:
                return HttpResponse(200, resp.json(), final_url, headers)
            except ValueError as exc:
                # PubChem, for example, can answer with an HTML page when overloaded.
                raise UpstreamError(
                    f"{source}: response is not valid JSON (HTTP {status}): {body_excerpt(resp)}"
                ) from exc

        raise UpstreamError(f"{source}: request failed")  # pragma: no cover - loop always returns


# -- process-wide default client -------------------------------------------------

_default: HttpClient | None = None
_default_lock = threading.Lock()


def get_client() -> HttpClient:
    """The HTTP client used by the connectors."""
    global _default
    with _default_lock:
        if _default is None:
            _default = HttpClient()
        return _default


def set_client(client: HttpClient | None) -> None:
    """Install a specific client (tests, replay) or reset to the default with None."""
    global _default
    with _default_lock:
        _default = client


def request_json(
    source: str,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    ok_404: bool = False,
) -> tuple[int, Any, str, dict[str, str]]:
    """Prototype-compatible helper: ``(status_code, data, final_url, headers)``."""
    return get_client().get_json(source, url, params, ok_404=ok_404).as_tuple()
