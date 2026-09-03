from __future__ import annotations

import argparse
import json
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


def _reclassified_status(bundle: dict[str, Any]) -> str:
    decisions = bundle.get("decisions") or []
    if len(decisions) != 2:
        raise GateError("alignment bundle must contain exactly two decisions")
    left, right = decisions
    same_outcome = (
        left.get("selection_status") == right.get("selection_status")
        and left.get("selected_candidate_id") == right.get("selected_candidate_id")
    )
    if bundle.get("accepted_for_evidence") is True:
        return "agreed_accepted"
    return "agreed_exclude" if same_outcome else "disagreed_excluded"


def run(
    *,
    source_manifest_path: Path,
    source_bundles_path: Path,
    source_alignment_path: Path,
    output_dir: Path,
    new_version: bool,
) -> dict[str, Any]:
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if source_manifest.get("status") != "complete":
        raise GateError("source alignment-verifier stage is not complete")
    bundles = load_jsonl(source_bundles_path)
    alignments = load_jsonl(source_alignment_path)
    if len(bundles) != int(
        (source_manifest.get("details") or {}).get("selected_task_count", -1)
    ):
        raise GateError("source bundle count does not match source manifest")

    statuses: dict[str, str] = {}
    corrected_bundles: list[dict[str, Any]] = []
    for bundle in bundles:
        task_id = str(bundle.get("alignment_task_id") or "")
        if not task_id or task_id in statuses:
            raise GateError("source bundles contain missing or duplicate task IDs")
        status = _reclassified_status(bundle)
        statuses[task_id] = status
        corrected = dict(bundle)
        corrected["merge_status"] = status
        corrected_bundles.append(corrected)

    corrected_rows: list[dict[str, Any]] = []
    matched: set[str] = set()
    for row in alignments:
        task_id = str(row.get("alignment_task_id") or "")
        if task_id in statuses:
            corrected = dict(row)
            corrected["dual_verifier_status"] = statuses[task_id]
            corrected_rows.append(corrected)
            matched.add(task_id)
        else:
            corrected_rows.append(row)
    if matched != set(statuses):
        raise GateError("not every bundle task was found in validated alignment")

    accepted_count = sum(
        bundle.get("merge_status") == "agreed_accepted"
        for bundle in corrected_bundles
    )
    agreed_exclude_count = sum(
        bundle.get("merge_status") == "agreed_exclude"
        for bundle in corrected_bundles
    )
    reviewer_disagreement_count = sum(
        bundle.get("merge_status") == "disagreed_excluded"
        for bundle in corrected_bundles
    )
    details = {
        **(source_manifest.get("details") or {}),
        "accepted_count": accepted_count,
        "agreed_exclude_count": agreed_exclude_count,
        "reviewer_disagreement_count": reviewer_disagreement_count,
        "disagreement_count": reviewer_disagreement_count,
        "excluded_count": agreed_exclude_count + reviewer_disagreement_count,
        "claude_calls_made": 0,
        "source_invocation_count": (source_manifest.get("details") or {}).get(
            "invocation_count"
        ),
        "reclassification_only": True,
    }

    ensure_output_available(output_dir, new_version)
    alignment_path = output_dir / "claude_http_alignment.reclassified.jsonl"
    bundles_path = output_dir / "alignment_verifier_bundles.reclassified.jsonl"
    summary_path = output_dir / "reclassification_summary.json"
    write_jsonl(alignment_path, corrected_rows)
    write_jsonl(bundles_path, corrected_bundles)
    write_json(
        summary_path,
        {
            "schema_version": "atobench.alignment_verifier_reclassification.v1",
            "status": "complete_no_calls",
            **details,
        },
    )
    manifest = stage_manifest(
        "04f_reclassify_alignment_verifier_results",
        [source_manifest_path, source_bundles_path, source_alignment_path],
        [alignment_path, bundles_path, summary_path],
        "complete_no_calls",
        dry_run=False,
        details=details,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--source-bundles", type=Path, required=True)
    parser.add_argument("--source-alignment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            source_manifest_path=args.source_manifest,
            source_bundles_path=args.source_bundles,
            source_alignment_path=args.source_alignment,
            output_dir=args.output,
            new_version=args.new_version,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    return 2
