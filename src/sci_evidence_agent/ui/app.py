"""Streamlit interface with four areas: question, answer, data results, call log.

Start it with ``sci-evidence ui`` or::

    streamlit run src/sci_evidence_agent/ui/app.py
"""

from __future__ import annotations

import uuid

import pandas as pd
import streamlit as st

from sci_evidence_agent import agent, config, storage
from sci_evidence_agent.citations import verify_run
from sci_evidence_agent.providers import list_providers
from sci_evidence_agent.schemas import Status, ToolResult

config.load_env()
storage.init_db()

st.set_page_config(page_title="Scientific database retrieval", page_icon="🔬", layout="wide")

EXAMPLES = [
    "What are the compound identifier, molecular formula and structure of aspirin?",
    "List up to 10 bioactivity assay records for aspirin from ChEMBL, with assay type, "
    "value and units.",
    "Find the UniProt entry for human EGFR: protein name, gene names, organism and source.",
    "Search the literature on aspirin and cyclooxygenase; list 5 papers with PMID and DOI.",
    "For aspirin and the human gene PTGS1, give the compound information from PubChem, the "
    "UniProt protein entry and related literature, grouped by database with sources.",
]

STATUS_STYLE = {
    Status.OK: ("✅", "ok"),
    Status.NO_MATCH: ("🔍", "no match"),
    Status.AMBIGUOUS: ("⚠️", "several candidates"),
    Status.ERROR: ("❌", "failed"),
}

if "session_id" not in st.session_state:
    st.session_state.session_id = uuid.uuid4().hex[:12]
if "run" not in st.session_state:
    st.session_state.run = None
if "question" not in st.session_state:
    st.session_state.question = ""


# -- sidebar: runtime status ---------------------------------------------------------
with st.sidebar:
    st.header("Status")

    providers = list_providers()
    usable = [p for p in providers if p[2]]

    # Default to the first usable model provider; the offline planner is always usable.
    options = [p[0] for p in usable]
    labels = {p[0]: p[1] for p in usable}
    default_index = next((i for i, key in enumerate(options) if key != "offline"), 0)

    chosen = st.selectbox("Provider", options, index=default_index, format_func=lambda k: labels[k])
    st.session_state.provider = chosen

    for key, _name, ok, detail in providers:
        if key == chosen:
            (st.success if ok else st.error)(detail, icon="✅" if ok else "❌")

    if chosen == "offline":
        st.warning(
            "The offline planner is not a model: tools are chosen by keyword rules and the "
            "summary is assembled from templates. The database queries are real; the "
            "understanding of the question is not.",
            icon="⚠️",
        )

    with st.expander("Other providers"):
        for key, name, ok, detail in providers:
            if key != chosen:
                st.caption(f"{'✅' if ok else '❌'} **{name}**: {detail}")

    st.divider()
    stats = storage.cache_stats()
    col_a, col_b = st.columns(2)
    col_a.metric("Cached queries", stats["cached_queries"])
    col_b.metric("Logged calls", stats["logged_calls"])

    if st.button("Clear cache", width="stretch"):
        st.toast(f"Removed {storage.clear_cache()} cache entries")

    st.divider()
    st.caption(
        "Sources: PubChem · ChEMBL · UniProt · PubMed · LOTUS\n\n"
        f"At most {agent.MAX_TOOL_ROUNDS} tool rounds per question\n\n"
        f"Session `{st.session_state.session_id}`"
    )
    st.caption(
        "ChEMBL data are CC BY-SA 3.0 (share-alike). This tool only queries and caches; "
        "it does not publish a dataset."
    )


# -- area 1: question ----------------------------------------------------------------
st.title("🔬 Scientific database retrieval")
st.caption(
    "question → tool calls → live database queries → structured results with sources → "
    "everything logged"
)

st.subheader("Question")
question = st.text_area(
    "Question",
    value=st.session_state.question,
    placeholder="e.g. What are the molecular formula and InChIKey of aspirin?",
    height=80,
    label_visibility="collapsed",
)

st.caption("Examples:")
for i, (col, example) in enumerate(zip(st.columns(len(EXAMPLES)), EXAMPLES, strict=True)):
    if col.button(f"Example {i + 1}", help=example, width="stretch"):
        st.session_state.question = example
        st.rerun()

if st.button("Ask", type="primary", disabled=not question.strip()):
    with st.spinner("Calling database tools ..."):
        st.session_state.run = agent.ask(
            question.strip(),
            st.session_state.session_id,
            provider=st.session_state.get("provider"),
        )

