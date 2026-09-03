from __future__ import annotations

import argparse
import collections
from pathlib import Path
from typing import Any

from .common import GateError, load_jsonl, stage_manifest, write_json, write_jsonl


def run(
    *,
    report_manifest_path: Path,
    output_dir: Path,
    per_stratum: int,
) -> dict[str, Any]:
    if per_stratum < 1:
        raise GateError("--per-stratum must be positive")
    reports = load_jsonl(report_manifest_path)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in reports:
        key = (str(row.get("aou") or ""), str(row.get("condition") or ""))
        grouped[key].append(row)
    expected = {(aou, condition) for aou in ("sqli", "basket", "jwt") for condition in ("C0", "C1")}
    if set(grouped) != expected:
        raise GateError(
            f"unexpected AOU/condition strata: {sorted(set(grouped) ^ expected)}"
        )
    selected: list[dict[str, Any]] = []
    model_order = sorted(
        {str(row.get("model") or "") for row in reports if row.get("model")}
    )
    for stratum_index, key in enumerate(sorted(expected)):
        rows = sorted(
            grouped[key],
            key=lambda row: (
                str(row.get("model") or ""),
                str(row.get("episode_id") or ""),
            ),
        )
        chosen: list[dict[str, Any]] = []
        used_models: set[str] = set()
        rows_by_model = {
            model: [row for row in rows if str(row.get("model") or "") == model]
            for model in model_order
        }
        start = (stratum_index * per_stratum) % len(model_order)
        rotated_models = model_order[start:] + model_order[:start]
        for model in rotated_models:
            if rows_by_model[model]:
                chosen.append(rows_by_model[model][0])
                used_models.add(model)
                if len(chosen) == per_stratum:
                    break
        if len(chosen) < per_stratum:
            for row in rows:
                if row not in chosen:
                    chosen.append(row)
                if len(chosen) == per_stratum:
                    break
        if len(chosen) != per_stratum:
            raise GateError(f"insufficient reports in stratum {key}")
        selected.extend(chosen)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "judge_packet_pilot_episode_manifest.jsonl"
    summary_path = output_dir / "judge_packet_pilot_selection.json"
    write_jsonl(manifest_path, selected)
    details = {
        "episode_count": len(selected),
        "planned_packet_count": len(selected) * 3,
        "per_aou_condition_stratum": per_stratum,
        "aou_condition_strata": 6,
        "model_counts": dict(
            collections.Counter(str(row.get("model")) for row in selected)
        ),
        "selection_uses_outcomes": False,
        "llm_calls": 0,
    }
    write_json(summary_path, details)
    manifest = stage_manifest(
        "07a_select_judge_packet_engineering_pilot",
        [report_manifest_path],
        [manifest_path, summary_path],
        "complete",
        dry_run=False,
        details=details,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-stratum", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        result = run(
            report_manifest_path=args.report_manifest,
            output_dir=args.output,
            per_stratum=args.per_stratum,
        )
    except GateError as exc:
        parser.error(str(exc))
    print(result)
    return 0
