#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.lines as mlines
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np


PUBLISHING_RCPARAMS = {
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "DejaVu Sans", "Liberation Sans"],
    "svg.fonttype": "none",
    "pdf.fonttype": 42,
}
matplotlib.rcParams.update(PUBLISHING_RCPARAMS)
matplotlib.rcParams["font.size"] = 7
matplotlib.rcParams["axes.linewidth"] = 0.8
matplotlib.rcParams["axes.spines.top"] = False
matplotlib.rcParams["axes.spines.right"] = False
matplotlib.rcParams["legend.frameon"] = False
matplotlib.rcParams["xtick.major.width"] = 0.7
matplotlib.rcParams["ytick.major.width"] = 0.7


AOU_ORDER = ["basket", "jwt", "sqli"]
AOU_LABELS = {"basket": "Basket", "jwt": "JWT", "sqli": "SQLi"}
AOU_COLORS = {
    "basket": "#0F4D92",
    "jwt": "#42949E",
    "sqli": "#B64342",
}
MODEL_ORDER = [
    "deepseek-v4-pro",
    "glm-5.2",
    "gpt-5.5",
    "kimi-k2.6",
    "qwen3.7-max",
]
MODEL_LABELS = {
    "deepseek-v4-pro": "DeepSeek-V4 Pro",
    "glm-5.2": "GLM-5.2",
    "gpt-5.5": "GPT-5.5",
    "kimi-k2.6": "Kimi-K2.6",
    "qwen3.7-max": "Qwen3.7-Max",
}
STATE_ORDER = [
    "grounded_verification",
    "unreported_verification",
    "unresolved_verification",
    "unsupported_closure",
    "state_unavailable",
]
OBSERVED_STATE_ORDER = STATE_ORDER[:-1]
STATE_LABELS = {
    "grounded_verification": "Grounded",
    "unreported_verification": "Unreported",
    "unresolved_verification": "Unresolved",
    "unsupported_closure": "Unsupported\nclosure",
    "state_unavailable": "Unavailable",
}
STATE_SHORT = {
    "grounded_verification": "G",
    "unreported_verification": "URp",
    "unresolved_verification": "URs",
    "unsupported_closure": "UC",
}
STATE_COLORS = {
    "grounded_verification": "#3775BA",
    "unreported_verification": "#8BCF8B",
    "unresolved_verification": "#E9A6A1",
    "unsupported_closure": "#B64342",
    "state_unavailable": "#CFCECE",
}
READINESS_ORDER = [
    "ready_supported",
    "ready_not_supported",
    "not_ready",
    "internally_conflicted",
]
READINESS_LABELS = {
    "ready_supported": "Ready + supported",
    "ready_not_supported": "Ready, unsupported",
    "not_ready": "Not ready",
    "internally_conflicted": "Conflicted",
}
READINESS_COLORS = {
    "ready_supported": "#3775BA",
    "ready_not_supported": "#E2B34A",
    "not_ready": "#E9A6A1",
    "internally_conflicted": "#9A4D8E",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def number(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def integer(value: str | None) -> int:
    return int(str(value))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def panel_label(ax: plt.Axes, label: str, x: float = -0.12, y: float = 1.04) -> None:
    ax.text(
        x,
        y,
        label,
        transform=ax.transAxes,
        fontsize=9,
        fontweight="bold",
        ha="left",
        va="bottom",
    )


def clean_axis(ax: plt.Axes) -> None:
    ax.spines["left"].set_color("#4D4D4D")
    ax.spines["bottom"].set_color("#4D4D4D")
    ax.tick_params(color="#4D4D4D", labelcolor="#272727", length=2.5)


def save_figure(fig: plt.Figure, base: Path) -> list[str]:
    base.parent.mkdir(parents=True, exist_ok=True)
    svg_path = base.with_suffix(".svg")
    pdf_path = base.with_suffix(".pdf")
    png_path = base.with_suffix(".png")
    tiff_path = base.with_suffix(".tiff")
    fig.savefig(svg_path, bbox_inches="tight", facecolor="white")
    fig.savefig(pdf_path, bbox_inches="tight", facecolor="white")
    fig.savefig(png_path, dpi=600, bbox_inches="tight", facecolor="white")
    fig.savefig(
        tiff_path,
        dpi=600,
        bbox_inches="tight",
        facecolor="white",
        pil_kwargs={"compression": "tiff_lzw"},
    )
    plt.close(fig)
    return [str(svg_path), str(pdf_path), str(png_path), str(tiff_path)]


def build_source_data(
    statistics_root: Path,
    membership_path: Path,
    source_dir: Path,
) -> dict[str, Any]:
    capability = read_csv(statistics_root / "capability_conditioned.csv")
    missingness = read_csv(statistics_root / "missingness_sensitivity.csv")
    transitions = read_csv(statistics_root / "verification_state_transitions.csv")
    scores = read_csv(statistics_root / "diagnostic_score_statistics.csv")
    stop = read_csv(statistics_root / "stop_descriptor_transitions.csv")
    memberships = read_jsonl(membership_path)

    pooled_capability = [
        row for row in capability if row["scope"] == "aou"
    ]
    pooled_missingness = [
        row for row in missingness if row["scope"] == "aou"
    ]
    pooled_transitions = [
        row for row in transitions if row["scope"] == "aou"
    ]
    model_capability = [
        row for row in capability if row["scope"] == "model_by_aou"
    ]
    model_missingness = [
        row for row in missingness if row["scope"] == "model_by_aou"
    ]
    model_vc = [
        row
        for row in scores
        if row["scope"] == "model_by_aou"
        and row["dimension"] == "verification_control"
    ]
    pooled_scores = [row for row in scores if row["scope"] == "aou"]

    capability_outcomes = []
    for aou in AOU_ORDER:
        subset = [
            row
            for row in memberships
            if row["aou"] == aou and row["c0_capability_target"]
        ]
        counts = Counter(row["c1_state"] for row in subset)
        capability_outcomes.append(
            {
                "aou": aou,
                "target_n": len(subset),
                **{f"{state}_n": counts[state] for state in STATE_ORDER},
            }
        )

    readiness = []
    for aou in AOU_ORDER:
        subset = [
            row
            for row in stop
            if row["scope"] == "aou"
            and row["aou"] == aou
            and row["descriptor"] == "readiness_state"
            and row["c0_value"] == "ready_supported"
        ]
        counts = {
            state: sum(
                int(row["transition_n"])
                for row in subset
                if row["c1_value"] == state
            )
            for state in READINESS_ORDER
        }
        readiness.append(
            {
                "aou": aou,
                "c0_ready_supported_n": sum(counts.values()),
                **{f"c1_{state}_n": counts[state] for state in READINESS_ORDER},
            }
        )

    tables = {
        "figure1_retention.csv": pooled_capability,
        "figure1_capability_outcomes.csv": capability_outcomes,
        "figure1_state_transitions.csv": pooled_transitions,
        "figure1_availability.csv": pooled_missingness,
        "figure1_stop_readiness.csv": readiness,
        "figure2_model_retention.csv": model_capability,
        "figure2_model_availability.csv": model_missingness,
        "figure2_model_verification_control.csv": model_vc,
        "figure_s1_pooled_score_diagnostics.csv": pooled_scores,
    }
    output_hashes = {}
    for name, rows in tables.items():
        path = source_dir / name
        write_csv(path, rows)
        output_hashes[name] = {
            "row_count": len(rows),
            "sha256": sha256(path),
        }
    input_files = (
        statistics_root / "capability_conditioned.csv",
        statistics_root / "missingness_sensitivity.csv",
        statistics_root / "verification_state_transitions.csv",
        statistics_root / "diagnostic_score_statistics.csv",
        statistics_root / "stop_descriptor_transitions.csv",
        membership_path,
    )
    manifest = {
        "schema_version": "atobench.result_figure_source_data.v1",
        "input_files": {
            path.name: {"sha256": sha256(path)}
            for path in input_files
        },
        "output_tables": output_hashes,
        "integrity": {
            "frozen_pair_count": len(memberships),
            "capability_target_count": sum(
                row["c0_capability_target"] for row in memberships
            ),
            "model_aou_strata_retained": len(model_capability),
            "excluded_rows": 0,
            "zero_target_model_aou_cells_retained_as_no_target": sum(
                int(row["capability_target_n"]) == 0
                for row in model_capability
            ),
            "state_unavailable_retained": True,
            "stop_numeric_promoted_to_primary": False,
        },
    }
    manifest_path = source_dir / "source_data_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "capability": pooled_capability,
        "capability_outcomes": capability_outcomes,
        "transitions": pooled_transitions,
        "missingness": pooled_missingness,
        "readiness": readiness,
        "model_capability": model_capability,
        "model_missingness": model_missingness,
        "model_vc": model_vc,
        "pooled_scores": pooled_scores,
        "manifest": manifest,
    }


def plot_retention(ax: plt.Axes, rows: list[dict[str, str]]) -> None:
    lookup = {row["aou"]: row for row in rows}
    y_values = np.arange(len(AOU_ORDER))[::-1]
    for y, aou in zip(y_values, AOU_ORDER):
        row = lookup[aou]
        estimate = float(row["observed_grounded_retention"])
        exact = (
            float(row["observed_grounded_retention_exact_ci_low"]),
            float(row["observed_grounded_retention_exact_ci_high"]),
        )
        bounds = (
            float(row["retention_worst_case_bound"]),
            float(row["retention_best_case_bound"]),
        )
        color = AOU_COLORS[aou]
        ax.plot(bounds, [y + 0.11, y + 0.11], lw=5.5, color=color, alpha=0.20)
        ax.plot(exact, [y - 0.03, y - 0.03], lw=1.4, color=color)
        ax.plot(estimate, y - 0.03, "o", ms=5.2, color=color, mec="white", mew=0.6)
        retained = int(row["retained_n"])
        observed = int(row["outcome_observed_n"])
        missing = int(row["outcome_missing_n"])
        ax.text(
            0.985,
            y + 0.20,
            f"{retained}/{observed}" + (f"; m={missing}" if missing else ""),
            ha="right",
            va="center",
            fontsize=5.8,
            color="#4D4D4D",
        )
    ax.set_yticks(y_values)
    ax.set_yticklabels([AOU_LABELS[aou] for aou in AOU_ORDER])
    ax.set_xlim(0, 1)
    ax.set_xticks(np.linspace(0, 1, 6))
    ax.set_xlabel("Grounded retention")
    ax.set_title("Capability-conditioned retention", loc="left", fontweight="bold")
    ax.grid(axis="x", color="#E7E7E7", lw=0.6)
    clean_axis(ax)
    exact_handle = mlines.Line2D([], [], color="#4D4D4D", marker="o", lw=1.2, ms=4)
    bound_handle = mlines.Line2D([], [], color="#767676", lw=5, alpha=0.25)
    ax.legend(
        [exact_handle, bound_handle],
        ["estimate + exact 95% CI", "missing-outcome bounds"],
        loc="lower left",
        fontsize=5.8,
        handlelength=1.8,
    )


def plot_capability_outcomes(
    ax: plt.Axes, rows: list[dict[str, Any]]
) -> None:
    lookup = {row["aou"]: row for row in rows}
    y_values = np.arange(len(AOU_ORDER))[::-1]
    left = np.zeros(len(AOU_ORDER))
    for state in STATE_ORDER:
        widths = np.array(
            [
                lookup[aou][f"{state}_n"] / lookup[aou]["target_n"]
                for aou in AOU_ORDER
            ]
        )
        bars = ax.barh(
            y_values,
            widths,
            left=left,
            height=0.58,
            color=STATE_COLORS[state],
            edgecolor="white",
            linewidth=0.6,
            label=STATE_LABELS[state].replace("\n", " "),
        )
        for index, (bar, aou) in enumerate(zip(bars, AOU_ORDER)):
            count = lookup[aou][f"{state}_n"]
            if count and widths[index] >= 0.075:
                color = "white" if state in {
                    "grounded_verification",
                    "unsupported_closure",
                } else "#272727"
                ax.text(
                    left[index] + widths[index] / 2,
                    bar.get_y() + bar.get_height() / 2,
                    str(count),
                    ha="center",
                    va="center",
                    fontsize=6,
                    color=color,
                    fontweight="bold",
                    path_effects=[pe.withStroke(linewidth=0.8, foreground="white", alpha=0.25)],
                )
        left += widths
    ax.set_yticks(y_values)
    ax.set_yticklabels([AOU_LABELS[aou] for aou in AOU_ORDER])
    ax.set_xlim(0, 1)
    ax.set_xticks([0, 0.5, 1])
    ax.set_xticklabels(["0", "50", "100"])
    ax.set_xlabel("Capability targets (%)")
    ax.set_title("C1 outcome composition", loc="left", fontweight="bold")
    clean_axis(ax)


def transition_matrix(
    rows: list[dict[str, str]], aou: str
) -> tuple[np.ndarray, int]:
    subset = [
        row for row in rows if row["aou"] == aou and row["scope"] == "aou"
    ]
    complete_n = int(subset[0]["complete_pair_n"])
    matrix = np.zeros((4, 4), dtype=float)
    for row in subset:
        i = OBSERVED_STATE_ORDER.index(row["c0_state"])
        j = OBSERVED_STATE_ORDER.index(row["c1_state"])
        matrix[i, j] = int(row["transition_n"])
    return matrix, complete_n


def plot_transition_heatmap(
    ax: plt.Axes,
    rows: list[dict[str, str]],
    aou: str,
    show_y: bool,
) -> None:
    matrix, complete_n = transition_matrix(rows, aou)
    rgba = np.ones((4, 4, 4), dtype=float)
    for i in range(4):
        for j, state in enumerate(OBSERVED_STATE_ORDER):
            base = np.array(mcolors.to_rgb(STATE_COLORS[state]))
            alpha = 0.08 + 0.82 * (matrix[i, j] / max(matrix.max(), 1))
            rgba[i, j, :3] = (1 - alpha) * np.ones(3) + alpha * base
            rgba[i, j, 3] = 1
    ax.imshow(rgba, aspect="equal")
    for i in range(4):
        for j in range(4):
            ax.text(
                j,
                i,
                str(int(matrix[i, j])) if matrix[i, j] else "·",
                ha="center",
                va="center",
                fontsize=6.2,
                color="#272727",
                fontweight="bold" if matrix[i, j] else "normal",
            )
    ax.set_xticks(range(4))
    ax.set_xticklabels([STATE_SHORT[state] for state in OBSERVED_STATE_ORDER])
    ax.set_yticks(range(4))
    ax.set_yticklabels(
        [STATE_SHORT[state] for state in OBSERVED_STATE_ORDER] if show_y else []
    )
    ax.set_xlabel("C1 state", labelpad=2)
    if show_y:
        ax.set_ylabel("C0 state", labelpad=2)
    ax.set_title(f"{AOU_LABELS[aou]}  n={complete_n}", fontsize=7, fontweight="bold")
    ax.tick_params(length=0, pad=1)
    for spine in ax.spines.values():
        spine.set_visible(False)


def plot_availability(ax: plt.Axes, rows: list[dict[str, str]]) -> None:
    lookup = {row["aou"]: row for row in rows}
    y_values = np.arange(len(AOU_ORDER))[::-1]
    ax.axvline(0, color="#767676", lw=0.8, ls="--", zorder=0)
    for y, aou in zip(y_values, AOU_ORDER):
        row = lookup[aou]
        estimate = float(row["paired_availability_difference_c1_minus_c0"])
        low = float(row["paired_availability_difference_bootstrap_ci_low"])
        high = float(row["paired_availability_difference_bootstrap_ci_high"])
        color = AOU_COLORS[aou]
        ax.plot([low, high], [y, y], color=color, lw=1.4)
        ax.plot(estimate, y, "o", color=color, ms=4.8, mec="white", mew=0.6)
        ax.text(
            0.34,
            y,
            f"{row['c0_observed_n']}→{row['c1_observed_n']}",
            ha="left",
            va="center",
            fontsize=6,
            color="#4D4D4D",
        )
    ax.set_yticks(y_values)
    ax.set_yticklabels([AOU_LABELS[aou] for aou in AOU_ORDER])
    ax.set_xlim(-0.35, 0.45)
    ax.set_xticks([-0.3, 0, 0.3])
    ax.set_xlabel("Availability difference (C1 − C0)")
    ax.set_title("Endpoint observability", loc="left", fontweight="bold")
    ax.grid(axis="x", color="#E7E7E7", lw=0.6)
    clean_axis(ax)


def plot_readiness(ax: plt.Axes, rows: list[dict[str, Any]]) -> None:
    lookup = {row["aou"]: row for row in rows}
    y_values = np.arange(len(AOU_ORDER))[::-1]
    left = np.zeros(len(AOU_ORDER))
    for state in READINESS_ORDER:
        widths = np.array(
            [
                lookup[aou][f"c1_{state}_n"]
                / lookup[aou]["c0_ready_supported_n"]
                for aou in AOU_ORDER
            ]
        )
        bars = ax.barh(
            y_values,
            widths,
            left=left,
            height=0.58,
            color=READINESS_COLORS[state],
            edgecolor="white",
            linewidth=0.6,
            label=READINESS_LABELS[state],
        )
        for index, (bar, aou) in enumerate(zip(bars, AOU_ORDER)):
            count = lookup[aou][f"c1_{state}_n"]
            if count and widths[index] >= 0.08:
                ax.text(
                    left[index] + widths[index] / 2,
                    bar.get_y() + bar.get_height() / 2,
                    str(count),
                    ha="center",
                    va="center",
                    fontsize=6,
                    fontweight="bold",
                    color=(
                        "white"
                        if state in {"ready_supported", "internally_conflicted"}
                        else "#272727"
                    ),
                )
        left += widths
    ax.set_yticks(y_values)
    ax.set_yticklabels([AOU_LABELS[aou] for aou in AOU_ORDER])
    ax.set_xlim(0, 1)
    ax.set_xticks([0, 0.5, 1])
    ax.set_xticklabels(["0", "50", "100"])
    ax.set_xlabel("C1 readiness among C0 ready + supported (%)")
    ax.set_title("Stop-decision readiness", loc="left", fontweight="bold")
    clean_axis(ax)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.50, -0.24),
        ncol=2,
        fontsize=5.3,
        columnspacing=0.9,
        handlelength=1.1,
    )


