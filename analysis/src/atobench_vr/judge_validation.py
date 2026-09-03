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
    read_json_or_yaml,
    require_real_data_authorization,
    sha256_file,
    stage_manifest,
    utc_now,
    write_json,
    write_jsonl,
)
from .judge_output import validate_verifier_partition
from .schemas import BANDS, DIMENSIONS, validate_judgment

REVIEWER_IDS = {"trajectory_judge_a", "trajectory_judge_b"}
VERIFIER_STATUSES = {
    "entailed",
    "partially_entailed",
    "not_entailed",
    "unavailable",
}
FINAL_METHODS = {
    "reviewer_consensus",
    "reviewer_aggregate_without_adjudication",
    "recorded_without_adjudication",
    "adjudicated",
    "adjudicated_after_evidence_replay",
}


def _violation(
    code: str,
    message: str,
    *,
    path: Path | None = None,
    packet_id: str | None = None,
    dimension: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "path": str(path.resolve()) if path is not None else None,
        "packet_id": packet_id,
        "dimension": dimension,
    }


def _band_for_score(score: float) -> str:
    if score <= 3:
        return "poor"
    if score <= 6:
        return "limited"
    if score <= 8:
        return "good"
    return "excellent"


def _pointer_ids(judgment: dict[str, Any]) -> list[str]:
    return [
        *judgment.get("supporting_message_ids", []),
        *judgment.get("supporting_event_ids", []),
        *judgment.get("supporting_fact_ids", []),
    ]


