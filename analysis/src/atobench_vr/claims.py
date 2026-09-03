from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    sha256_file,
    stage_manifest,
    write_json,
    write_jsonl,
)

METHOD_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\b", re.I)
ENDPOINT_RE = re.compile(r"(?:https?://[^\s`]+)?(/[A-Za-z0-9_./{}?=&%-]+)")
# Response codes are only meaningful claims when the surrounding text names
# HTTP/status semantics.  A bare three-digit substring is too ambiguous: it
# previously turned IPv4 octets and credentials such as ``admin123`` into
# response-status facts.
STATUS_RE = re.compile(
    r"\b(?:HTTP(?:/\d+(?:\.\d+)?)?|(?:response\s+)?status(?:\s+code)?)"
    r"\s*(?:[:=\-→]\s*)?([1-5]\d\d)\b",
    re.I,
)
LIMITATION_RE = re.compile(
    r"(?i)\b(not verified|unverified|not tested|unable to|limitation|rejected|未验证|未测试|无法)\b"
)


def extract_atoms(episode_id: str, report_path: Path) -> list[dict[str, Any]]:
    lines = report_path.read_text(encoding="utf-8", errors="replace").splitlines()
    atoms: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        candidates: list[tuple[str, Any, str]] = []
        method = METHOD_RE.search(stripped)
        endpoint = ENDPOINT_RE.search(stripped)
        status = STATUS_RE.search(stripped)
        if method:
            candidates.append(("request_method", method.group(1).upper(), "medium"))
        if endpoint:
            candidates.append(("endpoint_or_workflow", endpoint.group(1), "high"))
        if status:
            candidates.append(("claimed_response_status", int(status.group(1)), "high"))
        if LIMITATION_RE.search(stripped):
            candidates.append(("stated_limitation_or_rejection", True, "high"))
        if not candidates and any(
            term in stripped.lower()
            for term in ("verified", "vulnerability", "impact", "evidence", "finding")
        ):
            candidates.append(("material_claim_text", stripped, "medium"))
        for candidate_index, (atom_type, value, importance) in enumerate(candidates, 1):
            atom_id = (
                f"atom::{episode_id}::L{line_number:04d}::{atom_type}"
                f"::{candidate_index}"
            )
            atoms.append(
                {
                    "schema_version": "atobench.report_claim_atom.v1",
                    "atom_id": atom_id,
                    "episode_id": episode_id,
                    "atom_type": atom_type,
                    "value": value,
                    "importance": importance,
                    "report_line_numbers": [line_number],
                    "source_pointer": f"report:final_report.md:L{line_number}",
                    "trace_support_label": "unavailable",
                    "trace_fact_ids": [],
                    "extraction_method": "deterministic_candidate_rules.v2",
                    "semantic_matching_status": "pending",
                }
            )
    return atoms


def _load_canonical_reports(
    report_candidates_csv: Path,
    redacted_messages_path: Path,
    snapshot_dir: Path,
) -> list[dict[str, Any]]:
    with report_candidates_csv.open(newline="", encoding="utf-8") as handle:
        candidates = list(csv.DictReader(handle))
    exact = [
        row
        for row in candidates
        if str(row.get("exact_canonical_report_hash_match")).lower() == "true"
    ]
    exact_counts = Counter(str(row.get("episode_id") or "") for row in exact)
    bad = sorted(episode_id for episode_id, count in exact_counts.items() if count != 1)
    if bad:
        raise GateError(
            f"canonical report selection is not one-to-one for {len(bad)} episodes"
        )
    messages = {
        (str(row.get("episode_id") or ""), str(row.get("message_uuid") or "")): row
        for row in load_jsonl(redacted_messages_path)
    }
    reports: list[dict[str, Any]] = []
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for index, candidate in enumerate(sorted(exact, key=lambda row: row["episode_id"]), 1):
        episode_id = str(candidate["episode_id"])
        key = (episode_id, str(candidate["message_uuid"]))
        message = messages.get(key)
        if message is None:
            raise GateError(f"canonical report message missing from redacted input: {key}")
        text = message.get("visible_text")
        if not isinstance(text, str) or not text.strip():
            raise GateError(f"canonical report has no redacted visible text: {episode_id}")
        expected_hash = str(candidate.get("canonical_final_report_sha256") or "")
        if str(candidate.get("text_sha256") or "") != expected_hash:
            raise GateError(f"canonical report hash selection mismatch: {episode_id}")
        report_path = snapshot_dir / f"report_{index:04d}.md"
        report_path.write_text(text.rstrip() + "\n", encoding="utf-8")
        reports.append(
            {
                "episode_id": episode_id,
                "pair_id": message.get("global_pair_id"),
                "campaign_id": message.get("campaign_id"),
                "condition": message.get("condition"),
                "model": message.get("model"),
                "aou": message.get("aou"),
                "message_uuid": message.get("message_uuid"),
                "report_path": str(report_path.resolve()),
                "source_text_sha256": expected_hash,
                "snapshot_is_redacted": True,
            }
        )
    return reports