def make_main_figure(data: dict[str, Any], output_dir: Path) -> list[str]:
    fig = plt.figure(figsize=(7.2, 7.1))
    gs = fig.add_gridspec(
        3,
        4,
        height_ratios=[1.0, 1.35, 0.95],
        hspace=0.68,
        wspace=0.72,
    )
    ax_a = fig.add_subplot(gs[0, :2])
    ax_b = fig.add_subplot(gs[0, 2:])
    heat_grid = gs[1, :].subgridspec(1, 3, wspace=0.22)
    heat_axes = [fig.add_subplot(heat_grid[0, index]) for index in range(3)]
    ax_d = fig.add_subplot(gs[2, :2])
    ax_e = fig.add_subplot(gs[2, 2:])

    plot_retention(ax_a, data["capability"])
    plot_capability_outcomes(ax_b, data["capability_outcomes"])
    for index, (ax, aou) in enumerate(zip(heat_axes, AOU_ORDER)):
        plot_transition_heatmap(
            ax, data["transitions"], aou, show_y=index == 0
        )
    plot_availability(ax_d, data["missingness"])
    plot_readiness(ax_e, data["readiness"])

    panel_label(ax_a, "a")
    panel_label(ax_b, "b")
    panel_label(heat_axes[0], "c", x=-0.26)
    panel_label(ax_d, "d")
    panel_label(ax_e, "e")
    heat_axes[1].text(
        0.5,
        1.16,
        "Complete-pair verification-state transitions",
        transform=heat_axes[1].transAxes,
        ha="center",
        va="bottom",
        fontsize=7.5,
        fontweight="bold",
    )
    legend_handles = [
        mlines.Line2D(
            [], [], marker="s", ls="", ms=6, color=STATE_COLORS[state]
        )
        for state in OBSERVED_STATE_ORDER
    ]
    heat_axes[1].legend(
        legend_handles,
        [f"{STATE_SHORT[state]}  {STATE_LABELS[state].replace(chr(10), ' ')}" for state in OBSERVED_STATE_ORDER],
        loc="upper center",
        bbox_to_anchor=(0.5, -0.28),
        ncol=4,
        fontsize=5.2,
        handletextpad=0.3,
        columnspacing=0.7,
    )
    return save_figure(
        fig, output_dir / "fig1_verification_resilience_overview"
    )


