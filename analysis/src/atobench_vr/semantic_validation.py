from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    read_json,
    require_real_data_authorization,
    sha256_file,
    stage_manifest,
    write_json,
    write_jsonl,
)
from .semantic_output import validate_semantic_match


def run(
    *,
    semantic_manifest_path: Path,
    packets_root: Path,
    matches_root: Path,
    output_dir: Path,
    allowlist_path: Path | None,
    expected_episodes: int,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    inputs = [semantic_manifest_path, packets_root, matches_root]
    if allowlist_path is not None:
        inputs.append(allowlist_path)
    require_real_data_authorization(inputs, allow_real_data)
    rows = read_json(semantic_manifest_path).get("packets", [])
    if allowlist_path is not None:
        allowed = set(
            read_json(allowlist_path).get("semantic_packet_ids") or []
        )
        rows = [row for row in rows if row.get("semantic_packet_id") in allowed]
        if len(rows) != len(allowed):
            raise GateError("semantic validation allowlist does not match manifest")
    if len(rows) != expected_episodes:
        raise GateError(
            f"semantic validation selected {len(rows)} episodes, "
            f"expected {expected_episodes}"
        )
    if dry_run:
        return {
            "schema_version": "atobench.semantic_validation_plan.v1",
            "status": "dry_run",
            "selected_episode_count": len(rows),
            "model_calls_made": 0,
        }
    ensure_output_available(output_dir, new_version)
    violations: list[dict[str, Any]] = []
    validated: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: item["semantic_packet_id"]):
        semantic_packet_id = str(row["semantic_packet_id"])
        packet_path = packets_root / str(row["relative_path"]) / "packet.json"
        bundle_path = matches_root / semantic_packet_id / "semantic_match_bundle.json"
        if not packet_path.is_file() or not bundle_path.is_file():
            violations.append(
                {
                    "semantic_packet_id": semantic_packet_id,
                    "code": "missing_packet_or_bundle",
                }
            )
            continue
        packet = read_json(packet_path)
        bundle = read_json(bundle_path)
        if (
            bundle.get("semantic_packet_id") != semantic_packet_id
            or bundle.get("episode_pseudonym") != packet.get("episode_pseudonym")
            or bundle.get("aou") != packet.get("aou")
        ):
            violations.append(
                {
                    "semantic_packet_id": semantic_packet_id,
                    "code": "bundle_identity_mismatch",
                }
            )
            continue
        final = bundle.get("final")
        errors = (
            validate_semantic_match(final, packet)
            if isinstance(final, dict)
            else ["final is not an object"]
        )
        if errors:
            violations.append(
                {
                    "semantic_packet_id": semantic_packet_id,
                    "code": "invalid_final_semantic_match",
                    "errors": errors,
                }
            )
            continue
        for matcher_id, matcher in (bundle.get("matcher_outputs") or {}).items():
            matcher_errors = validate_semantic_match(matcher, packet)
            if matcher_errors:
                violations.append(
                    {
                        "semantic_packet_id": semantic_packet_id,
                        "code": "invalid_matcher_output",
                        "matcher_id": matcher_id,
                        "errors": matcher_errors,
                    }
                )
        validated.append(
            {
                "schema_version": "atobench.validated_report_semantic_match.v1",
                "semantic_packet_id": semantic_packet_id,
                "episode_pseudonym": packet["episode_pseudonym"],
                "aou": packet["aou"],
                "source_report_packet_id": packet["source_report_packet_id"],
                "registered_primary_fact_id": packet["registered_primary_fact_id"],
                "registered_primary_fact_type": packet[
                    "registered_primary_fact_type"
                ],
                "final": final,
                "adjudication_triggered": bundle.get("adjudication_triggered"),
                "bundle_path": str(bundle_path.resolve()),
                "bundle_sha256": sha256_file(bundle_path),
            }
        )
    index_path = output_dir / "validated_semantic_matches.jsonl"
    violations_path = output_dir / "semantic_validation_violations.jsonl"
    summary_path = output_dir / "semantic_validation_summary.json"
    write_jsonl(index_path, validated)
    write_jsonl(violations_path, violations)
    final_counts = collections.Counter(
        (
            "insufficient"
            if row["final"]["insufficient_evidence"]
            else f"closure_{str(row['final']['report_closure']).lower()}_"
            f"{row['final']['claim_trace_support']}"
        )
        for row in validated
    )
    summary = {
        "schema_version": "atobench.semantic_validation_summary.v1",
        "status": "PASS" if not violations and len(validated) == len(rows) else "FAIL",
        "selected_episode_count": len(rows),
        "validated_episode_count": len(validated),
        "violation_count": len(violations),
        "adjudication_trigger_count": sum(
            row["adjudication_triggered"] is True for row in validated
        ),
        "final_semantic_counts": dict(sorted(final_counts.items())),
        "model_calls_made": 0,
        "network_accessed": False,
    }
    write_json(summary_path, summary)
    manifest = stage_manifest(
        "12f_validate_report_semantic_matches",
        inputs,
        [index_path, violations_path, summary_path],
        "complete" if summary["status"] == "PASS" else "blocked",
        dry_run=False,
        details=summary,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    if summary["status"] != "PASS":
        raise GateError(
            f"semantic validation failed with {len(violations)} violations"
        )
    return summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--semantic-manifest", type=Path, required=True)
    parser.add_argument("--packets-root", type=Path, required=True)
    parser.add_argument("--matches-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-allowlist", type=Path)
    parser.add_argument("--expected-episodes", type=int, required=True)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(
            semantic_manifest_path=args.semantic_manifest,
            packets_root=args.packets_root,
            matches_root=args.matches_root,
            output_dir=args.output,
            allowlist_path=args.task_allowlist,
            expected_episodes=args.expected_episodes,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
