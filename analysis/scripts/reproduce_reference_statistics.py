#!/usr/bin/env python3
"""Rebuild the frozen Stage 20 statistics and compare scientific outputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.common import GateError, sha256_file
from atobench_vr.resilience_statistics import run as run_statistics
from atobench_vr.statistics_freeze import run as freeze_statistics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args()

    cohort = ROOT / "reference" / "latest" / "cohort"
    lock = ROOT / "reference" / "latest" / "analysis_spec.lock.json"
    reference = ROOT / "reference" / "latest" / "results" / "statistics"
    audit_path = ROOT / "reference" / "latest" / "audits" / "statistics_audit.json"
    freeze_dir = args.output / "01_statistics_freeze"
    statistics_dir = args.output / "02_statistics"

    try:
        freeze_statistics(
            pair_profiles_path=cohort / "pair_resilience_profiles.jsonl",
            episode_states_path=cohort / "unblinded_episode_states.jsonl",
            fact_registry_path=cohort / "episode_fact_registry.jsonl",
            output_dir=freeze_dir,
            lock_path=lock,
            allow_real_data=True,
            dry_run=False,
            new_version=args.new_version,
        )
        run_statistics(
            pair_profiles_path=cohort / "pair_resilience_profiles.jsonl",
            episode_states_path=cohort / "unblinded_episode_states.jsonl",
            membership_path=freeze_dir / "pair_analysis_population_membership.jsonl",
            population_registry_path=freeze_dir / "analysis_population_registry.json",
            output_dir=statistics_dir,
            lock_path=lock,
            allow_real_data=True,
            dry_run=False,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")

    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    expected = audit["reproducibility"]["output_sha256"]
    comparisons = {}
    for name, expected_hash in expected.items():
        generated = statistics_dir / name
        reference_path = reference / name
        actual_hash = sha256_file(generated)
        comparisons[name] = {
            "generated_sha256": actual_hash,
            "reference_sha256": sha256_file(reference_path),
            "audited_sha256": expected_hash,
            "match": actual_hash == expected_hash == sha256_file(reference_path),
        }
    status = "PASS" if all(row["match"] for row in comparisons.values()) else "FAIL"
    report = {
        "schema_version": "atobench.reference_reproduction.v1",
        "status": status,
        "scientific_output_count": len(comparisons),
        "comparisons": comparisons,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "reproduction_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
