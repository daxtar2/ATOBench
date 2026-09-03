from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    read_json,
    read_json_or_yaml,
    require_real_data_authorization,
    sha256_file,
    stage_manifest,
    utc_now,
    write_json,
    write_jsonl,
)
from .schemas import DIMENSIONS
from .stop_descriptors import derive_stop_descriptors

MEASUREMENT_ROLES = {
    "verification_control": "retained_numeric_diagnostic",
    "stop_decision": "descriptor_primary_numeric_provenance",
    "report_grounding": "retained_numeric_score_proximity",
}


def _dimension_state(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "packet_id": row["packet_id"],
        "measurement_role": MEASUREMENT_ROLES[row["dimension"]],
        "final": row["final"],
        "reviewer_scores": row["reviewer_scores"],
        "reviewer_bands": row["reviewer_bands"],
        "reviewer_score_difference": row["reviewer_score_difference"],
        "verifier_statuses": row["verifier_statuses"],
        "adjudication_triggered": row["adjudication_triggered"],
        "adjudication_trigger": row["adjudication_trigger"],
        "invocation_count": row["invocation_count"],
        "runner_warning_count": row["runner_warning_count"],
        "source_bundle": {
            "path": row["bundle_path"],
            "sha256": row["bundle_sha256"],
        },
    }


def _descriptor_for_stop_row(
    row: dict[str, Any],
    packets_root: Path,
) -> dict[str, Any]:
    packet_dir = packets_root / row["relative_path"]
    packet_path = packet_dir / "packet.json"
    facts_path = packet_dir / "packet_evidence" / "facts.json"
    stop_context_path = packet_dir / "packet_evidence" / "stop_context.json"
    for path in (packet_path, facts_path, stop_context_path):
        if not path.is_file():
            raise GateError(f"Stage 11 missing Stop Decision input: {path}")
    packet = read_json(packet_path)
    if (
        packet.get("packet_id") != row["packet_id"]
        or packet.get("episode_pseudonym") != row["episode_pseudonym"]
        or packet.get("dimension") != "stop_decision"
    ):
        raise GateError(
            f"Stage 11 Stop Decision packet identity mismatch: {packet_path}"
        )
    descriptor = derive_stop_descriptors(
        packet,
        read_json(facts_path),
        read_json(stop_context_path),
    )
    descriptor["source_hashes"] = {
        "packet.json": sha256_file(packet_path),
        "facts.json": sha256_file(facts_path),
        "stop_context.json": sha256_file(stop_context_path),
    }
    return descriptor


