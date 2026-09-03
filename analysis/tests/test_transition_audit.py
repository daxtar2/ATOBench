from __future__ import annotations

import json
from pathlib import Path

import pytest

from atobench_vr.common import GateError, write_jsonl
from atobench_vr.transition_audit import build_transition_audit_packets


def _record(aou: str, condition: str, index: int) -> dict:
    return {
        "schema_version": "atobench.learning_transition.v1",
        "record_id": f"transition-{aou}-{condition}-{index}",
        "view": "structured_transition",
        "identity": {"aou": aou, "condition": condition},
        "split": {"name": "train"},
        "observation": {"native_available": index % 2 == 0},
        "evidence": {"events": [{"role": "observation"}] if index % 3 == 0 else []},
        "intervention": {
            "events": [{"operation": "replace"}] if condition == "C1" else [],
            "target_aou_contact": condition == "C1" and index % 2 == 0,
        },
        "provenance": {"raw_paths_removed": True},
    }


def test_build_transition_audit_packets_is_deterministic_and_closed_set(
    tmp_path: Path,
) -> None:
    records = [
        _record(aou, condition, index)
        for aou in ("basket", "jwt", "sqli")
        for condition in ("C0", "C1")
        for index in range(3)
    ]
    source = tmp_path / "transitions.jsonl"
    write_jsonl(source, records)
    output = tmp_path / "audit"
    manifest = build_transition_audit_packets(
        transition_records_path=source,
        output_dir=output,
        sample_per_stratum=1,
        allow_real_data=False,
        dry_run=False,
        new_version=False,
    )
    assert manifest["status"] == "AWAITING_HUMAN_REVIEW"
    packets = [json.loads(line) for line in (output / "audit_packets.jsonl").read_text().splitlines()]
    assert len(packets) >= 6
    assert all(packet["review_status"] == "awaiting_human_review" for packet in packets)
    assert all(packet["free_text_collected"] is False for packet in packets)
    assert all(
        {question["response"] for question in packet["review_questions"]} == {None}
        for packet in packets
    )
    with pytest.raises(GateError, match="refusing to overwrite"):
        build_transition_audit_packets(
            transition_records_path=source,
            output_dir=output,
            sample_per_stratum=1,
            allow_real_data=False,
            dry_run=False,
            new_version=False,
        )


def test_transition_audit_requires_authorization_for_project_data(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "reference/latest/learning_transitions_v1/transition_records.jsonl"
    with pytest.raises(GateError, match="allow-real-data"):
        build_transition_audit_packets(
            transition_records_path=source,
            output_dir=tmp_path / "audit",
            sample_per_stratum=1,
            allow_real_data=False,
            dry_run=True,
            new_version=False,
        )
