from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from .census import build_census
from .common import (
    GateError,
    ensure_output_available,
    is_probably_real_path,
    read_json,
    read_json_or_yaml,
    require_real_data_authorization,
    sha256_file,
    sha256_json,
    stage_manifest,
    write_csv,
    write_json,
    write_jsonl,
)
from .redaction import (
    REDACTION_VERSION,
    StableRedactor,
    residual_sensitive_kinds,
    summarize_large_value,
)
from .schemas import validate_judgment
from .sessions import (
    build_action_cycles,
    discover_candidate_session_trees,
    discover_session_files,
    flatten_session,
    session_inventory_row,
    session_tree_inventory_row,
)

STAGES = {
    "01": "census_claude_sessions",
    "02": "flatten_claude_sessions",
    "03": "redact_session_records",
    "04": "align_claude_http",
    "05": "build_episode_fact_registry",
    "06": "extract_report_claim_atoms",
    "07": "build_judge_packets",
    "08": "run_trajectory_judges",
    "09": "adjudicate_judgments",
    "10": "validate_judges",
    "11": "build_episode_states",
    "12": "build_pair_profiles",
    "13": "run_resilience_statistics",
    "14": "export_results_facts",
    "15": "run_qa",
}


def _registered_roots(registry: Path) -> list[Path]:
    value = read_json_or_yaml(registry)
    return [Path(row["root"]) for row in value.get("campaigns", [])]


def _path_list(value: Path | list[Path]) -> list[Path]:
    return value if isinstance(value, list) else [value]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _csv_true(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _literal(value: str, expected: type) -> Any:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError) as exc:
        raise GateError(f"invalid census literal: {value[:120]}") from exc
    if not isinstance(parsed, expected):
        raise GateError(f"expected {expected.__name__} census literal")
    return parsed


