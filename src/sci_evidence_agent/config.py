"""Runtime configuration, read from environment variables.

Nothing here reads a secret: API keys are looked up by the provider and
connector that need them, at the moment they need them.

Environment variables
---------------------
``SCI_EVIDENCE_DATA_DIR``
    Directory for the SQLite cache/log and the raw-response archive.
    Default: ``./data`` in the current working directory.
``SCI_EVIDENCE_CACHE_TTL_HOURS``
    Lifetime of cached results in hours. Default: 24.
``SCI_EVIDENCE_USER_AGENT``
    User-Agent header sent to the databases. Wikimedia and NCBI ask for a
    descriptive value; add a contact address here if you run large jobs.
``SCI_EVIDENCE_WDQS_INTERVAL``
    Minimum seconds between two Wikidata Query Service requests. Default: 61.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from sci_evidence_agent import __version__

DEFAULT_DATA_DIR = "data"
DEFAULT_CACHE_TTL_HOURS = 24.0
DEFAULT_USER_AGENT = f"sci-evidence-agent/{__version__} (research software)"


def data_dir() -> Path:
    """Directory holding ``app.db`` and ``raw/``."""
    return Path(os.environ.get("SCI_EVIDENCE_DATA_DIR") or DEFAULT_DATA_DIR)


def cache_ttl() -> timedelta:
    """How long a cached result stays valid."""
    raw = os.environ.get("SCI_EVIDENCE_CACHE_TTL_HOURS")
    try:
        hours = float(raw) if raw else DEFAULT_CACHE_TTL_HOURS
    except ValueError:
        hours = DEFAULT_CACHE_TTL_HOURS
    return timedelta(hours=hours)


def user_agent() -> str:
    """User-Agent header for outgoing requests."""
    return os.environ.get("SCI_EVIDENCE_USER_AGENT") or DEFAULT_USER_AGENT


def load_env(path: str | os.PathLike[str] | None = None) -> bool:
    """Load ``.env`` from the current working directory (or ``path``) if it exists.

    Existing environment variables are not overridden, and parent directories
    are not searched. Only entry points (CLI, UI) call this; importing the
    library never touches ``.env``.
    """
    target = Path(path) if path is not None else Path.cwd() / ".env"
    if not target.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is a declared dependency, but stay tolerant
        return False
    return bool(load_dotenv(target, override=False))
