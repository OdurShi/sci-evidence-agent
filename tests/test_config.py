"""Configuration: environment handling and the shipped example file."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sci_evidence_agent import config
from sci_evidence_agent.providers.anthropic_provider import DEFAULT_MODEL, AnthropicProvider
from sci_evidence_agent.providers.openai_compat import OpenAICompatProvider

ROOT = Path(__file__).parent.parent


def test_env_example_contains_only_empty_placeholders():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    settings = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    assert len(settings) >= 13
    for line in settings:
        name, _, value = line.partition("=")
        assert name.isupper() and value == "", line  # a name and nothing after "="


def test_defaults(monkeypatch):
    for name in (
        "SCI_EVIDENCE_DATA_DIR",
        "SCI_EVIDENCE_CACHE_TTL_HOURS",
        "SCI_EVIDENCE_USER_AGENT",
    ):
        monkeypatch.delenv(name, raising=False)
    assert config.data_dir() == Path("data")
    assert config.cache_ttl() == timedelta(hours=24)
    assert config.user_agent() == "sci-evidence-agent/0.2.0 (research software)"


def test_empty_values_count_as_unset(monkeypatch):
    # Copying .env.example to .env sets every variable to an empty string.
    for name in (
        "SCI_EVIDENCE_DATA_DIR", "SCI_EVIDENCE_CACHE_TTL_HOURS", "SCI_EVIDENCE_USER_AGENT",
        "ANTHROPIC_MODEL", "LLM_MODEL", "LLM_BASE_URL", "ANTHROPIC_API_KEY", "LLM_API_KEY",
    ):  # fmt: skip
        monkeypatch.setenv(name, "")
    assert config.data_dir() == Path("data")
    assert config.cache_ttl() == timedelta(hours=24)
    assert config.user_agent().startswith("sci-evidence-agent/")
    assert AnthropicProvider().model == DEFAULT_MODEL
    assert AnthropicProvider().available() == (False, "ANTHROPIC_API_KEY is not set")
    provider = OpenAICompatProvider()
    assert provider.model == "gpt-4o-mini" and provider.base_url is None


def test_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("SCI_EVIDENCE_DATA_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("SCI_EVIDENCE_CACHE_TTL_HOURS", "0.5")
    monkeypatch.setenv("SCI_EVIDENCE_USER_AGENT", "my-lab-bot/1.0 (contact@example.org)")
    assert config.data_dir() == tmp_path / "store"
    assert config.cache_ttl() == timedelta(minutes=30)
    assert config.user_agent() == "my-lab-bot/1.0 (contact@example.org)"
    monkeypatch.setenv("SCI_EVIDENCE_CACHE_TTL_HOURS", "soon")
    assert config.cache_ttl() == timedelta(hours=24)  # an unusable value falls back


def test_env_file_is_read_only_from_the_working_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("SCI_EVIDENCE_TEST_FLAG", raising=False)
    parent = tmp_path
    child = tmp_path / "project"
    child.mkdir()
    (parent / ".env").write_text("SCI_EVIDENCE_TEST_FLAG=from-parent\n", encoding="utf-8")
    monkeypatch.chdir(child)
    assert config.load_env() is False  # parent directories are not searched
    (child / ".env").write_text("SCI_EVIDENCE_TEST_FLAG=from-cwd\n", encoding="utf-8")
    try:
        assert config.load_env() is True
        import os

        assert os.environ["SCI_EVIDENCE_TEST_FLAG"] == "from-cwd"
    finally:
        monkeypatch.delenv("SCI_EVIDENCE_TEST_FLAG", raising=False)
