from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    load_jsonl,
    require_real_data_authorization,
    sha256_file,
    sha256_json,
    write_json,
    write_jsonl,
)
from .learning_transitions import SCHEMA_VERSION, _contains_absolute_path
from .redaction import residual_sensitive_kinds

SCHEMA_VERSION_AUDIT = "atobench.transition_audit_packet.v1"
MANIFEST_VERSION = "atobench.transition_audit_selection.v1"


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{sha256_json(value)[:20]}"


def _questions() -> list[dict[str, Any]]:
    return [
        {
            "question_id": "projection_semantics",
            "prompt": "Does the structural projection faithfully describe this action boundary?",
            "allowed_values": ["pass", "fail", "uncertain"],
            "response": None,
        },
        {
            "question_id": "state_transition_consistency",
            "prompt": "Are state-before and state-after counts consistent with the displayed boundary?",
            "allowed_values": ["pass", "fail", "uncertain"],
            "response": None,
        },
        {
            "question_id": "privacy_projection",
            "prompt": "Does the packet appear free of body values, identities, resources, raw paths and free text?",
            "allowed_values": ["pass", "fail", "uncertain"],
            "response": None,
        },
        {
            "question_id": "training_use_boundary",
            "prompt": "Is the non-readiness boundary (not direct language SFT or offline RL) appropriate?",
            "allowed_values": ["pass", "fail", "uncertain"],
            "response": None,
        },
    ]


