from __future__ import annotations

import argparse
import concurrent.futures
import json
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    read_json,
    read_json_or_yaml,
    require_real_data_authorization,
    sha256_file,
    stage_manifest,
    utc_now,
    write_json,
)
from .judge_runner import RunnerConfig, _run_invocation
from .semantic_output import (
    canonicalize_semantic_match,
    semantic_decision_key,
    unavailable_semantic_match,
    validate_semantic_match,
)

MATCHER_AGENT = "atobench-report-semantic-matcher"
ADJUDICATOR_AGENT = "atobench-report-semantic-adjudicator"


@dataclass(frozen=True)
class SemanticConfig:
    runner: RunnerConfig
    matcher_ids: tuple[str, str]
    max_attempts_per_role: int

    @classmethod
    def load(
        cls,
        path: Path,
        executable_override: str | None = None,
    ) -> "SemanticConfig":
        value = read_json(path)
        matcher_ids = tuple(str(item) for item in value.get("matcher_ids", []))
        if len(matcher_ids) != 2 or len(set(matcher_ids)) != 2:
            raise GateError("semantic runner requires two unique matcher_ids")
        if tuple(value.get("tools", [])) != ("Read",):
            raise GateError("semantic runner must expose only the Read tool")
        attempts = int(value.get("max_attempts_per_role", 2))
        if attempts not in {1, 2}:
            raise GateError("semantic max_attempts_per_role must be 1 or 2")
        runner = RunnerConfig(
            claude_executable=executable_override or value["claude_executable"],
            model=value.get("model"),
            fallback_model=value.get("fallback_model"),
            effort=value.get("effort", "high"),
            timeout_seconds=int(value.get("timeout_seconds", 900)),
            max_budget_usd_per_call=value.get("max_budget_usd_per_call"),
            permission_mode=value.get("permission_mode", "dontAsk"),
            tools=tuple(value.get("tools", [])),
            no_session_persistence=bool(value.get("no_session_persistence", True)),
            disable_slash_commands=bool(value.get("disable_slash_commands", True)),
            no_chrome=bool(value.get("no_chrome", True)),
            reviewer_ids=(matcher_ids[0], matcher_ids[1]),
            alignment_verifier_ids=("unused_semantic_a", "unused_semantic_b"),
            parallel_reviewers=False,
            setting_sources=tuple(value.get("setting_sources", ["user", "project"])),
        )
        return cls(
            runner=runner,
            matcher_ids=(matcher_ids[0], matcher_ids[1]),
            max_attempts_per_role=attempts,
        )


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    write_json(temporary, value)
    temporary.replace(path)


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
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
    root.chmod(
        stat.S_IRUSR
        | stat.S_IXUSR
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH
    )


