"""SQLite cache, call log and raw-response archive (the provenance store).

Three things are stored separately:

``cache``      results keyed by a hash of (source, tool, arguments), with the
               original retrieval time; entries expire after the cache TTL.
``call_log``   arguments, duration and outcome of every tool call.
``raw/``       the raw upstream response of every successful query, as JSON.
               This is the last line of evidence for provenance.

Results with status ``ok``, ``no_match`` or ``ambiguous`` are cached.
``error`` results are never cached: the next call must retry.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sci_evidence_agent import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key          TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS call_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT,
    tool_name    TEXT NOT NULL,
    arguments    TEXT NOT NULL,
    status       TEXT NOT NULL,
    duration_ms  INTEGER NOT NULL,
    from_cache   INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    error        TEXT,
    called_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS query_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    question   TEXT NOT NULL,
    answer     TEXT,
    asked_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_call_log_session ON call_log(session_id);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def cache_key(source: str, func: str, args: dict[str, Any]) -> str:
    """Hash of a query. Keys are sorted so equal queries share one key."""
    blob = json.dumps({"s": source, "f": func, "a": args}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class Store:
    """Cache, logs and raw archive rooted at one data directory."""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        cache_ttl: timedelta | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else config.data_dir()
        self.db_path = self.data_dir / "app.db"
        self.raw_dir = self.data_dir / "raw"
        self.cache_ttl = cache_ttl if cache_ttl is not None else config.cache_ttl()
        self._now = now
        self._lock = threading.Lock()
        self._initialised = False
        #: When False every lookup is a miss while results are still written.
        #: The evaluation uses this to time each question against a cold cache.
        self.read_cache = True

    # -- connection handling -------------------------------------------------

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, commit on success and always close it."""
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                if not self._initialised:
                    conn.executescript(_SCHEMA)
                    self._initialised = True
                yield conn
                conn.commit()
            finally:
                conn.close()

    def init_db(self) -> None:
        """Create the tables. Safe to call repeatedly; also done lazily on first use."""
        with self._db():
            pass

    # -- cache ---------------------------------------------------------------

    def cache_get(self, key: str) -> tuple[dict[str, Any], str] | None:
        """Return ``(payload, original retrieval time)`` or None if absent or expired."""
        if not self.read_cache:
            return None
        with self._db() as conn:
            row = conn.execute(
                "SELECT payload, retrieved_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            fetched = datetime.fromisoformat(row["retrieved_at"])
        except ValueError:
            return None
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if self._now() - fetched > self.cache_ttl:
            return None
        return json.loads(row["payload"]), row["retrieved_at"]

    def cache_put(self, key: str, source: str, payload: dict[str, Any], retrieved_at: str) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, source, payload, retrieved_at) "
                "VALUES (?, ?, ?, ?)",
                (key, source, json.dumps(payload, ensure_ascii=False), retrieved_at),
            )

    def clear_cache(self) -> int:
        """Delete every cache entry and return how many there were."""
        with self._db() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM cache").fetchone()["n"]
            conn.execute("DELETE FROM cache")
        return int(n)

    # -- raw archive ---------------------------------------------------------

    def save_raw(
        self,
        source: str,
        key: str,
        data: Any,
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Archive a raw upstream response and return its path relative to the data directory.

        The file is an envelope: provenance metadata (source, tool, arguments,
        request URLs, retrieval time) plus the response under ``"response"``.
        """
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        path = self.raw_dir / f"{source.lower()}_{key}.json"
        envelope = {"source": source, **(meta or {}), "response": data}
        path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
        return path.relative_to(self.data_dir).as_posix()

    def resolve_raw(self, raw_path: str) -> Path:
        """Absolute location of an archived response."""
        return self.data_dir / raw_path

    def read_raw(self, raw_path: str) -> dict[str, Any] | None:
        """Load an archived envelope, or None if the file is missing or unreadable."""
        path = self.resolve_raw(raw_path)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    # -- logs ----------------------------------------------------------------

    def log_call(
        self,
        *,
        session_id: str | None,
        tool_name: str,
        arguments: dict[str, Any],
        status: str,
        duration_ms: int,
        from_cache: bool,
        record_count: int,
        error: str | None = None,
    ) -> None:
        with self._db() as conn:
            conn.execute(
                """INSERT INTO call_log
                   (session_id, tool_name, arguments, status, duration_ms, from_cache,
                    record_count, error, called_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False),
                    status,
                    duration_ms,
                    int(from_cache),
                    record_count,
                    error,
                    self._now().isoformat(timespec="seconds"),
                ),
            )

    def log_question(self, session_id: str | None, question: str, answer: str | None) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT INTO query_log (session_id, question, answer, asked_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, question, answer, self._now().isoformat(timespec="seconds")),
            )

    def recent_calls(self, session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM call_log"
        params: tuple[Any, ...] = ()
        if session_id:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        sql += " ORDER BY id DESC LIMIT ?"
        params += (limit,)
        with self._db() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def cache_stats(self) -> dict[str, int]:
        with self._db() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM cache").fetchone()["n"]
            calls = conn.execute("SELECT COUNT(*) AS n FROM call_log").fetchone()["n"]
        return {"cached_queries": int(total), "logged_calls": int(calls)}


# -- process-wide default store ------------------------------------------------

_default: Store | None = None
_default_lock = threading.Lock()


def get_store() -> Store:
    """The store used by the connectors. Created on first use from the environment."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Store()
        return _default


def configure(
    data_dir: str | Path | None = None,
    cache_ttl: timedelta | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> Store:
    """Replace the default store, e.g. to point at another data directory."""
    global _default
    with _default_lock:
        _default = Store(data_dir, cache_ttl, now)
        return _default


def set_store(store: Store | None) -> None:
    """Install a specific store (or reset to lazy creation with None)."""
    global _default
    with _default_lock:
        _default = store


# Module-level helpers that mirror the prototype's storage API.


def init_db() -> None:
    get_store().init_db()


def cache_get(key: str) -> tuple[dict[str, Any], str] | None:
    return get_store().cache_get(key)


def cache_put(key: str, source: str, payload: dict[str, Any], retrieved_at: str) -> None:
    get_store().cache_put(key, source, payload, retrieved_at)


def save_raw(source: str, key: str, data: Any, meta: dict[str, Any] | None = None) -> str:
    return get_store().save_raw(source, key, data, meta)


def log_call(**kwargs: Any) -> None:
    get_store().log_call(**kwargs)


def log_question(session_id: str | None, question: str, answer: str | None) -> None:
    get_store().log_question(session_id, question, answer)


def recent_calls(session_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    return get_store().recent_calls(session_id, limit)


def cache_stats() -> dict[str, int]:
    return get_store().cache_stats()


def clear_cache() -> int:
    return get_store().clear_cache()
