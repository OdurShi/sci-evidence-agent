"""Draw the two figures of the repository from files, without touching the network.

    python scripts/make_figures.py                       # default results directory
    python scripts/make_figures.py results/eval-2026-11-14

Outputs (PNG, 200 dpi, white background):

    figures/eval_per_category.png   per-category results, read from <results>/summary.json
    figures/architecture.png        architecture diagram (2400 x 1500 px)
    figures/teaser_1600x1000.png    the same diagram as a web teaser, exactly 1600 x 1000 px

Needs matplotlib (``pip install -e ".[eval]"``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Patch

ROOT = Path(__file__).resolve().parent.parent
FIGURES = ROOT / "figures"
DEFAULT_RESULTS = ROOT / "results" / "eval-2026-11-14"
DPI = 200

# One style for both figures: ink for text, hairline grid, two series hues.
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3de"
COLD = "#2a78d6"  # first pass (blue)
WARM = "#eb6834"  # second pass (orange)
PANEL = {
    "pipeline": ("#edf4fd", "#2a78d6"),
    "evidence": ("#e8f7f1", "#1baf7a"),
    "external": ("#f5f4f1", "#8a8984"),
}

CATEGORY_LABELS = {
    "compound": "single-source\ncompound",
    "bioactivity_chain": "compound →\nbioactivity chain",
    "protein": "protein",
    "literature": "literature",
    "cross_source": "cross-source",
}

plt.rcParams.update(
    {
        "font.family": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 9,
        "text.color": INK,
        "axes.labelcolor": INK_2,
        "axes.edgecolor": GRID,
        "xtick.color": INK_2,
        "ytick.color": INK,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
    }
)


# -- figure 1: evaluation ------------------------------------------------------------


def _style(ax: plt.Axes) -> None:
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.spines["bottom"].set_linewidth(0.8)
    ax.tick_params(axis="both", length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _share_panel(ax, categories, rows, key, num, den, title, colour, overall) -> None:
    """Horizontal bars of a share in percent, with the counts written at the bar end."""
    positions = range(len(categories))
    for y, category in zip(positions, categories, strict=True):
        m = rows[category]
        value = m[key]
        if value is None:
            ax.text(2, y, "not applicable", va="center", ha="left", color=INK_2, fontsize=8)
            continue
        ax.barh(y, 100 * value, height=0.3, color=colour, linewidth=0)
        ax.text(
            100 * value + 2.5,
            y,
            f"{100 * value:.0f}%  ({m[num]}/{m[den]})",
            va="center",
            ha="left",
            color=INK,
            fontsize=8,
        )
    ax.set_xlim(0, 150)
    ax.set_xticks([0, 50, 100])
    ax.set_xticklabels(["0", "50", "100%"])
    ax.set_ylim(len(categories) - 0.5, -0.5)
    ax.set_yticks(list(positions))
    ax.set_title(title, loc="left", fontsize=10, fontweight="bold", pad=16)
    total = overall[key]
    note = (
        "not applicable" if total is None else f"{100 * total:.1f}% ({overall[num]}/{overall[den]})"
    )
    ax.text(0, 1.035, f"all questions: {note}", transform=ax.transAxes, color=INK_2, fontsize=8)
    _style(ax)


def _latency_panel(ax, categories, cold, warm) -> None:
    """Median and 90th percentile per category on a log axis, for both passes."""
    floor = 1e-3
    for y, category in enumerate(categories):
        for offset, rows, colour in ((-0.17, cold, COLD), (0.17, warm, WARM)):
            median = max(rows[category]["latency_median_s"], floor)
            p90 = max(rows[category]["latency_p90_s"], floor)
            ax.plot(
                [median, p90], [y + offset] * 2, color=colour, linewidth=2, solid_capstyle="round"
            )
            ax.plot(
                median,
                y + offset,
                "o",
                color=colour,
                markersize=6.5,
                markeredgecolor="white",
                markeredgewidth=1.2,
            )
            ax.plot(
                p90,
                y + offset,
                "D",
                color=colour,
                markersize=5.5,
                markeredgecolor="white",
                markeredgewidth=1.2,
            )
    ax.set_xscale("log")
    ax.set_xlim(floor * 0.6, 200)
    ax.set_xticks([1e-3, 1e-2, 1e-1, 1, 10, 100])
    ax.set_xticklabels(["≤1 ms", "10 ms", "0.1 s", "1 s", "10 s", "100 s"])
    ax.minorticks_off()
    ax.set_ylim(len(categories) - 0.5, -0.5)
    ax.set_yticks(range(len(categories)))
    ax.set_title("Latency per question", loc="left", fontsize=10, fontweight="bold", pad=16)
    ax.text(
        0,
        1.035,
        "wall clock incl. rate-limit waits, log scale",
        transform=ax.transAxes,
        color=INK_2,
        fontsize=8,
    )
    _style(ax)
    ax.legend(
        handles=[
            Line2D([], [], marker="o", color=INK_2, linestyle="none", markersize=6, label="median"),
            Line2D(
                [],
                [],
                marker="D",
                color=INK_2,
                linestyle="none",
                markersize=5,
                label="90th percentile",
            ),
        ],
        loc="lower right",
        bbox_to_anchor=(1.0, 1.085),
        ncol=2,
        frameon=False,
        fontsize=8,
        handletextpad=0.2,
        columnspacing=1.0,
        borderaxespad=0.0,
    )


def figure_evaluation(results_dir: Path, out: Path) -> None:
    summary = json.loads((results_dir / "summary.json").read_text(encoding="utf-8"))
    names = list(summary["passes"])
    cold_block, warm_block = summary["passes"][names[0]], summary["passes"][names[-1]]
    cold, warm = cold_block["per_category"], warm_block["per_category"]
    categories = list(cold)
    labels = [CATEGORY_LABELS.get(c, c) for c in categories]

    fig, axes = plt.subplots(2, 3, figsize=(12, 7.2), sharey=True)
    panels = [
        (
            axes[0, 0],
            cold,
            "tool_selection_exact",
            "exact_n",
            "n",
            "Tool selection (exact sequence)",
            COLD,
            cold_block["overall"],
        ),
        (
            axes[0, 1],
            cold,
            "identifier_recall",
            "identifiers_found",
            "identifiers_expected",
            "Recall of expected identifiers",
            COLD,
            cold_block["overall"],
        ),
        (
            axes[0, 2],
            cold,
            "traceable_share",
            "traceable",
            "answer_identifiers",
            "Answer identifiers traceable to raw responses",
            COLD,
            cold_block["overall"],
        ),
        (
            axes[1, 0],
            cold,
            "handling",
            "non_ok_handled",
            "non_ok_questions",
            "Handling of no-match / ambiguous / error",
            COLD,
            cold_block["overall"],
        ),
        (
            axes[1, 2],
            warm,
            "cache_hit_rate",
            "cache_hits",
            "tool_calls",
            "Cache hit rate on the second pass",
            WARM,
            warm_block["overall"],
        ),
    ]
    for block in (cold, {"overall": cold_block["overall"]}):
        for m in block.values():
            m["exact_n"] = round(m["tool_selection_exact"] * m["n"])
    for ax, rows, key, num, den, title, colour, overall in panels:
        _share_panel(ax, categories, rows, key, num, den, title, colour, overall)
    _latency_panel(axes[1, 1], categories, cold, warm)

    for ax in axes[:, 0]:
        ax.set_yticklabels(labels, fontsize=9)

    bench = summary["benchmark"]
    fig.suptitle(
        f"Offline planner on {bench['name']} v{bench['version']}: {bench['n_questions']} questions "
        f"against the live APIs, {summary['started_utc'][:10]}",
        x=0.012,
        y=0.985,
        ha="left",
        fontsize=12,
        fontweight="bold",
    )
    fig.legend(
        handles=[
            Patch(color=COLD, label="first pass (cold: no cache reads)"),
            Patch(color=WARM, label="second pass (warm: cache filled by the first)"),
        ],
        loc="upper left",
        bbox_to_anchor=(0.006, 0.952),
        ncol=2,
        frameon=False,
        fontsize=9,
        handlelength=1.2,
        columnspacing=1.6,
    )
    fig.subplots_adjust(left=0.115, right=0.985, top=0.80, bottom=0.06, wspace=0.10, hspace=0.46)
    fig.savefig(out, dpi=DPI)
    plt.close(fig)


# -- figure 2: architecture ------------------------------------------------------------


def _box(ax, x, y, w, h, kind, title, lines, scale) -> None:
    fill, edge = PANEL[kind]
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0,rounding_size=0.16",
            facecolor=fill,
            edgecolor=edge,
            linewidth=1.3 * scale,
        )
    )
    top = y + h - 0.36
    ax.text(
        x + w / 2,
        top,
        title,
        ha="center",
        va="center",
        fontsize=11.5 * scale,
        fontweight="bold",
        color=INK,
    )
    for i, line in enumerate(lines):
        ax.text(
            x + w / 2,
            top - 0.46 - 0.36 * i,
            line,
            ha="center",
            va="center",
            fontsize=8.6 * scale,
            color=INK_2,
        )


def _arrow(ax, start, end, scale, label=None, label_at=None, curve=0.0, colour=INK_2) -> None:
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=13 * scale,
            linewidth=1.3 * scale,
            color=colour,
            connectionstyle=f"arc3,rad={curve}",
            shrinkA=0,
            shrinkB=0,
        )
    )
    if label:
        x, y = label_at or ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2 + 0.2)
        ax.text(
            x,
            y,
            label,
            ha="center",
            va="center",
            fontsize=8.2 * scale,
            color=INK_2,
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.2},
        )


def figure_architecture(out: Path, width_in: float, height_in: float) -> None:
    """The pipeline on a 16 x 10 canvas; fonts scale with the output width."""
    scale = width_in / 12.0
    fig = plt.figure(figsize=(width_in, height_in))
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, 16)
    ax.set_ylim(0, 10)
    ax.axis("off")

    ax.text(0.5, 9.42, "sci-evidence-agent", fontsize=17 * scale, fontweight="bold", va="center")
    ax.text(
        0.5,
        8.86,
        "question → planner → tools → databases → provenance store → verified answer",
        fontsize=10.5 * scale,
        color=INK_2,
        va="center",
    )

    # Top row: the request path.
    y, h = 5.35, 2.55
    _box(ax, 0.45, y + 0.55, 1.95, 1.45, "external", "Question", ["free text"], scale)
    _box(
        ax,
        3.05,
        y,
        2.75,
        h,
        "pipeline",
        "Planner",
        ["language model or", "offline keyword rules;", "providers are", "interchangeable"],
        scale,
    )
    _box(
        ax,
        6.55,
        y,
        2.75,
        h,
        "pipeline",
        "Tool executor",
        ["whitelist of 8 tools", "argument validation", "timing, call log", "at most 5 rounds"],
        scale,
    )
    _box(
        ax,
        10.05,
        y,
        2.65,
        h,
        "pipeline",
        "Connectors",
        ["uniform result:", "ok · no_match ·", "ambiguous · error", "rate limit, retries"],
        scale,
    )
    _box(
        ax,
        13.45,
        y,
        2.15,
        h,
        "external",
        "Databases",
        ["PubChem · ChEMBL", "UniProt · PubMed", "LOTUS / Wikidata", "(public APIs)"],
        scale,
    )

    mid = y + h / 2
    _arrow(ax, (2.4, mid + 0.02), (3.05, mid + 0.02), scale)
    _arrow(ax, (5.8, mid + 0.45), (6.55, mid + 0.45), scale, "tool calls", (6.175, mid + 0.78))
    _arrow(ax, (6.55, mid - 0.45), (5.8, mid - 0.45), scale, "results", (6.175, mid - 0.78))
    _arrow(ax, (9.3, mid), (10.05, mid), scale)
    _arrow(ax, (12.7, mid + 0.45), (13.45, mid + 0.45), scale, "HTTPS", (13.075, mid + 0.78))
    _arrow(ax, (13.45, mid - 0.45), (12.7, mid - 0.45), scale, "JSON", (13.075, mid - 0.78))

    # Bottom row: the evidence path.
    y2, h2 = 1.75, 2.05
    _box(
        ax,
        10.05,
        y2,
        4.1,
        h2,
        "evidence",
        "Provenance store",
        [
            "raw response of every query (JSON)",
            "cache with original retrieval time",
            "log of every call and question",
        ],
        scale,
    )
    _box(
        ax,
        4.85,
        y2,
        4.1,
        h2,
        "evidence",
        "Citation verifier",
        [
            "identifiers in the answer: CID, ChEMBL ID,",
            "UniProt accession, PMID, DOI, InChIKey",
            "must occur in the archived raw responses",
        ],
        scale,
    )
    _box(
        ax,
        0.45,
        y2 + 0.2,
        3.3,
        h2 - 0.4,
        "external",
        "Verified answer",
        ["records with sources;", "untraceable identifiers flagged"],
        scale,
    )

    green = PANEL["evidence"][1]
    _arrow(ax, (11.4, y), (11.4, y2 + h2), scale, "archive, cache", (11.4, 4.57), colour=green)
    _arrow(
        ax,
        (10.05, y2 + h2 / 2),
        (8.95, y2 + h2 / 2),
        scale,
        "raw",
        (9.5, y2 + h2 / 2 + 0.3),
        colour=green,
    )
    _arrow(ax, (5.3, y), (5.3, y2 + h2), scale, "answer", (5.3, 4.57), colour=green)
    _arrow(ax, (4.85, y2 + h2 / 2), (3.75, y2 + h2 / 2), scale, colour=green)

    ax.legend(
        handles=[
            Patch(
                facecolor=PANEL["pipeline"][0],
                edgecolor=PANEL["pipeline"][1],
                label="planning and tool execution",
            ),
            Patch(
                facecolor=PANEL["evidence"][0],
                edgecolor=PANEL["evidence"][1],
                label="provenance and verification",
            ),
            Patch(
                facecolor=PANEL["external"][0],
                edgecolor=PANEL["external"][1],
                label="input, output and external services",
            ),
        ],
        loc="lower left",
        bbox_to_anchor=(0.022, 0.035),
        ncol=3,
        frameon=False,
        fontsize=9 * scale,
        handlelength=1.4,
        columnspacing=2.0,
    )
    fig.savefig(out, dpi=DPI)
    plt.close(fig)


def main() -> None:
    results_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RESULTS
    FIGURES.mkdir(exist_ok=True)
    figure_architecture(FIGURES / "architecture.png", 12.0, 7.5)
    figure_architecture(FIGURES / "teaser_1600x1000.png", 8.0, 5.0)
    print("wrote figures/architecture.png and figures/teaser_1600x1000.png")
    if (results_dir / "summary.json").is_file():
        figure_evaluation(results_dir, FIGURES / "eval_per_category.png")
        print(f"wrote figures/eval_per_category.png from {results_dir / 'summary.json'}")
    else:
        print(f"skipped figures/eval_per_category.png: no summary.json in {results_dir}")


if __name__ == "__main__":
    main()
