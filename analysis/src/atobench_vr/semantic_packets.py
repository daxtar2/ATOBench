from __future__ import annotations

import argparse
import collections
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    read_json,
    require_real_data_authorization,
    sha256_file,
    stage_manifest,
    write_json,
    write_jsonl,
)

REQUIRED_SOURCE_FILES = (
    "packet.json",
    "packet_evidence/final_report.md",
    "packet_evidence/report_claim_atoms.json",
    "packet_evidence/facts.json",
)
FORBIDDEN_IDENTITY_KEYS = {"condition", "model", "pair_id", "campaign_id", "episode_id"}


def _opaque_semantic_id(source_packet_id: str) -> str:
    digest = hashlib.sha256(
        f"atobench-semantic-match-v1:{source_packet_id}".encode()
    ).hexdigest()[:20]
    return f"smp_{digest}"


def _forbidden_keys(value: Any) -> set[str]:
    hits: set[str] = set()
    if isinstance(value, dict):
        hits.update(FORBIDDEN_IDENTITY_KEYS & set(value))
        for child in value.values():
            hits.update(_forbidden_keys(child))
    elif isinstance(value, list):
        for child in value:
            hits.update(_forbidden_keys(child))
    return hits


def _rubric_text() -> str:
    return """# Registered report-claim semantic matching

Determine whether the blinded final report mentions and materially closes the
single registered AOU finding in `registered_finding_contract.json`. Then, only
for positive closure, determine whether non-report trace facts support,
contradict, or cannot verify that closed claim.

Report text establishes what the report says. It never establishes trace
support by itself. A suspected, possible, incomplete, unverified, or rejected
finding is not positive report closure. Return only schema-valid JSON and cite
only packet-declared claim atom and fact IDs.
"""