def retention_by_model_axes(
    ax: plt.Axes,
    rows: list[dict[str, str]],
    aou: str,
    show_y: bool,
) -> None:
    lookup = {
        row["model"]: row
        for row in rows
        if row["aou"] == aou and row["scope"] == "model_by_aou"
    }
    y_values = np.arange(len(MODEL_ORDER))[::-1]
    color = AOU_COLORS[aou]
    for y, model in zip(y_values, MODEL_ORDER):
        row = lookup[model]
        target = int(row["capability_target_n"])
        observed = int(row["outcome_observed_n"])
        retained = int(row["retained_n"])
        if observed == 0:
            ax.plot(0.04, y, marker="x", color="#A8A8A8", ms=4, mew=1)
            ax.text(
                0.10,
                y,
                "no target" if target == 0 else "no observed outcome",
                va="center",
                fontsize=5.3,
                color="#767676",
            )
            continue
        estimate = float(row["observed_grounded_retention"])
        low = float(row["observed_grounded_retention_exact_ci_low"])
        high = float(row["observed_grounded_retention_exact_ci_high"])
        ax.plot([low, high], [y, y], color=color, lw=1.15)
        ax.plot(
            estimate,
            y,
            "o",
            color=color,
            ms=3.4 + np.sqrt(observed) * 0.55,
            mec="white",
            mew=0.5,
        )
        ax.text(
            1.02,
            y,
            f"{retained}/{observed}",
            ha="left",
            va="center",
            fontsize=5.2,
            color="#4D4D4D",
            clip_on=False,
        )
    ax.set_yticks(y_values)
    ax.set_yticklabels(
        [MODEL_LABELS[model] for model in MODEL_ORDER] if show_y else []
    )
    ax.set_xlim(0, 1)
    ax.set_xticks([0, 0.5, 1])
    ax.set_title(AOU_LABELS[aou], fontweight="bold", color=color)
    ax.set_xlabel("Retention")
    ax.grid(axis="x", color="#E7E7E7", lw=0.55)
    clean_axis(ax)