def run_census(registry: Path, claude_root: Path | list[Path], output: Path, *, allow_real_data: bool, dry_run: bool, new_version: bool) -> dict[str, Any]:
    roots = _registered_roots(registry)
    claude_roots = _path_list(claude_root)
    require_real_data_authorization([*roots, *claude_roots], allow_real_data)
    if dry_run:
        return {
            "stage": "01_census_claude_sessions",
            "status": "dry_run",
            "campaign_count": len(roots),
            "claude_source_root_count": len(claude_roots),
        }
    ensure_output_available(output, new_version)
    campaigns, pairs, episodes, errors = build_census(roots)
    episode_ids = {row["episode_id"] for row in episodes if row.get("episode_id")}
    campaign_ids = {row["campaign_id"] for row in campaigns}
    candidate_trees: dict[Path, dict[str, Any]] = {}
    for source_root in claude_roots:
        discovered = discover_candidate_session_trees(
            source_root, episode_ids, campaign_ids
        )
        for tree_root, candidate in discovered.items():
            tree = candidate_trees.setdefault(
                tree_root,
                {
                    "root": tree_root,
                    "files": [],
                    "episode_ids": set(),
                    "path_campaign_match": False,
                    "source_roots": set(),
                },
            )
            tree["files"] = sorted({*tree["files"], *candidate["files"]})
            tree["episode_ids"].update(candidate["episode_ids"])
            tree["path_campaign_match"] = (
                tree["path_campaign_match"] or candidate["path_campaign_match"]
            )
            tree["source_roots"].add(str(source_root.resolve()))
    session_rows: list[dict[str, Any]] = []
    for root, candidate in sorted(candidate_trees.items(), key=lambda item: str(item[0])):
        row = session_tree_inventory_row(root, candidate["files"])
        row["path_campaign_match"] = candidate["path_campaign_match"]
        row["source_roots"] = sorted(candidate["source_roots"])
        row["engagement_ids"] = sorted(set(row["engagement_ids"]) & episode_ids)
        session_rows.append(row)
    session_files = sorted(
        {path for candidate in candidate_trees.values() for path in candidate["files"]}
    )
    file_source_roots: dict[Path, set[str]] = {}
    for candidate in candidate_trees.values():
        for path in candidate["files"]:
            file_source_roots.setdefault(path, set()).update(candidate["source_roots"])
    file_rows = []
    for path in session_files:
        row = session_inventory_row(path)
        row["source_roots"] = sorted(file_source_roots[path])
        file_rows.append(row)
    by_engagement: dict[str, list[dict[str, Any]]] = {}
    by_report_hash: dict[str, list[dict[str, Any]]] = {}
    tree_conflicts: list[dict[str, Any]] = []
    for session in session_rows:
        known_ids = session["engagement_ids"]
        if len(known_ids) > 1:
            tree_conflicts.append(
                {
                    "canonical_session_tree_id": session["canonical_session_tree_id"],
                    "error": "one session tree contains multiple canonical episode IDs",
                    "episode_ids": known_ids,
                }
            )
        for engagement_id in known_ids:
            by_engagement.setdefault(engagement_id, []).append(session)
        for text_hash in session["_exact_text_hashes"]:
            by_report_hash.setdefault(text_hash, []).append(session)
    mapping_rows: list[dict[str, Any]] = []
    mapped_by_pair: dict[str, int] = {}
    for episode in episodes:
        exact_candidates = by_engagement.get(episode.get("episode_id"), [])
        report_candidates = (
            by_report_hash.get(episode.get("final_report_sha256"), [])
            if episode.get("final_report_sha256")
            else []
        )
        candidates = exact_candidates or report_candidates
        mapping_basis = "exact_episode_id" if exact_candidates else (
            "exact_final_report_hash" if report_candidates else "none"
        )
        if len(candidates) == 1:
            candidate = candidates[0]
            invalid_reasons = []
            if candidate["main_session_count"] != 1:
                invalid_reasons.append("main_session_count_not_one")
            if candidate["parse_error_count"]:
                invalid_reasons.append("session_parse_error")
            if len(candidate["engagement_ids"]) > 1:
                invalid_reasons.append("tree_maps_multiple_episodes")
            if invalid_reasons:
                mapping_status = "invalid_tree"
            else:
                mapping_status = (
                    "unique_exact_tree"
                    if mapping_basis == "exact_episode_id"
                    else "unique_report_hash"
                )
        else:
            candidate = None
            invalid_reasons = []
            mapping_status = "ambiguous_tree" if len(candidates) > 1 else "missing"
        if mapping_status in {"unique_exact_tree", "unique_report_hash"}:
            mapped_by_pair[episode["global_pair_id"]] = mapped_by_pair.get(episode["global_pair_id"], 0) + 1
        mapping_rows.append(
            {
                "episode_id": episode["episode_id"],
                "global_episode_id": episode["global_episode_id"],
                "global_pair_id": episode["global_pair_id"],
                "campaign_id": episode["campaign_id"],
                "aou": episode.get("aou", ""),
                "condition": episode["condition"],
                "model": episode.get("model", ""),
                "main_session_id": candidate["main_session_id"] if candidate else "",
                "subagent_ids": candidate["subagent_ids"] if candidate else [],
                "canonical_session_tree_id": candidate["canonical_session_tree_id"] if candidate else "",
                "session_source_roots": candidate["source_roots"] if candidate else [],
                "session_file_paths": candidate["session_file_paths"] if candidate else [],
                "file_sha256": candidate["file_sha256"] if candidate else {},
                "engagement_id_match": bool(exact_candidates) and len(exact_candidates) == 1,
                "final_report_hash_match": bool(report_candidates) and len(report_candidates) == 1,
                "mapping_basis": mapping_basis,
                "mapping_status": mapping_status,
                "exclusion_reason": (
                    ""
                    if mapping_status in {"unique_exact_tree", "unique_report_hash"}
                    else ",".join(invalid_reasons) or mapping_status
                ),
            }
        )
    pair_coverage_rows = [
        {
            **pair,
            "mapped_episode_count": mapped_by_pair.get(pair["global_pair_id"], 0),
            "complete_log_pair": (
                pair.get("execution_valid_pair", pair["pair_complete"])
                and mapped_by_pair.get(pair["global_pair_id"], 0) == 2
            ),
        }
        for pair in pairs
    ]
    outputs = [
        output / "campaign_census.csv",
        output / "pair_census.csv",
        output / "episode_census.csv",
        output / "claude_file_inventory.csv",
        output / "session_inventory.csv",
        output / "episode_session_mapping.csv",
        output / "pair_log_coverage.csv",
        output / "census_errors.jsonl",
        output / "census_reconciliation.json",
    ]
    write_csv(outputs[0], campaigns, ["campaign_id", "root", "manifest_status", "planned_pair_count", "planned_episode_count", "execution_valid_episode_count", "paired_analysis_eligible_episode_count", "manifest_failure_count", "raw_episode_count", "unmatched_raw_episode_count"])
    write_csv(outputs[1], pairs, ["campaign_id", "pair_id", "global_pair_id", "model", "aou", "unit_id", "block_id", "planned_c0", "planned_c1", "execution_valid_c0", "execution_valid_c1", "pair_complete", "execution_valid_pair"])
    write_csv(outputs[2], episodes, ["campaign_id", "episode_id", "pair_id", "global_pair_id", "global_episode_id", "condition", "slot", "model", "model_selector", "aou", "unit_id", "block_id", "planned", "event_started", "event_complete", "returncode", "raw_episode_present", "raw_status", "outcome", "started_at", "ended_at", "raw_episode_source", "final_report_sha256", "execution_valid", "paired_analysis_eligible", "mapping_basis", "event_count", "terminal_status"])
    write_csv(outputs[3], file_rows, ["session_id", "source_path", "source_sha256", "source_roots", "is_subagent", "record_count", "parse_error_count", "optional_sidecar_output_count", "output_coverage_status", "missing_output_inferred", "engagement_ids"])
    public_session_rows = [{key: value for key, value in row.items() if not key.startswith("_")} for row in session_rows]
    write_csv(outputs[4], public_session_rows, ["canonical_session_tree_id", "tree_root", "source_roots", "main_session_id", "main_session_count", "subagent_ids", "session_file_paths", "file_sha256", "record_count", "parse_error_count", "optional_sidecar_output_count", "output_coverage_status", "missing_output_inferred", "engagement_ids", "path_campaign_match"])
    write_csv(outputs[5], mapping_rows, ["episode_id", "global_episode_id", "global_pair_id", "campaign_id", "aou", "condition", "model", "main_session_id", "subagent_ids", "canonical_session_tree_id", "session_source_roots", "session_file_paths", "file_sha256", "engagement_id_match", "final_report_hash_match", "mapping_basis", "mapping_status", "exclusion_reason"])
    write_csv(outputs[6], pair_coverage_rows, ["campaign_id", "pair_id", "global_pair_id", "model", "aou", "planned_c0", "planned_c1", "execution_valid_c0", "execution_valid_c1", "pair_complete", "execution_valid_pair", "mapped_episode_count", "complete_log_pair"])
    errors.extend(tree_conflicts)
    write_jsonl(outputs[7], errors)
    blocking_conflict_count = len(tree_conflicts) + sum(
        row["mapping_status"] in {"ambiguous_tree", "invalid_tree"}
        for row in mapping_rows
    )
    mapping_status_values = (
        "unique_exact_tree",
        "unique_report_hash",
        "ambiguous_tree",
        "invalid_tree",
        "missing",
    )
    episode_by_global_id = {
        row["global_episode_id"]: row for row in episodes
    }
    reconciliation_campaigns = []
    for campaign in campaigns:
        campaign_id = campaign["campaign_id"]
        campaign_mappings = [
            row for row in mapping_rows if row["campaign_id"] == campaign_id
        ]
        mapped_global_ids = {
            row["global_episode_id"]
            for row in campaign_mappings
            if row["mapping_status"] in {"unique_exact_tree", "unique_report_hash"}
        }
        reconciliation_campaigns.append(
            {
                "campaign_id": campaign_id,
                "planned_episode_count": campaign.get("planned_episode_count", 0),
                "execution_valid_episode_count": campaign.get(
                    "execution_valid_episode_count", 0
                ),
                "paired_analysis_eligible_episode_count": campaign.get(
                    "paired_analysis_eligible_episode_count", 0
                ),
                "mapped_episode_count": len(mapped_global_ids),
                "mapped_paired_analysis_eligible_episode_count": sum(
                    bool(episode_by_global_id[global_id].get("paired_analysis_eligible"))
                    for global_id in mapped_global_ids
                ),
                "missing_session_mapping_count": sum(
                    row["mapping_status"] == "missing"
                    for row in campaign_mappings
                ),
            }
        )
    paired_eligible_count = sum(
        bool(row.get("paired_analysis_eligible")) for row in episodes
    )
    successful_singletons = [
        row["global_episode_id"]
        for row in episodes
        if row.get("execution_valid") and not row.get("paired_analysis_eligible")
    ]
    mapped_paired_count = sum(
        row["mapping_status"] in {"unique_exact_tree", "unique_report_hash"}
        and bool(episode_by_global_id[row["global_episode_id"]].get("paired_analysis_eligible"))
        for row in mapping_rows
    )
    reconciliation = {
        "schema_version": "atobench.census_reconciliation.v1",
        "scope": "four immutable registry campaigns only",
        "unregistered_in_progress_campaigns_excluded": True,
        "planned_episode_count": len(episodes),
        "execution_valid_episode_count_before_pair_gate": sum(
            bool(row.get("execution_valid")) for row in episodes
        ),
        "successful_singleton_episode_count": len(successful_singletons),
        "successful_singleton_global_episode_ids": successful_singletons,
        "execution_invalid_episode_count": sum(
            not bool(row.get("execution_valid")) for row in episodes
        ),
        "paired_analysis_eligible_episode_count": paired_eligible_count,
        "execution_valid_pair_count": sum(
            bool(row.get("execution_valid_pair")) for row in pairs
        ),
        "reconciliation_equation": (
            f"{len(episodes)} planned = {paired_eligible_count} paired-eligible + "
            f"{len(successful_singletons)} successful singletons excluded by pair gate + "
            f"{sum(not bool(row.get('execution_valid')) for row in episodes)} execution-invalid"
        ),
        "mapped_paired_analysis_eligible_episode_count": mapped_paired_count,
        "unmapped_paired_analysis_eligible_episode_count": (
            paired_eligible_count - mapped_paired_count
        ),
        "candidate_session_tree_count": len(session_rows),
        "claude_source_roots": [str(path.resolve()) for path in claude_roots],
        "inline_only_session_tree_count": sum(
            row["optional_sidecar_output_count"] == 0 for row in session_rows
        ),
        "session_tree_with_optional_sidecar_count": sum(
            row["optional_sidecar_output_count"] > 0 for row in session_rows
        ),
        "all_candidate_session_trees_inline_only": all(
            row["optional_sidecar_output_count"] == 0 for row in session_rows
        ),
        "zero_output_sidecars_interpretation": (
            "canonical JSONL contains inline outputs; zero optional tasks/*.output "
            "sidecars is not missing data"
        ),
        "blocking_conflict_count": blocking_conflict_count,
        "campaigns": reconciliation_campaigns,
    }
    write_json(outputs[8], reconciliation)
    manifest = stage_manifest(
        "01_census_claude_sessions",
        [registry, *claude_roots, *[root / "campaign_manifest.json" for root in roots], *[root / "events.jsonl" for root in roots]],
        outputs,
        "blocked_census_conflicts" if blocking_conflict_count else "complete",
        dry_run=False,
        details={
            "campaign_count": len(campaigns),
            "claude_source_root_count": len(claude_roots),
            "claude_source_roots": [str(path.resolve()) for path in claude_roots],
            "pair_count": len(pairs),
            "episode_count": len(episodes),
            "planned_episode_count": sum(row.get("planned_episode_count", 0) for row in campaigns),
            "execution_valid_episode_count": sum(row.get("execution_valid_episode_count", 0) for row in campaigns),
            "paired_analysis_eligible_episode_count": paired_eligible_count,
            "execution_valid_pair_count": sum(bool(row.get("execution_valid_pair")) for row in pairs),
            "candidate_session_tree_count": len(session_rows),
            "candidate_session_file_count": len(file_rows),
            "mapping_status_counts": {
                status: sum(row["mapping_status"] == status for row in mapping_rows)
                for status in mapping_status_values
            },
            "mapped_execution_valid_episode_count": sum(
                row["mapping_status"] in {"unique_exact_tree", "unique_report_hash"}
                and next(
                    episode["execution_valid"]
                    for episode in episodes
                    if episode["global_episode_id"] == row["global_episode_id"]
                )
                for row in mapping_rows
            ),
            "mapped_paired_analysis_eligible_episode_count": mapped_paired_count,
            "blocking_conflict_count": blocking_conflict_count,
            "inline_only_session_tree_count": sum(row["optional_sidecar_output_count"] == 0 for row in session_rows),
            "zero_sidecars_are_missing": False,
            "scope_note": "registry-only; unregistered in-progress campaigns excluded",
        },
    )
    write_json(output / "stage_manifest.json", manifest)
    return manifest


