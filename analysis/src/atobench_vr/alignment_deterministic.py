from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .alignment import _enrich_event_body_hashes, _event_view
from .alignment_verifier import _task
from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    require_real_data_authorization,
    stage_manifest,
    write_json,
    write_jsonl,
)


def run(
    *,
    alignment_path: Path,
    action_cycles_path: Path,
    http_events_paths: list[Path],
    output_dir: Path,
    tolerance_seconds: float,
    allow_real_data: bool,
    new_version: bool,
) -> dict[str, Any]:
    if not http_events_paths:
        raise GateError("deterministic alignment requires at least one HTTP source")
    require_real_data_authorization(
        [alignment_path, action_cycles_path, *http_events_paths],
        allow_real_data,
    )
    alignments = load_jsonl(alignment_path)
    cycles = {
        str(row["action_cycle_id"]): row
        for row in load_jsonl(action_cycles_path)
        if row.get("action_cycle_id")
    }
    event_rows = [
        event for path in http_events_paths for event in load_jsonl(path)
    ]
    enrichment = _enrich_event_body_hashes(event_rows)
    event_views = {
        view["event_id"]: view
        for index, event in enumerate(event_rows)
        for view in [_event_view(event, index)]
    }

    resolved_rows: list[dict[str, Any]] = []
    deterministic_counts: Counter[str] = Counter()
    processed_task_ids: set[str] = set()
    for row in alignments:
        if row.get("dual_verifier_status") != "pending_dual_verification":
            resolved_rows.append(row)
            continue
        cycle_id = str(row.get("action_cycle_id") or "")
        cycle = cycles.get(cycle_id)
        if cycle is None:
            raise GateError(f"pending alignment has no action cycle: {cycle_id}")
        task, source_to_public = _task(
            row, cycle, event_views, tolerance_seconds
        )
        task_id = str(task["alignment_task_id"])
        if task_id in processed_task_ids:
            raise GateError(f"duplicate deterministic task ID: {task_id}")
        processed_task_ids.add(task_id)
        satisfying = [
            candidate
            for candidate in task["candidates"]
            if all(candidate["deterministic_constraints"].values())
        ]
        resolved = dict(row)
        resolved["alignment_task_id"] = task_id
        if len(satisfying) == 1:
            public_id = str(satisfying[0]["candidate_id"])
            public_to_source = {
                public: source for source, public in source_to_public.items()
            }
            source_id = public_to_source[public_id]
            resolved["alignment_status"] = "timestamp_unique"
            resolved["accepted_event_ids"] = [source_id]
            resolved["accepted_for_evidence"] = True
            resolved["dual_verifier_status"] = "deterministic_unique_accepted"
            deterministic_counts["deterministic_unique_accepted"] += 1
        elif satisfying:
            resolved["alignment_status"] = "ambiguous"
            resolved["accepted_event_ids"] = []
            resolved["accepted_for_evidence"] = False
            resolved["dual_verifier_status"] = "deterministic_multiple_exclude"
            deterministic_counts["deterministic_multiple_exclude"] += 1
        else:
            resolved["alignment_status"] = "ambiguous"
            resolved["accepted_event_ids"] = []
            resolved["accepted_for_evidence"] = False
            resolved["dual_verifier_status"] = "deterministic_no_match_exclude"
            deterministic_counts["deterministic_no_match_exclude"] += 1
        resolved_rows.append(resolved)

    pending_count = sum(
        row.get("dual_verifier_status") == "pending_dual_verification"
        for row in resolved_rows
    )
    if pending_count:
        raise GateError("deterministic resolution left pending rows")
    accepted_count = sum(
        bool(row.get("accepted_for_evidence")) for row in resolved_rows
    )
    details = {
        "schema_version": "atobench.deterministic_alignment_resolution.v1",
        "policy": "deterministic_first_unique_candidate",
        "alignment_row_count": len(resolved_rows),
        "processed_pending_count": len(processed_task_ids),
        "deterministic_unique_accepted_count": deterministic_counts[
            "deterministic_unique_accepted"
        ],
        "deterministic_multiple_exclude_count": deterministic_counts[
            "deterministic_multiple_exclude"
        ],
        "deterministic_no_match_exclude_count": deterministic_counts[
            "deterministic_no_match_exclude"
        ],
        "accepted_alignment_row_count": accepted_count,
        "pending_alignment_count": pending_count,
        "claude_calls_made": 0,
        "timestamp_tolerance_seconds": tolerance_seconds,
        **enrichment,
    }
    if (
        sum(deterministic_counts.values()) != len(processed_task_ids)
        or accepted_count < deterministic_counts["deterministic_unique_accepted"]
    ):
        raise GateError("deterministic alignment count invariant failed")

    ensure_output_available(output_dir, new_version)
    alignment_output = output_dir / "claude_http_alignment.final.jsonl"
    summary_output = output_dir / "deterministic_resolution_summary.json"
    write_jsonl(alignment_output, resolved_rows)
    write_json(summary_output, details)
    manifest = stage_manifest(
        "04i_resolve_alignment_deterministically",
        [alignment_path, action_cycles_path, *http_events_paths],
        [alignment_output, summary_output],
        "complete_no_calls",
        dry_run=False,
        details=details,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alignment", type=Path, required=True)
    parser.add_argument("--action-cycles", type=Path, required=True)
    parser.add_argument("--http-events", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timestamp-tolerance-seconds", type=float, default=3.0)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            alignment_path=args.alignment,
            action_cycles_path=args.action_cycles,
            http_events_paths=args.http_events,
            output_dir=args.output,
            tolerance_seconds=args.timestamp_tolerance_seconds,
            allow_real_data=args.allow_real_data,
            new_version=args.new_version,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    return 2
