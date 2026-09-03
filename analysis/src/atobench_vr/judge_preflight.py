from __future__ import annotations

import argparse
import collections
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    read_json,
    read_json_or_yaml,
    stage_manifest,
    write_json,
    write_jsonl,
)
from .judge_runner import AGENT_FILES, RunnerConfig, _check_packet


def run(
    *,
    packet_manifest_path: Path,
    packets_root: Path,
    config_path: Path,
    lock_path: Path,
    agents_dir: Path,
    output_dir: Path,
    max_calls: int,
) -> dict[str, Any]:
    if max_calls < 1:
        raise GateError("--max-calls must be positive")
    packet_manifest = read_json(packet_manifest_path)
    config = RunnerConfig.load(config_path)
    lock = read_json_or_yaml(lock_path)
    for filename in AGENT_FILES.values():
        if not (agents_dir / filename).is_file():
            raise GateError(f"missing canonical agent spec: {filename}")

    rows: list[dict[str, Any]] = []
    by_episode_pseudonym: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for item in packet_manifest.get("packets") or []:
        relative_path = str(item.get("relative_path") or "")
        packet_dir = packets_root / relative_path
        packet = _check_packet(packet_dir)
        if packet["packet_id"] != item.get("packet_id"):
            raise GateError(f"packet manifest identity mismatch: {relative_path}")
        row = {
            "packet_id": packet["packet_id"],
            "episode_pseudonym": packet["episode_pseudonym"],
            "dimension": packet["dimension"],
            "relative_path": relative_path,
            "reviewer_calls": 2,
            "evidence_verifier_calls": 2,
            "adjudicator_calls_min": 0,
            "adjudicator_calls_max": 1,
            "minimum_calls": 4,
            "maximum_calls": 5,
        }
        rows.append(row)
        by_episode_pseudonym[packet["episode_pseudonym"]].append(row)
    if not rows:
        raise GateError("packet manifest contains no packets")
    if int(packet_manifest.get("leakage_hit_count", -1)) != 0:
        raise GateError("packet manifest has private-identity leakage hits")
    if int(packet_manifest.get("secret_hit_count", -1)) != 0:
        raise GateError("packet manifest has secret hits")

    minimum_calls = sum(row["minimum_calls"] for row in rows)
    maximum_calls = sum(row["maximum_calls"] for row in rows)
    if maximum_calls > max_calls:
        raise GateError(
            f"worst-case call plan {maximum_calls} exceeds --max-calls {max_calls}"
        )
    complete_episodes = [
        (pseudonym, episode_rows)
        for pseudonym, episode_rows in sorted(by_episode_pseudonym.items())
        if {row["dimension"] for row in episode_rows}
        == {"verification_control", "stop_decision", "report_grounding"}
    ]
    if not complete_episodes:
        raise GateError("no episode has all three Judge dimensions")
    canary = sorted(rows, key=lambda row: (row["dimension"], row["packet_id"]))[0]
    episode_pseudonym, episode_rows = complete_episodes[0]

    authorization_blockers = []
    if lock.get("freeze_status") != "frozen":
        authorization_blockers.append("freeze_status_not_frozen")
    if lock.get("judge_calls_authorized") is not True:
        authorization_blockers.append("judge_calls_not_authorized")
    details = {
        "packet_count": len(rows),
        "episode_count": len(by_episode_pseudonym),
        "reviewer_calls": len(rows) * 2,
        "evidence_verifier_calls": len(rows) * 2,
        "conditional_adjudicator_calls_max": len(rows),
        "minimum_calls": minimum_calls,
        "maximum_calls": maximum_calls,
        "max_calls": max_calls,
        "call_ceiling_passed": True,
        "claude_calls_made": 0,
        "configured_model": config.model,
        "configured_effort": config.effort,
        "temperature": "not_exposed_by_claude_code_cli",
        "thinking_control": "effort_high",
        "authorization_blockers": authorization_blockers,
        "real_calls_authorized": not authorization_blockers,
        "hash_authorization_gates": False,
        "packet_safety_gate_passed": True,
        "recommended_tiers": {
            "one_packet_canary": {
                "packet_id": canary["packet_id"],
                "relative_path": canary["relative_path"],
                "minimum_calls": 4,
                "maximum_calls": 5,
            },
            "one_episode_three_dimensions": {
                "episode_pseudonym": episode_pseudonym,
                "packet_ids": sorted(row["packet_id"] for row in episode_rows),
                "minimum_calls": 12,
                "maximum_calls": 15,
            },
            "full_engineering_pilot": {
                "packet_count": len(rows),
                "minimum_calls": minimum_calls,
                "maximum_calls": maximum_calls,
            },
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = output_dir / "judge_call_plan.jsonl"
    summary_path = output_dir / "judge_execution_preflight.json"
    write_jsonl(plan_path, rows)
    write_json(
        summary_path,
        {
            "schema_version": "atobench.judge_execution_preflight.v1",
            "status": "blocked_pending_authorization"
            if authorization_blockers
            else "authorized_not_started",
            "details": details,
        },
    )
    manifest = stage_manifest(
        "08a_judge_execution_preflight",
        [
            packet_manifest_path,
            config_path,
            lock_path,
            *[agents_dir / name for name in AGENT_FILES.values()],
        ],
        [plan_path, summary_path],
        "complete_no_calls",
        dry_run=False,
        details=details,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet-manifest", type=Path, required=True)
    parser.add_argument("--packets-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--agents-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(
            packet_manifest_path=args.packet_manifest,
            packets_root=args.packets_root,
            config_path=args.config,
            lock_path=args.lock,
            agents_dir=args.agents_dir,
            output_dir=args.output,
            max_calls=args.max_calls,
        )
    except GateError as exc:
        parser.error(str(exc))
    print(result)
    return 0