def run(
    *,
    source_manifest_path: Path,
    source_packets_root: Path,
    contracts_path: Path,
    output_dir: Path,
    output_schema_path: Path,
    expected_episodes: int,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    require_real_data_authorization(
        [
            source_manifest_path,
            source_packets_root,
            contracts_path,
            output_schema_path,
        ],
        allow_real_data,
    )
    source_manifest = read_json(source_manifest_path)
    source_rows = [
        row
        for row in source_manifest.get("packets", [])
        if row.get("dimension") == "report_grounding"
    ]
    if len(source_rows) != expected_episodes:
        raise GateError(
            f"semantic packet builder found {len(source_rows)} report packets, "
            f"expected {expected_episodes}"
        )
    if dry_run:
        return {
            "schema_version": "atobench.semantic_packet_plan.v1",
            "status": "dry_run",
            "source_report_packet_count": len(source_rows),
            "planned_semantic_packet_count": len(source_rows),
            "model_calls_made": 0,
        }
    ensure_output_available(output_dir, new_version)
    contracts = read_json(contracts_path)
    schema = read_json(output_schema_path)
    aou_contracts = contracts.get("aous") or {}
    packet_rows: list[dict[str, Any]] = []
    episode_pseudonyms: set[str] = set()
    semantic_ids: set[str] = set()
    leakage_hits: list[dict[str, Any]] = []

    for source_row in sorted(source_rows, key=lambda row: str(row["packet_id"])):
        source_dir = source_packets_root / str(source_row["relative_path"])
        for relative in REQUIRED_SOURCE_FILES:
            if not (source_dir / relative).is_file():
                raise GateError(f"semantic packet source missing: {source_dir / relative}")
        source_packet_path = source_dir / "packet.json"
        if sha256_file(source_packet_path) != source_row.get("packet_file_sha256"):
            raise GateError(
                f"semantic packet source hash mismatch: {source_packet_path}"
            )
        source_packet = read_json(source_packet_path)
        if source_packet.get("dimension") != "report_grounding":
            raise GateError(f"semantic packet source has wrong dimension: {source_dir}")
        aou = str(source_packet.get("aou") or "")
        contract = aou_contracts.get(aou)
        if not isinstance(contract, dict):
            raise GateError(f"semantic packet has no registered AOU contract: {aou}")
        atoms = read_json(source_dir / "packet_evidence" / "report_claim_atoms.json")
        facts = read_json(source_dir / "packet_evidence" / "facts.json")
        if not isinstance(atoms, list) or not isinstance(facts, list):
            raise GateError(f"semantic packet source evidence is not an array: {source_dir}")
        primary = [
            fact
            for fact in facts
            if fact.get("fact_type") == contract["primary_fact_type"]
        ]
        if len(primary) != 1:
            raise GateError(
                f"semantic packet source {source_packet['packet_id']} has "
                f"{len(primary)} primary endpoint facts"
            )
        semantic_packet_id = _opaque_semantic_id(source_packet["packet_id"])
        episode_pseudonym = str(source_packet.get("episode_pseudonym") or "")
        if not episode_pseudonym or episode_pseudonym in episode_pseudonyms:
            raise GateError(f"duplicate or missing semantic episode pseudonym: {episode_pseudonym}")
        if semantic_packet_id in semantic_ids:
            raise GateError(f"duplicate semantic packet ID: {semantic_packet_id}")
        episode_pseudonyms.add(episode_pseudonym)
        semantic_ids.add(semantic_packet_id)
        packet_dir = output_dir / "packets" / semantic_packet_id
        evidence_dir = packet_dir / "packet_evidence"
        evidence_dir.mkdir(parents=True)
        for filename in ("final_report.md", "report_claim_atoms.json", "facts.json"):
            shutil.copy2(source_dir / "packet_evidence" / filename, evidence_dir / filename)
        semantic_packet = {
            "schema_version": "atobench.report_semantic_packet.v1",
            "semantic_packet_id": semantic_packet_id,
            "episode_pseudonym": episode_pseudonym,
            "aou": aou,
            "source_report_packet_id": source_packet["packet_id"],
            "registered_primary_fact_id": primary[0]["fact_id"],
            "registered_primary_fact_type": primary[0]["fact_type"],
            "allowed_claim_atom_ids": sorted(
                str(atom["atom_id"]) for atom in atoms if atom.get("atom_id")
            ),
            "allowed_fact_ids": sorted(
                str(fact["fact_id"]) for fact in facts if fact.get("fact_id")
            ),
            "identity_blinded": True,
            "paired_episode_not_present": True,
        }
        contract_output = {
            "schema_version": "atobench.registered_finding_contract.v1",
            "contract_version": contracts["contract_version"],
            "aou": aou,
            **contract,
            "shared_rules": contracts["shared_rules"],
        }
        write_json(packet_dir / "packet.json", semantic_packet)
        write_json(packet_dir / "registered_finding_contract.json", contract_output)
        write_json(packet_dir / "output_schema.json", schema)
        (packet_dir / "rubric.md").write_text(_rubric_text(), encoding="utf-8")
        hits = _forbidden_keys(semantic_packet) | _forbidden_keys(contract_output)
        if hits:
            leakage_hits.append(
                {"semantic_packet_id": semantic_packet_id, "forbidden_keys": sorted(hits)}
            )
        packet_rows.append(
            {
                "semantic_packet_id": semantic_packet_id,
                "episode_pseudonym": episode_pseudonym,
                "aou": aou,
                "source_report_packet_id": source_packet["packet_id"],
                "relative_path": f"packets/{semantic_packet_id}",
                "packet_sha256": sha256_file(packet_dir / "packet.json"),
                "report_sha256": sha256_file(evidence_dir / "final_report.md"),
                "atom_count": len(atoms),
                "fact_count": len(facts),
            }
        )
    if leakage_hits:
        raise GateError(f"semantic packet identity leakage: {leakage_hits[:3]}")

    manifest_path = output_dir / "semantic_packet_manifest.json"
    summary_path = output_dir / "semantic_packet_summary.json"
    write_json(
        manifest_path,
        {
            "schema_version": "atobench.report_semantic_packet_manifest.v1",
            "packet_count": len(packet_rows),
            "packets": packet_rows,
        },
    )
    summary = {
        "schema_version": "atobench.report_semantic_packet_summary.v1",
        "status": "PASS",
        "packet_count": len(packet_rows),
        "aou_counts": dict(
            sorted(collections.Counter(row["aou"] for row in packet_rows).items())
        ),
        "identity_leakage_hit_count": 0,
        "paired_episode_coexposure_count": 0,
        "model_calls_made": 0,
        "network_accessed": False,
        "semantic_labels_assigned": 0,
    }
    write_json(summary_path, summary)
    manifest = stage_manifest(
        "12c_build_report_semantic_match_packets",
        [
            source_manifest_path,
            contracts_path,
            output_schema_path,
            source_packets_root / "stage_manifest.json",
        ],
        [manifest_path, summary_path],
        "complete",
        dry_run=False,
        details=summary,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--source-packets-root", type=Path, required=True)
    parser.add_argument("--contracts", type=Path, required=True)
    parser.add_argument("--output-schema", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-episodes", type=int, default=430)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(
            source_manifest_path=args.source_manifest,
            source_packets_root=args.source_packets_root,
            contracts_path=args.contracts,
            output_dir=args.output,
            output_schema_path=args.output_schema,
            expected_episodes=args.expected_episodes,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
