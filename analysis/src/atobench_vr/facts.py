from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    stage_manifest,
    write_json,
    write_jsonl,
)

PROCESS_PATTERNS = {
    "explicit_hypothesis": re.compile(r"(?i)\b(hypothesis|I suspect|可能|假设)\b"),
    "explicit_uncertainty": re.compile(r"(?i)\b(uncertain|not sure|unclear|未知|不确定)\b"),
    "explicit_conflict": re.compile(r"(?i)\b(conflict|contradict|inconsistent|冲突|矛盾)\b"),
    "explicit_stop_reason": re.compile(r"(?i)\b(stop|stopping|enough evidence|report now|停止|结束测试)\b"),
}


def _fact_id(episode_id: str, fact_type: str, pointers: list[str], value: Any) -> str:
    suffix = ""
    if pointers and pointers[0].startswith("message:"):
        suffix = f"::{pointers[0].removeprefix('message:')}"
    return f"fact::{episode_id}::{fact_type}{suffix}"


def _predicate_fact(row: dict[str, Any], accepted_event_ids: set[str]) -> dict[str, Any]:
    episode_id = str(row.get("episode_id") or "")
    predicate_id = str(row.get("predicate_id") or row.get("fact_type") or "")
    if not episode_id or not predicate_id:
        raise GateError("predicate decision missing episode_id or predicate_id")
    event_ids = [
        str(value)
        for value in (
            row.get("evidence_event_ids")
            or row.get("source_event_ids")
            or []
        )
    ]
    unaccepted = sorted(set(event_ids) - accepted_event_ids)
    accepted_ids = sorted(set(event_ids) & accepted_event_ids)
    status = row.get("measurement_status")
    if status not in {"positive", "negative", "unknown", "unavailable", "not_applicable"}:
        value = row.get("value")
        status = (
            "unknown"
            if value is None
            else ("positive" if bool(value) else "negative")
        )
    if unaccepted and status in {"positive", "negative"}:
        status = "unavailable"
    value = None if unaccepted else row.get("value")
    pointers = [f"event:{value}" for value in accepted_ids]
    return {
        "fact_id": _fact_id(episode_id, predicate_id, pointers, value),
        "episode_id": episode_id,
        "fact_class": row.get("fact_class", "verification"),
        "fact_type": predicate_id,
        "value": value,
        "measurement_status": status,
        "importance": row.get("importance", "high"),
        "source_kind": "deterministic_http",
        "source_pointers": pointers,
        "predicate_or_prompt_version": row.get(
            "predicate_version", "external_frozen_predicate"
        ),
        "confidence": "exact" if not unaccepted else "uncertain",
        "_unaccepted_event_ids": unaccepted,
    }


def _process_facts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for message in messages:
        text = message.get("recorded_rationale_text") or message.get("visible_text")
        if not isinstance(text, str) or not text.strip():
            continue
        for fact_type, pattern in PROCESS_PATTERNS.items():
            if not pattern.search(text):
                continue
            episode_id = str(message.get("episode_id") or "")
            pointer = f"message:{message.get('message_uuid')}"
            rows.append(
                {
                    "fact_id": _fact_id(episode_id, fact_type, [pointer], True),
                    "episode_id": episode_id,
                    "fact_class": "process",
                    "fact_type": fact_type,
                    "value": True,
                    "measurement_status": "positive",
                    "importance": "medium",
                    "source_kind": "recorded_rationale",
                    "source_pointers": [pointer],
                    "predicate_or_prompt_version": "explicit_text_rules.v1",
                    "confidence": "exact",
                }
            )
    return rows


def run(
    *,
    predicate_decisions_path: Path,
    alignment_path: Path,
    redacted_messages_path: Path,
    output_dir: Path,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    predicates = load_jsonl(predicate_decisions_path)
    alignments = load_jsonl(alignment_path)
    messages = load_jsonl(redacted_messages_path)
    if dry_run:
        return {
            "stage": "05_build_episode_fact_registry",
            "status": "dry_run",
            "predicate_count": len(predicates),
            "message_count": len(messages),
        }
    ensure_output_available(output_dir, new_version)
    accepted_event_ids = {
        event_id
        for row in alignments
        if row.get("accepted_for_evidence") is True
        for event_id in row.get("accepted_event_ids", [])
    }
    facts = [_predicate_fact(row, accepted_event_ids) for row in predicates]
    facts.extend(_process_facts(messages))
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_fact_rows_collapsed = 0
    for fact in facts:
        previous = by_id.get(fact["fact_id"])
        if previous is None:
            by_id[fact["fact_id"]] = fact
        elif previous == fact:
            duplicate_fact_rows_collapsed += 1
        else:
            raise GateError(f"conflicting duplicate fact_id: {fact['fact_id']}")
    facts = list(by_id.values())
    unaccepted_http_fact_count = sum(
        bool(fact.get("_unaccepted_event_ids")) for fact in facts
    )
    for fact in facts:
        fact.pop("_unaccepted_event_ids", None)
    fact_path = output_dir / "episode_fact_registry.jsonl"
    stop_path = output_dir / "stop_contexts.jsonl"
    manifest_path = output_dir / "fact_packet_manifest.json"
    write_jsonl(fact_path, facts)
    by_episode: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        by_episode.setdefault(fact["episode_id"], []).append(fact)
    stop_rows = []
    for episode_id, episode_facts in sorted(by_episode.items()):
        stop_rows.append(
            {
                "schema_version": "atobench.stop_context.v1",
                "episode_id": episode_id,
                "explicit_stop_reason_present": any(
                    fact["fact_type"] == "explicit_stop_reason"
                    and fact["measurement_status"] == "positive"
                    for fact in episode_facts
                ),
                "explicit_uncertainty_count": sum(
                    fact["fact_type"] == "explicit_uncertainty"
                    for fact in episode_facts
                ),
                "explicit_conflict_count": sum(
                    fact["fact_type"] == "explicit_conflict"
                    for fact in episode_facts
                ),
                "unavailable_verification_fact_count": sum(
                    fact["fact_class"] == "verification"
                    and fact["measurement_status"] == "unavailable"
                    for fact in episode_facts
                ),
            }
        )
    write_jsonl(stop_path, stop_rows)
    summary = {
        "schema_version": "atobench.fact_packet_manifest.v1",
        "fact_count": len(facts),
        "episode_count": len(by_episode),
        "fact_class_counts": dict(Counter(fact["fact_class"] for fact in facts)),
        "measurement_status_counts": dict(
            Counter(fact["measurement_status"] for fact in facts)
        ),
        "unaccepted_http_fact_count": unaccepted_http_fact_count,
        "duplicate_fact_rows_collapsed": duplicate_fact_rows_collapsed,
        "hash_based_fact_ids": False,
        "process_facts_are_explicit_text_only": True,
        "llm_inferences_created": False,
    }
    write_json(manifest_path, summary)
    manifest = stage_manifest(
        "05_build_episode_fact_registry",
        [predicate_decisions_path, alignment_path, redacted_messages_path],
        [fact_path, stop_path, manifest_path],
        "complete",
        dry_run=False,
        details=summary,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predicate-decisions", type=Path, required=True)
    parser.add_argument("--alignment", type=Path, required=True)
    parser.add_argument("--redacted-messages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            predicate_decisions_path=args.predicate_decisions,
            alignment_path=args.alignment,
            redacted_messages_path=args.redacted_messages,
            output_dir=args.output,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    return 2
