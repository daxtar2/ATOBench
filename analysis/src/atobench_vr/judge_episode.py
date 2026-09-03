from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    load_jsonl,
    read_json,
    stage_manifest,
    utc_now,
    write_json,
)
from .judge_runner import run_packet


DIMENSIONS = ("verification_control", "stop_decision", "report_grounding")


def _select_episode_rows(
    call_plan_path: Path,
    episode_pseudonym: str,
) -> list[dict[str, Any]]:
    rows = [
        row
        for row in load_jsonl(call_plan_path)
        if row.get("episode_pseudonym") == episode_pseudonym
    ]
    by_dimension = {str(row.get("dimension")): row for row in rows}
    if set(by_dimension) != set(DIMENSIONS) or len(rows) != len(DIMENSIONS):
        raise GateError(
            f"episode tier must contain exactly one packet per dimension: "
            f"{episode_pseudonym}"
        )
    return [by_dimension[dimension] for dimension in DIMENSIONS]


def run(
    *,
    packet_root: Path,
    call_plan_path: Path,
    episode_pseudonym: str,
    output_root: Path,
    config_path: Path,
    lock_path: Path,
    agents_dir: Path,
    max_calls: int,
    allow_judge_calls: bool,
) -> dict[str, Any]:
    rows = _select_episode_rows(call_plan_path, episode_pseudonym)
    if max_calls < 1 or max_calls > 15:
        raise GateError("episode call cap must be between 1 and 15")
    planned_maximum = sum(int(row["maximum_calls"]) for row in rows)
    if planned_maximum > max_calls:
        raise GateError("selected episode exceeds global call cap")

    output_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    calls_used = 0
    for row in rows:
        relative_path = Path(str(row["relative_path"]))
        packet_dir = packet_root / relative_path
        output_dir = output_root / str(row["dimension"])
        completed = output_dir / "judgment_bundle.json"
        incomplete = output_dir / "RUN_INCOMPLETE.json"
        if completed.is_file():
            bundle = read_json(completed)
            if bundle.get("packet_id") != row["packet_id"]:
                raise GateError("completed episode-tier output packet mismatch")
        elif incomplete.is_file():
            raise GateError(
                f"incomplete packet requires recovery before continuing: {output_dir}"
            )
        else:
            if calls_used + int(row["maximum_calls"]) > max_calls:
                raise GateError("global episode call cap would be exceeded")
            bundle = run_packet(
                packet_dir=packet_dir,
                output_dir=output_dir,
                config_path=config_path,
                lock_path=lock_path,
                agents_dir=agents_dir,
                allow_judge_calls=allow_judge_calls,
                synthetic_smoke=False,
                allow_adjudication=False,
            )
        invocation_count = int(bundle["invocation_count"])
        calls_used += invocation_count
        if calls_used > max_calls:
            raise GateError("global episode call cap exceeded")
        results.append(
            {
                "dimension": row["dimension"],
                "packet_id": row["packet_id"],
                "invocation_count": invocation_count,
                "adjudication_triggered": bundle["adjudication_triggered"],
                "final": bundle["final"],
                "output_dir": str(output_dir.resolve()),
            }
        )

    summary = {
        "schema_version": "atobench.one_episode_judge_tier.v1",
        "episode_pseudonym": episode_pseudonym,
        "dimensions": results,
        "invocation_count": calls_used,
        "max_calls": max_calls,
        "complete": len(results) == 3,
        "created_at": utc_now(),
    }
    summary_path = output_root / "episode_tier_summary.json"
    write_json(summary_path, summary)
    manifest = stage_manifest(
        "08j_run_one_episode_three_dimensions",
        [
            call_plan_path,
            lock_path,
            config_path,
            *[packet_root / str(row["relative_path"]) / "packet.json" for row in rows],
        ],
        [
            summary_path,
            *[
                output_root / str(row["dimension"]) / "judgment_bundle.json"
                for row in rows
            ],
        ],
        "complete",
        dry_run=False,
        details={
            "episode_pseudonym": episode_pseudonym,
            "packet_count": 3,
            "invocation_count": calls_used,
            "max_calls": max_calls,
        },
    )
    write_json(output_root / "stage_manifest.json", manifest)
    return summary


def cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet-root", type=Path, required=True)
    parser.add_argument("--call-plan", type=Path, required=True)
    parser.add_argument("--episode-pseudonym", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--agents-dir", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--allow-judge-calls", action="store_true")
    args = parser.parse_args()
    try:
        summary = run(
            packet_root=args.packet_root,
            call_plan_path=args.call_plan,
            episode_pseudonym=args.episode_pseudonym,
            output_root=args.output_root,
            config_path=args.config,
            lock_path=args.lock,
            agents_dir=args.agents_dir,
            max_calls=args.max_calls,
            allow_judge_calls=args.allow_judge_calls,
        )
    except GateError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0
