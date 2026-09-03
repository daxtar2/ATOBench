from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .alignment import _enrich_event_body_hashes, _event_view
from .alignment_verifier import _task, _verify_manifest_bound_input
from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    require_real_data_authorization,
    stage_manifest,
    write_json,
)


FEATURES = (
    "main_plus_subagent",
    "rationale_sparse",
    "long_trajectory",
    "transformed_lineage",
)


def _tie(seed: str, episode_id: str) -> str:
    return hashlib.sha256(f"{seed}:{episode_id}".encode()).hexdigest()


def run(
    *,
    alignment_path: Path,
    action_cycles_path: Path,
    messages_path: Path,
    http_events_paths: list[Path],
    output_dir: Path,
    tolerance_seconds: float,
    seed: str,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    if not http_events_paths:
        raise GateError("alignment pilot requires normalized HTTP inputs")
    inputs = [
        alignment_path,
        action_cycles_path,
        messages_path,
        *http_events_paths,
    ]
    require_real_data_authorization(inputs, allow_real_data)
    _verify_manifest_bound_input(alignment_path, "Stage 04 alignment")
    _verify_manifest_bound_input(action_cycles_path, "Stage 03 action cycles")
    _verify_manifest_bound_input(messages_path, "Stage 03 messages")
    for path in http_events_paths:
        _verify_manifest_bound_input(path, "normalized HTTP input")
    alignments = load_jsonl(alignment_path)
    cycles_list = load_jsonl(action_cycles_path)
    cycles = {
        str(row["action_cycle_id"]): row
        for row in cycles_list
        if row.get("action_cycle_id")
    }
    events = [
        event
        for path in http_events_paths
        for event in load_jsonl(path)
    ]
    _enrich_event_body_hashes(events)
    event_views = {
        view["event_id"]: view
        for index, event in enumerate(events)
        for view in [_event_view(event, index)]
    }
    episode_metadata: dict[str, dict[str, Any]] = {}
    transformed: dict[str, bool] = defaultdict(bool)
    for event in events:
        identity = event.get("identity") or {}
        episode_id = str(identity.get("episode_id") or event.get("episode_id"))
        metadata = {
            "aou": identity.get("aou"),
            "condition": identity.get("condition"),
            "model": identity.get("model"),
        }
        previous = episode_metadata.get(episode_id)
        if previous is not None and previous != metadata:
            raise GateError(f"conflicting pilot identity metadata: {episode_id}")
        episode_metadata[episode_id] = metadata
        intervention = event.get("intervention") or {}
        transformed[episode_id] = transformed[episode_id] or bool(
            intervention.get("transform_applied")
        )
    cycle_counts = Counter(str(row.get("episode_id")) for row in cycles_list)
    median_cycles = statistics.median(cycle_counts.values())
    has_subagent: dict[str, bool] = defaultdict(bool)
    for row in cycles_list:
        if row.get("agent_id"):
            has_subagent[str(row.get("episode_id"))] = True
    rationale_chars: dict[str, int] = defaultdict(int)
    with messages_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            value = row.get("recorded_rationale_text")
            if value is not None:
                rationale_chars[str(row.get("episode_id"))] += len(str(value))
    median_rationale = statistics.median(
        rationale_chars.get(episode_id, 0) for episode_id in episode_metadata
    )

    task_records: list[dict[str, Any]] = []
    for row in alignments:
        if row.get("dual_verifier_status") != "pending_dual_verification":
            continue
        cycle = cycles.get(str(row.get("action_cycle_id")))
        if cycle is None:
            raise GateError("pilot pending row has no action cycle")
        task, _ = _task(row, cycle, event_views, tolerance_seconds)
        satisfying = sum(
            all(candidate["deterministic_constraints"].values())
            for candidate in task["candidates"]
        )
        task_class = (
            "unique_deterministic"
            if satisfying == 1
            else "no_deterministic"
            if satisfying == 0
            else "multiple_deterministic"
        )
        task_records.append(
            {
                "task_id": task["alignment_task_id"],
                "episode_id": str(row.get("episode_id")),
                "action_cycle_id": row.get("action_cycle_id"),
                "request_index": row.get("request_index"),
                "task_class": task_class,
                "candidate_count": len(task["candidates"]),
            }
        )
    tasks_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in task_records:
        tasks_by_episode[task["episode_id"]].append(task)

    features: dict[str, dict[str, bool]] = {}
    for episode_id in episode_metadata:
        features[episode_id] = {
            "main_plus_subagent": has_subagent[episode_id],
            "rationale_sparse": rationale_chars.get(episode_id, 0)
            <= median_rationale,
            "long_trajectory": cycle_counts[episode_id] > median_cycles,
            "transformed_lineage": transformed[episode_id],
        }
    selected: list[str] = []
    covered: set[str] = set()
    model_counts: Counter[str] = Counter()
    for aou in ("basket", "jwt", "sqli"):
        for condition in ("C0", "C1"):
            candidates = [
                episode_id
                for episode_id, metadata in episode_metadata.items()
                if metadata["aou"] == aou
                and metadata["condition"] == condition
                and tasks_by_episode.get(episode_id)
            ]
            if len(candidates) < 2:
                raise GateError(f"pilot stratum has fewer than two episodes: {aou}/{condition}")
            for _ in range(2):
                remaining = [episode_id for episode_id in candidates if episode_id not in selected]
                remaining.sort(
                    key=lambda episode_id: (
                        -int(model_counts[str(episode_metadata[episode_id]["model"])] == 0),
                        model_counts[str(episode_metadata[episode_id]["model"])],
                        -sum(
                            feature not in covered and enabled
                            for feature, enabled in features[episode_id].items()
                        ),
                        len(tasks_by_episode[episode_id]),
                        _tie(seed, episode_id),
                    )
                )
                chosen = remaining[0]
                selected.append(chosen)
                model_counts[str(episode_metadata[chosen]["model"])] += 1
                covered.update(
                    feature
                    for feature, enabled in features[chosen].items()
                    if enabled
                )
    if len(selected) != 12:
        raise GateError("alignment pilot selection did not produce 12 episodes")

    allowlisted: list[dict[str, Any]] = []
    episode_rows: list[dict[str, Any]] = []
    for episode_id in selected:
        episode_tasks = sorted(
            tasks_by_episode[episode_id],
            key=lambda row: (row["task_class"], row["task_id"]),
        )
        unique = next(
            (row for row in episode_tasks if row["task_class"] == "unique_deterministic"),
            None,
        )
        challenge = next(
            (
                row
                for row in episode_tasks
                if row["task_class"] in {"multiple_deterministic", "no_deterministic"}
            ),
            None,
        )
        chosen_tasks = [row for row in (unique, challenge) if row is not None]
        if not chosen_tasks:
            chosen_tasks = episode_tasks[:1]
        if len(chosen_tasks) == 1:
            second = next(
                (row for row in episode_tasks if row["task_id"] != chosen_tasks[0]["task_id"]),
                None,
            )
            if second is not None:
                chosen_tasks.append(second)
        allowlisted.extend(chosen_tasks[:2])
        episode_rows.append(
            {
                **episode_metadata[episode_id],
                "episode_id": episode_id,
                **features[episode_id],
                "cycle_count": cycle_counts[episode_id],
                "rationale_char_count": rationale_chars.get(episode_id, 0),
                "pending_task_count": len(episode_tasks),
                "selected_task_count": min(2, len(chosen_tasks)),
            }
        )
    reviewer_count = 2
    planned_calls = len(allowlisted) * reviewer_count
    allowlist = {
        "schema_version": "atobench.alignment_verifier_task_allowlist.v1",
        "status": "draft_unauthorized",
        "authorized_for_calls": False,
        "selection_seed": seed,
        "episode_count": len(selected),
        "task_count": len(allowlisted),
        "reviewer_count": reviewer_count,
        "max_calls": planned_calls,
        "task_ids": [row["task_id"] for row in allowlisted],
    }
    summary = {
        "schema_version": "atobench.alignment_verifier_pilot_selection.v1",
        "status": "selected_no_calls",
        "episode_count": len(selected),
        "task_count": len(allowlisted),
        "planned_calls": planned_calls,
        "claude_calls_made": 0,
        "stratum_counts": dict(
            sorted(
                Counter(
                    f"{row['aou']}:{row['condition']}" for row in episode_rows
                ).items()
            )
        ),
        "model_counts": dict(sorted(model_counts.items())),
        "feature_coverage": {
            feature: sum(bool(row[feature]) for row in episode_rows)
            for feature in FEATURES
        },
        "task_class_counts": dict(
            sorted(Counter(row["task_class"] for row in allowlisted).items())
        ),
        "report_closure_stratification_status": (
            "not_used_to_avoid_outcome_inspection_before_pilot"
        ),
    }
    if dry_run:
        return summary
    ensure_output_available(output_dir, new_version)
    episode_path = output_dir / "pilot_episode_selection_private.csv"
    with episode_path.open("w", newline="", encoding="utf-8") as handle:
        fields = list(episode_rows[0])
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(episode_rows)
    allowlist_path = output_dir / "alignment_task_allowlist.json"
    summary_path = output_dir / "pilot_selection_summary.json"
    write_json(allowlist_path, allowlist)
    write_json(summary_path, summary)
    manifest = stage_manifest(
        "04c_select_alignment_verifier_pilot",
        inputs,
        [episode_path, allowlist_path, summary_path],
        "selected_no_calls",
        dry_run=False,
        details=summary,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alignment", type=Path, required=True)
    parser.add_argument("--action-cycles", type=Path, required=True)
    parser.add_argument("--messages", type=Path, required=True)
    parser.add_argument("--http-events", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timestamp-tolerance-seconds", type=float, default=3.0)
    parser.add_argument("--selection-seed", default="atobench-alignment-pilot-v1")
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            alignment_path=args.alignment,
            action_cycles_path=args.action_cycles,
            messages_path=args.messages,
            http_events_paths=args.http_events,
            output_dir=args.output,
            tolerance_seconds=args.timestamp_tolerance_seconds,
            seed=args.selection_seed,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    return 2
