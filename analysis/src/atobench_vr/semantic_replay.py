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
    utc_now,
    write_json,
)
from .semantic_output import (
    canonicalize_semantic_match,
    semantic_decision_key,
    validate_semantic_match,
)
from .semantic_runner import _consensus


MATCHER_IDS = ("semantic_matcher_a", "semantic_matcher_b")


def _raw_role(source_dir: Path, role_id: str) -> dict[str, Any]:
    path = source_dir / "raw" / f"{role_id}.json"
    if not path.is_file():
        raise GateError(f"semantic replay missing raw role output: {path}")
    value = read_json(path)
    if not isinstance(value, dict):
        raise GateError(f"semantic replay raw role is not an object: {path}")
    return value


def run(
    *,
    semantic_manifest_path: Path,
    packets_root: Path,
    source_matches_root: Path,
    output_root: Path,
    allowlist_path: Path | None,
    expected_episodes: int,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    inputs = [semantic_manifest_path, packets_root, source_matches_root]
    if allowlist_path is not None:
        inputs.append(allowlist_path)
    require_real_data_authorization(inputs, allow_real_data)
    rows = read_json(semantic_manifest_path).get("packets", [])
    if allowlist_path is not None:
        allowlist_ids = list(
            read_json(allowlist_path).get("semantic_packet_ids") or []
        )
        allowed = set(allowlist_ids)
        if len(allowed) != len(allowlist_ids):
            raise GateError("semantic replay allowlist contains duplicate packet IDs")
        rows = [row for row in rows if row.get("semantic_packet_id") in allowed]
        if len(rows) != len(allowed):
            raise GateError("semantic replay allowlist does not match manifest")
    if len(rows) != expected_episodes:
        raise GateError(
            f"semantic replay selected {len(rows)} episodes, "
            f"expected {expected_episodes}"
        )
    if dry_run:
        return {
            "schema_version": "atobench.semantic_replay_plan.v1",
            "status": "dry_run",
            "selected_episode_count": len(rows),
            "model_calls_made": 0,
            "network_accessed": False,
        }

    ensure_output_available(output_root, new_version)
    events: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for selected_index, row in enumerate(rows, 1):
        semantic_packet_id = str(row["semantic_packet_id"])
        packet_dir = packets_root / str(row["relative_path"])
        packet = read_json(packet_dir / "packet.json")
        source_dir = source_matches_root / semantic_packet_id
        source_bundle = read_json(source_dir / "semantic_match_bundle.json")
        outputs: dict[str, dict[str, Any]] = {}
        role_warnings: dict[str, list[str]] = {}
        for role_id in MATCHER_IDS:
            output, warnings = canonicalize_semantic_match(
                _raw_role(source_dir, role_id),
                packet,
                role_id,
            )
            outputs[role_id] = output
            role_warnings[role_id] = warnings

        matcher_a = outputs[MATCHER_IDS[0]]
        matcher_b = outputs[MATCHER_IDS[1]]
        disagreement = (
            semantic_decision_key(matcher_a)
            != semantic_decision_key(matcher_b)
        )
        adjudication = None
        resolution_method = "replayed_matcher_consensus"
        if disagreement:
            adjudicator_path = source_dir / "raw" / "semantic_adjudicator.json"
            if not adjudicator_path.is_file():
                unresolved.append(
                    {
                        "semantic_packet_id": semantic_packet_id,
                        "code": "new_disagreement_without_source_adjudication",
                        "matcher_a_key": list(semantic_decision_key(matcher_a)),
                        "matcher_b_key": list(semantic_decision_key(matcher_b)),
                    }
                )
                continue
            adjudication, adjudication_warnings = canonicalize_semantic_match(
                read_json(adjudicator_path),
                packet,
                "semantic_adjudicator",
            )
            role_warnings["semantic_adjudicator"] = adjudication_warnings
            final = dict(adjudication)
            final["matcher_id"] = "adjudicated_replay"
            resolution_method = "replayed_source_adjudication"
        else:
            final = _consensus(matcher_a, matcher_b)
            final["matcher_id"] = "matcher_consensus_replay"

        errors = validate_semantic_match(final, packet)
        if errors:
            unresolved.append(
                {
                    "semantic_packet_id": semantic_packet_id,
                    "code": "invalid_replayed_final",
                    "errors": errors,
                }
            )
            continue

        output_dir = output_root / semantic_packet_id
        output_dir.mkdir(parents=True)
        bundle = {
            "schema_version": "atobench.report_semantic_match_bundle.v1",
            "semantic_packet_id": semantic_packet_id,
            "episode_pseudonym": packet["episode_pseudonym"],
            "aou": packet["aou"],
            "matcher_outputs": outputs,
            "adjudication_triggered": disagreement,
            "adjudication_output": adjudication,
            "final": final,
            "invocation_attempt_count": 0,
            "successful_invocation_count": 0,
            "source_invocation_attempt_count": source_bundle.get(
                "invocation_attempt_count", 0
            ),
            "source_bundle_path": str(
                (source_dir / "semantic_match_bundle.json").resolve()
            ),
            "resolution_method": resolution_method,
            "replay_warnings": role_warnings,
            "model_calls_made": 0,
            "network_accessed": False,
            "created_at": utc_now(),
        }
        write_json(output_dir / "semantic_match_bundle.json", bundle)
        events.append(
            {
                "selected_index": selected_index,
                "semantic_packet_id": semantic_packet_id,
                "aou": packet["aou"],
                "resolution_method": resolution_method,
                "source_insufficient": source_bundle["final"].get(
                    "insufficient_evidence"
                )
                is True,
                "replayed_insufficient": final.get("insufficient_evidence") is True,
                "report_closure": final.get("report_closure"),
                "claim_trace_support": final.get("claim_trace_support"),
            }
        )

    summary = {
        "schema_version": "atobench.semantic_replay_summary.v1",
        "status": (
            "PASS"
            if not unresolved and len(events) == len(rows)
            else "BLOCKED"
        ),
        "selected_episode_count": len(rows),
        "replayed_episode_count": len(events),
        "unresolved_count": len(unresolved),
        "source_insufficient_count": sum(
            event["source_insufficient"] for event in events
        ),
        "replayed_insufficient_count": sum(
            event["replayed_insufficient"] for event in events
        ),
        "recovered_insufficient_count": sum(
            event["source_insufficient"] and not event["replayed_insufficient"]
            for event in events
        ),
        "resolution_method_counts": dict(
            sorted(
                collections.Counter(
                    event["resolution_method"] for event in events
                ).items()
            )
        ),
        "final_semantic_counts": dict(
            sorted(
                collections.Counter(
                    (
                        "insufficient"
                        if event["replayed_insufficient"]
                        else f"closure_{str(event['report_closure']).lower()}_"
                        f"{event['claim_trace_support']}"
                    )
                    for event in events
                ).items()
            )
        ),
        "model_calls_made": 0,
        "network_accessed": False,
    }
    write_json(output_root / "replay_events.json", events)
    write_json(output_root / "replay_unresolved.json", unresolved)
    write_json(output_root / "semantic_replay_summary.json", summary)
    manifest = stage_manifest(
        "12j_replay_semantic_match_full_cohort",
        inputs,
        [
            output_root / "replay_events.json",
            output_root / "replay_unresolved.json",
            output_root / "semantic_replay_summary.json",
        ],
        "complete" if summary["status"] == "PASS" else "blocked",
        dry_run=False,
        details=summary,
    )
    write_json(output_root / "stage_manifest.json", manifest)
    if unresolved:
        raise GateError(
            f"semantic replay has {len(unresolved)} unresolved packets"
        )
    return summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--semantic-manifest", type=Path, required=True)
    parser.add_argument("--packets-root", type=Path, required=True)
    parser.add_argument("--source-matches-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-allowlist", type=Path)
    parser.add_argument("--expected-episodes", type=int, required=True)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(
            semantic_manifest_path=args.semantic_manifest,
            packets_root=args.packets_root,
            source_matches_root=args.source_matches_root,
            output_root=args.output,
            allowlist_path=args.task_allowlist,
            expected_episodes=args.expected_episodes,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
