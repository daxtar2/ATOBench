#!/usr/bin/env python3
"""Validate trajectory-dynamics source tables and figure outputs."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage-dir", required=True, type=Path)
    parser.add_argument("--expected-episodes", type=int, default=430)
    parser.add_argument("--expected-pairs", type=int, default=215)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stage = args.stage_dir.resolve()
    data = stage / "source_data"
    episodes = read_csv(data / "episode_trajectory_diagnostics.csv")
    profiles = read_csv(data / "model_capability_profiles.csv")
    flows = read_csv(data / "ato_behavior_outcome_paths.csv")
    pairs = read_csv(data / "paired_trajectory_dynamics.csv")

    violations: list[str] = []
    if len(episodes) != args.expected_episodes:
        violations.append(
            f"expected {args.expected_episodes} episodes, observed {len(episodes)}"
        )
    if len({row["episode_id"] for row in episodes}) != len(episodes):
        violations.append("episode_id is not unique")
    expected_conditions = {"C0": args.expected_pairs, "C1": args.expected_pairs}
    if Counter(row["condition"] for row in episodes) != expected_conditions:
        violations.append(
            f"condition counts are not {args.expected_pairs} Native / "
            f"{args.expected_pairs} ATO"
        )
    if (
        len(pairs) != args.expected_pairs
        or len({row["pair_id"] for row in pairs}) != args.expected_pairs
    ):
        violations.append(
            f"paired trajectory table is not {args.expected_pairs} unique pairs"
        )
    if sum(int(row["episode_n"]) for row in flows) != args.expected_pairs:
        violations.append(
            f"ATO flow paths do not partition all {args.expected_pairs} ATO episodes"
        )
    flow_by_aou = Counter()
    for row in flows:
        flow_by_aou[row["aou"]] += int(row["episode_n"])
    expected_flow = Counter(
        row["aou"] for row in episodes if row["condition"] == "C1"
    )
    if dict(flow_by_aou) != expected_flow:
        violations.append(
            f"ATO flow AOU totals mismatch: {dict(flow_by_aou)} != {expected_flow}"
        )
    if len(profiles) != 10:
        violations.append(f"expected 10 model profiles, observed {len(profiles)}")
    rate_fields = [
        "primary_evidence_rate",
        "adaptive_verification_rate",
        "verification_control_rate",
        "supported_stop_rate",
        "supported_report_rate",
    ]
    for row in profiles:
        if row["aggregation"] != "macro_average_across_aou":
            violations.append("model profile is not AOU-macro-averaged")
        for field in rate_fields:
            value = float(row[field])
            if not 0 <= value <= 1:
                violations.append(f"{row['model']} {row['condition']} {field}={value}")

    figure_stems = [
        "fig1_model_capability_radar_small_multiples",
        "fig2_ato_behavior_evidence_stop_report_flow",
        "figs1_grounded_paired_dots",
    ]
    missing_figures: list[str] = []
    for stem in figure_stems:
        for suffix in [".png", ".pdf", ".svg"]:
            path = stage / "figures" / f"{stem}{suffix}"
            if not path.exists() or path.stat().st_size == 0:
                missing_figures.append(str(path))
    if missing_figures:
        violations.append(f"missing figure outputs: {missing_figures}")

    report = {
        "schema_version": "atobench.trajectory_dynamics_validation.v1",
        "status": "PASS" if not violations else "FAIL",
        "episode_count": len(episodes),
        "pair_count": len(pairs),
        "ato_flow_episode_count": sum(int(row["episode_n"]) for row in flows),
        "profile_count": len(profiles),
        "violation_count": len(violations),
        "violations": violations,
    }
    output = stage / "validation_report.json"
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if violations:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