def matrix_from_model_rows(
    rows: list[dict[str, str]], value_key: str
) -> tuple[np.ndarray, np.ndarray]:
    matrix = np.full((len(AOU_ORDER), len(MODEL_ORDER)), np.nan)
    counts = np.zeros_like(matrix)
    lookup = {(row["aou"], row["model"]): row for row in rows}
    for i, aou in enumerate(AOU_ORDER):
        for j, model in enumerate(MODEL_ORDER):
            row = lookup[(aou, model)]
            value = number(row[value_key])
            matrix[i, j] = np.nan if value is None else value
            count_key = (
                "pair_n"
                if value_key == "paired_availability_difference_c1_minus_c0"
                else "paired_numeric_n"
            )
            counts[i, j] = int(row[count_key])
    return matrix, counts


def labeled_heatmap(
    ax: plt.Axes,
    matrix: np.ndarray,
    counts: np.ndarray,
    title: str,
    vlim: float,
    count_label: bool,
) -> None:
    masked = np.ma.masked_invalid(matrix)
    cmap = plt.get_cmap("RdBu").copy()
    cmap.set_bad("#F0F0F0")
    im = ax.imshow(masked, cmap=cmap, vmin=-vlim, vmax=vlim, aspect="auto")
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            if np.isnan(matrix[i, j]):
                text = "NA"
            elif count_label:
                text = f"{matrix[i, j]:+.2f}\n(n={int(counts[i, j])})"
            else:
                text = f"{matrix[i, j]:+.2f}"
            ax.text(j, i, text, ha="center", va="center", fontsize=5.2)
    ax.set_xticks(range(len(MODEL_ORDER)))
    ax.set_xticklabels(
        [MODEL_LABELS[model].replace(" ", "\n", 1) for model in MODEL_ORDER],
        rotation=0,
        fontsize=5.4,
    )
    ax.set_yticks(range(len(AOU_ORDER)))
    ax.set_yticklabels([AOU_LABELS[aou] for aou in AOU_ORDER])
    ax.set_title(title, loc="left", fontweight="bold")
    ax.tick_params(length=0, pad=2)
    for spine in ax.spines.values():
        spine.set_visible(False)
    cbar = ax.figure.colorbar(im, ax=ax, fraction=0.035, pad=0.025)
    cbar.ax.tick_params(labelsize=5.2, length=2)