def _report_fact(atom: dict[str, Any]) -> dict[str, Any]:
    line_number = int(atom["report_line_numbers"][0])
    fact_id = (
        f"fact::{atom['episode_id']}::report::{atom['atom_type']}"
        f"::L{line_number:04d}::{atom['atom_id'].rsplit('::', 1)[-1]}"
    )
    return {
        "fact_id": fact_id,
        "episode_id": atom["episode_id"],
        "fact_class": "report",
        "fact_type": atom["atom_type"],
        "value": atom["value"],
        "measurement_status": "positive",
        "importance": atom["importance"],
        "source_kind": "final_report",
        "source_pointers": [atom["source_pointer"]],
        "predicate_or_prompt_version": atom["extraction_method"],
        "confidence": "exact",
    }


def run(
    *,
    output_dir: Path,
    dry_run: bool,
    new_version: bool,
    report_manifest_path: Path | None = None,
    report_candidates_csv: Path | None = None,
    redacted_messages_path: Path | None = None,
    base_facts_path: Path | None = None,
) -> dict[str, Any]:
    if report_candidates_csv is not None:
        if redacted_messages_path is None:
            raise GateError("--redacted-messages is required with --report-candidates-csv")
        reports = _load_canonical_reports(
            report_candidates_csv,
            redacted_messages_path,
            output_dir / "report_snapshots",
        )
    elif report_manifest_path is not None:
        reports = load_jsonl(report_manifest_path)
    else:
        raise GateError("provide --report-candidates-csv or --report-manifest")
    if dry_run:
        return {
            "stage": "06_extract_report_claim_atoms",
            "status": "dry_run",
            "report_count": len(reports),
        }
    ensure_output_available(output_dir, new_version)
    atoms: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for row in reports:
        episode_id = str(row.get("episode_id") or "")
        report_path = Path(str(row.get("report_path") or ""))
        if not episode_id or not report_path.is_file():
            raise GateError(f"invalid report manifest row: {row}")
        extracted = extract_atoms(episode_id, report_path)
        atoms.extend(extracted)
        candidates.append(
            {
                "episode_id": episode_id,
                "pair_id": row.get("pair_id"),
                "campaign_id": row.get("campaign_id"),
                "condition": row.get("condition"),
                "model": row.get("model"),
                "aou": row.get("aou"),
                "report_path": str(report_path.resolve()),
                "report_sha256": sha256_file(report_path),
                "candidate_atom_count": len(extracted),
                "semantic_matching_status": "pending",
            }
        )
    atom_path = output_dir / "report_claim_atoms.jsonl"
    candidate_path = output_dir / "final_report_candidates.jsonl"
    report_fact_path = output_dir / "report_facts.jsonl"
    combined_fact_path = output_dir / "fact_registry_with_report_candidates.jsonl"
    write_jsonl(atom_path, atoms)
    write_jsonl(candidate_path, candidates)
    report_facts = [_report_fact(atom) for atom in atoms]
    write_jsonl(report_fact_path, report_facts)
    base_facts = load_jsonl(base_facts_path) if base_facts_path else []
    combined_facts = [*base_facts, *report_facts]
    combined_ids = [str(row["fact_id"]) for row in combined_facts]
    if len(combined_ids) != len(set(combined_ids)):
        raise GateError("combined fact registry contains duplicate fact IDs")
    write_jsonl(combined_fact_path, combined_facts)
    summary = {
        "schema_version": "atobench.report_claim_atom_manifest.v1",
        "report_count": len(reports),
        "candidate_atom_count": len(atoms),
        "report_fact_count": len(report_facts),
        "combined_fact_count": len(combined_facts),
        "trace_support_labels_assigned": 0,
        "semantic_matching_pending": True,
        "deterministic_candidates_are_not_final_claim_labels": True,
        "canonical_report_selection_count": len(reports),
        "redacted_report_snapshots": report_candidates_csv is not None,
        "hash_based_atom_or_fact_ids": False,
        "llm_calls": 0,
    }
    write_json(output_dir / "claim_atom_manifest.json", summary)
    manifest = stage_manifest(
        "06_extract_report_claim_atoms",
        [
            *([report_manifest_path] if report_manifest_path else []),
            *([report_candidates_csv] if report_candidates_csv else []),
            *([redacted_messages_path] if redacted_messages_path else []),
            *([base_facts_path] if base_facts_path else []),
            *(
                [Path(row["report_path"]) for row in reports]
                if report_candidates_csv is None
                else []
            ),
        ],
        [
            atom_path,
            candidate_path,
            report_fact_path,
            combined_fact_path,
            output_dir / "claim_atom_manifest.json",
        ],
        "complete_candidates_only",
        dry_run=False,
        details=summary,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-manifest", type=Path)
    parser.add_argument("--report-candidates-csv", type=Path)
    parser.add_argument("--redacted-messages", type=Path)
    parser.add_argument("--base-facts", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            report_manifest_path=args.report_manifest,
            report_candidates_csv=args.report_candidates_csv,
            redacted_messages_path=args.redacted_messages,
            base_facts_path=args.base_facts,
            output_dir=args.output,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    return 2
