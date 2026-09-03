"""Audit persisted artifacts for an ATOBench cross-model campaign.

This is a data-integrity gate, not a behavioral adjudicator. It verifies that a
campaign has enough machine-readable material for later BRS aggregation and
blind report adjudication:

- campaign manifest and event log are parseable;
- each model/AOU config has committed C0/C1 assignment attempts;
- every committed episode has a distinct episode id and saved run artifacts;
- paired C0/C1 target-state fingerprints match;
- turns/episodes/orchestrator JSONL files are parseable with true file-line
  iteration (do not use str.splitlines() on HTTP bodies that may contain binary
  control characters);
- final reports and run-validity records exist.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class JsonlStats:
    path: str
    records: int
    bad_records: int
    bytes: int


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def _jsonl_stats(path: Path, *, relative_to: Path) -> JsonlStats:
    records = 0
    bad = 0
    if path.exists():
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                    records += 1
                except json.JSONDecodeError:
                    bad += 1
    return JsonlStats(
        path=str(path.relative_to(relative_to)),
        records=records,
        bad_records=bad,
        bytes=path.stat().st_size if path.exists() else 0,
    )


def _latest_committed_attempts(experiment_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    by_slot_condition: dict[tuple[str, str], dict[str, Any]] = {}
    for attempt in experiment_manifest.get("protocol_v3_assignment_attempts") or []:
        if attempt.get("status") != "committed":
            continue
        key = (str(attempt.get("canonical_slot")), str(attempt.get("condition")))
        by_slot_condition[key] = attempt
    return list(by_slot_condition.values())


def _archived_run_dir(experiment_dir: Path, episode_id: str) -> Path | None:
    matches = sorted((experiment_dir / "frozen_runs").glob(f"**/runs/{episode_id}"))
    if matches:
        return matches[0]
    return None


def _run_artifact_check(attempt: dict[str, Any], *, experiment_dir: Path) -> dict[str, Any]:
    validity = attempt.get("validity") or {}
    artifacts = validity.get("artifacts") or {}
    episode_id = str(attempt.get("episode_id") or "")
    archived = _archived_run_dir(experiment_dir, episode_id) if episode_id else None
    run_dir = archived or Path(str(validity.get("run_dir") or ""))

    if archived is not None:
        turns_path = archived / "turns.jsonl"
        report_path = archived / "final_report.txt"
        summary_path = archived / "episode_summary.json"
    else:
        turns_path = Path(str(artifacts.get("turns_jsonl") or ""))
        report_path = Path(str(artifacts.get("final_report") or ""))
        summary_path = Path(str(artifacts.get("episode_summary") or ""))

    turns_parseable = False
    turns_records = 0
    turns_bad = 0
    if turns_path.exists():
        with turns_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                    turns_records += 1
                except json.JSONDecodeError:
                    turns_bad += 1
        turns_parseable = turns_bad == 0

    report_chars = 0
    if report_path.exists():
        report_chars = len(report_path.read_text(encoding="utf-8", errors="replace").strip())

    return {
        "episode_id": attempt.get("episode_id"),
        "condition": attempt.get("condition"),
        "canonical_slot": attempt.get("canonical_slot"),
        "target_state_fingerprint": attempt.get("target_state_fingerprint"),
        "artifact_source": "frozen_archive" if archived is not None else "validity_artifacts",
        "validity_status": validity.get("status"),
        "validity_is_valid": validity.get("is_valid"),
        "validity_reasons": validity.get("reasons") or [],
        "validity_warnings": validity.get("warnings") or [],
        "run_dir": str(run_dir) if str(run_dir) else None,
        "run_dir_exists": run_dir.exists() if str(run_dir) else False,
        "turns_jsonl": str(turns_path) if str(turns_path) else None,
        "turns_exists": turns_path.exists(),
        "turns_parseable": turns_parseable,
        "turns_records": turns_records,
        "turns_bad_records": turns_bad,
        "turns_count_from_validity": (validity.get("counts") or {}).get("turns"),
        "deceptive_turns_from_validity": (validity.get("counts") or {}).get("deceptive_turns"),
        "episode_summary_exists": summary_path.exists() if str(summary_path) else False,
        "final_report_exists": report_path.exists() if str(report_path) else False,
        "final_report_chars": report_chars,
    }


def audit_campaign(campaign_root: Path) -> dict[str, Any]:
    campaign_root = campaign_root.resolve()
    manifest_path = campaign_root / "campaign_manifest.json"
    errors: list[str] = []
    warnings: list[str] = []

    if not manifest_path.exists():
        raise FileNotFoundError(f"missing campaign manifest: {manifest_path}")

    manifest = _read_json(manifest_path)
    planned = manifest.get("planned_episodes") or []
    configs = manifest.get("configs") or []
    expected_planned = int(manifest.get("rounds") or 0) * len(manifest.get("models") or []) * len(manifest.get("aous") or []) * len(manifest.get("conditions") or [])
    if len(planned) != expected_planned:
        errors.append(f"planned_episode_count_mismatch expected={expected_planned} actual={len(planned)}")

    jsonl_files = [campaign_root / "events.jsonl"]
    jsonl_files.extend(sorted((campaign_root / "logs").glob("*/*/turns.jsonl")))
    jsonl_files.extend(sorted((campaign_root / "logs").glob("*/*/episodes.jsonl")))
    jsonl_files.extend(sorted((campaign_root / "logs").glob("*/*/orchestrator.jsonl")))
    jsonl_stats = [_jsonl_stats(path, relative_to=campaign_root) for path in jsonl_files]
    for stats in jsonl_stats:
        if stats.bad_records:
            errors.append(f"jsonl_parse_error path={stats.path} bad_records={stats.bad_records}")

    pair_records: list[dict[str, Any]] = []
    episode_ids: set[str] = set()
    duplicate_episode_ids: set[str] = set()

    for cfg_record in configs:
        cfg_path = Path(str(cfg_record["config"]))
        cfg = _read_yaml(cfg_path)
        target_dir = Path(str((cfg.get("target") or {})["target_dir"]))
        experiment_id = str(cfg["experiment_id"])
        experiment_dir = target_dir / "experiments" / experiment_id
        experiment_manifest_path = experiment_dir / "manifest.json"
        if not experiment_manifest_path.exists():
            errors.append(f"experiment_manifest_missing config={cfg_path}")
            continue
        experiment_manifest = _read_json(experiment_manifest_path)
        committed = _latest_committed_attempts(experiment_manifest)
        by_block: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for attempt in committed:
            canonical_slot = str(attempt.get("canonical_slot") or "")
            block = canonical_slot.rsplit(":", 1)[0]
            condition = str(attempt.get("condition"))
            by_block[block][condition] = attempt

            episode_id = str(attempt.get("episode_id") or "")
            if episode_id in episode_ids:
                duplicate_episode_ids.add(episode_id)
            if episode_id:
                episode_ids.add(episode_id)

        for block, conditions in sorted(by_block.items()):
            c0 = conditions.get("C0")
            c1 = conditions.get("C1")
            pair_id = f"{manifest.get('campaign_id')}::{cfg_record.get('model')}::{cfg_record.get('aou')}::{block.split(':')[-1]}"
            if c0 is None or c1 is None:
                errors.append(f"pair_missing_condition pair_id={pair_id} conditions={sorted(conditions)}")
            c0_check = _run_artifact_check(c0, experiment_dir=experiment_dir) if c0 else None
            c1_check = _run_artifact_check(c1, experiment_dir=experiment_dir) if c1 else None
            fingerprint_match = bool(
                c0
                and c1
                and c0.get("target_state_fingerprint")
                and c0.get("target_state_fingerprint") == c1.get("target_state_fingerprint")
            )
            if c0 and c1 and not fingerprint_match:
                errors.append(f"pair_fingerprint_mismatch pair_id={pair_id}")
            for label, check in (("C0", c0_check), ("C1", c1_check)):
                if not check:
                    continue
                if not check["validity_is_valid"]:
                    errors.append(f"invalid_run pair_id={pair_id} condition={label}")
                if not check["turns_exists"] or not check["turns_parseable"] or not check["turns_records"]:
                    errors.append(f"turns_unusable pair_id={pair_id} condition={label}")
                if not check["final_report_exists"] or not check["final_report_chars"]:
                    errors.append(f"final_report_unusable pair_id={pair_id} condition={label}")
                if label == "C0" and check["deceptive_turns_from_validity"] not in (0, None):
                    errors.append(f"c0_deceptive_turns_nonzero pair_id={pair_id}")
                if label == "C1" and check["deceptive_turns_from_validity"] == 0:
                    warnings.append(f"c1_no_deceptive_contact pair_id={pair_id}")
            pair_records.append(
                {
                    "pair_id": pair_id,
                    "model": cfg_record.get("model"),
                    "model_selector": cfg_record.get("model_selector"),
                    "aou": cfg_record.get("aou"),
                    "unit_id": cfg_record.get("unit_id"),
                    "block": block,
                    "fingerprint_match": fingerprint_match,
                    "c0": c0_check,
                    "c1": c1_check,
                }
            )

    if duplicate_episode_ids:
        errors.append("duplicate_episode_ids=" + ",".join(sorted(duplicate_episode_ids)))

    status = "pass" if not errors else "fail"
    return {
        "schema_version": "atobench.cross_model_artifact_audit.v1",
        "campaign_root": str(campaign_root),
        "campaign_id": manifest.get("campaign_id"),
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "planned_episodes": len(planned),
            "expected_planned_episodes": expected_planned,
            "configs": len(configs),
            "pairs": len(pair_records),
            "unique_episode_ids": len(episode_ids),
            "jsonl_files_checked": len(jsonl_stats),
        },
        "jsonl": [stats.__dict__ for stats in jsonl_stats],
        "pairs": pair_records,
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Cross-model artifact audit",
        "",
        f"Campaign: `{report['campaign_id']}`",
        f"Status: `{report['status']}`",
        "",
        "## Counts",
        "",
    ]
    for key, value in report["counts"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Errors", ""])
    if report["errors"]:
        lines.extend(f"- `{item}`" for item in report["errors"])
    else:
        lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    if report["warnings"]:
        lines.extend(f"- `{item}`" for item in report["warnings"])
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Pair inventory",
            "",
            "| Pair | Model | AOU | C0 episode | C1 episode | fingerprint | C0 turns | C1 turns | C1 deceptive |",
            "|---|---|---|---|---|---|---:|---:|---:|",
        ]
    )
    for pair in report["pairs"]:
        c0 = pair.get("c0") or {}
        c1 = pair.get("c1") or {}
        lines.append(
            "| {pair_id} | {model} | {aou} | `{c0_ep}` | `{c1_ep}` | {fp} | {c0_turns} | {c1_turns} | {c1_dec} |".format(
                pair_id=pair["pair_id"],
                model=pair["model"],
                aou=pair["aou"],
                c0_ep=c0.get("episode_id"),
                c1_ep=c1.get("episode_id"),
                fp="match" if pair.get("fingerprint_match") else "mismatch",
                c0_turns=c0.get("turns_records"),
                c1_turns=c1.get("turns_records"),
                c1_dec=c1.get("deceptive_turns_from_validity"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit saved artifacts for an ATOBench cross-model campaign.")
    parser.add_argument("campaign_root", type=Path)
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument("--md-output", type=Path, default=None)
    args = parser.parse_args()

    report = audit_campaign(args.campaign_root)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.md_output:
        args.md_output.parent.mkdir(parents=True, exist_ok=True)
        args.md_output.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "errors": report["errors"], "warnings": report["warnings"], "counts": report["counts"]}, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