def make_model_figure(data: dict[str, Any], output_dir: Path) -> list[str]:
    fig = plt.figure(figsize=(7.2, 5.2))
    gs = fig.add_gridspec(
        2,
        6,
        height_ratios=[1.28, 1.0],
        hspace=0.48,
        wspace=0.75,
    )
    top_axes = [
        fig.add_subplot(gs[0, index * 2 : (index + 1) * 2])
        for index in range(3)
    ]
    for index, (ax, aou) in enumerate(zip(top_axes, AOU_ORDER)):
        retention_by_model_axes(
            ax, data["model_capability"], aou, show_y=index == 0
        )
    top_axes[1].text(
        0.5,
        1.20,
        "Model-within-AOU grounded retention (exact 95% CI)",
        transform=top_axes[1].transAxes,
        ha="center",
        va="bottom",
        fontweight="bold",
        fontsize=7.5,
    )
    panel_label(top_axes[0], "a", x=-0.32)
    ax_b = fig.add_subplot(gs[1, :3])
    ax_c = fig.add_subplot(gs[1, 3:])
    availability, availability_n = matrix_from_model_rows(
        data["model_missingness"],
        "paired_availability_difference_c1_minus_c0",
    )
    vc, vc_n = matrix_from_model_rows(
        data["model_vc"], "paired_mean_delta"
    )
    labeled_heatmap(
        ax_b,
        availability,
        availability_n,
        "Availability difference (C1 − C0)",
        vlim=0.5,
        count_label=False,
    )
    labeled_heatmap(
        ax_c,
        vc,
        vc_n,
        "Verification Control score change",
        vlim=2.0,
        count_label=True,
    )
    panel_label(ax_b, "b", x=-0.20)
    panel_label(ax_c, "c", x=-0.20)
    fig.text(
        0.5,
        0.012,
        "Model strata are descriptive; exact intervals and eligible counts preclude rank interpretation.",
        ha="center",
        va="bottom",
        fontsize=5.8,
        color="#606060",
    )
    return save_figure(
        fig, output_dir / "fig2_model_stratified_patterns"
    )


