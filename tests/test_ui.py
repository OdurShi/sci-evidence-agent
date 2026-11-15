"""Streamlit interface: the script must start and render without a browser.

Only start-up and one offline question are covered, using Streamlit's own
test harness. Skipped when the optional ``ui`` extra is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("pandas")

from streamlit.testing.v1 import AppTest

APP = Path(__file__).parent.parent / "src" / "sci_evidence_agent" / "ui" / "app.py"


def test_app_starts_with_the_offline_planner_selected(upstream, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value.endswith("Scientific database retrieval")
    assert app.selectbox[0].value == "offline"  # no key in the test environment
    assert any("not a model" in w.value for w in app.warning)
    assert any("Type a question" in i.value for i in app.info)


def test_app_answers_a_question_offline(upstream, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.text_area[0].set_value("What is the molecular formula of aspirin?").run()
    next(b for b in app.button if b.label == "Ask").click().run()

    assert not app.exception
    assert any("`CID 2244`" in m.value for m in app.markdown)
    assert any("not model output" in s.value for s in app.subheader)
    assert any("Identifier check: 2 identifiers" in c.value for c in app.caption)
    assert len(app.dataframe) == 2  # the records and the call log
