"""Plumbing shared by the connectors: cache lookup, archiving, call logging."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from sci_evidence_agent import storage
from sci_evidence_agent.http import UpstreamError
from sci_evidence_agent.schemas import Status, ToolResult, error_result, utc_now

# Hard upper bounds for one request. Exceeding them is silently truncated
# (ChEMBL) or rejected (UniProt) upstream, so the value is clamped here.
LIMIT_CAP = {
    "ChEMBL": 1000,  # limit=2000 is silently reduced to 1000
    "UniProt": 500,  # size=600 is answered with HTTP 400
    "PubChem": 1000,
    "PubMed": 100,  # esummary gets slow with many PMIDs
    "LOTUS": 200,  # large SPARQL queries tend to time out (HTTP 504)
}


def clamp_limit(source: str, limit: int) -> tuple[int, list[str]]:
    """Clamp ``limit`` to what the upstream service accepts and say what changed."""
    cap = LIMIT_CAP.get(source, 1000)
    if limit > cap:
        return cap, [
            f"{source} returns at most {cap} records per request; limit lowered from {limit}"
        ]
    if limit < 1:
        return 1, [f"limit must be at least 1; raised from {limit} to 1"]
    return limit, []


def run_connector(
    *,
    source: str,
    func_name: str,
    args: dict[str, Any],
    fetch: Callable[[], ToolResult],
    session_id: str | None = None,
    use_cache: bool = True,
) -> ToolResult:
    """Run one connector call: cache lookup, live request, cache write, call log.

    ``fetch`` only sends the request(s) and returns a ToolResult. Any exception
    it raises becomes an ``error`` result, so callers never see a traceback.
    Results with status ok / no_match / ambiguous are cached; errors are not.
    """
    store = storage.get_store()
    key = storage.cache_key(source, func_name, args)
    started = time.monotonic()

    if use_cache:
        hit = store.cache_get(key)
        if hit is not None:
            payload, fetched_at = hit
            result = ToolResult(**payload)
            result.from_cache = True
            result.retrieved_at = fetched_at  # the original time, not "now"
            store.log_call(
                session_id=session_id,
                tool_name=func_name,
                arguments=args,
                status=result.status.value,
                duration_ms=int((time.monotonic() - started) * 1000),
                from_cache=True,
                record_count=len(result.records),
            )
            return result

    try:
        result = fetch()
    except UpstreamError as exc:
        result = error_result(source, args, str(exc))
    except Exception as exc:
        result = error_result(source, args, f"{type(exc).__name__}: {exc}")

    duration_ms = int((time.monotonic() - started) * 1000)

    if result.status in (Status.OK, Status.NO_MATCH, Status.AMBIGUOUS):
        store.cache_put(key, source, result.model_dump(mode="json"), result.retrieved_at)

    store.log_call(
        session_id=session_id,
        tool_name=func_name,
        arguments=args,
        status=result.status.value,
        duration_ms=duration_ms,
        from_cache=False,
        record_count=len(result.records),
        error=result.error,
    )
    return result


def archive(
    source: str,
    func_name: str,
    args: dict[str, Any],
    raw: Any,
    urls: list[str] | None = None,
) -> str:
    """Write a raw response to the archive and return its relative path.

    The file name uses the same key as the cache entry, so a cached result and
    its raw evidence can always be matched.
    """
    key = storage.cache_key(source, func_name, args)
    meta = {
        "tool": func_name,
        "arguments": args,
        "request_urls": list(urls or []),
        "archived_at": utc_now(),
    }
    return storage.save_raw(source, key, raw, meta)