def make_diagnostic_figure(
    data: dict[str, Any], output_dir: Path
) -> list[str]:
    rows = data["pooled_scores"]
    dimensions = [
        "verification_control",
        "report_grounding",
        "stop_decision",
    ]
    dimension_labels = [
        "Verification Control",
        "Report Grounding",
        "Stop Decision*",
    ]
    fig, axes = plt.subplots(1, 3, figsize=(7.2, 2.8), sharex=True, sharey=False)
    lookup = {(row["aou"], row["dimension"]): row for row in rows}
    for index, (ax, aou) in enumerate(zip(axes, AOU_ORDER)):
        y_values = np.arange(len(dimensions))[::-1]
        ax.axvline(0, color="#767676", lw=0.8, ls="--")
        for y, dimension in zip(y_values, dimensions):
            row = lookup[(aou, dimension)]
            estimate = float(row["paired_mean_delta"])
            low = float(row["paired_mean_delta_bootstrap_ci_low"])
            high = float(row["paired_mean_delta_bootstrap_ci_high"])
            significant_direction = high < 0 or low > 0
            color = (
                "#B64342"
                if high < 0
                else "#2E9E44"
                if low > 0
                else "#767676"
            )
            marker_face = "white" if dimension == "stop_decision" else color
            ax.plot([low, high], [y, y], color=color, lw=1.3)
            ax.plot(
                estimate,
                y,
                "o",
                ms=4.5,
                mfc=marker_face,
                mec=color,
                mew=1,
            )
            ax.text(
                1.03,
                y,
                f"n={row['paired_numeric_n']}",
                ha="left",
                va="center",
                fontsize=5.4,
                color="#606060",
            )
        ax.set_yticks(y_values)
        ax.set_yticklabels(dimension_labels if index == 0 else [])
        ax.set_xlim(-1.5, 1.0)
        ax.set_xticks([-1, 0, 1])
        ax.set_title(AOU_LABELS[aou], fontweight="bold", color=AOU_COLORS[aou])
        ax.set_xlabel("Mean score change (C1 − C0)")
        ax.grid(axis="x", color="#E7E7E7", lw=0.55)
        clean_axis(ax)
        panel_label(ax, chr(ord("a") + index), x=-0.30 if index == 0 else -0.15)
    fig.text(
        0.5,
        0.018,
        "* Stop Decision numeric score is engineering provenance; descriptor transitions are primary.",
        ha="center",
        va="bottom",
        fontsize=5.6,
        color="#606060",
    )
    fig.subplots_adjust(bottom=0.22, top=0.88, left=0.09, right=0.97, wspace=0.30)
    return save_figure(
        fig, output_dir / "figs1_judge_score_diagnostics"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--statistics-root", type=Path, required=True)
    parser.add_argument("--population-membership", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()

    source_dir = args.output_root / "source_data"
    figure_dir = args.output_root / "figures"
    data = build_source_data(
        args.statistics_root, args.population_membership, source_dir
    )
    outputs = {
        "main": make_main_figure(data, figure_dir),
        "model": make_model_figure(data, figure_dir),
        "diagnostic": make_diagnostic_figure(data, figure_dir),
    }
    report = {
        "schema_version": "atobench.result_figures.v1",
        "status": "PASS",
        "backend": "python_matplotlib",
        "figures": outputs,
        "source_data_manifest": str(source_dir / "source_data_manifest.json"),
        "data_integrity": data["manifest"]["integrity"],
        "paper_modified": False,
    }
    (args.output_root / "figure_build_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
