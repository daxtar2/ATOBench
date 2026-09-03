from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import shutil
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .alignment import _enrich_event_body_hashes, _event_view
from .common import (
    GateError,
    ensure_output_available,
    is_probably_real_path,
    load_jsonl,
    read_json,
    require_real_data_authorization,
    sha256_file,
    sha256_json,
    stage_manifest,
    write_json,
    write_jsonl,
)
from .judge_runner import RunnerConfig, _claude_version, _run_invocation


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _authorization(
    lock_path: Path,
    allow_calls: bool,
    synthetic_smoke: bool,
) -> None:
    if synthetic_smoke:
        return
    if not allow_calls:
        raise GateError("real alignment-verifier calls require --allow-alignment-verifier-calls")


def _verify_manifest_bound_input(path: Path, label: str) -> None:
    if not is_probably_real_path(path):
        return
    manifest_path = path.parent / "stage_manifest.json"
    if not manifest_path.is_file():
        raise GateError(f"{label} manifest missing: {manifest_path}")
    manifest = read_json(manifest_path)
    if manifest.get("status") != "complete":
        raise GateError(f"{label} manifest is not complete: {manifest_path}")


def _authorized_task_ids(
    allowlist_path: Path,
    *,
    tasks: list[dict[str, Any]],
    max_calls: int,
) -> set[str]:
    allowlist = read_json(allowlist_path)
    if allowlist.get("status") not in {
        "draft_unauthorized",
        "frozen_authorized",
        "calibration_regression",
    }:
        raise GateError("alignment task allowlist has an invalid status")
    all_task_ids = {str(task["alignment_task_id"]) for task in tasks}
    selected = {str(value) for value in allowlist.get("task_ids", [])}
    if not selected or not selected.issubset(all_task_ids):
        raise GateError("alignment task allowlist contains invalid task IDs")
    planned_calls = len(selected) * 2
    if planned_calls > max_calls:
        raise GateError(
            f"alignment verifier plan requires {planned_calls} calls, "
            f"exceeding --max-calls={max_calls}"
        )
    return selected