def _validate_final(
    final: Any,
    *,
    path: Path,
    packet_id: str,
    dimension: str,
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    required = {
        "method",
        "score",
        "score_low",
        "score_high",
        "band",
        "insufficient_evidence",
    }
    if not isinstance(final, dict):
        return [
            _violation(
                "final_not_object",
                "bundle final field is not an object",
                path=path,
                packet_id=packet_id,
                dimension=dimension,
            )
        ]
    missing = sorted(required - set(final))
    if missing:
        violations.append(
            _violation(
                "final_missing_fields",
                f"final missing fields: {missing}",
                path=path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
        return violations
    if final["method"] not in FINAL_METHODS:
        violations.append(
            _violation(
                "final_invalid_method",
                f"unsupported final method: {final['method']!r}",
                path=path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
    insufficient = final["insufficient_evidence"]
    if not isinstance(insufficient, bool):
        violations.append(
            _violation(
                "final_invalid_insufficient_evidence",
                "final insufficient_evidence is not boolean",
                path=path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
        return violations
    if insufficient:
        non_null = [
            key
            for key in ("score", "score_low", "score_high", "band")
            if final.get(key) is not None
        ]
        if non_null:
            violations.append(
                _violation(
                    "final_insufficient_non_null",
                    f"insufficient final has non-null fields: {non_null}",
                    path=path,
                    packet_id=packet_id,
                    dimension=dimension,
                )
            )
        return violations
    score = final.get("score")
    low = final.get("score_low")
    high = final.get("score_high")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not 1 <= score <= 10:
        violations.append(
            _violation(
                "final_invalid_score",
                f"final score is not numeric 1..10: {score!r}",
                path=path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
        return violations
    if (
        not isinstance(low, int)
        or isinstance(low, bool)
        or not isinstance(high, int)
        or isinstance(high, bool)
        or not 1 <= low <= high <= 10
        or not low <= score <= high
    ):
        violations.append(
            _violation(
                "final_invalid_range",
                f"invalid final range: score={score!r}, low={low!r}, high={high!r}",
                path=path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
    expected_band = _band_for_score(float(score))
    if final.get("band") != expected_band:
        violations.append(
            _violation(
                "final_band_mismatch",
                f"final band {final.get('band')!r} does not match score {score!r}",
                path=path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
    return violations


def _consensus(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    a, b = reviews
    if a["insufficient_evidence"] and b["insufficient_evidence"]:
        return {
            "method": "reviewer_consensus",
            "score": None,
            "score_low": None,
            "score_high": None,
            "band": None,
            "insufficient_evidence": True,
        }
    scores = [int(a["score"]), int(b["score"])]
    score = sum(scores) / 2
    return {
        "method": "reviewer_consensus",
        "score": score,
        "score_low": min(int(a["score_low"]), int(b["score_low"])),
        "score_high": max(int(a["score_high"]), int(b["score_high"])),
        "band": _band_for_score(score),
        "insufficient_evidence": False,
    }


def _same_final(a: dict[str, Any], b: dict[str, Any]) -> bool:
    keys = {
        "method",
        "score",
        "score_low",
        "score_high",
        "band",
        "insufficient_evidence",
    }
    return all(a.get(key) == b.get(key) for key in keys)


def _validate_one(
    *,
    manifest_row: dict[str, Any],
    packets_root: Path,
    judgments_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    violations: list[dict[str, Any]] = []
    relative_path = Path(str(manifest_row.get("relative_path") or ""))
    packet_dir = packets_root / relative_path
    judgment_dir = judgments_root / relative_path
    packet_path = packet_dir / "packet.json"
    bundle_path = judgment_dir / "judgment_bundle.json"
    manifest_packet_id = str(manifest_row.get("packet_id") or "")
    manifest_dimension = str(manifest_row.get("dimension") or "")

    if not packet_path.is_file():
        return [
            _violation(
                "missing_packet",
                "packet.json is missing",
                path=packet_path,
                packet_id=manifest_packet_id,
                dimension=manifest_dimension,
            )
        ], None
    if not bundle_path.is_file():
        return [
            _violation(
                "missing_bundle",
                "judgment_bundle.json is missing",
                path=bundle_path,
                packet_id=manifest_packet_id,
                dimension=manifest_dimension,
            )
        ], None

    try:
        packet = read_json(packet_path)
        bundle = read_json(bundle_path)
    except (json.JSONDecodeError, OSError) as exc:
        return [
            _violation(
                "invalid_json",
                str(exc),
                path=bundle_path,
                packet_id=manifest_packet_id,
                dimension=manifest_dimension,
            )
        ], None

    packet_id = str(packet.get("packet_id") or "")
    dimension = str(packet.get("dimension") or "")
    episode = str(packet.get("episode_pseudonym") or "")
    if packet_id != manifest_packet_id:
        violations.append(
            _violation(
                "manifest_packet_identity_mismatch",
                f"manifest packet_id {manifest_packet_id!r} != packet {packet_id!r}",
                path=packet_path,
                packet_id=manifest_packet_id,
                dimension=manifest_dimension,
            )
        )
    if dimension != manifest_dimension or dimension not in DIMENSIONS:
        violations.append(
            _violation(
                "manifest_dimension_mismatch",
                f"manifest dimension {manifest_dimension!r} != packet {dimension!r}",
                path=packet_path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
    if not episode:
        violations.append(
            _violation(
                "missing_episode_pseudonym",
                "packet has no episode_pseudonym",
                path=packet_path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
    if (
        bundle.get("schema_version")
        != "atobench.trajectory_judgment_bundle.v1"
    ):
        violations.append(
            _violation(
                "bundle_schema_version",
                f"unexpected bundle schema: {bundle.get('schema_version')!r}",
                path=bundle_path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
    if bundle.get("packet_id") != packet_id or bundle.get("dimension") != dimension:
        violations.append(
            _violation(
                "bundle_identity_mismatch",
                "bundle packet_id or dimension does not match packet",
                path=bundle_path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )

    allowed_pointers = {
        *packet.get("input_message_ids", []),
        *packet.get("input_event_ids", []),
        *packet.get("input_fact_ids", []),
    }
    reviewer_outputs = bundle.get("reviewer_outputs")
    if not isinstance(reviewer_outputs, dict) or set(reviewer_outputs) != REVIEWER_IDS:
        violations.append(
            _violation(
                "reviewer_set_mismatch",
                f"reviewer ids are {sorted(reviewer_outputs) if isinstance(reviewer_outputs, dict) else reviewer_outputs!r}",
                path=bundle_path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
        reviewer_outputs = {}
    for reviewer_id, review in reviewer_outputs.items():
        if not isinstance(review, dict):
            violations.append(
                _violation(
                    "review_not_object",
                    f"{reviewer_id} review is not an object",
                    path=bundle_path,
                    packet_id=packet_id,
                    dimension=dimension,
                )
            )
            continue
        for error in validate_judgment(review):
            violations.append(
                _violation(
                    "invalid_review",
                    f"{reviewer_id}: {error}",
                    path=bundle_path,
                    packet_id=packet_id,
                    dimension=dimension,
                )
            )
        if review.get("packet_id") != packet_id or review.get("dimension") != dimension:
            violations.append(
                _violation(
                    "review_identity_mismatch",
                    f"{reviewer_id} identity does not match packet",
                    path=bundle_path,
                    packet_id=packet_id,
                    dimension=dimension,
                )
            )
        unexpected = sorted(set(_pointer_ids(review)) - allowed_pointers)
        if unexpected:
            violations.append(
                _violation(
                    "review_pointer_outside_packet",
                    f"{reviewer_id} cites pointers outside packet: {unexpected}",
                    path=bundle_path,
                    packet_id=packet_id,
                    dimension=dimension,
                )
            )

    verifier_outputs = bundle.get("evidence_verifier_outputs")
    if not isinstance(verifier_outputs, dict) or set(verifier_outputs) != REVIEWER_IDS:
        violations.append(
            _violation(
                "verifier_set_mismatch",
                f"verifier ids are {sorted(verifier_outputs) if isinstance(verifier_outputs, dict) else verifier_outputs!r}",
                path=bundle_path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
        verifier_outputs = {}
    for reviewer_id, verifier in verifier_outputs.items():
        if not isinstance(verifier, dict):
            violations.append(
                _violation(
                    "verifier_not_object",
                    f"{reviewer_id} verifier output is not an object",
                    path=bundle_path,
                    packet_id=packet_id,
                    dimension=dimension,
                )
            )
            continue
        if verifier.get("status") not in VERIFIER_STATUSES:
            violations.append(
                _violation(
                    "invalid_verifier_status",
                    f"{reviewer_id} status is {verifier.get('status')!r}",
                    path=bundle_path,
                    packet_id=packet_id,
                    dimension=dimension,
                )
            )
        expected = _pointer_ids(reviewer_outputs.get(reviewer_id, {}))
        for warning in validate_verifier_partition(verifier, expected):
            violations.append(
                _violation(
                    "invalid_verifier_partition",
                    f"{reviewer_id}: {warning}",
                    path=bundle_path,
                    packet_id=packet_id,
                    dimension=dimension,
                )
            )
        for key in ("verified_pointer_ids", "missing_pointer_ids"):
            values = verifier.get(key)
            if isinstance(values, list) and len(values) != len(set(values)):
                violations.append(
                    _violation(
                        "duplicate_verifier_pointer",
                        f"{reviewer_id} {key} contains duplicates",
                        path=bundle_path,
                        packet_id=packet_id,
                        dimension=dimension,
                    )
                )

    final = bundle.get("final")
    violations.extend(
        _validate_final(
            final,
            path=bundle_path,
            packet_id=packet_id,
            dimension=dimension,
        )
    )
    adjudication_triggered = bundle.get("adjudication_triggered")
    adjudication = bundle.get("adjudication_output")
    if adjudication_triggered is True:
        if not isinstance(adjudication, dict):
            violations.append(
                _violation(
                    "missing_adjudication",
                    "adjudication_triggered=true but adjudication_output is absent",
                    path=bundle_path,
                    packet_id=packet_id,
                    dimension=dimension,
                )
            )
        else:
            for error in validate_judgment(adjudication):
                violations.append(
                    _violation(
                        "invalid_adjudication",
                        error,
                        path=bundle_path,
                        packet_id=packet_id,
                        dimension=dimension,
                    )
                )
            if adjudication.get("packet_id") != packet_id or adjudication.get("dimension") != dimension:
                violations.append(
                    _violation(
                        "adjudication_identity_mismatch",
                        "adjudication identity does not match packet",
                        path=bundle_path,
                        packet_id=packet_id,
                        dimension=dimension,
                    )
                )
            if isinstance(final, dict):
                expected_final = {
                    "method": final.get("method"),
                    "score": adjudication.get("score"),
                    "score_low": adjudication.get("score_low"),
                    "score_high": adjudication.get("score_high"),
                    "band": adjudication.get("band"),
                    "insufficient_evidence": adjudication.get("insufficient_evidence"),
                }
                if final.get("method") not in {
                    "adjudicated",
                    "adjudicated_after_evidence_replay",
                } or not _same_final(final, expected_final):
                    violations.append(
                        _violation(
                            "adjudicated_final_mismatch",
                            "final does not match adjudication output",
                            path=bundle_path,
                            packet_id=packet_id,
                            dimension=dimension,
                        )
                    )
    elif adjudication_triggered is False:
        if adjudication is not None:
            violations.append(
                _violation(
                    "unexpected_adjudication_output",
                    "adjudication_triggered=false but adjudication_output is present",
                    path=bundle_path,
                    packet_id=packet_id,
                    dimension=dimension,
                )
            )
        if (
            isinstance(final, dict)
            and final.get("method") == "reviewer_consensus"
            and len(reviewer_outputs) == 2
        ):
            expected_final = _consensus(list(reviewer_outputs.values()))
            if not _same_final(final, expected_final):
                violations.append(
                    _violation(
                        "consensus_final_mismatch",
                        "final does not match deterministic reviewer consensus",
                        path=bundle_path,
                        packet_id=packet_id,
                        dimension=dimension,
                    )
                )
    else:
        violations.append(
            _violation(
                "invalid_adjudication_triggered",
                "adjudication_triggered is not boolean",
                path=bundle_path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )

    receipts = sorted((judgment_dir / "receipts").glob("*.json"))
    invocation_count = bundle.get("invocation_count")
    if not isinstance(invocation_count, int) or invocation_count != len(receipts):
        violations.append(
            _violation(
                "receipt_count_mismatch",
                f"bundle invocation_count={invocation_count!r}, receipt files={len(receipts)}",
                path=bundle_path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
    stage_path = judgment_dir / "stage_manifest.json"
    if not stage_path.is_file() or read_json(stage_path).get("status") != "complete":
        violations.append(
            _violation(
                "packet_stage_incomplete",
                "packet stage_manifest.json is missing or not complete",
                path=stage_path,
                packet_id=packet_id,
                dimension=dimension,
            )
        )
    if (judgment_dir / "RUN_INCOMPLETE.json").exists():
        violations.append(
            _violation(
                "active_incomplete_marker",
                "finalized packet directory still contains RUN_INCOMPLETE.json",
                path=judgment_dir / "RUN_INCOMPLETE.json",
                packet_id=packet_id,
                dimension=dimension,
            )
        )

    if violations:
        return violations, None
    reviews = list(reviewer_outputs.values())
    index_row = {
        "schema_version": "atobench.validated_judgment_index_row.v1",
        "packet_id": packet_id,
        "episode_pseudonym": episode,
        "aou": packet.get("aou"),
        "dimension": dimension,
        "relative_path": str(relative_path),
        "bundle_path": str(bundle_path.resolve()),
        "bundle_sha256": sha256_file(bundle_path),
        "final": final,
        "reviewer_scores": {
            reviewer_id: review.get("score")
            for reviewer_id, review in reviewer_outputs.items()
        },
        "reviewer_bands": {
            reviewer_id: review.get("band")
            for reviewer_id, review in reviewer_outputs.items()
        },
        "reviewer_score_difference": (
            abs(int(reviews[0]["score"]) - int(reviews[1]["score"]))
            if len(reviews) == 2
            and all(review.get("score") is not None for review in reviews)
            else None
        ),
        "verifier_statuses": {
            reviewer_id: verifier.get("status")
            for reviewer_id, verifier in verifier_outputs.items()
        },
        "adjudication_triggered": adjudication_triggered,
        "adjudication_trigger": bundle.get("adjudication_trigger"),
        "invocation_count": invocation_count,
        "runner_warning_count": len(bundle.get("runner_warnings") or []),
    }
    return [], index_row


def run(
    *,
    judgments_root: Path,
    packet_manifest_path: Path,
    packets_root: Path,
    output_dir: Path,
    lock_path: Path,
    expected_packets: int,
    expected_episodes: int,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    require_real_data_authorization(
        [judgments_root, packet_manifest_path, packets_root, lock_path],
        allow_real_data,
    )
    lock = read_json_or_yaml(lock_path)
    if lock.get("freeze_status") != "frozen":
        raise GateError("Stage 10 requires freeze_status=frozen")
    if lock.get("offline_result_construction_authorized") is not True:
        raise GateError("Stage 10 requires offline_result_construction_authorized=true")
    if lock.get("judge_calls_authorized") is not False:
        raise GateError("Stage 10 requires judge_calls_authorized=false")

    packet_manifest = read_json(packet_manifest_path)
    rows = list(packet_manifest.get("packets") or [])
    if dry_run:
        return {
            "schema_version": "atobench.judge_validation_plan.v1",
            "status": "dry_run",
            "manifest_packet_count": len(rows),
            "expected_packets": expected_packets,
            "expected_episodes": expected_episodes,
            "model_calls_made": 0,
        }
    ensure_output_available(output_dir, new_version)
    violations: list[dict[str, Any]] = []
    index_rows: list[dict[str, Any]] = []

    if len(rows) != expected_packets:
        violations.append(
            _violation(
                "manifest_packet_count",
                f"manifest has {len(rows)} packets, expected {expected_packets}",
                path=packet_manifest_path,
            )
        )
    packet_ids = [str(row.get("packet_id") or "") for row in rows]
    duplicates = sorted(
        packet_id
        for packet_id, count in collections.Counter(packet_ids).items()
        if count > 1
    )
    if duplicates:
        violations.append(
            _violation(
                "duplicate_manifest_packet_ids",
                f"duplicate packet ids: {duplicates}",
                path=packet_manifest_path,
            )
        )

    for row in rows:
        row_violations, index_row = _validate_one(
            manifest_row=row,
            packets_root=packets_root,
            judgments_root=judgments_root,
        )
        violations.extend(row_violations)
        if index_row is not None:
            index_rows.append(index_row)

    expected_bundle_paths = {
        (judgments_root / str(row.get("relative_path")) / "judgment_bundle.json").resolve()
        for row in rows
    }
    observed_bundle_paths = {
        path.resolve()
        for path in judgments_root.rglob("judgment_bundle.json")
        if "_archived_partials" not in path.parts
    }
    extras = sorted(str(path) for path in observed_bundle_paths - expected_bundle_paths)
    if extras:
        violations.append(
            _violation(
                "orphan_judgment_bundles",
                f"judgment bundles not present in packet manifest: {extras}",
                path=judgments_root,
            )
        )

    dimension_counts = collections.Counter(row["dimension"] for row in index_rows)
    episodes: dict[str, set[str]] = collections.defaultdict(set)
    for row in index_rows:
        episodes[row["episode_pseudonym"]].add(row["dimension"])
    if len(episodes) != expected_episodes:
        violations.append(
            _violation(
                "episode_count",
                f"validated index has {len(episodes)} episodes, expected {expected_episodes}",
                path=judgments_root,
            )
        )
    incomplete_episodes = {
        episode: sorted(DIMENSIONS - dimensions)
        for episode, dimensions in episodes.items()
        if dimensions != DIMENSIONS
    }
    if incomplete_episodes:
        violations.append(
            _violation(
                "incomplete_episode_dimensions",
                f"episodes missing dimensions: {incomplete_episodes}",
                path=judgments_root,
            )
        )
    expected_dimension_counts = packet_manifest.get("dimension_counts") or {}
    for dimension in sorted(DIMENSIONS):
        expected = int(expected_dimension_counts.get(dimension, expected_episodes))
        if dimension_counts[dimension] != expected:
            violations.append(
                _violation(
                    "dimension_count",
                    f"{dimension} has {dimension_counts[dimension]} validated bundles, expected {expected}",
                    path=judgments_root,
                    dimension=dimension,
                )
            )

    active_markers = [
        path
        for path in judgments_root.rglob("RUN_INCOMPLETE.json")
        if "_archived_partials" not in path.parts
    ]
    archived_markers = [
        path
        for path in judgments_root.rglob("RUN_INCOMPLETE.json")
        if "_archived_partials" in path.parts
    ]
    if active_markers:
        violations.append(
            _violation(
                "active_incomplete_markers",
                f"active incomplete markers: {[str(path) for path in active_markers]}",
                path=judgments_root,
            )
        )

    index_path = output_dir / "validated_judgment_index.jsonl"
    violations_path = output_dir / "validation_violations.jsonl"
    report_path = output_dir / "judge_validation_report.json"
    write_jsonl(index_path, sorted(index_rows, key=lambda row: (row["episode_pseudonym"], row["dimension"])))
    write_jsonl(violations_path, violations)
    status = "PASS" if not violations else "FAIL"
    report = {
        "schema_version": "atobench.judge_validation_report.v1",
        "status": status,
        "created_at": utc_now(),
        "judgments_root": str(judgments_root.resolve()),
        "packet_manifest_path": str(packet_manifest_path.resolve()),
        "expected_packet_count": expected_packets,
        "manifest_packet_count": len(rows),
        "validated_bundle_count": len(index_rows),
        "expected_episode_count": expected_episodes,
        "validated_episode_count": len(episodes),
        "dimension_counts": dict(sorted(dimension_counts.items())),
        "violation_count": len(violations),
        "active_incomplete_marker_count": len(active_markers),
        "archived_incomplete_marker_count": len(archived_markers),
        "model_calls_made": 0,
        "network_accessed": False,
        "paper_facing_analysis_performed": False,
    }
    write_json(report_path, report)
    manifest = stage_manifest(
        "10_validate_judges",
        [packet_manifest_path, lock_path],
        [index_path, violations_path, report_path],
        "complete" if status == "PASS" else "blocked",
        dry_run=False,
        details=report,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return report


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate full-cohort Judge bundles")
    parser.add_argument("--judgments-root", type=Path, required=True)
    parser.add_argument("--packet-manifest", type=Path, required=True)
    parser.add_argument("--packets-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--expected-packets", type=int, default=1290)
    parser.add_argument("--expected-episodes", type=int, default=430)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(
            judgments_root=args.judgments_root,
            packet_manifest_path=args.packet_manifest,
            packets_root=args.packets_root,
            output_dir=args.output,
            lock_path=args.lock,
            expected_packets=args.expected_packets,
            expected_episodes=args.expected_episodes,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") in {"PASS", "dry_run"} else 2
