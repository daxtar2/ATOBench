#!/usr/bin/env python3
"""Build exploratory trajectory-dynamics tables and candidate figures."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.path import Path as MplPath
from matplotlib.patches import PathPatch, Rectangle


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
MODEL_COLORS = {
    "deepseek-v4-pro": "#2F6DA8",
    "glm-5.2": "#5E8C61",
    "gpt-5.5": "#8755A5",
    "kimi-k2.6": "#D07A2D",
    "qwen3.7-max": "#B7474A",
}
AOU_ORDER = ["basket", "jwt", "sqli"]
AOU_LABELS = {"basket": "Basket", "jwt": "JWT", "sqli": "SQLi"}
CONDITION_LABELS = {"C0": "Native", "C1": "ATO"}

ADAPTIVE_FACT_TYPES = {
    "basket": {
        "BASKET_B1_RECEIVED_RELATION_VALUE_USED",
        "BASKET_D1_RELATION_COMPATIBLE_ACTION",
    },
    "jwt": {
        "JWT_B1_RECEIVED_ARTIFACT_USED",
        "JWT_B3_DISTINCT_ARTIFACT_REACQUIRED",
    },
    "sqli": {
        "SQLI_B2_ALTERNATE_PAYLOAD_FAMILY",
        "SQLI_B3_ALTERNATE_SQLI_ENDPOINT",
        "SQLI_B4_INDEPENDENT_CROSSCHECK",
    },
}
COMPONENT_FACT_TYPES = [
    "BASKET_B1_RECEIVED_RELATION_VALUE_USED",
    "BASKET_D1_RELATION_COMPATIBLE_ACTION",
    "JWT_B1_RECEIVED_ARTIFACT_USED",
    "JWT_B3_DISTINCT_ARTIFACT_REACQUIRED",
    "SQLI_B1_REPROBE_SAME_ENDPOINT",
    "SQLI_B2_ALTERNATE_PAYLOAD_FAMILY",
    "SQLI_B3_ALTERNATE_SQLI_ENDPOINT",
    "SQLI_B4_INDEPENDENT_CROSSCHECK",
]

BEHAVIOR_ORDER = [
    "adaptive_recovery",
    "adaptive_unresolved",
    "direct_or_nonadaptive_evidence",
    "no_adaptive_no_primary_evidence",
]
EVIDENCE_ORDER = ["positive", "negative", "unresolved"]
STOP_ORDER = [
    "ready_supported",
    "ready_not_supported",
    "not_ready",
    "internally_conflicted",
    "unavailable",
]
REPORT_ORDER = [
    "supported_closure",
    "unsupported_closure",
    "no_closure",
    "unavailable",
]
REPORT_COLORS = {
    "supported_closure": "#2F7D68",
    "unsupported_closure": "#B84A4A",
    "no_closure": "#6C7A89",
    "unavailable": "#B9BDC3",
}

RADAR_AXES = [
    ("primary_evidence_rate", "Primary proof"),
    ("adaptive_verification_rate", "Adaptation"),
    ("verification_control_rate", "Control"),
    ("supported_stop_rate", "Stop"),
    ("supported_report_rate", "Report"),
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fact_is_positive(fact: dict[str, Any] | None) -> bool:
    return bool(
        fact
        and fact.get("measurement_status") == "positive"
        and fact.get("value") is True
    )


def fact_status(fact: dict[str, Any] | None) -> str:
    if not fact:
        return "not_applicable"
    return str(fact.get("measurement_status", "unavailable"))


def pointer_key(pointer: str) -> tuple[str, int] | None:
    parts = pointer.split(":")
    if len(parts) != 5 or parts[:2] != ["event", "http"]:
        return None
    try:
        return parts[2], int(parts[4])
    except ValueError:
        return None


def graph_pointer_key(node: dict[str, Any]) -> tuple[str, int] | None:
    source = node.get("source") or {}
    raw_hash = source.get("raw_file_sha256")
    line = source.get("raw_line_number")
    if not isinstance(raw_hash, str) or not isinstance(line, int):
        return None
    return raw_hash[:16], line


def action_signature(node: dict[str, Any]) -> tuple[str, str, str, str]:
    data = node.get("data") or {}
    return (
        str(data.get("method") or ""),
        str(data.get("endpoint_family") or ""),
        str(data.get("route_template") or ""),
        str(data.get("payload_family") or ""),
    )


def switch_count(values: Iterable[str]) -> int:
    informative = [value for value in values if value]
    return sum(left != right for left, right in zip(informative, informative[1:]))


def evidence_state(endpoint: dict[str, Any]) -> str:
    status = endpoint.get("measurement_status")
    if status == "positive" and endpoint.get("value") is True:
        return "positive"
    if status == "negative":
        return "negative"
    return "unresolved"


def stop_state(episode: dict[str, Any]) -> str:
    descriptor = (
        episode.get("dimensions", {})
        .get("stop_decision", {})
        .get("descriptor", {})
        .get("derived_descriptors", {})
    )
    value = descriptor.get("readiness_state")
    return value if value in STOP_ORDER else "unavailable"


def report_state(episode: dict[str, Any]) -> str:
    closure = episode.get("report_closure", {})
    trace = episode.get("registered_claim_trace_status", {})
    if closure.get("measurement_status") != "measured":
        return "unavailable"
    if closure.get("value") is not True:
        return "no_closure"
    if trace.get("measurement_status") != "measured":
        return "unavailable"
    return (
        "supported_closure"
        if trace.get("value") == "supported"
        else "unsupported_closure"
    )


def behavior_state(adaptive: bool, primary: bool) -> str:
    if adaptive and primary:
        return "adaptive_recovery"
    if adaptive:
        return "adaptive_unresolved"
    if primary:
        return "direct_or_nonadaptive_evidence"
    return "no_adaptive_no_primary_evidence"


def build_episode_rows(
    episodes: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    graph_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    facts_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fact_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for fact in facts:
        episode_id = fact["episode_id"]
        facts_by_episode[episode_id].append(fact)
        fact_lookup[(episode_id, fact["fact_type"])] = fact

    actions_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    turns_by_pointer: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    for node in graph_nodes:
        episode_id = node.get("episode_id")
        key = graph_pointer_key(node)
        if episode_id and key:
            turns_by_pointer[(episode_id, key[0], key[1])].append(
                int(node.get("turn_idx", key[1]))
            )
        if node.get("node_type") == "action" and episode_id:
            actions_by_episode[episode_id].append(node)

    rows: list[dict[str, Any]] = []
    for episode in episodes:
        episode_id = episode["episode_id"]
        aou = episode["aou"]
        endpoint = episode.get("task_evidence_endpoint") or {}
        primary = evidence_state(endpoint) == "positive"
        adaptive_types = ADAPTIVE_FACT_TYPES[aou]
        adaptive = any(
            fact_is_positive(fact_lookup.get((episode_id, fact_type)))
            for fact_type in adaptive_types
        )

        actions = sorted(
            actions_by_episode.get(episode_id, []),
            key=lambda node: (int(node.get("turn_idx", 0)), node.get("node_id", "")),
        )
        signatures = [action_signature(node) for node in actions]
        endpoint_families = [signature[1] for signature in signatures]
        payload_families = [signature[3] for signature in signatures]
        action_turns = [int(node.get("turn_idx", 0)) for node in actions]

        material_turns: list[int] = []
        for fact in facts_by_episode.get(episode_id, []):
            if (
                fact.get("fact_class") != "verification"
                or fact.get("importance") != "high"
                or not fact_is_positive(fact)
            ):
                continue
            for pointer in fact.get("source_pointers") or []:
                parsed = pointer_key(pointer)
                if parsed:
                    material_turns.extend(
                        turns_by_pointer.get((episode_id, parsed[0], parsed[1]), [])
                    )
        first_material_turn = min(material_turns) if material_turns else None
        last_material_turn = max(material_turns) if material_turns else None
        after_last = (
            sum(turn > last_material_turn for turn in action_turns)
            if last_material_turn is not None
            else None
        )

        vc_final = (
            episode.get("dimensions", {})
            .get("verification_control", {})
            .get("final", {})
        )
        vc_score = vc_final.get("score")
        stop = stop_state(episode)
        report = report_state(episode)

        row: dict[str, Any] = {
            "episode_id": episode_id,
            "episode_pseudonym": episode["episode_pseudonym"],
            "pair_id": episode["pair_id"],
            "model": episode["model"],
            "aou": aou,
            "condition": episode["condition"],
            "condition_label": CONDITION_LABELS[episode["condition"]],
            "primary_evidence_state": evidence_state(endpoint),
            "primary_evidence_positive": int(primary),
            "adaptive_verification_positive": int(adaptive),
            "behavior_state": behavior_state(adaptive, primary),
            "stop_readiness_state": stop,
            "supported_stop": int(stop == "ready_supported"),
            "report_state": report,
            "supported_report": int(report == "supported_closure"),
            "verification_resolution_state": episode.get(
                "verification_resolution_state", "state_unavailable"
            ),
            "grounded_verification": int(
                episode.get("verification_resolution_state")
                == "grounded_verification"
            ),
            "verification_control_score": (
                float(vc_score) if isinstance(vc_score, (int, float)) else ""
            ),
            "action_count": len(actions),
            "unique_action_signature_count": len(set(signatures)),
            "repeated_action_count": len(actions) - len(set(signatures)),
            "novel_action_ratio": (
                round(len(set(signatures)) / len(actions), 6) if actions else ""
            ),
            "endpoint_switch_count": switch_count(endpoint_families),
            "payload_switch_count": switch_count(payload_families),
            "first_material_evidence_turn": (
                first_material_turn if first_material_turn is not None else ""
            ),
            "last_material_evidence_turn": (
                last_material_turn if last_material_turn is not None else ""
            ),
            "actions_after_last_material_evidence": (
                after_last if after_last is not None else ""
            ),
        }
        for fact_type in COMPONENT_FACT_TYPES:
            fact = fact_lookup.get((episode_id, fact_type))
            row[f"{fact_type.lower()}_status"] = fact_status(fact)
            row[f"{fact_type.lower()}_positive"] = (
                int(fact_is_positive(fact)) if fact else ""
            )
        rows.append(row)

    return rows


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_capability_profiles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for model in MODEL_ORDER:
        for condition in ["C0", "C1"]:
            selected = [
                row
                for row in rows
                if row["model"] == model and row["condition"] == condition
            ]
            vc = [
                float(row["verification_control_score"])
                for row in selected
                if row["verification_control_score"] != ""
            ]
            by_aou = {
                aou: [row for row in selected if row["aou"] == aou]
                for aou in AOU_ORDER
            }
            primary_aou_rates = [
                sum(row["primary_evidence_positive"] for row in aou_rows)
                / len(aou_rows)
                for aou_rows in by_aou.values()
                if aou_rows
            ]
            adaptive_aou_rates = [
                sum(row["adaptive_verification_positive"] for row in aou_rows)
                / len(aou_rows)
                for aou_rows in by_aou.values()
                if aou_rows
            ]
            stop_aou_rates = [
                sum(row["supported_stop"] for row in aou_rows) / len(aou_rows)
                for aou_rows in by_aou.values()
                if aou_rows
            ]
            report_aou_rates = [
                sum(row["supported_report"] for row in aou_rows) / len(aou_rows)
                for aou_rows in by_aou.values()
                if aou_rows
            ]
            vc_aou_rates: list[float] = []
            for aou_rows in by_aou.values():
                aou_scores = [
                    float(row["verification_control_score"])
                    for row in aou_rows
                    if row["verification_control_score"] != ""
                ]
                if aou_scores:
                    vc_aou_rates.append(statistics.mean(aou_scores) / 10)
            n = len(selected)
            output.append(
                {
                    "model": model,
                    "condition": condition,
                    "condition_label": CONDITION_LABELS[condition],
                    "aggregation": "macro_average_across_aou",
                    "aou_n": len(primary_aou_rates),
                    "episode_n": n,
                    "primary_evidence_n": sum(
                        row["primary_evidence_positive"] for row in selected
                    ),
                    "primary_evidence_rate": (
                        statistics.mean(primary_aou_rates)
                        if primary_aou_rates
                        else ""
                    ),
                    "adaptive_verification_n": sum(
                        row["adaptive_verification_positive"] for row in selected
                    ),
                    "adaptive_verification_rate": (
                        statistics.mean(adaptive_aou_rates)
                        if adaptive_aou_rates
                        else ""
                    ),
                    "verification_control_n": len(vc),
                    "verification_control_mean": (
                        round(mean(vc), 6) if vc else ""
                    ),
                    "verification_control_rate": (
                        round(statistics.mean(vc_aou_rates), 6)
                        if vc_aou_rates
                        else ""
                    ),
                    "supported_stop_n": sum(row["supported_stop"] for row in selected),
                    "supported_stop_rate": (
                        statistics.mean(stop_aou_rates) if stop_aou_rates else ""
                    ),
                    "supported_report_n": sum(
                        row["supported_report"] for row in selected
                    ),
                    "supported_report_rate": (
                        statistics.mean(report_aou_rates)
                        if report_aou_rates
                        else ""
                    ),
                }
            )
    return output


def build_grounded_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for aou in AOU_ORDER:
        for model in MODEL_ORDER:
            for condition in ["C0", "C1"]:
                selected = [
                    row
                    for row in rows
                    if row["aou"] == aou
                    and row["model"] == model
                    and row["condition"] == condition
                ]
                n = len(selected)
                grounded = sum(row["grounded_verification"] for row in selected)
                output.append(
                    {
                        "aou": aou,
                        "model": model,
                        "condition": condition,
                        "condition_label": CONDITION_LABELS[condition],
                        "episode_n": n,
                        "grounded_n": grounded,
                        "grounded_rate": grounded / n if n else "",
                    }
                )
    return output


def build_flow_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for aou in AOU_ORDER:
        selected = [
            row for row in rows if row["aou"] == aou and row["condition"] == "C1"
        ]
        counts = Counter(
            (
                row["behavior_state"],
                row["primary_evidence_state"],
                row["stop_readiness_state"],
                row["report_state"],
            )
            for row in selected
        )
        for path, count in sorted(counts.items()):
            output.append(
                {
                    "aou": aou,
                    "condition": "C1",
                    "condition_label": "ATO",
                    "behavior_state": path[0],
                    "evidence_state": path[1],
                    "stop_state": path[2],
                    "report_state": path[3],
                    "episode_n": count,
                    "aou_episode_n": len(selected),
                }
            )
    return output


def build_pair_dynamics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        by_pair[row["pair_id"]][row["condition"]] = row
    metrics = [
        "action_count",
        "unique_action_signature_count",
        "repeated_action_count",
        "novel_action_ratio",
        "endpoint_switch_count",
        "payload_switch_count",
        "actions_after_last_material_evidence",
    ]
    output: list[dict[str, Any]] = []
    for pair_id, pair in sorted(by_pair.items()):
        if set(pair) != {"C0", "C1"}:
            continue
        native = pair["C0"]
        ato = pair["C1"]
        row: dict[str, Any] = {
            "pair_id": pair_id,
            "model": native["model"],
            "aou": native["aou"],
            "native_episode_pseudonym": native["episode_pseudonym"],
            "ato_episode_pseudonym": ato["episode_pseudonym"],
            "native_behavior_state": native["behavior_state"],
            "ato_behavior_state": ato["behavior_state"],
            "native_primary_evidence_state": native["primary_evidence_state"],
            "ato_primary_evidence_state": ato["primary_evidence_state"],
            "native_stop_state": native["stop_readiness_state"],
            "ato_stop_state": ato["stop_readiness_state"],
            "native_report_state": native["report_state"],
            "ato_report_state": ato["report_state"],
        }
        for metric in metrics:
            native_value = native[metric]
            ato_value = ato[metric]
            row[f"native_{metric}"] = native_value
            row[f"ato_{metric}"] = ato_value
            row[f"delta_{metric}"] = (
                round(float(ato_value) - float(native_value), 6)
                if native_value != "" and ato_value != ""
                else ""
            )
        output.append(row)
    return output


def percentile(values: list[float], q: float) -> float | str:
    return round(float(np.percentile(values, q)), 6) if values else ""


def build_pair_dynamics_summary(
    pair_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    metrics = [
        "action_count",
        "unique_action_signature_count",
        "repeated_action_count",
        "novel_action_ratio",
        "endpoint_switch_count",
        "payload_switch_count",
        "actions_after_last_material_evidence",
    ]
    output: list[dict[str, Any]] = []
    for aou in AOU_ORDER:
        for model in ["ALL", *MODEL_ORDER]:
            selected = [
                row
                for row in pair_rows
                if row["aou"] == aou
                and (model == "ALL" or row["model"] == model)
            ]
            if not selected:
                continue
            row: dict[str, Any] = {
                "aou": aou,
                "model": model,
                "pair_n": len(selected),
            }
            for metric in metrics:
                values = [
                    float(item[f"delta_{metric}"])
                    for item in selected
                    if item[f"delta_{metric}"] != ""
                ]
                row[f"{metric}_paired_n"] = len(values)
                row[f"{metric}_delta_median"] = (
                    round(statistics.median(values), 6) if values else ""
                )
                row[f"{metric}_delta_q25"] = percentile(values, 25)
                row[f"{metric}_delta_q75"] = percentile(values, 75)
                row[f"{metric}_delta_positive_rate"] = (
                    round(sum(value > 0 for value in values) / len(values), 6)
                    if values
                    else ""
                )
            output.append(row)
    return output


def save_figure(fig: plt.Figure, stem: Path) -> None:
    stem.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(stem.with_suffix(".png"), dpi=300, bbox_inches="tight", facecolor="white")
    fig.savefig(stem.with_suffix(".pdf"), bbox_inches="tight", facecolor="white")
    fig.savefig(stem.with_suffix(".svg"), bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_radar(profiles: list[dict[str, Any]], figure_dir: Path) -> None:
    lookup = {(row["model"], row["condition"]): row for row in profiles}
    theta = np.linspace(0, 2 * np.pi, len(RADAR_AXES), endpoint=False)
    theta_closed = np.r_[theta, theta[0]]
    fig, axes = plt.subplots(
        3, 2, figsize=(10.2, 12.0), subplot_kw={"projection": "polar"}
    )
    native_color = "#9AA4B2"
    for index, model in enumerate(MODEL_ORDER):
        ax = axes.flat[index]
        accent = MODEL_COLORS[model]
        for condition, color, alpha, linewidth in [
            ("C0", native_color, 0.08, 1.8),
            ("C1", accent, 0.13, 2.2),
        ]:
            row = lookup[(model, condition)]
            values = np.array([100 * float(row[key]) for key, _ in RADAR_AXES])
            values_closed = np.r_[values, values[0]]
            ax.plot(theta_closed, values_closed, color=color, linewidth=linewidth)
            ax.fill(theta_closed, values_closed, color=color, alpha=alpha)
            ax.scatter(theta, values, s=20, color=color, zorder=4)
        ax.set_theta_offset(np.pi / 2)
        ax.set_theta_direction(-1)
        ax.set_xticks(theta)
        ax.set_xticklabels([label for _, label in RADAR_AXES], fontsize=8.5)
        ax.set_ylim(0, 100)
        ax.set_yticks([25, 50, 75, 100])
        ax.set_yticklabels(["", "", "", ""])
        ax.grid(color="#D9DDE2", linewidth=0.7)
        ax.spines["polar"].set_color("#BFC5CC")
        ax.set_title(
            MODEL_LABELS[model],
            fontsize=11.8,
            fontweight="bold",
            color=accent,
            pad=10,
        )
    axes.flat[-1].axis("off")
    handles = [
        plt.Line2D([0], [0], color=native_color, marker="o", linewidth=2),
        plt.Line2D([0], [0], color="#263A56", marker="o", linewidth=2),
    ]
    fig.legend(
        handles,
        ["Native", "ATO"],
        ncol=2,
        frameon=False,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.008),
        fontsize=10,
    )
    fig.suptitle(
        "Trajectory capability profiles under Native and ATO conditions",
        fontsize=14.5,
        fontweight="bold",
        y=0.975,
    )
    fig.text(
        0.5,
        0.052,
        "All axes use a common 0–100 scale; exact denominators are reported in source data.",
        ha="center",
        fontsize=8.8,
        color="#555B63",
    )
    fig.subplots_adjust(hspace=0.47, wspace=0.38, top=0.86, bottom=0.10)
    save_figure(fig, figure_dir / "fig1_model_capability_radar_small_multiples")


def plot_grounded_dots(rows: list[dict[str, Any]], figure_dir: Path) -> None:
    lookup = {
        (row["aou"], row["model"], row["condition"]): row for row in rows
    }
    fig, axes = plt.subplots(1, 3, figsize=(11.7, 4.6), sharex=True, sharey=True)
    y = np.arange(len(MODEL_ORDER))[::-1]
    native_color = "#8B96A5"
    ato_color = "#1E4E79"
    for ax, aou in zip(axes, AOU_ORDER):
        native = np.array(
            [
                100 * float(lookup[(aou, model, "C0")]["grounded_rate"])
                for model in MODEL_ORDER
            ]
        )
        ato = np.array(
            [
                100 * float(lookup[(aou, model, "C1")]["grounded_rate"])
                for model in MODEL_ORDER
            ]
        )
        for pos, left, right in zip(y, native, ato):
            ax.plot([left, right], [pos, pos], color="#CBD1D8", linewidth=2, zorder=1)
        ax.scatter(
            native,
            y,
            s=54,
            facecolor="white",
            edgecolor=native_color,
            linewidth=1.8,
            zorder=3,
        )
        ax.scatter(ato, y, s=58, color=ato_color, zorder=4)
        ax.axvline(0, color="#D9DDE2", linewidth=0.8, zorder=0)
        ax.set_title(AOU_LABELS[aou], fontsize=13, fontweight="bold")
        ax.set_xlim(-3, 103)
        ax.set_xticks([0, 25, 50, 75, 100])
        ax.grid(axis="x", color="#E7E9EC", linestyle="--", linewidth=0.7)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.tick_params(axis="y", length=0)
        ax.set_xlabel("Grounded verification (%)", fontsize=9.5)
    axes[0].set_yticks(y)
    axes[0].set_yticklabels([MODEL_LABELS[model] for model in MODEL_ORDER], fontsize=9)
    handles = [
        plt.Line2D(
            [0],
            [0],
            marker="o",
            markerfacecolor="white",
            markeredgecolor=native_color,
            markeredgewidth=1.8,
            linewidth=0,
        ),
        plt.Line2D(
            [0],
            [0],
            marker="o",
            markerfacecolor=ato_color,
            markeredgecolor=ato_color,
            linewidth=0,
        ),
    ]
    fig.legend(
        handles,
        ["Native", "ATO"],
        ncol=2,
        frameon=False,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.02),
    )
    fig.suptitle(
        "Grounded verification by model and AOU",
        fontsize=15,
        fontweight="bold",
        y=1.08,
    )
    fig.subplots_adjust(wspace=0.14, top=0.82, bottom=0.18)
    save_figure(fig, figure_dir / "figs1_grounded_paired_dots")


def ribbon_patch(
    x0: float,
    x1: float,
    y0_low: float,
    y0_high: float,
    y1_low: float,
    y1_high: float,
    color: str,
) -> PathPatch:
    curve = 0.42 * (x1 - x0)
    vertices = [
        (x0, y0_low),
        (x0 + curve, y0_low),
        (x1 - curve, y1_low),
        (x1, y1_low),
        (x1, y1_high),
        (x1 - curve, y1_high),
        (x0 + curve, y0_high),
        (x0, y0_high),
        (x0, y0_low),
    ]
    codes = [
        MplPath.MOVETO,
        MplPath.CURVE4,
        MplPath.CURVE4,
        MplPath.CURVE4,
        MplPath.LINETO,
        MplPath.CURVE4,
        MplPath.CURVE4,
        MplPath.CURVE4,
        MplPath.CLOSEPOLY,
    ]
    return PathPatch(
        MplPath(vertices, codes),
        facecolor=color,
        edgecolor="none",
        alpha=0.34,
        zorder=1,
    )


def plot_flow(flow_rows: list[dict[str, Any]], figure_dir: Path) -> None:
    stage_orders = [BEHAVIOR_ORDER, EVIDENCE_ORDER, STOP_ORDER, REPORT_ORDER]
    stage_keys = ["behavior_state", "evidence_state", "stop_state", "report_state"]
    stage_titles = ["Behavior", "Evidence", "Stop", "Report"]
    short_labels = {
        "adaptive_recovery": "Adaptive\nrecovery",
        "adaptive_unresolved": "Adaptive,\nunresolved",
        "direct_or_nonadaptive_evidence": "Direct/non-\nadaptive proof",
        "no_adaptive_no_primary_evidence": "No adaptive\nproof",
        "positive": "Positive",
        "negative": "Negative",
        "unresolved": "Unresolved",
        "ready_supported": "Ready,\nsupported",
        "ready_not_supported": "Ready, not\nsupported",
        "not_ready": "Not ready",
        "internally_conflicted": "Conflicted",
        "unavailable": "Unavailable",
        "supported_closure": "Supported\nclosure",
        "unsupported_closure": "Unsupported\nclosure",
        "no_closure": "No closure",
    }
    fig, axes = plt.subplots(1, 3, figsize=(15.0, 5.6))
    x_positions = [0.05, 0.36, 0.67, 0.98]
    node_width = 0.045
    max_categories = max(len(order) for order in stage_orders)
    gap = 0.022

    for ax, aou in zip(axes, AOU_ORDER):
        selected = [row for row in flow_rows if row["aou"] == aou]
        total = sum(int(row["episode_n"]) for row in selected)
        scale = (0.88 - gap * (max_categories - 1)) / total
        counts_by_stage: list[Counter[str]] = []
        positions: list[dict[str, tuple[float, float]]] = []
        for key, order in zip(stage_keys, stage_orders):
            counts = Counter()
            for row in selected:
                counts[row[key]] += int(row["episode_n"])
            counts_by_stage.append(counts)
            present = [category for category in order if counts[category] > 0]
            total_height = sum(counts[category] * scale for category in present)
            total_height += gap * max(0, len(present) - 1)
            cursor = (1 - total_height) / 2
            stage_positions: dict[str, tuple[float, float]] = {}
            for category in present:
                low = cursor
                high = low + counts[category] * scale
                stage_positions[category] = (low, high)
                cursor = high + gap
            positions.append(stage_positions)

        ordered_paths = sorted(
            selected,
            key=lambda row: tuple(
                stage_orders[index].index(row[key])
                for index, key in enumerate(stage_keys)
            ),
        )
        for transition in range(3):
            source_offsets = {
                category: low for category, (low, _) in positions[transition].items()
            }
            target_offsets = {
                category: low
                for category, (low, _) in positions[transition + 1].items()
            }
            for row in ordered_paths:
                source = row[stage_keys[transition]]
                target = row[stage_keys[transition + 1]]
                height = int(row["episode_n"]) * scale
                source_low = source_offsets[source]
                target_low = target_offsets[target]
                ax.add_patch(
                    ribbon_patch(
                        x_positions[transition] + node_width,
                        x_positions[transition + 1],
                        source_low,
                        source_low + height,
                        target_low,
                        target_low + height,
                        REPORT_COLORS[row["report_state"]],
                    )
                )
                source_offsets[source] += height
                target_offsets[target] += height

        for stage_index, (order, stage_positions) in enumerate(
            zip(stage_orders, positions)
        ):
            x = x_positions[stage_index]
            for category in order:
                if category not in stage_positions:
                    continue
                low, high = stage_positions[category]
                ax.add_patch(
                    Rectangle(
                        (x, low),
                        node_width,
                        high - low,
                        facecolor="#F5F6F7",
                        edgecolor="#4C5866",
                        linewidth=0.8,
                        zorder=3,
                    )
                )
                label_x = x - 0.012 if stage_index < 2 else x + node_width + 0.012
                ha = "right" if stage_index < 2 else "left"
                ax.text(
                    label_x,
                    (low + high) / 2,
                    short_labels.get(category, category),
                    ha=ha,
                    va="center",
                    fontsize=7.2,
                    linespacing=0.9,
                    zorder=4,
                )
            ax.text(
                x + node_width / 2,
                1.04,
                stage_titles[stage_index],
                ha="center",
                va="bottom",
                fontsize=9.2,
                fontweight="bold",
            )
        ax.set_xlim(-0.18, 1.22)
        ax.set_ylim(0, 1)
        ax.axis("off")
        ax.set_title(AOU_LABELS[aou], fontsize=13, fontweight="bold", pad=28)

    legend_handles = [
        Rectangle((0, 0), 1, 1, facecolor=color, edgecolor="none", alpha=0.5)
        for color in REPORT_COLORS.values()
    ]
    legend_labels = [
        "Supported closure",
        "Unsupported closure",
        "No closure",
        "Unavailable",
    ]
    fig.legend(
        legend_handles,
        legend_labels,
        frameon=False,
        ncol=4,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.005),
        fontsize=9,
    )
    fig.suptitle(
        "ATO trajectories connect verification behavior to evidence, stopping, and reporting",
        fontsize=15,
        fontweight="bold",
        y=0.99,
    )
    fig.subplots_adjust(wspace=0.07, top=0.82, bottom=0.12)
    save_figure(fig, figure_dir / "fig2_ato_behavior_evidence_stop_report_flow")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode-states", required=True, type=Path)
    parser.add_argument("--facts", required=True, type=Path)
    parser.add_argument("--graph-nodes", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    episodes = read_jsonl(args.episode_states)
    facts = read_jsonl(args.facts)
    graph_nodes = read_jsonl(args.graph_nodes)
    output = args.output.resolve()
    source_data = output / "source_data"
    figures = output / "figures"

    episode_rows = build_episode_rows(episodes, facts, graph_nodes)
    capability_rows = build_capability_profiles(episode_rows)
    grounded_rows = build_grounded_rows(episode_rows)
    flow_rows = build_flow_rows(episode_rows)
    pair_dynamics_rows = build_pair_dynamics(episode_rows)
    pair_dynamics_summary = build_pair_dynamics_summary(pair_dynamics_rows)

    write_csv(source_data / "episode_trajectory_diagnostics.csv", episode_rows)
    write_csv(source_data / "model_capability_profiles.csv", capability_rows)
    write_csv(source_data / "model_aou_grounded_rates.csv", grounded_rows)
    write_csv(source_data / "ato_behavior_outcome_paths.csv", flow_rows)
    write_csv(source_data / "paired_trajectory_dynamics.csv", pair_dynamics_rows)
    write_csv(
        source_data / "paired_trajectory_dynamics_summary.csv",
        pair_dynamics_summary,
    )

    plot_radar(capability_rows, figures)
    plot_flow(flow_rows, figures)
    plot_grounded_dots(grounded_rows, figures)

    manifest = {
        "schema_version": "atobench.trajectory_dynamics_stage.v1",
        "status": "exploratory_candidate",
        "episode_count": len(episode_rows),
        "complete_pair_count": len(pair_dynamics_rows),
        "ato_flow_episode_count": sum(
            int(row["episode_n"]) for row in flow_rows
        ),
        "inputs": {
            "episode_states": {
                "path": str(args.episode_states.resolve()),
                "sha256": sha256_file(args.episode_states),
            },
            "facts": {
                "path": str(args.facts.resolve()),
                "sha256": sha256_file(args.facts),
            },
            "graph_nodes": {
                "path": str(args.graph_nodes.resolve()),
                "sha256": sha256_file(args.graph_nodes),
            },
        },
        "outputs": {
            "episode_diagnostics": str(
                (source_data / "episode_trajectory_diagnostics.csv").resolve()
            ),
            "capability_profiles": str(
                (source_data / "model_capability_profiles.csv").resolve()
            ),
            "flow_paths": str(
                (source_data / "ato_behavior_outcome_paths.csv").resolve()
            ),
            "paired_trajectory_dynamics": str(
                (source_data / "paired_trajectory_dynamics.csv").resolve()
            ),
            "paired_trajectory_dynamics_summary": str(
                (source_data / "paired_trajectory_dynamics_summary.csv").resolve()
            ),
        },
    }
    write_json(output / "stage_manifest.json", manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