def _workspace(
    packet_dir: Path,
    agent_spec: Path,
    request_name: str,
    request: dict[str, Any],
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory(prefix="atobench-semantic-")
    root = Path(temporary.name)
    for name in (
        "packet.json",
        "registered_finding_contract.json",
        "rubric.md",
        "output_schema.json",
    ):
        shutil.copy2(packet_dir / name, root / name)
    shutil.copytree(packet_dir / "packet_evidence", root / "packet_evidence")
    write_json(root / request_name, request)
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    shutil.copy2(agent_spec, agents / agent_spec.name)
    _make_read_only(root)
    return temporary, root


def _failure_receipt(
    packet: dict[str, Any],
    role_id: str,
    attempt: int,
    error: Exception,
) -> dict[str, Any]:
    return {
        "schema_version": "atobench.semantic_invocation_failure.v1",
        "semantic_packet_id": packet["semantic_packet_id"],
        "role_id": role_id,
        "attempt": attempt,
        "created_at": utc_now(),
        "error_type": type(error).__name__,
        "error": str(error),
        "retryable": attempt == 1,
    }


def _raw_semantic_output_requires_retry(
    raw: dict[str, Any],
    canonical: dict[str, Any],
) -> bool:
    """Retry malformed semantic objects, but preserve explicit uncertainty.

    A model may intentionally return an unavailable decision when the packet
    is genuinely insufficient. That is a scientific label and must not be
    retried merely to force a non-null answer. In contrast, schema-shaped
    wrappers such as ``{"file_path": ...}``, missing semantic fields, or
    internally unusable positive decisions are protocol failures and receive
    the one bounded retry configured for the role.
    """
    if canonical.get("insufficient_evidence") is not True:
        return False
    explicit_unavailable = (
        raw.get("insufficient_evidence") is True
        and raw.get("report_closure") is None
        and raw.get("claim_trace_support") == "unavailable"
    )
    return not explicit_unavailable


def _invoke_role(
    *,
    semantic_config: SemanticConfig,
    packet_dir: Path,
    packet: dict[str, Any],
    agent_spec: Path,
    agent_name: str,
    role_id: str,
    request_name: str,
    request: dict[str, Any],
    prompt: str,
    output_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    schema = read_json(packet_dir / "output_schema.json")
    receipts: list[dict[str, Any]] = []
    stderr_records: list[str] = []
    for attempt in range(1, semantic_config.max_attempts_per_role + 1):
        temporary, workspace = _workspace(
            packet_dir,
            agent_spec,
            request_name,
            request,
        )
        try:
            raw, receipt, stderr = _run_invocation(
                config=semantic_config.runner,
                workspace=workspace,
                agent_name=agent_name,
                prompt=prompt,
                schema=schema,
                invocation_id=(
                    f"{packet['semantic_packet_id']}:{role_id}:attempt-{attempt}"
                ),
                spec_hash=sha256_file(agent_spec),
                packet_hash=sha256_file(packet_dir / "packet.json"),
            )
            receipt["role_id"] = role_id
            receipt["attempt"] = attempt
            receipts.append(receipt)
            if stderr:
                stderr_records.append(stderr)
            canonical, warnings = canonicalize_semantic_match(raw, packet, role_id)
            _atomic_write_json(
                output_dir / "raw" / f"{role_id}_attempt_{attempt}.json",
                raw,
            )
            _atomic_write_json(
                output_dir / "warnings" / f"{role_id}_attempt_{attempt}.json",
                {"warnings": warnings},
            )
            if (
                _raw_semantic_output_requires_retry(raw, canonical)
                and attempt < semantic_config.max_attempts_per_role
            ):
                continue
            _atomic_write_json(output_dir / "raw" / f"{role_id}.json", raw)
            _atomic_write_json(output_dir / "outputs" / f"{role_id}.json", canonical)
            _atomic_write_json(
                output_dir / "warnings" / f"{role_id}.json",
                {"warnings": warnings},
            )
            return canonical, receipts, stderr_records
        except Exception as exc:
            failure = _failure_receipt(packet, role_id, attempt, exc)
            receipts.append(failure)
            _atomic_write_json(
                output_dir / "failures" / f"{role_id}_attempt_{attempt}.json",
                failure,
            )
        finally:
            temporary.cleanup()
    fallback = unavailable_semantic_match(
        packet,
        role_id,
        reason_code="role_attempts_exhausted",
        reason="All bounded matcher attempts failed or timed out.",
    )
    _atomic_write_json(output_dir / "outputs" / f"{role_id}.json", fallback)
    return fallback, receipts, stderr_records


def _consensus(
    a: dict[str, Any],
    b: dict[str, Any],
) -> dict[str, Any]:
    final = dict(a)
    final["matcher_id"] = "matcher_consensus"
    final["matched_claim_atom_ids"] = sorted(
        set(a["matched_claim_atom_ids"]) | set(b["matched_claim_atom_ids"])
    )
    final["supporting_fact_ids"] = sorted(
        set(a["supporting_fact_ids"]) | set(b["supporting_fact_ids"])
    )
    final["contradicting_fact_ids"] = sorted(
        set(a["contradicting_fact_ids"]) | set(b["contradicting_fact_ids"])
    )
    confidence_order = {"low": 0, "medium": 1, "high": 2}
    final["confidence"] = min(
        (a["confidence"], b["confidence"]),
        key=lambda value: confidence_order[value],
    )
    final["reason_code"] = "dual_matcher_consensus"
    final["reason"] = "Two isolated matchers agreed on the semantic decision."
    return final


def run_packet(
    *,
    packet_dir: Path,
    output_dir: Path,
    config_path: Path,
    lock_path: Path,
    agents_dir: Path,
    allow_calls: bool,
    synthetic_smoke: bool,
    executable_override: str | None,
    new_version: bool,
    dry_run: bool,
) -> dict[str, Any]:
    packet = read_json(packet_dir / "packet.json")
    if packet.get("schema_version") != "atobench.report_semantic_packet.v1":
        raise GateError("invalid semantic packet schema")
    semantic_config = SemanticConfig.load(config_path, executable_override)
    lock = read_json_or_yaml(lock_path)
    if not synthetic_smoke and not dry_run:
        if not allow_calls:
            raise GateError("real semantic matcher calls require --allow-semantic-match-calls")
        if lock.get("freeze_status") != "frozen":
            raise GateError("semantic matcher calls require freeze_status=frozen")
        if lock.get("semantic_match_calls_authorized") is not True:
            raise GateError("semantic match lock does not authorize calls")
    if dry_run:
        return {
            "schema_version": "atobench.semantic_packet_call_plan.v1",
            "semantic_packet_id": packet["semantic_packet_id"],
            "base_calls": 2,
            "no_retry_max_calls": 3,
            "hard_retry_max_calls": 3 * semantic_config.max_attempts_per_role,
            "calls_made": 0,
        }
    ensure_output_available(output_dir, new_version)
    marker = {
        "schema_version": "atobench.semantic_run_incomplete.v1",
        "semantic_packet_id": packet["semantic_packet_id"],
        "created_at": utc_now(),
    }
    _atomic_write_json(output_dir / "RUN_INCOMPLETE.json", marker)
    matcher_spec = agents_dir / "atobench-report-semantic-matcher.md"
    adjudicator_spec = agents_dir / "atobench-report-semantic-adjudicator.md"
    for path in (matcher_spec, adjudicator_spec):
        if not path.is_file():
            raise GateError(f"missing semantic agent spec: {path}")

    outputs: dict[str, dict[str, Any]] = {}
    all_receipts: list[dict[str, Any]] = []
    all_stderr: list[str] = []
    for matcher_id in semantic_config.matcher_ids:
        checkpoint = output_dir / "outputs" / f"{matcher_id}.json"
        if checkpoint.is_file():
            output = read_json(checkpoint)
            errors = validate_semantic_match(output, packet)
            if errors:
                raise GateError(
                    f"invalid semantic resume checkpoint {matcher_id}: {errors}"
                )
            outputs[matcher_id] = output
            continue
        request = {
            "schema_version": "atobench.semantic_match_request.v1",
            "semantic_packet_id": packet["semantic_packet_id"],
            "matcher_id": matcher_id,
            "allowed_files": [
                "packet.json",
                "registered_finding_contract.json",
                "rubric.md",
                "packet_evidence/final_report.md",
                "packet_evidence/report_claim_atoms.json",
                "packet_evidence/facts.json",
                "output_schema.json",
            ],
        }
        output, receipts, stderr = _invoke_role(
            semantic_config=semantic_config,
            packet_dir=packet_dir,
            packet=packet,
            agent_spec=matcher_spec,
            agent_name=MATCHER_AGENT,
            role_id=matcher_id,
            request_name="match_request.json",
            request=request,
            prompt=(
                f"Match semantic packet {packet['semantic_packet_id']}. "
                f"Use matcher_id={matcher_id}. Read match_request.json and only "
                "the declared packet files. Return exactly one JSON object."
            ),
            output_dir=output_dir,
        )
        outputs[matcher_id] = output
        all_receipts.extend(receipts)
        all_stderr.extend(stderr)
        for index, receipt in enumerate(receipts, 1):
            _atomic_write_json(
                output_dir
                / "receipts"
                / f"{matcher_id}_attempt_{index}.json",
                receipt,
            )

    matcher_a = outputs[semantic_config.matcher_ids[0]]
    matcher_b = outputs[semantic_config.matcher_ids[1]]
    adjudication_triggered = (
        semantic_decision_key(matcher_a) != semantic_decision_key(matcher_b)
    )
    adjudication = None
    if adjudication_triggered:
        request = {
            "schema_version": "atobench.semantic_adjudication_request.v1",
            "semantic_packet_id": packet["semantic_packet_id"],
            "trigger": "semantic_decision_disagreement",
            "allowed_files": [
                "packet.json",
                "registered_finding_contract.json",
                "rubric.md",
                "packet_evidence/final_report.md",
                "packet_evidence/report_claim_atoms.json",
                "packet_evidence/facts.json",
                "output_schema.json",
                "adjudication_request.json",
            ],
            "disagreement_fields": [
                field
                for field in (
                    "insufficient_evidence",
                    "registered_finding_mentioned",
                    "report_closure",
                    "claim_trace_support",
                )
                if matcher_a.get(field) != matcher_b.get(field)
            ],
            "matcher_outputs": outputs,
        }
        adjudication, receipts, stderr = _invoke_role(
            semantic_config=semantic_config,
            packet_dir=packet_dir,
            packet=packet,
            agent_spec=adjudicator_spec,
            agent_name=ADJUDICATOR_AGENT,
            role_id="semantic_adjudicator",
            request_name="adjudication_request.json",
            request=request,
            prompt=(
                f"Resolve semantic packet {packet['semantic_packet_id']}. Read "
                "adjudication_request.json and every packet evidence file declared "
                "in its allowed_files list before deciding. Use "
                "matcher_id=semantic_adjudicator. Return exactly one JSON object."
            ),
            output_dir=output_dir,
        )
        all_receipts.extend(receipts)
        all_stderr.extend(stderr)
        for index, receipt in enumerate(receipts, 1):
            _atomic_write_json(
                output_dir
                / "receipts"
                / f"semantic_adjudicator_attempt_{index}.json",
                receipt,
            )
        final = dict(adjudication)
        final["matcher_id"] = "adjudicated"
    else:
        final = _consensus(matcher_a, matcher_b)
    errors = validate_semantic_match(final, packet)
    if errors:
        final = unavailable_semantic_match(
            packet,
            "runner_final_fallback",
            reason_code="invalid_final_semantic_match",
            reason="Final semantic record failed deterministic validation.",
        )
    receipt_paths = sorted((output_dir / "receipts").glob("*.json"))
    persisted_receipts = [read_json(path) for path in receipt_paths]
    bundle = {
        "schema_version": "atobench.report_semantic_match_bundle.v1",
        "semantic_packet_id": packet["semantic_packet_id"],
        "episode_pseudonym": packet["episode_pseudonym"],
        "aou": packet["aou"],
        "matcher_outputs": outputs,
        "adjudication_triggered": adjudication_triggered,
        "adjudication_output": adjudication,
        "final": final,
        "invocation_attempt_count": len(persisted_receipts),
        "successful_invocation_count": sum(
            receipt.get("schema_version") == "atobench.claude_invocation_receipt.v1"
            for receipt in persisted_receipts
        ),
        "created_at": utc_now(),
    }
    _atomic_write_json(output_dir / "semantic_match_bundle.json", bundle)
    if all_stderr:
        _atomic_write_json(output_dir / "stderr_records.json", all_stderr)
    manifest = stage_manifest(
        "12e_run_report_semantic_matchers",
        [
            packet_dir / "packet.json",
            packet_dir / "registered_finding_contract.json",
            packet_dir / "output_schema.json",
            config_path,
            lock_path,
            matcher_spec,
            adjudicator_spec,
        ],
        [
            output_dir / "semantic_match_bundle.json",
            *receipt_paths,
        ],
        "complete",
        dry_run=False,
        details={
            "semantic_packet_id": packet["semantic_packet_id"],
            "adjudication_triggered": adjudication_triggered,
            "invocation_attempt_count": len(persisted_receipts),
        },
    )
    _atomic_write_json(output_dir / "stage_manifest.json", manifest)
    incomplete = output_dir / "RUN_INCOMPLETE.json"
    if incomplete.exists():
        incomplete.unlink()
    return bundle


def _run_one(
    row: dict[str, Any],
    *,
    packets_root: Path,
    output_root: Path,
    config_path: Path,
    lock_path: Path,
    agents_dir: Path,
    allow_calls: bool,
    executable_override: str | None,
) -> dict[str, Any]:
    semantic_packet_id = str(row["semantic_packet_id"])
    output_dir = output_root / semantic_packet_id
    event = {
        "semantic_packet_id": semantic_packet_id,
        "started_at": utc_now(),
    }
    try:
        if (output_dir / "semantic_match_bundle.json").is_file():
            event["mode"] = "skip_completed"
        else:
            event["mode"] = (
                "resume_partial" if output_dir.exists() else "fresh_run"
            )
            run_packet(
                packet_dir=packets_root / str(row["relative_path"]),
                output_dir=output_dir,
                config_path=config_path,
                lock_path=lock_path,
                agents_dir=agents_dir,
                allow_calls=allow_calls,
                synthetic_smoke=False,
                executable_override=executable_override,
                new_version=False,
                dry_run=False,
            )
        event["status"] = "complete"
    except Exception as exc:
        event["status"] = "failed"
        event["error"] = str(exc)
    bundle_path = output_dir / "semantic_match_bundle.json"
    if bundle_path.is_file():
        bundle = read_json(bundle_path)
        event["invocation_attempt_count"] = int(
            bundle.get("invocation_attempt_count") or 0
        )
        event["adjudication_triggered"] = (
            bundle.get("adjudication_triggered") is True
        )
        event["known_cost_usd"] = round(
            sum(
                float(read_json(path).get("cost_usd") or 0)
                for path in (output_dir / "receipts").glob("*.json")
            ),
            6,
        )
    event["finished_at"] = utc_now()
    return event


def _emit_cohort_progress(
    event: dict[str, Any],
    events: list[dict[str, Any]],
    total: int,
) -> None:
    print(
        json.dumps(
            {
                "schema_version": "atobench.semantic_match_progress.v1",
                "completed": len(events),
                "total": total,
                "selected_index": event.get("selected_index"),
                "semantic_packet_id": event.get("semantic_packet_id"),
                "status": event.get("status"),
                "mode": event.get("mode"),
                "invocation_attempt_count": event.get(
                    "invocation_attempt_count", 0
                ),
                "adjudication_triggered": event.get(
                    "adjudication_triggered", False
                ),
                "completed_count": sum(
                    row.get("status") == "complete" for row in events
                ),
                "skipped_completed": sum(
                    row.get("mode") == "skip_completed" for row in events
                ),
                "failed_count": sum(
                    row.get("status") == "failed" for row in events
                ),
                "known_cost_usd": round(
                    sum(float(row.get("known_cost_usd") or 0) for row in events),
                    6,
                ),
            },
            sort_keys=True,
        ),
        flush=True,
    )


def run_cohort(
    *,
    semantic_manifest_path: Path,
    packets_root: Path,
    output_root: Path,
    config_path: Path,
    lock_path: Path,
    agents_dir: Path,
    allowlist_path: Path | None,
    allow_calls: bool,
    workers: int,
    continue_on_error: bool,
    executable_override: str | None,
    allow_real_data: bool,
    dry_run: bool,
) -> dict[str, Any]:
    require_real_data_authorization(
        [semantic_manifest_path, packets_root, lock_path], allow_real_data
    )
    rows = read_json(semantic_manifest_path).get("packets", [])
    if allowlist_path is not None:
        allowlist = read_json(allowlist_path)
        allowed = set(allowlist.get("semantic_packet_ids") or [])
        rows = [row for row in rows if row.get("semantic_packet_id") in allowed]
        if len(rows) != len(allowed):
            raise GateError("semantic allowlist contains unknown or duplicate packets")
        if not dry_run and allowlist.get("authorized_for_calls") is not True:
            raise GateError("semantic allowlist is not authorized for calls")
    config = SemanticConfig.load(config_path, executable_override)
    if dry_run:
        return {
            "schema_version": "atobench.semantic_match_cohort_plan.v1",
            "status": "dry_run",
            "selected_episode_count": len(rows),
            "base_call_count": len(rows) * 2,
            "no_retry_max_call_count": len(rows) * 3,
            "hard_retry_max_call_count": (
                len(rows) * 3 * config.max_attempts_per_role
            ),
            "workers": workers,
            "calls_made": 0,
        }
    output_root.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    if workers == 1:
        for selected_index, row in enumerate(rows, 1):
            event = _run_one(
                row,
                packets_root=packets_root,
                output_root=output_root,
                config_path=config_path,
                lock_path=lock_path,
                agents_dir=agents_dir,
                allow_calls=allow_calls,
                executable_override=executable_override,
            )
            event["selected_index"] = selected_index
            events.append(event)
            _emit_cohort_progress(event, events, len(rows))
            if event["status"] == "failed" and not continue_on_error:
                break
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _run_one,
                    row,
                    packets_root=packets_root,
                    output_root=output_root,
                    config_path=config_path,
                    lock_path=lock_path,
                    agents_dir=agents_dir,
                    allow_calls=allow_calls,
                    executable_override=executable_override,
                ): selected_index
                for selected_index, row in enumerate(rows, 1)
            }
            for future in concurrent.futures.as_completed(futures):
                event = future.result()
                event["selected_index"] = futures[future]
                events.append(event)
                _emit_cohort_progress(event, events, len(rows))
    summary = {
        "schema_version": "atobench.semantic_match_cohort_summary.v1",
        "status": (
            "complete"
            if len(events) == len(rows)
            and all(event["status"] == "complete" for event in events)
            else "partial"
        ),
        "selected_episode_count": len(rows),
        "completed_count": sum(event["status"] == "complete" for event in events),
        "skipped_completed": sum(
            event.get("mode") == "skip_completed" for event in events
        ),
        "failed_count": sum(event["status"] == "failed" for event in events),
        "events": sorted(events, key=lambda event: event["semantic_packet_id"]),
    }
    _atomic_write_json(output_root / "cohort_summary.json", summary)
    return summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--semantic-manifest", type=Path, required=True)
    parser.add_argument("--packets-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--agents-dir", type=Path, required=True)
    parser.add_argument("--task-allowlist", type=Path)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--allow-semantic-match-calls", action="store_true")
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--claude-executable")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    try:
        report = run_cohort(
            semantic_manifest_path=args.semantic_manifest,
            packets_root=args.packets_root,
            output_root=args.output,
            config_path=args.config,
            lock_path=args.lock,
            agents_dir=args.agents_dir,
            allowlist_path=args.task_allowlist,
            allow_calls=args.allow_semantic_match_calls,
            workers=args.workers,
            continue_on_error=args.continue_on_error,
            executable_override=args.claude_executable,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