def run_flatten(
    session_root: Path | list[Path] | None,
    output: Path,
    *,
    mapping: Path | None = None,
    episode_census: Path | None = None,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    """Flatten only census-authorized session trees for real data.

    The root-scanning mode remains available for isolated synthetic smoke
    fixtures. A real source root without Stage 01 mapping inputs is blocked.
    """
    if mapping or episode_census:
        if not mapping or not episode_census:
            raise GateError("Stage 02 requires both --mapping and --episode-census")
        require_real_data_authorization([mapping, episode_census], allow_real_data)
        mapping_rows = _read_csv(mapping)
        census_rows = _read_csv(episode_census)
        eligible = {
            row["global_episode_id"]: row
            for row in census_rows
            if _csv_true(row.get("paired_analysis_eligible"))
        }
        selected = [
            row
            for row in mapping_rows
            if row.get("global_episode_id") in eligible
            and row.get("mapping_status") in {"unique_exact_tree", "unique_report_hash"}
        ]
        selected_by_global = {row["global_episode_id"]: row for row in selected}
        missing = sorted(set(eligible) - set(selected_by_global))
        if missing:
            raise GateError(
                f"Stage 02 paired cohort has {len(missing)} unmapped episodes"
            )
        if len(selected_by_global) != len(selected):
            raise GateError("Stage 02 has duplicate mappings for a canonical episode")
        file_owner: dict[Path, str] = {}
        work: list[tuple[dict[str, str], dict[str, str], list[Path], dict[str, str]]] = []
        for global_episode_id in sorted(eligible):
            episode = eligible[global_episode_id]
            mapped = selected_by_global[global_episode_id]
            paths = [Path(value) for value in _literal(mapped["session_file_paths"], list)]
            hashes = _literal(mapped["file_sha256"], dict)
            if not paths:
                raise GateError(f"mapped episode has no session files: {global_episode_id}")
            for path in paths:
                resolved = path.resolve()
                previous = file_owner.setdefault(resolved, global_episode_id)
                if previous != global_episode_id:
                    raise GateError(
                        f"session file reused across episodes: {resolved}"
                    )
                if not path.is_file():
                    raise GateError(f"mapped session file missing: {path}")
                expected_hash = hashes.get(str(resolved))
                actual_hash = sha256_file(path)
                if not expected_hash or expected_hash != actual_hash:
                    raise GateError(f"mapped session hash mismatch: {path}")
            work.append((episode, mapped, paths, hashes))
        files = sorted(file_owner)
        if dry_run:
            return {
                "stage": "02_flatten_claude_sessions",
                "status": "dry_run",
                "paired_episode_count": len(work),
                "file_count": len(files),
            }
        ensure_output_available(output, new_version)
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        report_candidates: list[dict[str, Any]] = []
        episodes_with_rows: set[str] = set()
        for episode, mapped, paths, _ in work:
            episode_rows: list[dict[str, Any]] = []
            for path in paths:
                flat, parse_errors = flatten_session(
                    path,
                    episode_id=episode["episode_id"],
                    session_tree_id=mapped["canonical_session_tree_id"],
                )
                episode_rows.extend(flat)
                errors.extend(parse_errors)
            episode_rows.sort(
                key=lambda row: (
                    row.get("timestamp") is None,
                    row.get("timestamp") or "",
                    row["source_file_sha256"],
                    row["source_line_number"],
                    row["message_uuid"],
                )
            )
            for sequence_index, row in enumerate(episode_rows):
                row["sequence_index"] = sequence_index
                row["global_episode_id"] = episode["global_episode_id"]
                row["global_pair_id"] = episode["global_pair_id"]
                row["campaign_id"] = episode["campaign_id"]
                row["condition"] = episode["condition"]
                row["model"] = episode["model"]
                row["aou"] = episode["aou"]
                text = row.get("visible_text")
                if isinstance(text, str):
                    text_hash = hashlib.sha256(text.encode()).hexdigest()
                    exact_report = text_hash == episode.get("final_report_sha256")
                    if exact_report or "FINAL_FINDINGS" in text:
                        report_candidates.append(
                            {
                                "episode_id": episode["episode_id"],
                                "global_episode_id": episode["global_episode_id"],
                                "session_tree_id": row["session_tree_id"],
                                "session_id": row["session_id"],
                                "agent_id": row.get("agent_id"),
                                "message_uuid": row["message_uuid"],
                                "message_content_id": row["message_content_id"],
                                "source_file_sha256": row["source_file_sha256"],
                                "source_line_number": row["source_line_number"],
                                "text_sha256": text_hash,
                                "canonical_final_report_sha256": episode.get(
                                    "final_report_sha256"
                                ),
                                "exact_canonical_report_hash_match": exact_report,
                                "contains_final_findings_marker": "FINAL_FINDINGS" in text,
                            }
                        )
            if episode_rows:
                episodes_with_rows.add(episode["global_episode_id"])
            rows.extend(episode_rows)
        missing_content = sorted(set(eligible) - episodes_with_rows)
        if missing_content:
            raise GateError(
                f"Stage 02 has {len(missing_content)} episodes without message content"
            )
        source_mode = "census_authorized_paired_cohort"
        expected_episode_count = len(work)
        stage_inputs = [mapping, episode_census, *files]
    else:
        if session_root is None:
            raise GateError("Stage 02 requires census inputs or --claude-root")
        session_roots = _path_list(session_root)
        if any(is_probably_real_path(root) for root in session_roots):
            raise GateError(
                "real Stage 02 root scanning is forbidden; provide "
                "--mapping and --episode-census"
            )
        require_real_data_authorization(session_roots, allow_real_data)
        files = sorted(
            {
                path
                for root in session_roots
                for path in discover_session_files(root)
            }
        )
        if dry_run:
            return {
                "stage": "02_flatten_claude_sessions",
                "status": "dry_run",
                "file_count": len(files),
            }
        ensure_output_available(output, new_version)
        rows = []
        errors = []
        report_candidates = []
        for path in files:
            flat, parse_errors = flatten_session(path)
            rows.extend(flat)
            errors.extend(parse_errors)
        source_mode = "synthetic_root_scan"
        expected_episode_count = None
        stage_inputs = files

    flat_path = output / "flattened_messages.jsonl"
    cycle_path = output / "action_cycles.jsonl"
    report_path = output / "final_report_candidates.csv"
    error_path = output / "parse_errors.jsonl"
    write_jsonl(flat_path, rows)
    cycles = build_action_cycles(rows)
    write_jsonl(cycle_path, cycles)
    write_csv(
        report_path,
        report_candidates,
        [
            "episode_id",
            "global_episode_id",
            "session_tree_id",
            "session_id",
            "agent_id",
            "message_uuid",
            "message_content_id",
            "source_file_sha256",
            "source_line_number",
            "text_sha256",
            "canonical_final_report_sha256",
            "exact_canonical_report_hash_match",
            "contains_final_findings_marker",
        ],
    )
    write_jsonl(error_path, errors)
    cycle_episode_ids = {row["episode_id"] for row in cycles}
    flattened_episode_ids = {row["episode_id"] for row in rows}
    duplicate_message_content_id_count = len(rows) - len(
        {row["message_content_id"] for row in rows}
    )
    duplicate_action_cycle_id_count = len(cycles) - len(
        {row["action_cycle_id"] for row in cycles}
    )
    exact_report_counts: dict[str, int] = {}
    for row in report_candidates:
        if row["exact_canonical_report_hash_match"]:
            exact_report_counts[row["episode_id"]] = (
                exact_report_counts.get(row["episode_id"], 0) + 1
            )
    final_report_exact_conflict_count = (
        sum(exact_report_counts.get(episode_id, 0) != 1 for episode_id in flattened_episode_ids)
        if source_mode == "census_authorized_paired_cohort"
        else 0
    )
    blocking_quality_count = (
        len(errors)
        + duplicate_message_content_id_count
        + duplicate_action_cycle_id_count
        + final_report_exact_conflict_count
    )
    status = "complete" if not blocking_quality_count else "blocked_quality_gates"
    manifest = stage_manifest(
        "02_flatten_claude_sessions",
        stage_inputs,
        [flat_path, cycle_path, report_path, error_path],
        status,
        dry_run=False,
        details={
            "source_mode": source_mode,
            "episode_count": len(flattened_episode_ids),
            "expected_episode_count": expected_episode_count,
            "message_item_count": len(rows),
            "action_cycle_count": len(cycles),
            "episode_with_action_cycle_count": len(cycle_episode_ids),
            "final_report_candidate_count": len(report_candidates),
            "exact_final_report_hash_match_count": sum(
                bool(row["exact_canonical_report_hash_match"])
                for row in report_candidates
            ),
            "final_report_exact_conflict_count": final_report_exact_conflict_count,
            "parse_error_count": len(errors),
            "duplicate_message_content_id_count": duplicate_message_content_id_count,
            "duplicate_action_cycle_id_count": duplicate_action_cycle_id_count,
            "cycles_without_linked_result_count": sum(
                not row["tool_result_content_ids"] for row in cycles
            ),
            "parsed_http_request_count": sum(
                len(row["parsed_http_requests"]) for row in cycles
            ),
            "blocking_quality_count": blocking_quality_count,
            "unmapped_rows_present": any(
                row.get("episode_id") == "UNMAPPED" for row in rows
            ),
        },
    )
    write_json(output / "stage_manifest.json", manifest)
    return manifest


def run_redact(
    flattened: Path,
    output: Path,
    *,
    action_cycles: Path | None = None,
    allow_real_data: bool = False,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    inputs = [flattened, *([action_cycles] if action_cycles else [])]
    require_real_data_authorization(inputs, allow_real_data)
    if is_probably_real_path(flattened) and action_cycles is None:
        raise GateError("real Stage 03 requires --action-cycles")
    source_manifest_path = flattened.parent / "stage_manifest.json"
    if source_manifest_path.is_file():
        source_manifest = read_json(source_manifest_path)
        if source_manifest.get("status") != "complete":
            raise GateError("Stage 03 requires a complete Stage 02 manifest")
        recorded_hashes = {
            Path(row["path"]).resolve(): row.get("sha256")
            for row in source_manifest.get("outputs", [])
        }
        for path in inputs:
            expected = recorded_hashes.get(path.resolve())
            if not expected or expected != sha256_file(path):
                raise GateError(f"Stage 03 input does not match Stage 02 manifest: {path}")
    if dry_run:
        return {
            "stage": "03_redact_session_records",
            "status": "dry_run",
            "message_input": str(flattened.resolve()),
            "action_cycle_input": str(action_cycles.resolve()) if action_cycles else None,
        }
    ensure_output_available(output, new_version)

    redactors: dict[str, StableRedactor] = {}
    placeholder_counts: dict[str, int] = {}
    residual_counts: dict[str, int] = {}
    large_body_count = 0
    row_count = 0
    cycle_count = 0
    message_episodes: set[str] = set()
    cycle_episodes: set[str] = set()

    def redactor_for(row: dict[str, Any]) -> StableRedactor:
        scope = str(row.get("global_pair_id") or row.get("episode_id") or "")
        if not scope:
            raise GateError("redaction row missing pair/episode scope")
        if scope not in redactors:
            salt = hashlib.sha256(
                f"{REDACTION_VERSION}:{scope}".encode()
            ).hexdigest()
            redactors[scope] = StableRedactor(salt)
        return redactors[scope]

    def account_redacted(value: Any) -> None:
        if isinstance(value, str):
            for kind in re.findall(r"<([A-Z_]+)_[A-F0-9]{8}>", value):
                placeholder_counts[kind] = placeholder_counts.get(kind, 0) + 1
            for kind in residual_sensitive_kinds(value):
                residual_counts[kind] = residual_counts.get(kind, 0) + 1
        elif isinstance(value, dict):
            for item in value.values():
                account_redacted(item)
        elif isinstance(value, list):
            for item in value:
                account_redacted(item)

    def redact_text(value: str, redactor: StableRedactor) -> str:
        nonlocal large_body_count
        result = summarize_large_value(value, redactor)
        if isinstance(result, dict):
            large_body_count += 1
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        return str(result)

    message_target = output / "redacted_messages.jsonl"
    with (
        flattened.open(encoding="utf-8") as source,
        message_target.open("w", encoding="utf-8") as destination,
    ):
        for raw in source:
            if not raw.strip():
                continue
            row = json.loads(raw)
            redactor = redactor_for(row)
            for key in ("recorded_rationale_text", "visible_text"):
                if row.get(key) is not None:
                    if isinstance(row[key], str):
                        value = redact_text(row[key], redactor)
                    else:
                        value = summarize_large_value(row[key], redactor)
                        if isinstance(value, dict) and value.get("redacted_large_body"):
                            large_body_count += 1
                    row[key] = value
                    account_redacted(value)
            for key in ("tool_input_redacted", "tool_result_redacted"):
                if row.get(key) is not None:
                    value = summarize_large_value(row[key], redactor)
                    if isinstance(value, dict) and value.get("redacted_large_body"):
                        large_body_count += 1
                    row[key] = value
                    account_redacted(value)
            row["redaction_version"] = REDACTION_VERSION
            destination.write(json.dumps(row, sort_keys=True) + "\n")
            row_count += 1
            message_episodes.add(str(row.get("episode_id")))

    outputs = [message_target]
    cycle_target = output / "redacted_action_cycles.jsonl"
    if action_cycles:
        with (
            action_cycles.open(encoding="utf-8") as source,
            cycle_target.open("w", encoding="utf-8") as destination,
        ):
            for raw in source:
                if not raw.strip():
                    continue
                row = json.loads(raw)
                redactor = redactor_for(row)
                for key in ("tool_input_redacted", "tool_result_redacted"):
                    if row.get(key) is not None:
                        value = summarize_large_value(row[key], redactor)
                        if isinstance(value, dict) and value.get("redacted_large_body"):
                            large_body_count += 1
                        row[key] = value
                        account_redacted(value)
                if row.get("parsed_http_requests") is not None:
                    row["parsed_http_requests"] = redactor.redact_value(
                        row["parsed_http_requests"]
                    )
                    account_redacted(row["parsed_http_requests"])
                row["redaction_version"] = REDACTION_VERSION
                destination.write(json.dumps(row, sort_keys=True) + "\n")
                cycle_count += 1
                cycle_episodes.add(str(row.get("episode_id")))
        outputs.append(cycle_target)

    qa = {
        "schema_version": "atobench.redaction_qa.v1",
        "redaction_version": REDACTION_VERSION,
        "message_row_count": row_count,
        "action_cycle_count": cycle_count,
        "message_episode_count": len(message_episodes),
        "action_cycle_episode_count": len(cycle_episodes),
        "pair_scope_count": len(redactors),
        "placeholder_occurrence_counts": dict(sorted(placeholder_counts.items())),
        "large_body_summary_count": large_body_count,
        "residual_sensitive_pattern_counts": dict(sorted(residual_counts.items())),
        "residual_sensitive_pattern_count": sum(residual_counts.values()),
        "private_mapping_exported": False,
        "source_pointer_retained_private": True,
    }
    qa_path = output / "redaction_qa.json"
    error_path = output / "redaction_errors.jsonl"
    write_json(qa_path, qa)
    write_jsonl(error_path, [])
    outputs.extend([qa_path, error_path])
    blocking_count = (
        qa["residual_sensitive_pattern_count"]
        + int(bool(action_cycles) and message_episodes != cycle_episodes)
    )
    manifest = stage_manifest(
        "03_redact_session_records",
        inputs,
        outputs,
        "complete" if not blocking_count else "blocked_redaction_qa",
        dry_run=False,
        details={**qa, "blocking_count": blocking_count},
    )
    write_json(output / "stage_manifest.json", manifest)
    return manifest


def run_framework_stage(stage: str, output: Path, *, dry_run: bool, new_version: bool) -> dict[str, Any]:
    name = STAGES[stage]
    if stage in {"04", "05", "06", "07", "08"}:
        raise GateError(
            f"stage {stage} is implemented by its dedicated numbered script; "
            f"use scripts/{stage}_{name}.py --help"
        )
    if dry_run:
        return {"stage": f"{stage}_{name}", "status": "dry_run"}
    ensure_output_available(output, new_version)
    manifest = stage_manifest(
        f"{stage}_{name}",
        [],
        [],
        "framework_ready_not_executed",
        dry_run=False,
        details={"scientific_outputs_created": False},
    )
    write_json(output / "stage_manifest.json", manifest)
    return manifest


def _fixture(root: Path) -> tuple[Path, Path]:
    campaigns = root / "campaigns"
    claude = root / "claude"
    campaign = campaigns / "fixture_campaign"
    campaign.mkdir(parents=True)
    write_json(
        campaign / "campaign_manifest.json",
        {
            "campaign_id": "fixture_campaign",
            "status": "complete",
            "planned_pairs": 1,
            "planned_episodes": 2,
            "failures": [],
        },
    )
    write_jsonl(
        campaign / "events.jsonl",
        [
            {"episode_id": "ep_fixture_c0", "pair_id": "P01", "condition": "C0", "event": "started"},
            {"episode_id": "ep_fixture_c0", "pair_id": "P01", "condition": "C0", "event": "complete"},
            {"episode_id": "ep_fixture_c1", "pair_id": "P01", "condition": "C1", "event": "started"},
            {"episode_id": "ep_fixture_c1", "pair_id": "P01", "condition": "C1", "event": "complete"},
        ],
    )
    session = claude / "fixture-campaign-project" / "main-session.jsonl"
    session.parent.mkdir(parents=True)
    write_jsonl(
        session,
        [
            {
                "sessionId": "main-session",
                "uuid": "m1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "model": "hidden-in-packet",
                    "content": [
                        {"type": "thinking", "thinking": "Check the response independently."},
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "curl -H 'Authorization: Bearer abc.secret.val' http://fixture/api"}},
                    ],
                },
            },
            {
                "sessionId": "main-session",
                "uuid": "m2",
                "parentUuid": "m1",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "email tester@example.test"}],
                },
            },
        ],
    )
    registry = root / "campaign_registry.yaml"
    write_json(registry, {"campaigns": [{"campaign_id": "fixture_campaign", "root": str(campaign)}]})
    return registry, claude


