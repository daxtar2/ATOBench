#!/usr/bin/env python3
"""Rerun packets that failed in a previous full-cohort judge pass.

Usage:
    python3 scripts/08m_rerun_failed_packets.py \
        --summary /path/to/full_cohort_summary.json \
        --packets-root /path/to/packets \
        --output-root /path/to/output \
        --config /path/to/judge_runner_config.json \
        --lock /path/to/analysis_spec.full_cohort.lock.yaml \
        --agents-dir /path/to/subagent_specs \
        [--workers 2] \
        [--claude-executable /path/to/claude]

The script archives any partial output directory for each failed packet, then
re-invokes the single-packet judge.  It writes a new summary of the rerun.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import functools
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.common import GateError, read_json, utc_now, write_json
from atobench_vr.judge_runner import run_packet


def _archive_partial(output_dir: Path, packet_id: str) -> Path | None:
    if not output_dir.exists() or not any(output_dir.iterdir()):
        return None
    stamp = utc_now().replace(":", "").replace("+00:00", "Z")
    archive_root = output_dir.parent / "_archived_rerun_partials"
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_dir = archive_root / f"{output_dir.name}__{packet_id}__{stamp}"
    shutil.move(str(output_dir), str(archive_dir))
    return archive_dir


def _rerun_one(
    failure: dict[str, Any],
    *,
    packets_root: Path,
    output_root: Path,
    config_path: Path,
    lock_path: Path,
    agents_dir: Path,
    executable_override: str | None,
) -> dict[str, Any]:
    """Top-level helper so it can be used with ProcessPoolExecutor."""
    relative_path = str(failure["relative_path"])
    packet_id = str(failure["packet_id"])
    dimension = str(failure["dimension"])
    packet_dir = packets_root / relative_path
    output_dir = output_root / relative_path

    event: dict[str, Any] = {
        "packet_id": packet_id,
        "dimension": dimension,
        "relative_path": relative_path,
        "started_at": utc_now(),
    }
    try:
        archived = _archive_partial(output_dir, packet_id)
        if archived:
            event["archived_partial_dir"] = str(archived.resolve())
        run_packet(
            packet_dir=packet_dir,
            output_dir=output_dir,
            config_path=config_path,
            lock_path=lock_path,
            agents_dir=agents_dir,
            allow_judge_calls=True,
            synthetic_smoke=False,
            executable_override=executable_override,
            new_version=False,
            dry_run=False,
            allow_adjudication=True,
        )
        event["status"] = "complete"
    except Exception as exc:
        event["status"] = "failed"
        event["error"] = str(exc)
    finally:
        event["finished_at"] = utc_now()
    return event


def run(
    *,
    summary_path: Path,
    packets_root: Path,
    output_root: Path,
    config_path: Path,
    lock_path: Path,
    agents_dir: Path,
    workers: int,
    executable_override: str | None,
) -> dict[str, Any]:
    summary = read_json(summary_path)
    failures = summary.get("failures", [])
    if not failures:
        print("No failed packets to rerun.")
        return summary

    rerun_events: list[dict[str, Any]] = []
    rerun_failures: list[dict[str, Any]] = []
    rerun_successes: list[dict[str, Any]] = []

    rerun_kwargs = {
        "packets_root": packets_root,
        "output_root": output_root,
        "config_path": config_path,
        "lock_path": lock_path,
        "agents_dir": agents_dir,
        "executable_override": executable_override,
    }

    if workers == 1:
        rerun_events = [_rerun_one(failure, **rerun_kwargs) for failure in failures]
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
            rerun_events = list(
                executor.map(functools.partial(_rerun_one, **rerun_kwargs), failures)
            )

    for event in rerun_events:
        if event["status"] == "complete":
            rerun_successes.append(event)
        else:
            rerun_failures.append(event)

    rerun_summary = {
        "schema_version": "atobench.full_cohort_rerun_summary.v1",
        "created_at": utc_now(),
        "original_summary": str(summary_path.resolve()),
        "packets_root": str(packets_root.resolve()),
        "output_root": str(output_root.resolve()),
        "workers": workers,
        "attempted": len(failures),
        "succeeded": len(rerun_successes),
        "failed": len(rerun_failures),
        "events": rerun_events,
        "failures": rerun_failures,
    }
    rerun_summary_path = output_root / "full_cohort_rerun_summary.json"
    write_json(rerun_summary_path, rerun_summary)
    print(f"Rerun summary written to {rerun_summary_path}")
    print(f"Attempted: {rerun_summary['attempted']}, Succeeded: {rerun_summary['succeeded']}, Failed: {rerun_summary['failed']}")
    return rerun_summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rerun failed full-cohort judge packets")
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--packets-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--agents-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--claude-executable")
    args = parser.parse_args(argv)
    try:
        run(
            summary_path=args.summary,
            packets_root=args.packets_root,
            output_root=args.output_root,
            config_path=args.config,
            lock_path=args.lock,
            agents_dir=args.agents_dir,
            workers=args.workers,
            executable_override=args.claude_executable,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