run = st.session_state.run


def render_result(result: ToolResult) -> None:
    """One expandable block per tool result."""
    icon, label = STATUS_STYLE[result.status]
    cache_tag = " · from cache" if result.from_cache else ""

    with st.expander(
        f"{icon} {result.source}: {label}{cache_tag}", expanded=result.status is not Status.ERROR
    ):
        meta = st.columns(4)
        meta[0].caption(f"**Status** {result.status.value}")
        meta[1].caption(f"**Records** {len(result.records)}")
        meta[2].caption(f"**Retrieved** {result.retrieved_at}")
        meta[3].caption(f"**Cache** {'yes' if result.from_cache else 'no'}")

        if result.truncated:
            total = "unknown" if result.total_available is None else result.total_available
            st.warning(f"Truncated: {len(result.records)} records shown, upstream total {total}")
        for warning in result.warnings:
            st.info(warning, icon="ℹ️")
        if result.error:
            st.error(f"Request failed: {result.error}", icon="❌")
        if result.status is Status.NO_MATCH:
            st.info("No matching record. This is different from a failed request.", icon="🔍")

        if result.records:
            frame = pd.DataFrame(result.records)
            column_config = {}
            if "source_url" in frame.columns:
                column_config["source_url"] = st.column_config.LinkColumn(
                    "source", display_text="open"
                )
            st.dataframe(frame, width="stretch", hide_index=True, column_config=column_config)

        detail = st.columns(2)
        if result.source_url:
            detail[0].caption(f"[Check in the database]({result.source_url})")
        if result.raw_path:
            detail[1].caption(f"Raw response: `{result.raw_path}`")

        st.caption(f"Query arguments: `{result.query}`")


if run is None:
    st.info("Type a question or pick an example above.", icon="👆")
    st.stop()


# -- area 2: answer ------------------------------------------------------------------
st.divider()
st.subheader("Answer" if run.is_real_model else "Summary (not model output)")

if run.error:
    st.error(run.error, icon="❌")
elif run.answer:
    if not run.is_real_model:
        st.warning(
            "This text was assembled from templates by the keyword planner. It is **not "
            "model output**. The records under 'Data results' come from real queries; this "
            "summary only arranges them.",
            icon="⚠️",
        )
    st.markdown(run.answer)
else:
    st.warning("No answer text was returned.")

if run.stopped_early:
    st.warning(
        f"The limit of {agent.MAX_TOOL_ROUNDS} tool rounds was reached; the answer may be "
        "incomplete.",
        icon="⚠️",
    )

meta_line = f"{run.provider_name} · {run.provider_model} · {run.rounds} round(s)"
if run.usage:
    meta_line += (
        f" · input {run.usage.get('input_tokens', 0)} tok"
        f" · output {run.usage.get('output_tokens', 0)} tok"
    )
st.caption(meta_line)

if run.answer:
    report = verify_run(run)
    counts = report.counts()
    if report.ok:
        st.caption(
            f"Identifier check: {counts['identifiers']} identifiers in the answer, "
            f"{counts['traceable']} traceable to archived raw responses, "
            f"{counts['echoed']} taken from the question."
        )
    else:
        listed = ", ".join(str(i) for i in report.untraceable)
        st.error(
            f"Identifier check: {counts['untraceable']} identifier(s) do not occur in any "
            f"archived raw response and need a manual check: {listed}",
            icon="❌",
        )


# -- area 3: data results ------------------------------------------------------------
st.divider()
st.subheader("Data results")

if not run.results:
    st.caption("No database query was made for this question.")
else:
    for result in run.results:
        render_result(result)


# -- area 4: call log ----------------------------------------------------------------
st.divider()
st.subheader("Call log")

calls = storage.recent_calls(st.session_state.session_id, limit=50)
if not calls:
    st.caption("No tool call in this session yet.")
else:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "tool": c["tool_name"],
                    "arguments": c["arguments"],
                    "status": c["status"],
                    "duration (ms)": c["duration_ms"],
                    "cache hit": "yes" if c["from_cache"] else "no",
                    "records": c["record_count"],
                    "error": c["error"] or "",
                    "time": c["called_at"],
                }
                for c in calls
            ]
        ),
        width="stretch",
        hide_index=True,
    )

    live = [c for c in calls if not c["from_cache"]]
    if live:
        mean = sum(c["duration_ms"] for c in live) / len(live)
        st.caption(
            f"{len(calls)} calls, {len(calls) - len(live)} from cache; "
            f"mean duration of live requests {mean:.0f} ms"
        )