def smoke() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="atobench-vr-smoke-") as tmp:
        root = Path(tmp)
        registry, claude = _fixture(root)
        census = run_census(registry, claude, root / "01_census", allow_real_data=False, dry_run=False, new_version=False)
        flatten = run_flatten(claude, root / "02_sessions", allow_real_data=False, dry_run=False, new_version=False)
        redact = run_redact(root / "02_sessions" / "flattened_messages.jsonl", root / "03_redacted", dry_run=False, new_version=False)
        inventory = (root / "01_census" / "session_inventory.csv").read_text()
        redacted_text = (root / "03_redacted" / "redacted_messages.jsonl").read_text()
        checks = {
            "two_episodes": census["details"]["episode_count"] == 2,
            "namespaced_pair": "fixture_campaign::P01" in (root / "01_census" / "pair_census.csv").read_text(),
            "output_zero_is_inline_only": "canonical_jsonl_inline_only" in inventory and ",False," in inventory,
            "flattened": flatten["details"]["message_item_count"] == 3,
            "bearer_redacted": "abc.secret.value" not in redacted_text and "<BEARER_" in redacted_text,
            "email_redacted": "tester@example.test" not in redacted_text and "<EMAIL_" in redacted_text,
            "no_network": not census["network_accessed"] and not flatten["network_accessed"] and not redact["network_accessed"],
        }
        if not all(checks.values()):
            raise GateError(f"smoke failure: {checks}")
        judgment = {
            "schema_version": "atobench.trajectory_judgment.v1",
            "packet_id": "fixture-packet",
            "dimension": "verification_control",
            "score": 7,
            "score_low": 7,
            "score_high": 8,
            "band": "good",
            "confidence": "medium",
            "insufficient_evidence": False,
            "reason_code": "fixture",
            "material_strengths": [],
            "material_deficiencies": [],
            "supporting_message_ids": ["m1"],
            "supporting_event_ids": [],
            "supporting_fact_ids": [],
            "forbidden_identity_not_accessed": True,
            "outside_knowledge_not_used": True,
        }
        checks["judgment_schema"] = not validate_judgment(judgment)
        if not checks["judgment_schema"]:
            raise GateError("judgment schema smoke failed")
        return {
            "schema_version": "atobench.verification_resilience.smoke.v1",
            "status": "PASS",
            "checks": checks,
            "fixture_hash": sha256_json(checks),
            "real_campaigns_read": False,
            "judge_calls_made": False,
            "scientific_analysis_performed": False,
        }


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("smoke")
    stage_parser = sub.add_parser("stage")
    stage_parser.add_argument("stage", choices=STAGES)
    stage_parser.add_argument("--registry", type=Path)
    stage_parser.add_argument("--claude-root", type=Path, action="append")
    stage_parser.add_argument("--mapping", type=Path)
    stage_parser.add_argument("--episode-census", type=Path)
    stage_parser.add_argument("--action-cycles", type=Path)
    stage_parser.add_argument("--input", type=Path)
    stage_parser.add_argument("--output", type=Path, required=True)
    stage_parser.add_argument("--dry-run", action="store_true")
    stage_parser.add_argument("--new-version", action="store_true")
    stage_parser.add_argument("--allow-real-data", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "smoke":
            print(json.dumps(smoke(), indent=2, sort_keys=True))
            return 0
        if args.stage == "01":
            if not args.registry or not args.claude_root:
                parser.error("stage 01 requires --registry and --claude-root")
            result = run_census(args.registry, args.claude_root, args.output, allow_real_data=args.allow_real_data, dry_run=args.dry_run, new_version=args.new_version)
        elif args.stage == "02":
            if not args.claude_root and not (args.mapping and args.episode_census):
                parser.error(
                    "stage 02 requires --mapping and --episode-census "
                    "(or --claude-root for synthetic fixtures)"
                )
            result = run_flatten(
                args.claude_root,
                args.output,
                mapping=args.mapping,
                episode_census=args.episode_census,
                allow_real_data=args.allow_real_data,
                dry_run=args.dry_run,
                new_version=args.new_version,
            )
        elif args.stage == "03":
            if not args.input:
                parser.error("stage 03 requires --input")
            result = run_redact(
                args.input,
                args.output,
                action_cycles=args.action_cycles,
                allow_real_data=args.allow_real_data,
                dry_run=args.dry_run,
                new_version=args.new_version,
            )
        else:
            result = run_framework_stage(args.stage, args.output, dry_run=args.dry_run, new_version=args.new_version)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    return 2