def _make_read_only(workspace: Path) -> None:
    for path in sorted(workspace.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        elif path.is_dir():
            path.chmod(
                stat.S_IRUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )
    workspace.chmod(
        stat.S_IRUSR
        | stat.S_IXUSR
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH
    )


def _workspace(
    task: dict[str, Any],
    schema_path: Path,
    agent_spec: Path,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory(prefix="atobench-alignment-verifier-")
    root = Path(temporary.name)
    write_json(root / "alignment_task.json", task)
    shutil.copy2(schema_path, root / "output_schema.json")
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    shutil.copy2(agent_spec, agents / agent_spec.name)
    _make_read_only(root)
    return temporary, root


def _task(
    row: dict[str, Any],
    cycle: dict[str, Any],
    event_views: dict[str, dict[str, Any]],
    tolerance_seconds: float,
) -> tuple[dict[str, Any], dict[str, str]]:
    task_id = "aln_" + sha256_json(
        {
            "action_cycle_id": row.get("action_cycle_id"),
            "request_index": row.get("request_index"),
            "candidate_event_ids": row.get("candidate_event_ids"),
        }
    )[:20]
    request = next(
        (
            item
            for item in cycle.get("parsed_http_requests", [])
            if item.get("request_index") == row.get("request_index")
        ),
        None,
    )
    if request is None:
        raise GateError(f"alignment row has no matching parsed request: {task_id}")
    cycle_time = _timestamp(cycle.get("timestamp"))
    source_to_public: dict[str, str] = {}
    candidates = []
    for index, source_id in enumerate(row.get("candidate_event_ids", []), 1):
        event = event_views.get(str(source_id))
        if event is None:
            raise GateError(f"alignment candidate event is missing: {source_id}")
        public_id = f"H{index:04d}"
        source_to_public[str(source_id)] = public_id
        event_time = _timestamp(event.get("timestamp"))
        delta = (
            abs((event_time - cycle_time).total_seconds())
            if event_time is not None and cycle_time is not None
            else None
        )
        payload_nonconflict = not (
            request.get("request_body_sha256")
            and event.get("request_body_sha256")
            and request["request_body_sha256"] != event["request_body_sha256"]
        )
        constraints = {
            "method_match": request.get("method") == event.get("method"),
            "route_match": request.get("canonical_route")
            == event.get("canonical_route"),
            "payload_nonconflict": payload_nonconflict,
            "timestamp_within_tolerance": delta is not None
            and delta <= tolerance_seconds,
        }
        candidates.append(
            {
                "candidate_id": public_id,
                "method": event.get("method"),
                "canonical_route": event.get("canonical_route"),
                "absolute_time_delta_seconds": delta,
                "deterministic_constraints": constraints,
            }
        )
    task = {
        "schema_version": "atobench.alignment_verifier_task.v1",
        "alignment_task_id": task_id,
        "request": {
            "method": request.get("method"),
            "canonical_route": request.get("canonical_route"),
        },
        "timestamp_tolerance_seconds": tolerance_seconds,
        "candidates": candidates,
        "selection_rule": (
            "Treat every deterministic_constraints boolean as authoritative. "
            "A payload conflict exists only when payload_nonconflict=false; never "
            "infer a conflict from omitted or one-sided body-hash evidence. Select "
            "a candidate only when exactly one candidate has all deterministic "
            "constraints=true. If two or more candidates pass, return ambiguous; "
            "absolute_time_delta_seconds is only a tolerance check and must never "
            "be used as a nearest-candidate tie-break."
        ),
        "forbidden_fields_omitted": [
            "episode_id",
            "pair_id",
            "campaign_id",
            "condition",
            "model",
            "report_outcome",
            "judge_scores",
        ],
    }
    return task, source_to_public


def _validate_decision(
    decision: dict[str, Any],
    *,
    task: dict[str, Any],
    reviewer_id: str,
) -> None:
    required = {
        "schema_version",
        "alignment_task_id",
        "reviewer_id",
        "selection_status",
        "selected_candidate_id",
        "constraint_results",
        "reason",
        "forbidden_identity_not_accessed",
        "outside_knowledge_not_used",
    }
    if set(decision) != required:
        raise GateError(
            f"invalid alignment decision fields from {reviewer_id}: "
            f"missing={sorted(required - set(decision))}, "
            f"extra={sorted(set(decision) - required)}"
        )
    if decision["schema_version"] != "atobench.alignment_verifier_decision.v1":
        raise GateError("invalid alignment decision schema_version")
    if decision["alignment_task_id"] != task["alignment_task_id"]:
        raise GateError("alignment task identity mismatch")
    if decision["reviewer_id"] != reviewer_id:
        raise GateError("alignment reviewer identity mismatch")
    if decision["selection_status"] not in {
        "selected",
        "ambiguous",
        "no_proxy_match",
        "parse_failure",
    }:
        raise GateError("invalid alignment selection_status")
    candidate_ids = {row["candidate_id"] for row in task["candidates"]}
    selected = decision["selected_candidate_id"]
    if decision["selection_status"] == "selected":
        if selected not in candidate_ids:
            raise GateError("alignment decision selected an unknown candidate")
    elif selected is not None:
        raise GateError("non-selected alignment decision must use null candidate")
    expected_constraints = {
        "method_match",
        "route_match",
        "payload_nonconflict",
        "timestamp_within_tolerance",
        "unique_best_candidate",
    }
    constraints = decision["constraint_results"]
    if set(constraints) != expected_constraints or not all(
        isinstance(value, bool) for value in constraints.values()
    ):
        raise GateError("invalid alignment constraint_results")
    if decision["forbidden_identity_not_accessed"] is not True:
        raise GateError("alignment identity-access attestation failed")
    if decision["outside_knowledge_not_used"] is not True:
        raise GateError("alignment outside-knowledge attestation failed")


def _merge(
    decisions: list[dict[str, Any]],
    task: dict[str, Any],
    public_to_source: dict[str, str],
) -> tuple[str, list[str], str]:
    if len(decisions) != 2:
        raise GateError("alignment merge requires exactly two verifier decisions")
    selected = [decision["selected_candidate_id"] for decision in decisions]
    both_selected = all(
        decision["selection_status"] == "selected" for decision in decisions
    )
    same = both_selected and selected[0] == selected[1]
    if not same:
        same_exclusion = (
            decisions[0]["selection_status"] == decisions[1]["selection_status"]
            and decisions[0]["selected_candidate_id"]
            == decisions[1]["selected_candidate_id"]
        )
        if same_exclusion:
            return "ambiguous", [], "agreed_exclude"
        return "ambiguous", [], "disagreed_excluded"
    candidate = next(
        row for row in task["candidates"] if row["candidate_id"] == selected[0]
    )
    deterministic_pass = all(candidate["deterministic_constraints"].values())
    verifier_pass = all(
        all(decision["constraint_results"].values()) for decision in decisions
    )
    satisfying = [
        row
        for row in task["candidates"]
        if all(row["deterministic_constraints"].values())
    ]
    unique = len(satisfying) == 1 and satisfying[0]["candidate_id"] == selected[0]
    if not (deterministic_pass and verifier_pass and unique):
        return "ambiguous", [], "constraint_failed_excluded"
    source_id = public_to_source[str(selected[0])]
    return "timestamp_unique", [source_id], "agreed_accepted"


def run(
    *,
    alignment_path: Path,
    action_cycles_path: Path,
    http_events_paths: list[Path],
    output_dir: Path,
    config_path: Path,
    lock_path: Path,
    agent_spec_path: Path,
    output_schema_path: Path,
    tolerance_seconds: float,
    allow_real_data: bool,
    allow_calls: bool,
    synthetic_smoke: bool,
    executable_override: str | None,
    task_allowlist_path: Path | None,
    max_calls: int | None,
    preflight_only: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    if not http_events_paths:
        raise GateError("Stage 04b requires at least one --http-events input")
    inputs = [
        alignment_path,
        action_cycles_path,
        *http_events_paths,
        config_path,
        lock_path,
        agent_spec_path,
        output_schema_path,
    ]
    require_real_data_authorization(
        [alignment_path, action_cycles_path, *http_events_paths],
        allow_real_data or synthetic_smoke,
    )
    _verify_manifest_bound_input(alignment_path, "Stage 04 alignment")
    _verify_manifest_bound_input(action_cycles_path, "Stage 03 action cycles")
    for path in http_events_paths:
        _verify_manifest_bound_input(path, "normalized HTTP input")
    alignments = load_jsonl(alignment_path)
    cycles = {
        str(row["action_cycle_id"]): row
        for row in load_jsonl(action_cycles_path)
        if row.get("action_cycle_id")
    }
    event_rows = [
        event
        for path in http_events_paths
        for event in load_jsonl(path)
    ]
    enrichment = _enrich_event_body_hashes(event_rows)
    event_views = {
        view["event_id"]: view
        for index, event in enumerate(event_rows)
        for view in [_event_view(event, index)]
    }
    pending = [
        row
        for row in alignments
        if row.get("dual_verifier_status") == "pending_dual_verification"
    ]
    tasks: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    candidate_counts: dict[int, int] = {}
    unique_satisfying_count = 0
    no_satisfying_count = 0
    multiple_satisfying_count = 0
    for row in pending:
        cycle_id = str(row.get("action_cycle_id") or "")
        cycle = cycles.get(cycle_id)
        if cycle is None:
            raise GateError(f"pending alignment has no action cycle: {cycle_id}")
        task, _ = _task(row, cycle, event_views, tolerance_seconds)
        task_id = str(task["alignment_task_id"])
        if task_id in task_ids:
            raise GateError(f"duplicate alignment verifier task ID: {task_id}")
        task_ids.add(task_id)
        candidates = task["candidates"]
        if not candidates:
            raise GateError(f"pending alignment has no verifier candidates: {task_id}")
        candidate_counts[len(candidates)] = candidate_counts.get(len(candidates), 0) + 1
        satisfying = [
            candidate
            for candidate in candidates
            if all(candidate["deterministic_constraints"].values())
        ]
        if len(satisfying) == 1:
            unique_satisfying_count += 1
        elif not satisfying:
            no_satisfying_count += 1
        else:
            multiple_satisfying_count += 1
        serialized = json.dumps(task, sort_keys=True)
        private_tokens = {
            str(value)
            for key in (
                "episode_id",
                "global_episode_id",
                "global_pair_id",
                "session_id",
                "session_tree_id",
                "action_cycle_id",
            )
            for value in [cycle.get(key)]
            if value is not None and len(str(value)) >= 6
        }
        leaked = sorted(token for token in private_tokens if token in serialized)
        if leaked:
            raise GateError(f"alignment task leaked private identity: {task_id}")
        tasks.append(task)
    preflight = {
        "schema_version": "atobench.alignment_verifier_preflight.v1",
        "status": "preflight_complete_no_calls",
        "pending_alignment_count": len(pending),
        "materialized_task_count": len(tasks),
        "unique_task_id_count": len(task_ids),
        "candidate_count_distribution": {
            str(key): value for key, value in sorted(candidate_counts.items())
        },
        "unique_deterministic_candidate_count": unique_satisfying_count,
        "no_deterministic_candidate_count": no_satisfying_count,
        "multiple_deterministic_candidate_count": multiple_satisfying_count,
        "planned_claude_calls": len(pending) * 2,
        "claude_calls_made": 0,
        "private_identity_leak_count": 0,
        "http_source_count": len(http_events_paths),
        "http_event_count": len(event_rows),
        **enrichment,
    }
    if dry_run:
        return {
            "schema_version": "atobench.alignment_verifier_run_plan.v1",
            "status": "dry_run",
            **{key: value for key, value in preflight.items() if key not in {"schema_version", "status"}},
        }
    if preflight_only:
        ensure_output_available(output_dir, new_version)
        preflight_path = output_dir / "alignment_verifier_preflight.json"
        write_json(preflight_path, preflight)
        manifest = stage_manifest(
            "04b_validate_timestamp_alignments_preflight",
            inputs,
            [preflight_path],
            "preflight_complete_no_calls",
            dry_run=False,
            details=preflight,
        )
        write_json(output_dir / "stage_manifest.json", manifest)
        return manifest
    _authorization(lock_path, allow_calls, synthetic_smoke)
    selected_task_ids = set(task_ids)
    if not synthetic_smoke:
        if task_allowlist_path is None:
            raise GateError("real verifier calls require --task-allowlist")
        if max_calls is None or max_calls <= 0:
            raise GateError("real verifier calls require a positive --max-calls")
        selected_task_ids = _authorized_task_ids(
            task_allowlist_path,
            tasks=tasks,
            max_calls=max_calls,
        )
        inputs.append(task_allowlist_path)
    ensure_output_available(output_dir, new_version)
    config = RunnerConfig.load(config_path, executable_override)
    schema = read_json(output_schema_path)
    version = _claude_version(config.claude_executable) if pending else None
    spec_hash = sha256_file(agent_spec_path)
    all_receipts: list[dict[str, Any]] = []
    bundles: list[dict[str, Any]] = []
    updated_by_key: dict[tuple[str, Any], dict[str, Any]] = {}

    task_by_id = {
        task["alignment_task_id"]: task
        for task in tasks
    }
    for row in pending:
        cycle_id = str(row.get("action_cycle_id") or "")
        cycle = cycles.get(cycle_id)
        if cycle is None:
            raise GateError(f"pending alignment has no action cycle: {cycle_id}")
        generated_task, source_to_public = _task(
            row, cycle, event_views, tolerance_seconds
        )
        if generated_task["alignment_task_id"] not in selected_task_ids:
            continue
        task = task_by_id[generated_task["alignment_task_id"]]
        public_to_source = {
            public: source for source, public in source_to_public.items()
        }
        task_hash = sha256_json(task)

        def one(reviewer_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
            temporary, workspace = _workspace(task, output_schema_path, agent_spec_path)
            try:
                decision, receipt, _ = _run_invocation(
                    config=config,
                    workspace=workspace,
                    agent_name="atobench-alignment-verifier",
                    prompt=(
                        f"Review alignment task {task['alignment_task_id']} as "
                        f"reviewer_id {reviewer_id}. The complete redacted task is "
                        "included below; the workspace files are redundant audit "
                        "copies, so do not fail merely because a file read is "
                        "unavailable. Return JSON only.\n\n"
                        f"ALIGNMENT_TASK_JSON={json.dumps(task, sort_keys=True)}"
                    ),
                    schema=schema,
                    invocation_id=f"{task['alignment_task_id']}:{reviewer_id}",
                    spec_hash=spec_hash,
                    packet_hash=task_hash,
                )
            finally:
                temporary.cleanup()
            _validate_decision(decision, task=task, reviewer_id=reviewer_id)
            return decision, receipt

        reviewer_ids = config.alignment_verifier_ids
        if config.parallel_reviewers:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(one, reviewer_ids))
        else:
            results = [one(reviewer_id) for reviewer_id in reviewer_ids]
        decisions = [decision for decision, _ in results]
        receipts = [receipt for _, receipt in results]
        status, accepted, dual_status = _merge(
            decisions, task, public_to_source
        )
        updated = dict(row)
        updated["alignment_status"] = status
        updated["accepted_event_ids"] = accepted
        updated["accepted_for_evidence"] = bool(accepted)
        updated["dual_verifier_status"] = dual_status
        updated["alignment_task_id"] = task["alignment_task_id"]
        key = (str(row.get("action_cycle_id")), row.get("request_index"))
        updated_by_key[key] = updated
        bundles.append(
            {
                "schema_version": "atobench.alignment_verifier_bundle.v1",
                "alignment_task_id": task["alignment_task_id"],
                "task_sha256": task_hash,
                "decisions": decisions,
                "merge_status": dual_status,
                "accepted_candidate_id": (
                    source_to_public[accepted[0]] if accepted else None
                ),
                "accepted_for_evidence": bool(accepted),
            }
        )
        all_receipts.extend(receipts)

    updated_rows = []
    for row in alignments:
        key = (str(row.get("action_cycle_id")), row.get("request_index"))
        updated_rows.append(updated_by_key.get(key, row))
    validated_path = output_dir / "claude_http_alignment.validated.jsonl"
    bundle_path = output_dir / "alignment_verifier_bundles.jsonl"
    receipt_dir = output_dir / "receipts"
    receipt_dir.mkdir(parents=True)
    write_jsonl(validated_path, updated_rows)
    write_jsonl(bundle_path, bundles)
    for index, receipt in enumerate(all_receipts, 1):
        write_json(receipt_dir / f"{index:04d}_alignment_verifier.json", receipt)
    accepted_count = sum(
        bundle["accepted_for_evidence"] for bundle in bundles
    )
    disagreement_count = sum(
        bundle["merge_status"] == "disagreed_excluded" for bundle in bundles
    )
    agreed_exclude_count = sum(
        bundle["merge_status"] == "agreed_exclude" for bundle in bundles
    )
    manifest = stage_manifest(
        "04b_validate_timestamp_alignments",
        [
            alignment_path,
            action_cycles_path,
            *http_events_paths,
            config_path,
            lock_path,
            agent_spec_path,
            output_schema_path,
        ],
        [validated_path, bundle_path, *sorted(receipt_dir.glob("*.json"))],
        "synthetic_smoke_complete" if synthetic_smoke else "complete",
        dry_run=False,
        details={
            "pending_alignment_count": len(pending),
            "selected_task_count": len(selected_task_ids),
            "max_calls": max_calls,
            "accepted_count": accepted_count,
            "disagreement_count": disagreement_count,
            "reviewer_disagreement_count": disagreement_count,
            "agreed_exclude_count": agreed_exclude_count,
            "excluded_count": len(selected_task_ids) - accepted_count,
            "invocation_count": len(all_receipts),
            "claude_code_version": version,
            "synthetic_smoke": synthetic_smoke,
        },
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alignment", type=Path, required=True)
    parser.add_argument("--action-cycles", type=Path, required=True)
    parser.add_argument(
        "--http-events",
        type=Path,
        action="append",
        required=True,
        help="repeat for each hash-bound normalized HTTP source",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--agent-spec", type=Path, required=True)
    parser.add_argument("--output-schema", type=Path, required=True)
    parser.add_argument("--timestamp-tolerance-seconds", type=float, default=3.0)
    parser.add_argument("--claude-executable")
    parser.add_argument("--task-allowlist", type=Path)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--allow-alignment-verifier-calls", action="store_true")
    parser.add_argument("--synthetic-smoke", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            alignment_path=args.alignment,
            action_cycles_path=args.action_cycles,
            http_events_paths=args.http_events,
            output_dir=args.output,
            config_path=args.config,
            lock_path=args.lock,
            agent_spec_path=args.agent_spec,
            output_schema_path=args.output_schema,
            tolerance_seconds=args.timestamp_tolerance_seconds,
            allow_real_data=args.allow_real_data,
            allow_calls=args.allow_alignment_verifier_calls,
            synthetic_smoke=args.synthetic_smoke,
            executable_override=args.claude_executable,
            task_allowlist_path=args.task_allowlist,
            max_calls=args.max_calls,
            preflight_only=args.preflight_only,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    return 2
