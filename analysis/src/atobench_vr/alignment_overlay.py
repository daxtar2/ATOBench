from __future__ import annotations

import argparse
import json
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


def _by_task(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        task_id = str(row.get("alignment_task_id") or "")
        if not task_id:
            continue
        if task_id in result:
            raise GateError(f"{label} contains duplicate task ID: {task_id}")
        result[task_id] = row
    return result


def run(
    *,
    base_manifest_path: Path,
    base_bundles_path: Path,
    base_alignment_path: Path,
    override_manifest_path: Path,
    override_bundles_path: Path,
    override_alignment_path: Path,
    output_dir: Path,
    new_version: bool,
) -> dict[str, Any]:
    base_manifest = json.loads(base_manifest_path.read_text(encoding="utf-8"))
    override_manifest = json.loads(
        override_manifest_path.read_text(encoding="utf-8")
    )
    if not str(base_manifest.get("status", "")).startswith("complete"):
        raise GateError("base alignment stage is not complete")
    if not str(override_manifest.get("status", "")).startswith("complete"):
        raise GateError("override alignment stage is not complete")

    base_bundles = load_jsonl(base_bundles_path)
    override_bundles = load_jsonl(override_bundles_path)
    base_bundle_map = _by_task(base_bundles, "base bundles")
    override_bundle_map = _by_task(override_bundles, "override bundles")
    if not override_bundle_map or not set(override_bundle_map).issubset(base_bundle_map):
        raise GateError("override bundle task IDs are not a non-empty base subset")
    merged_bundles = [
        override_bundle_map.get(str(row["alignment_task_id"]), row)
        for row in base_bundles
    ]

    base_rows = load_jsonl(base_alignment_path)
    override_rows = load_jsonl(override_alignment_path)
    override_row_map = {
        task_id: row
        for task_id, row in _by_task(override_rows, "override alignment").items()
        if task_id in override_bundle_map
    }
    if set(override_row_map) != set(override_bundle_map):
        raise GateError("override alignment is missing one or more bundle tasks")
    merged_rows = [
        override_row_map.get(str(row.get("alignment_task_id") or ""), row)
        for row in base_rows
    ]
    if len(merged_rows) != len(base_rows):
        raise GateError("overlay changed alignment row count")

    merge_counts = Counter(
        str(bundle.get("merge_status")) for bundle in merged_bundles
    )
    accepted_count = merge_counts["agreed_accepted"]
    agreed_exclude_count = merge_counts["agreed_exclude"]
    reviewer_disagreement_count = merge_counts["disagreed_excluded"]
    details = {
        "base_selected_task_count": len(base_bundles),
        "override_task_count": len(override_bundles),
        "selected_task_count": len(merged_bundles),
        "accepted_count": accepted_count,
        "agreed_exclude_count": agreed_exclude_count,
        "reviewer_disagreement_count": reviewer_disagreement_count,
        "disagreement_count": reviewer_disagreement_count,
        "excluded_count": agreed_exclude_count + reviewer_disagreement_count,
        "accepted_alignment_row_count": sum(
            bool(row.get("accepted_for_evidence")) for row in merged_rows
        ),
        "pending_alignment_count": sum(
            row.get("dual_verifier_status") == "pending_dual_verification"
            for row in merged_rows
        ),
        "claude_calls_made": 0,
        "source_invocation_count": (
            int((base_manifest.get("details") or {}).get("source_invocation_count") or 46)
            + int((override_manifest.get("details") or {}).get("invocation_count") or 0)
        ),
        "overlay_only": True,
    }

    ensure_output_available(output_dir, new_version)
    alignment_path = output_dir / "claude_http_alignment.canonical.jsonl"
    bundles_path = output_dir / "alignment_verifier_bundles.canonical.jsonl"
    summary_path = output_dir / "overlay_summary.json"
    write_jsonl(alignment_path, merged_rows)
    write_jsonl(bundles_path, merged_bundles)
    write_json(
        summary_path,
        {
            "schema_version": "atobench.alignment_verifier_overlay.v1",
            "status": "complete_no_calls",
            **details,
        },
    )
    manifest = stage_manifest(
        "04h_overlay_alignment_rule_repair",
        [
            base_manifest_path,
            base_bundles_path,
            base_alignment_path,
            override_manifest_path,
            override_bundles_path,
            override_alignment_path,
        ],
        [alignment_path, bundles_path, summary_path],
        "complete_no_calls",
        dry_run=False,
        details=details,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-manifest", type=Path, required=True)
    parser.add_argument("--base-bundles", type=Path, required=True)
    parser.add_argument("--base-alignment", type=Path, required=True)
    parser.add_argument("--override-manifest", type=Path, required=True)
    parser.add_argument("--override-bundles", type=Path, required=True)
    parser.add_argument("--override-alignment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            base_manifest_path=args.base_manifest,
            base_bundles_path=args.base_bundles,
            base_alignment_path=args.base_alignment,
            override_manifest_path=args.override_manifest,
            override_bundles_path=args.override_bundles,
            override_alignment_path=args.override_alignment,
            output_dir=args.output,
            new_version=args.new_version,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    return 2
