from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    read_json,
    require_real_data_authorization,
    stage_manifest,
    write_csv,
    write_json,
    write_jsonl,
)
from .sessions import canonical_route


ROUTE_PARAMETER_RE = re.compile(r"\{[^{}]+\}")


def _route_key(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return ROUTE_PARAMETER_RE.sub("{id}", canonical_route(value))


def _raw_body_hashes(
    path: Path,
) -> dict[int, str | None]:
    hashes: dict[int, str | None] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise GateError(
                    f"invalid raw HTTP JSONL at {path}:{line_number}: {exc}"
                ) from exc
            request = row.get("request") if isinstance(row.get("request"), dict) else {}
            meta = (
                request.get("body_meta")
                if isinstance(request.get("body_meta"), dict)
                else {}
            )
            body = request.get("body")
            if body is None or meta.get("truncated") is True:
                hashes[line_number] = None
            else:
                hashes[line_number] = hashlib.sha256(str(body).encode()).hexdigest()
    return hashes


def _enrich_event_body_hashes(events: list[dict[str, Any]]) -> dict[str, int]:
    cache: dict[Path, dict[int, str | None]] = {}
    enriched = 0
    unavailable = 0
    for event in events:
        action = event.get("action") if isinstance(event.get("action"), dict) else {}
        if action.get("request_body_sha256") or action.get("body_sha256"):
            continue
        integrity = (
            event.get("integrity")
            if isinstance(event.get("integrity"), dict)
            else {}
        )
        pointer = integrity.get("raw_source_pointer")
        if not isinstance(pointer, str):
            unavailable += 1
            continue
        raw_path_text, separator, line_text = pointer.rpartition(":")
        if not separator or not line_text.isdigit():
            unavailable += 1
            continue
        raw_path = Path(raw_path_text).resolve()
        if raw_path not in cache:
            if not raw_path.is_file():
                raise GateError(f"normalized HTTP raw source missing: {raw_path}")
            cache[raw_path] = _raw_body_hashes(raw_path)
        body_hash = cache[raw_path].get(int(line_text))
        if body_hash:
            action["request_body_sha256"] = body_hash
            enriched += 1
        else:
            unavailable += 1
    return {
        "raw_source_file_count": len(cache),
        "request_body_hash_enriched_count": enriched,
        "request_body_hash_unavailable_count": unavailable,
    }


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_view(event: dict[str, Any], index: int) -> dict[str, Any]:
    identity = event.get("identity") if isinstance(event.get("identity"), dict) else {}
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    observation = (
        event.get("observation") if isinstance(event.get("observation"), dict) else {}
    )
    episode_id = event.get("episode_id") or identity.get("episode_id")
    method = event.get("method") or action.get("method")
    route = (
        event.get("canonical_route")
        or action.get("route_template")
        or action.get("canonical_route")
    )
    raw_url = event.get("url") or event.get("path")
    if not route and raw_url:
        route = canonical_route(str(raw_url))
    integrity = (
        event.get("integrity") if isinstance(event.get("integrity"), dict) else {}
    )
    raw_file_sha256 = integrity.get("raw_file_sha256")
    raw_line_number = integrity.get("raw_line_number")
    derived_event_id = (
        f"http:{str(raw_file_sha256)[:16]}:line:{raw_line_number}"
        if raw_file_sha256 and raw_line_number
        else f"{episode_id or 'UNKNOWN'}:http:{index}"
    )
    return {
        "event_id": str(
            event.get("event_id")
            or event.get("id")
            or derived_event_id
        ),
        "episode_id": episode_id,
        "method": str(method).upper() if method else None,
        "canonical_route": _route_key(route),
        "request_body_sha256": (
            event.get("request_body_sha256")
            or action.get("request_body_sha256")
            or action.get("body_sha256")
        ),
        "timestamp": event.get("timestamp") or identity.get("timestamp"),
        "turn_idx": identity.get("turn_idx") or event.get("turn_idx"),
        "status_code": event.get("status_code") or observation.get("status_code"),
        "raw": event,
    }


def _request_candidates(
    request: dict[str, Any],
    cycle: dict[str, Any],
    events: list[dict[str, Any]],
    tolerance_seconds: float,
) -> tuple[list[dict[str, Any]], str]:
    identity = [
        event
        for event in events
        if event["episode_id"] == cycle.get("episode_id")
        and event["method"] == request.get("method")
        and event["canonical_route"] == _route_key(request.get("canonical_route"))
    ]
    request_body_hash = request.get("request_body_sha256")
    if request_body_hash:
        body_matches = [
            event
            for event in identity
            if event.get("request_body_sha256") == request_body_hash
        ]
        if body_matches:
            if len(body_matches) == 1:
                return body_matches, "identity_method_route_body"
            cycle_time = _timestamp(cycle.get("timestamp"))
            if cycle_time:
                timed_body_matches = []
                for event in body_matches:
                    event_time = _timestamp(event.get("timestamp"))
                    if event_time is None:
                        continue
                    if (
                        abs((event_time - cycle_time).total_seconds())
                        <= tolerance_seconds
                    ):
                        timed_body_matches.append(event)
                if timed_body_matches:
                    return timed_body_matches, "identity_method_route_body_timestamp"
            return body_matches, "identity_method_route_body"
    cycle_time = _timestamp(cycle.get("timestamp"))
    if cycle_time:
        timed = []
        for event in identity:
            event_time = _timestamp(event.get("timestamp"))
            if event_time is None:
                continue
            if abs((event_time - cycle_time).total_seconds()) <= tolerance_seconds:
                timed.append(event)
        if timed:
            return timed, "identity_method_route_timestamp"
    return identity, "identity_method_route"


def align(
    cycles: list[dict[str, Any]],
    http_events: list[dict[str, Any]],
    *,
    tolerance_seconds: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events = [_event_view(event, index) for index, event in enumerate(http_events)]
    event_ids = [event["event_id"] for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise GateError("normalized HTTP inputs contain duplicate stable event IDs")
    events_by_identity: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        events_by_identity[
            (
                str(event.get("episode_id")),
                str(event.get("method")),
                str(event.get("canonical_route")),
            )
        ].append(event)
    rows: list[dict[str, Any]] = []
    used_events: set[str] = set()
    candidate_events: set[str] = set()
    for cycle in cycles:
        requests = cycle.get("parsed_http_requests") or []
        if not requests:
            rows.append(
                {
                    "schema_version": "atobench.claude_http_alignment.v1",
                    "episode_id": cycle.get("episode_id"),
                    "action_cycle_id": cycle.get("action_cycle_id"),
                    "request_index": None,
                    "alignment_status": "non_http_tool",
                    "candidate_event_ids": [],
                    "accepted_event_ids": [],
                    "match_basis": None,
                    "accepted_for_evidence": False,
                    "dual_verifier_status": "not_applicable",
                    "constraint_pass": True,
                }
            )
            continue
        for request in requests:
            identity_events = events_by_identity.get(
                (
                    str(cycle.get("episode_id")),
                    str(request.get("method")),
                    str(_route_key(request.get("canonical_route"))),
                ),
                [],
            )
            candidates, basis = _request_candidates(
                request, cycle, identity_events, tolerance_seconds
            )
            candidate_ids = [event["event_id"] for event in candidates]
            exact_body = basis in {
                "identity_method_route_body",
                "identity_method_route_body_timestamp",
            }
            timestamp_assisted = basis == "identity_method_route_timestamp"
            if len(candidates) == 1 and exact_body:
                status = "exact_unique"
                accepted = candidate_ids
                accepted_for_evidence = True
                verifier_status = "not_required"
            elif len(candidates) == 1 and timestamp_assisted:
                status = "timestamp_unique"
                accepted = []
                accepted_for_evidence = False
                verifier_status = "pending_dual_verification"
            elif len(candidates) == 1:
                status = "ambiguous"
                accepted = []
                accepted_for_evidence = False
                verifier_status = "not_required"
            elif len(candidates) > 1:
                status = "ambiguous"
                accepted = []
                accepted_for_evidence = False
                verifier_status = (
                    "pending_dual_verification"
                    if timestamp_assisted
                    else "not_applicable"
                )
            else:
                status = "no_proxy_match"
                accepted = []
                accepted_for_evidence = False
                verifier_status = "not_applicable"
            candidate_events.update(candidate_ids)
            used_events.update(accepted)
            rows.append(
                {
                    "schema_version": "atobench.claude_http_alignment.v1",
                    "episode_id": cycle.get("episode_id"),
                    "action_cycle_id": cycle.get("action_cycle_id"),
                    "request_index": request.get("request_index"),
                    "method": request.get("method"),
                    "canonical_route": request.get("canonical_route"),
                    "alignment_status": status,
                    "candidate_event_ids": candidate_ids,
                    "accepted_event_ids": accepted,
                    "match_basis": basis,
                    "accepted_for_evidence": accepted_for_evidence,
                    "dual_verifier_status": verifier_status,
                    "constraint_pass": True,
                }
            )
    event_lookup = {event["event_id"]: event for event in events}
    cycle_lookup = {
        str(cycle.get("action_cycle_id")): cycle
        for cycle in cycles
    }
    accepted_claims: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for event_id in row["accepted_event_ids"]:
            accepted_claims[event_id].append(row)
    for event_id, claims in accepted_claims.items():
        if len(claims) < 2:
            continue
        event_time = _timestamp(event_lookup[event_id].get("timestamp"))
        ranked: list[tuple[float, dict[str, Any]]] = []
        if event_time is not None:
            for row in claims:
                cycle_time = _timestamp(
                    cycle_lookup[str(row.get("action_cycle_id"))].get("timestamp")
                )
                if cycle_time is not None:
                    ranked.append(
                        (abs((event_time - cycle_time).total_seconds()), row)
                    )
        ranked.sort(key=lambda item: item[0])
        winner = (
            ranked[0][1]
            if ranked and (len(ranked) == 1 or ranked[0][0] < ranked[1][0])
            else None
        )
        for row in claims:
            if row is winner:
                row["event_conflict_resolution"] = "unique_nearest_timestamp"
                continue
            row["alignment_status"] = "ambiguous"
            row["accepted_event_ids"] = []
            row["accepted_for_evidence"] = False
            row["dual_verifier_status"] = "not_applicable"
            row["event_conflict_resolution"] = (
                "excluded_non_nearest"
                if winner is not None
                else "excluded_no_unique_nearest"
            )
    exact_by_cycle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("accepted_for_evidence"):
            row["alignment_status"] = "exact_unique"
            exact_by_cycle[str(row.get("action_cycle_id"))].append(row)
    for cycle_rows in exact_by_cycle.values():
        if len(cycle_rows) > 1:
            for row in cycle_rows:
                row["alignment_status"] = "exact_one_to_many"
    for event in events:
        if event["event_id"] not in candidate_events:
            rows.append(
                {
                    "schema_version": "atobench.claude_http_alignment.v1",
                    "episode_id": event["episode_id"],
                    "action_cycle_id": None,
                    "request_index": None,
                    "method": event["method"],
                    "canonical_route": event["canonical_route"],
                    "alignment_status": "proxy_only_event",
                    "candidate_event_ids": [event["event_id"]],
                    "accepted_event_ids": [],
                    "match_basis": None,
                    "accepted_for_evidence": False,
                    "dual_verifier_status": "not_applicable",
                    "constraint_pass": True,
                }
            )
    errors = []
    accepted_owner: dict[str, str] = {}
    for row in rows:
        for event_id in row["accepted_event_ids"]:
            owner = accepted_owner.get(event_id)
            if owner and owner != row["action_cycle_id"]:
                errors.append(
                    {
                        "error": "http_event_mapped_to_multiple_action_cycles",
                        "event_id": event_id,
                        "first_cycle": owner,
                        "second_cycle": row["action_cycle_id"],
                    }
                )
            accepted_owner[event_id] = row["action_cycle_id"]
    return rows, errors


def run(
    *,
    action_cycles_path: Path,
    http_events_paths: list[Path],
    output_dir: Path,
    tolerance_seconds: float,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    if not http_events_paths:
        raise GateError("Stage 04 requires at least one --http-events input")
    inputs = [action_cycles_path, *http_events_paths]
    require_real_data_authorization(inputs, allow_real_data)
    cycle_manifest_path = action_cycles_path.parent / "stage_manifest.json"
    if cycle_manifest_path.is_file():
        cycle_manifest = read_json(cycle_manifest_path)
        if cycle_manifest.get("status") != "complete":
            raise GateError("Stage 04 requires a complete Stage 03 manifest")
    for path in http_events_paths:
        source_manifest_path = path.parent / "stage_manifest.json"
        if not source_manifest_path.is_file():
            continue
        source_manifest = read_json(source_manifest_path)
        if source_manifest.get("status") != "complete":
            raise GateError(f"normalized HTTP source is not complete: {path}")
    cycles = load_jsonl(action_cycles_path)
    events = [
        event
        for path in http_events_paths
        for event in load_jsonl(path)
    ]
    cycle_episodes = {str(row.get("episode_id")) for row in cycles}
    event_episodes = {
        str(
            row.get("episode_id")
            or (
                row.get("identity", {}).get("episode_id")
                if isinstance(row.get("identity"), dict)
                else None
            )
        )
        for row in events
    }
    if cycle_episodes != event_episodes:
        raise GateError(
            "Stage 04 action-cycle and normalized-HTTP episode cohorts differ"
        )
    episode_metadata: dict[str, dict[str, Any]] = {}
    for event in events:
        identity = (
            event.get("identity")
            if isinstance(event.get("identity"), dict)
            else {}
        )
        episode_id = str(event.get("episode_id") or identity.get("episode_id"))
        metadata = {
            "aou": identity.get("aou"),
            "condition": identity.get("condition"),
            "model": identity.get("model"),
        }
        previous = episode_metadata.get(episode_id)
        if previous is not None and previous != metadata:
            raise GateError(f"conflicting HTTP identity metadata for {episode_id}")
        episode_metadata[episode_id] = metadata
    cycles_per_episode = Counter(str(row.get("episode_id")) for row in cycles)
    median_cycle_count = statistics.median(cycles_per_episode.values())
    episode_has_subagent: dict[str, bool] = defaultdict(bool)
    for cycle in cycles:
        episode_id = str(cycle.get("episode_id"))
        if cycle.get("agent_id"):
            episode_has_subagent[episode_id] = True
    for episode_id, metadata in episode_metadata.items():
        metadata["tree_shape"] = (
            "main_plus_subagent"
            if episode_has_subagent[episode_id]
            else "main_only"
        )
        metadata["trajectory_length_bucket"] = (
            "short"
            if cycles_per_episode[episode_id] <= median_cycle_count
            else "long"
        )
    if dry_run:
        return {
            "stage": "04_align_claude_http",
            "status": "dry_run",
            "action_cycle_count": len(cycles),
            "http_event_count": len(events),
            "episode_count": len(cycle_episodes),
            "http_source_count": len(http_events_paths),
        }
    ensure_output_available(output_dir, new_version)
    enrichment = _enrich_event_body_hashes(events)
    rows, errors = align(cycles, events, tolerance_seconds=tolerance_seconds)
    alignment_path = output_dir / "claude_http_alignment.jsonl"
    error_path = output_dir / "alignment_errors.jsonl"
    summary_path = output_dir / "episode_alignment_summary.csv"
    sample_path = output_dir / "alignment_review_sample.csv"
    write_jsonl(alignment_path, rows)
    by_episode: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_episode[str(row.get("episode_id"))][row["alignment_status"]] += 1
    summary_rows = [
        {
            **episode_metadata.get(episode_id, {}),
            "episode_id": episode_id,
            "total_alignment_rows": sum(counts.values()),
            "accepted_exact": counts["exact_unique"] + counts["exact_one_to_many"],
            "timestamp_pending": sum(
                1
                for row in rows
                if str(row.get("episode_id")) == episode_id
                and row["dual_verifier_status"] == "pending_dual_verification"
            ),
            "ambiguous": counts["ambiguous"],
            "no_proxy_match": counts["no_proxy_match"],
            "proxy_only_event": counts["proxy_only_event"],
        }
        for episode_id, counts in sorted(by_episode.items())
    ]
    write_csv(
        summary_path,
        summary_rows,
        [
            "aou",
            "condition",
            "model",
            "tree_shape",
            "trajectory_length_bucket",
            "episode_id",
            "total_alignment_rows",
            "accepted_exact",
            "timestamp_pending",
            "ambiguous",
            "no_proxy_match",
            "proxy_only_event",
        ],
    )
    cycle_metadata = {
        str(row.get("action_cycle_id")): episode_metadata.get(
            str(row.get("episode_id")), {}
        )
        for row in cycles
    }
    sample_rows: list[dict[str, Any]] = []
    seen_strata: set[tuple[str, str, str, str, str, str]] = set()
    for row in sorted(
        (row for row in rows if row.get("action_cycle_id")),
        key=lambda item: (
            str(item.get("episode_id")),
            str(item.get("action_cycle_id")),
            int(item.get("request_index") or 0),
        ),
    ):
        metadata = cycle_metadata.get(str(row.get("action_cycle_id")), {})
        stratum = (
            str(metadata.get("aou")),
            str(metadata.get("condition")),
            str(metadata.get("model")),
            str(metadata.get("tree_shape")),
            str(metadata.get("trajectory_length_bucket")),
            str(row.get("alignment_status")),
        )
        if stratum in seen_strata:
            continue
        seen_strata.add(stratum)
        sample_rows.append(
            {
                **metadata,
                "episode_id": row.get("episode_id"),
                "action_cycle_id": row.get("action_cycle_id"),
                "request_index": row.get("request_index"),
                "alignment_status": row.get("alignment_status"),
                "match_basis": row.get("match_basis"),
                "candidate_count": len(row.get("candidate_event_ids") or []),
                "accepted_count": len(row.get("accepted_event_ids") or []),
                "accepted_for_evidence": row.get("accepted_for_evidence"),
                "dual_verifier_status": row.get("dual_verifier_status"),
                "constraint_pass": row.get("constraint_pass"),
            }
        )
    write_csv(
        sample_path,
        sample_rows,
        [
            "aou",
            "condition",
            "model",
            "tree_shape",
            "trajectory_length_bucket",
            "episode_id",
            "action_cycle_id",
            "request_index",
            "alignment_status",
            "match_basis",
            "candidate_count",
            "accepted_count",
            "accepted_for_evidence",
            "dual_verifier_status",
            "constraint_pass",
        ],
    )
    accepted_rows = [row for row in rows if row["accepted_for_evidence"]]
    accepted_constraint_pass_rate = (
        sum(bool(row["constraint_pass"]) for row in accepted_rows)
        / len(accepted_rows)
        if accepted_rows
        else 1.0
    )
    if accepted_constraint_pass_rate != 1.0:
        errors.append({"error": "accepted_constraint_pass_rate_below_one"})
    write_jsonl(error_path, errors)
    status = "complete" if not errors else "blocked"
    manifest = stage_manifest(
        "04_align_claude_http",
        inputs,
        [alignment_path, summary_path, sample_path, error_path],
        status,
        dry_run=False,
        details={
            "alignment_row_count": len(rows),
            "action_cycle_count": len(cycles),
            "http_event_count": len(events),
            "episode_count": len(cycle_episodes),
            "http_source_count": len(http_events_paths),
            "status_counts": dict(
                sorted(Counter(row["alignment_status"] for row in rows).items())
            ),
            "accepted_for_evidence_count": sum(
                bool(row["accepted_for_evidence"]) for row in rows
            ),
            "accepted_constraint_pass_rate": accepted_constraint_pass_rate,
            "event_conflict_excluded_count": sum(
                str(row.get("event_conflict_resolution", "")).startswith("excluded_")
                for row in rows
            ),
            "pending_dual_verification_count": sum(
                row["dual_verifier_status"] == "pending_dual_verification"
                for row in rows
            ),
            "error_count": len(errors),
            "cross_episode_accepted_count": 0,
            "review_sample_count": len(sample_rows),
            **enrichment,
            "timestamp_tolerance_seconds": tolerance_seconds,
        },
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    if errors:
        raise GateError(f"alignment blocking errors: {errors[:3]}")
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action-cycles", type=Path, required=True)
    parser.add_argument(
        "--http-events",
        type=Path,
        action="append",
        required=True,
        help="repeat for each normalized HTTP source",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timestamp-tolerance-seconds", type=float, default=3.0)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            action_cycles_path=args.action_cycles,
            http_events_paths=args.http_events,
            output_dir=args.output,
            tolerance_seconds=args.timestamp_tolerance_seconds,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    return 2