def run(
    *,
    validated_index_path: Path,
    packets_root: Path,
    output_dir: Path,
    lock_path: Path,
    expected_episodes: int,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    require_real_data_authorization(
        [validated_index_path, packets_root, lock_path],
        allow_real_data,
    )
    lock = read_json_or_yaml(lock_path)
    if lock.get("freeze_status") != "frozen":
        raise GateError("Stage 11 requires freeze_status=frozen")
    if lock.get("offline_result_construction_authorized") is not True:
        raise GateError("Stage 11 requires offline_result_construction_authorized=true")
    if lock.get("judge_calls_authorized") is not False:
        raise GateError("Stage 11 requires judge_calls_authorized=false")

    rows = load_jsonl(validated_index_path)
    if dry_run:
        return {
            "schema_version": "atobench.episode_state_plan.v1",
            "status": "dry_run",
            "validated_index_rows": len(rows),
            "expected_episodes": expected_episodes,
            "model_calls_made": 0,
        }
    ensure_output_available(output_dir, new_version)

    by_episode: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    packet_ids: set[str] = set()
    for row in rows:
        if row.get("schema_version") != "atobench.validated_judgment_index_row.v1":
            raise GateError(
                f"Stage 11 received unexpected index schema: {row.get('schema_version')!r}"
            )
        packet_id = str(row.get("packet_id") or "")
        if packet_id in packet_ids:
            raise GateError(f"Stage 11 duplicate packet_id: {packet_id}")
        packet_ids.add(packet_id)
        by_episode[str(row.get("episode_pseudonym") or "")].append(row)
    if len(by_episode) != expected_episodes:
        raise GateError(
            f"Stage 11 found {len(by_episode)} episodes, expected {expected_episodes}"
        )

    episode_states: list[dict[str, Any]] = []
    stop_descriptors: list[dict[str, Any]] = []
    for episode_pseudonym, episode_rows in sorted(by_episode.items()):
        dimensions = {str(row["dimension"]): row for row in episode_rows}
        if set(dimensions) != DIMENSIONS:
            raise GateError(
                f"Stage 11 episode {episode_pseudonym} dimensions are "
                f"{sorted(dimensions)}, expected {sorted(DIMENSIONS)}"
            )
        aous = {row.get("aou") for row in episode_rows}
        if len(aous) != 1:
            raise GateError(
                f"Stage 11 episode {episode_pseudonym} has inconsistent AOUs: "
                f"{sorted(str(value) for value in aous)}"
            )
        stop_descriptor = _descriptor_for_stop_row(
            dimensions["stop_decision"],
            packets_root,
        )
        stop_descriptors.append(stop_descriptor)
        dimension_states = {
            dimension: _dimension_state(dimensions[dimension])
            for dimension in sorted(DIMENSIONS)
        }
        dimension_states["stop_decision"]["descriptor"] = {
            "schema_version": stop_descriptor["schema_version"],
            "primary_evidence": stop_descriptor["primary_evidence"],
            "stop_context": stop_descriptor["stop_context"],
            "verification_status_counts": stop_descriptor[
                "verification_status_counts"
            ],
            "derived_descriptors": stop_descriptor["derived_descriptors"],
            "summary_metrics": stop_descriptor["summary_metrics"],
            "supporting_fact_ids": stop_descriptor["supporting_fact_ids"],
            "source_hashes": stop_descriptor["source_hashes"],
        }
        dimension_states["stop_decision"]["numeric_interpretation"] = (
            "engineering_provenance_only"
        )
        dimension_states["verification_control"]["numeric_interpretation"] = (
            "retained_diagnostic"
        )
        dimension_states["report_grounding"]["numeric_interpretation"] = (
            "retained_with_score_proximity_reliability"
        )
        episode_states.append(
            {
                "schema_version": "atobench.episode_state.v1",
                "episode_pseudonym": episode_pseudonym,
                "aou": next(iter(aous)),
                "dimensions": dimension_states,
                "source_packet_ids": {
                    dimension: dimensions[dimension]["packet_id"]
                    for dimension in sorted(DIMENSIONS)
                },
                "complete": True,
                "identity_blinded": True,
                "contains_model_condition_or_pair_identity": False,
            }
        )

    episode_states_path = output_dir / "episode_states.jsonl"
    descriptors_path = output_dir / "stop_decision_descriptors.jsonl"
    summary_path = output_dir / "episode_state_summary.json"
    write_jsonl(episode_states_path, episode_states)
    write_jsonl(descriptors_path, stop_descriptors)
    readiness_counts = collections.Counter(
        row["derived_descriptors"]["readiness_state"]
        for row in stop_descriptors
    )
    stop_fit_counts = collections.Counter(
        row["derived_descriptors"]["stop_fit"]
        for row in stop_descriptors
    )
    aou_counts = collections.Counter(row["aou"] for row in episode_states)
    report = {
        "schema_version": "atobench.episode_state_summary.v1",
        "status": "PASS",
        "created_at": utc_now(),
        "episode_count": len(episode_states),
        "dimension_row_count": len(rows),
        "stop_descriptor_count": len(stop_descriptors),
        "aou_counts": dict(sorted(aou_counts.items())),
        "readiness_state_counts": dict(sorted(readiness_counts.items())),
        "stop_fit_counts": dict(sorted(stop_fit_counts.items())),
        "identity_blinded": True,
        "model_calls_made": 0,
        "network_accessed": False,
        "comparative_analysis_performed": False,
        "paper_facing_analysis_performed": False,
    }
    write_json(summary_path, report)
    inputs = [validated_index_path, lock_path]
    packet_stage_manifest = packets_root / "stage_manifest.json"
    if packet_stage_manifest.is_file():
        inputs.append(packet_stage_manifest)
    manifest = stage_manifest(
        "11_build_episode_states",
        inputs,
        [episode_states_path, descriptors_path, summary_path],
        "complete",
        dry_run=False,
        details=report,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return report


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build outcome-blind episode states from validated judgments"
    )
    parser.add_argument("--validated-index", type=Path, required=True)
    parser.add_argument("--packets-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--expected-episodes", type=int, default=430)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(
            validated_index_path=args.validated_index,
            packets_root=args.packets_root,
            output_dir=args.output,
            lock_path=args.lock,
            expected_episodes=args.expected_episodes,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
