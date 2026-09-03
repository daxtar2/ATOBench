from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    read_json,
    require_real_data_authorization,
    stage_manifest,
    write_json,
)


def run(
    *,
    semantic_manifest_path: Path,
    output_dir: Path,
    expected_episodes: int,
    authorize_calls: bool,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    require_real_data_authorization([semantic_manifest_path], allow_real_data)
    rows = read_json(semantic_manifest_path).get("packets", [])
    if len(rows) != expected_episodes:
        raise GateError(
            f"semantic full cohort has {len(rows)} packets, "
            f"expected {expected_episodes}"
        )
    packet_ids = [str(row.get("semantic_packet_id") or "") for row in rows]
    episode_ids = [str(row.get("episode_pseudonym") or "") for row in rows]
    relative_paths = [str(row.get("relative_path") or "") for row in rows]
    if any(not value for value in packet_ids + episode_ids + relative_paths):
        raise GateError("semantic full cohort contains empty identity fields")
    if len(set(packet_ids)) != len(packet_ids):
        raise GateError("semantic full cohort contains duplicate packet IDs")
    if len(set(episode_ids)) != len(episode_ids):
        raise GateError("semantic full cohort contains duplicate episode identities")
    if len(set(relative_paths)) != len(relative_paths):
        raise GateError("semantic full cohort contains duplicate relative paths")

    summary = {
        "schema_version": "atobench.semantic_match_full_cohort_summary.v1",
        "status": (
            "frozen_authorized" if authorize_calls else "draft_unauthorized"
        ),
        "selected_episode_count": len(rows),
        "aou_episode_counts": dict(
            sorted(collections.Counter(str(row.get("aou")) for row in rows).items())
        ),
        "base_call_count": len(rows) * 2,
        "no_retry_maximum_call_count": len(rows) * 3,
        "hard_retry_call_ceiling": len(rows) * 3 * 2,
        "authorized_for_calls": authorize_calls,
        "model_calls_made": 0,
        "network_accessed": False,
        "hash_gate_enabled": False,
    }
    if dry_run:
        return {
            **summary,
            "status": "dry_run",
            "authorized_for_calls": False,
        }

    ensure_output_available(output_dir, new_version)
    allowlist_path = output_dir / "semantic_match_full_cohort_allowlist.json"
    summary_path = output_dir / "semantic_match_full_cohort_summary.json"
    write_json(
        allowlist_path,
        {
            "schema_version": "atobench.semantic_match_task_allowlist.v1",
            "scope": "full_cohort",
            "authorization_status": summary["status"],
            "authorized_for_calls": authorize_calls,
            "expected_episode_count": len(rows),
            "semantic_packet_ids": sorted(packet_ids),
            "max_base_calls": len(rows) * 2,
            "max_adjudication_calls_without_retry": len(rows),
            "hard_retry_call_ceiling": len(rows) * 3 * 2,
            "hash_gate_enabled": False,
        },
    )
    write_json(summary_path, summary)
    manifest = stage_manifest(
        "12g_freeze_semantic_match_full_cohort",
        [semantic_manifest_path],
        [allowlist_path, summary_path],
        summary["status"],
        dry_run=False,
        details=summary,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--semantic-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-episodes", type=int, default=430)
    parser.add_argument("--authorize-calls", action="store_true")
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(
            semantic_manifest_path=args.semantic_manifest,
            output_dir=args.output,
            expected_episodes=args.expected_episodes,
            authorize_calls=args.authorize_calls,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