def _packet(record: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    identity = record["identity"]
    packet = {
        "schema_version": SCHEMA_VERSION_AUDIT,
        "audit_packet_id": _stable_id("tap", record["record_id"]),
        "source_transition_record_id": record["record_id"],
        "selection_reasons": sorted(reasons),
        "stratum": {
            "aou": identity.get("aou"),
            "condition": identity.get("condition"),
            "split": record["split"].get("name"),
        },
        "review_payload": record,
        "review_questions": _questions(),
        "review_status": "awaiting_human_review",
        "reviewer_identity_collected": False,
        "free_text_collected": False,
        "provenance": {
            "source_transition_schema_version": record.get("schema_version"),
            "raw_paths_removed": True,
            "hidden_chain_of_thought_included": False,
        },
    }
    return packet


def _protocol(sample_per_stratum: int, packet_count: int) -> str:
    return f"""# Structured transition audit protocol v1

Status: `AWAITING_HUMAN_REVIEW`

This package contains {packet_count} deterministic, structurally redacted
transition packets. The core sample uses {sample_per_stratum} records per
`AOU × condition` stratum, with supplemental coverage selections for native
observations, evidence events, intervention events and target-AOU contact.

For every packet, answer the four closed-set questions as `pass`, `fail` or
`uncertain`. Do not paste raw requests, responses, credentials, identities,
file paths, model reasoning, or other free text into a review artifact. Store
review outcomes in a separately authorized, versioned response file; this
selection package remains immutable.

A `pass` is a projection-quality judgment, not proof that the underlying
pentest action was safe, effective, or reward-worthy. A failed packet blocks
its use in any later transition-label/reward construction until the projection
is fixed and re-exported.
"""


def build_transition_audit_packets(
    *,
    transition_records_path: Path,
    output_dir: Path,
    sample_per_stratum: int,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    require_real_data_authorization([transition_records_path], allow_real_data)
    if sample_per_stratum <= 0:
        raise GateError("sample-per-stratum must be positive")
    plan = {
        "schema_version": MANIFEST_VERSION,
        "status": "DRY_RUN_READY",
        "input_path": str(transition_records_path),
        "sample_per_stratum": sample_per_stratum,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    if dry_run:
        return plan
    if (output_dir / "selection_manifest.json").exists() and not new_version:
        raise GateError(
            f"refusing to overwrite completed transition audit selection {output_dir}; use a new output directory"
        )
    records = load_jsonl(transition_records_path)
    if not records:
        raise GateError("transition records are empty")
    invalid = [
        row.get("record_id", "<unknown>")
        for row in records
        if row.get("schema_version") != SCHEMA_VERSION
        or row.get("view") != "structured_transition"
    ]
    if invalid:
        raise GateError(f"invalid transition records: {invalid[:3]}")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        identity = record.get("identity") or {}
        grouped[(str(identity.get("aou")), str(identity.get("condition")))].append(record)
    selected: dict[str, list[str]] = {}
    reasons: dict[str, set[str]] = defaultdict(set)
    for stratum, rows in sorted(grouped.items()):
        ordered = sorted(rows, key=lambda row: sha256_json(["transition-audit-v1", row["record_id"]]))
        for row in ordered[:sample_per_stratum]:
            selected[row["record_id"]] = []
            reasons[row["record_id"]].add(f"stratum:{stratum[0]}:{stratum[1]}")

    coverage = {
        "native_observation": lambda row: row["observation"].get("native_available") is True,
        "evidence_event": lambda row: bool(row["evidence"].get("events")),
        "intervention_event": lambda row: bool(row["intervention"].get("events")),
        "target_aou_contact": lambda row: row["intervention"].get("target_aou_contact") is True,
    }
    for aou in sorted({key[0] for key in grouped}):
        aou_rows = [row for row in records if row["identity"].get("aou") == aou]
        for label, predicate in coverage.items():
            candidates = sorted(
                (row for row in aou_rows if predicate(row)),
                key=lambda row: sha256_json(["transition-audit-coverage-v1", label, row["record_id"]]),
            )
            if candidates:
                record = candidates[0]
                selected[record["record_id"]] = []
                reasons[record["record_id"]].add(f"coverage:{label}")

    by_id = {row["record_id"]: row for row in records}
    packets = [
        _packet(by_id[record_id], list(reasons[record_id]))
        for record_id in sorted(selected)
    ]
    errors: list[str] = []
    if len({packet["audit_packet_id"] for packet in packets}) != len(packets):
        errors.append("audit_packet_id is not unique")
    if any(_contains_absolute_path(packet) for packet in packets):
        errors.append("absolute path leaked into audit packet")
    sensitive_packets = 0
    sensitive_counts: Counter[str] = Counter()
    for packet in packets:
        kinds = residual_sensitive_kinds(json.dumps(packet, sort_keys=True, ensure_ascii=False))
        if kinds:
            sensitive_packets += 1
            sensitive_counts.update(kinds)
    if sensitive_packets:
        errors.append(f"residual sensitive content in {sensitive_packets} audit packets")

    output_dir.mkdir(parents=True, exist_ok=True)
    packets_path = output_dir / "audit_packets.jsonl"
    protocol_path = output_dir / "AUDIT_PROTOCOL.md"
    summary_path = output_dir / "selection_summary.json"
    manifest_path = output_dir / "selection_manifest.json"
    write_jsonl(packets_path, packets)
    protocol_path.write_text(_protocol(sample_per_stratum, len(packets)), encoding="utf-8")
    summary = {
        "schema_version": MANIFEST_VERSION,
        "status": "PASS_AWAITING_HUMAN_REVIEW" if not errors else "FAIL",
        "packet_count": len(packets),
        "sample_per_stratum": sample_per_stratum,
        "by_aou": dict(sorted(Counter(packet["stratum"]["aou"] for packet in packets).items())),
        "by_condition": dict(sorted(Counter(packet["stratum"]["condition"] for packet in packets).items())),
        "selection_reason_counts": dict(
            sorted(Counter(reason for packet in packets for reason in packet["selection_reasons"]).items())
        ),
        "residual_sensitive_packet_count": sensitive_packets,
        "residual_sensitive_kind_counts": dict(sorted(sensitive_counts.items())),
        "human_review_completed": False,
        "free_text_collected": False,
        "violations": errors,
    }
    write_json(summary_path, summary)
    outputs = [packets_path, protocol_path, summary_path]
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "status": "AWAITING_HUMAN_REVIEW" if not errors else "FAILED",
        "source_transition_records": {
            "sha256": sha256_file(transition_records_path),
            "record_count": len(records),
        },
        "outputs": {
            path.name: {"sha256": sha256_file(path), "bytes": path.stat().st_size}
            for path in outputs
        },
        "packet_count": len(packets),
        "sample_per_stratum": sample_per_stratum,
        "human_review_completed": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    write_json(manifest_path, manifest)
    if errors:
        raise GateError(f"transition audit selection failed: {errors[:3]}")
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transitions", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sample-per-stratum", type=int, default=8)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = build_transition_audit_packets(
            transition_records_path=args.transitions,
            output_dir=args.output,
            sample_per_stratum=args.sample_per_stratum,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
