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
    require_real_data_authorization,
    stage_manifest,
    write_json,
)


def run(
    *,
    semantic_manifest_path: Path,
    packet_lineage_path: Path,
    output_dir: Path,
    pair_count: int,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    require_real_data_authorization(
        [semantic_manifest_path, packet_lineage_path], allow_real_data
    )
    semantic_rows = read_json(semantic_manifest_path).get("packets", [])
    semantic_by_source = {
        str(row["source_report_packet_id"]): row for row in semantic_rows
    }
    lineage_rows = [
        row
        for row in load_jsonl(packet_lineage_path)
        if row.get("dimension") == "report_grounding"
        and row.get("packet_id") in semantic_by_source
    ]
    by_pair: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for lineage in lineage_rows:
        by_pair[str(lineage["pair_id"])].append(lineage)
    candidates = []
    for pair_id, rows in by_pair.items():
        if {row.get("condition") for row in rows} != {"C0", "C1"} or len(rows) != 2:
            raise GateError(f"semantic pilot pair is not exact C0/C1: {pair_id}")
        candidates.append(
            {
                "pair_id": pair_id,
                "aou": semantic_by_source[str(rows[0]["packet_id"])]["aou"],
                "model": str(rows[0]["model"]),
                "rows": rows,
            }
        )
    if dry_run:
        return {
            "schema_version": "atobench.semantic_match_pilot_plan.v1",
            "status": "dry_run",
            "candidate_pair_count": len(candidates),
            "selected_pair_count": pair_count,
            "selected_episode_count": pair_count * 2,
            "base_call_count": pair_count * 4,
        }
    ensure_output_available(output_dir, new_version)
    aous = sorted({row["aou"] for row in candidates})
    if pair_count % len(aous):
        raise GateError("semantic pilot pair count must divide evenly across AOUs")
    target_per_aou = pair_count // len(aous)
    selected: list[dict[str, Any]] = []
    model_counts: collections.Counter[str] = collections.Counter()
    for aou in aous:
        pool = [row for row in candidates if row["aou"] == aou]
        for _ in range(target_per_aou):
            if not pool:
                raise GateError(f"semantic pilot lacks candidates for {aou}")
            choice = min(
                pool,
                key=lambda row: (model_counts[row["model"]], row["model"], row["pair_id"]),
            )
            pool.remove(choice)
            selected.append(choice)
            model_counts[choice["model"]] += 1
    task_ids = []
    private_rows = []
    for pair in sorted(selected, key=lambda row: row["pair_id"]):
        for lineage in sorted(pair["rows"], key=lambda row: row["condition"]):
            semantic = semantic_by_source[str(lineage["packet_id"])]
            task_ids.append(semantic["semantic_packet_id"])
            private_rows.append(
                {
                    "semantic_packet_id": semantic["semantic_packet_id"],
                    "pair_id": pair["pair_id"],
                    "condition": lineage["condition"],
                    "model": pair["model"],
                    "aou": pair["aou"],
                }
            )
    allowlist_path = output_dir / "semantic_match_pilot_allowlist.json"
    private_path = output_dir / "private_selection.json"
    summary_path = output_dir / "semantic_match_pilot_summary.json"
    write_json(
        allowlist_path,
        {
            "schema_version": "atobench.semantic_match_task_allowlist.v1",
            "authorization_status": "draft_unauthorized",
            "authorized_for_calls": False,
            "semantic_packet_ids": sorted(task_ids),
            "max_base_calls": len(task_ids) * 2,
            "max_adjudication_calls_without_retry": len(task_ids),
            "hard_retry_call_ceiling": len(task_ids) * 3 * 2,
        },
    )
    write_json(private_path, private_rows)
    summary = {
        "schema_version": "atobench.semantic_match_pilot_summary.v1",
        "status": "PASS",
        "selection_is_outcome_blind": True,
        "selected_pair_count": len(selected),
        "selected_episode_count": len(task_ids),
        "aou_pair_counts": dict(
            sorted(collections.Counter(row["aou"] for row in selected).items())
        ),
        "model_pair_counts": dict(sorted(model_counts.items())),
        "condition_episode_counts": dict(
            sorted(collections.Counter(row["condition"] for row in private_rows).items())
        ),
        "base_call_count": len(task_ids) * 2,
        "no_retry_maximum_call_count": len(task_ids) * 3,
        "hard_retry_call_ceiling": len(task_ids) * 3 * 2,
        "authorized_for_calls": False,
        "model_calls_made": 0,
    }
    write_json(summary_path, summary)
    manifest = stage_manifest(
        "12d_select_semantic_match_pilot",
        [semantic_manifest_path, packet_lineage_path],
        [allowlist_path, private_path, summary_path],
        "draft_unauthorized",
        dry_run=False,
        details=summary,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--semantic-manifest", type=Path, required=True)
    parser.add_argument("--packet-lineage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pair-count", type=int, default=12)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(
            semantic_manifest_path=args.semantic_manifest,
            packet_lineage_path=args.packet_lineage,
            output_dir=args.output,
            pair_count=args.pair_count,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
