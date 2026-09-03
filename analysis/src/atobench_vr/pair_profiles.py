from __future__ import annotations

import argparse
import collections
import csv
import json
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    read_json_or_yaml,
    require_real_data_authorization,
    sha256_file,
    stage_manifest,
    utc_now,
    write_json,
    write_jsonl,
)
from .schemas import DIMENSIONS
from .semantic_output import derive_verification_resolution_state


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _csv_true(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _unique_by(
    rows: list[dict[str, Any]],
    key: str,
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = str(row.get(key) or "")
        if not value:
            raise GateError(f"Stage 12 {label} row has no {key}")
        if value in result:
            raise GateError(f"Stage 12 duplicate {label} {key}: {value}")
        result[value] = row
    return result


def _lineage_identity(
    state: dict[str, Any],
    lineage_by_packet: dict[str, dict[str, Any]],
) -> dict[str, str]:
    source_packet_ids = state.get("source_packet_ids")
    if not isinstance(source_packet_ids, dict) or set(source_packet_ids) != DIMENSIONS:
        raise GateError(
            f"Stage 12 episode {state.get('episode_pseudonym')} has invalid packet set"
        )
    lineage_rows = []
    for dimension in sorted(DIMENSIONS):
        packet_id = str(source_packet_ids[dimension])
        lineage = lineage_by_packet.get(packet_id)
        if lineage is None:
            raise GateError(f"Stage 12 missing private lineage for packet {packet_id}")
        if lineage.get("dimension") != dimension:
            raise GateError(
                f"Stage 12 lineage dimension mismatch for packet {packet_id}"
            )
        lineage_rows.append(lineage)
    identity_fields = (
        "episode_id",
        "pair_id",
        "campaign_id",
        "condition",
        "model",
    )
    identity: dict[str, str] = {}
    for field in identity_fields:
        values = {str(row.get(field) or "") for row in lineage_rows}
        if len(values) != 1 or "" in values:
            raise GateError(
                f"Stage 12 inconsistent {field} across packets for "
                f"{state.get('episode_pseudonym')}: {sorted(values)}"
            )
        identity[field] = next(iter(values))
    return identity


def _task_evidence_endpoint(state: dict[str, Any]) -> dict[str, Any]:
    primary = (
        state.get("dimensions", {})
        .get("stop_decision", {})
        .get("descriptor", {})
        .get("primary_evidence")
    )
    if not isinstance(primary, dict):
        raise GateError(
            f"Stage 12 episode {state.get('episode_pseudonym')} has no task endpoint"
        )
    return {
        "fact_id": primary.get("fact_id"),
        "fact_type": primary.get("fact_type"),
        "measurement_status": primary.get("measurement_status"),
        "value": primary.get("value"),
    }


def _dimension_transition(
    pair_id: str,
    dimension: str,
    c0: dict[str, Any],
    c1: dict[str, Any],
) -> dict[str, Any]:
    c0_dimension = c0["dimensions"][dimension]
    c1_dimension = c1["dimensions"][dimension]
    c0_final = c0_dimension["final"]
    c1_final = c1_dimension["final"]
    score0 = c0_final.get("score")
    score1 = c1_final.get("score")
    numeric = isinstance(score0, (int, float)) and isinstance(score1, (int, float))
    if numeric:
        delta = score1 - score0
        direction = "higher" if delta > 0 else "lower" if delta < 0 else "unchanged"
    else:
        delta = None
        direction = "unavailable"
    low0 = c0_final.get("score_low")
    high0 = c0_final.get("score_high")
    low1 = c1_final.get("score_low")
    high1 = c1_final.get("score_high")
    interval_numeric = all(
        isinstance(value, (int, float))
        for value in (low0, high0, low1, high1)
    )
    return {
        "schema_version": "atobench.pair_score_transition.v1",
        "pair_id": pair_id,
        "dimension": dimension,
        "measurement_role": c0_dimension["measurement_role"],
        "numeric_interpretation": c0_dimension["numeric_interpretation"],
        "c0": c0_final,
        "c1": c1_final,
        "score_delta_c1_minus_c0": delta,
        "score_direction": direction,
        "band_transition": {
            "from": c0_final.get("band"),
            "to": c1_final.get("band"),
        },
        "conservative_delta_range": {
            "low": low1 - high0 if interval_numeric else None,
            "high": high1 - low0 if interval_numeric else None,
        },
        "statistical_interpretation_performed": False,
    }


def _episode_projection(
    state: dict[str, Any],
    identity: dict[str, str],
    census: dict[str, str],
    semantic_match: dict[str, Any] | None,
) -> dict[str, Any]:
    task_endpoint = _task_evidence_endpoint(state)
    verification_state, verification_reason = derive_verification_resolution_state(
        task_endpoint,
        semantic_match,
    )
    if semantic_match is None:
        report_closure = {
            "measurement_status": "unavailable",
            "value": None,
            "reason": "registered_report_closure_not_semantically_frozen",
        }
        claim_trace_status = {
            "measurement_status": "unavailable",
            "value": None,
            "reason": "semantic_report_claim_matching_not_frozen",
        }
    else:
        report_closure = {
            "measurement_status": (
                "unavailable"
                if semantic_match.get("report_closure") is None
                else "measured"
            ),
            "value": semantic_match.get("report_closure"),
            "reason": semantic_match.get("reason_code"),
        }
        claim_trace_status = {
            "measurement_status": (
                "unavailable"
                if semantic_match.get("claim_trace_support") == "unavailable"
                else "measured"
            ),
            "value": semantic_match.get("claim_trace_support"),
            "reason": semantic_match.get("reason_code"),
        }
    return {
        "schema_version": "atobench.unblinded_episode_state.v1",
        "episode_id": identity["episode_id"],
        "episode_pseudonym": state["episode_pseudonym"],
        "pair_id": identity["pair_id"],
        "campaign_id": identity["campaign_id"],
        "condition": identity["condition"],
        "model": identity["model"],
        "aou": state["aou"],
        "global_episode_id": census.get("global_episode_id"),
        "unit_id": census.get("unit_id"),
        "block_id": census.get("block_id"),
        "task_evidence_endpoint": task_endpoint,
        "report_closure": report_closure,
        "registered_claim_trace_status": claim_trace_status,
        "semantic_match": semantic_match,
        "verification_resolution_state": verification_state,
        "verification_resolution_state_reason": verification_reason,
        "dimensions": state["dimensions"],
        "source_packet_ids": state["source_packet_ids"],
        "source_episode_state": {
            "episode_pseudonym": state["episode_pseudonym"],
        },
        "complete_pair_eligible": True,
        "private_identity_joined": True,
        "must_never_be_sent_to_judge": True,
    }


def run(
    *,
    episode_states_path: Path,
    packet_lineage_path: Path,
    pair_census_path: Path,
    episode_census_path: Path,
    output_dir: Path,
    lock_path: Path,
    expected_pairs: int,
    expected_episodes: int,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
    semantic_matches_path: Path | None = None,
) -> dict[str, Any]:
    inputs = [
        episode_states_path,
        packet_lineage_path,
        pair_census_path,
        episode_census_path,
        lock_path,
    ]
    if semantic_matches_path is not None:
        inputs.append(semantic_matches_path)
    require_real_data_authorization(inputs, allow_real_data)
    lock = read_json_or_yaml(lock_path)
    if lock.get("freeze_status") != "frozen":
        raise GateError("Stage 12 requires freeze_status=frozen")
    if lock.get("offline_result_construction_authorized") is not True:
        raise GateError("Stage 12 requires offline_result_construction_authorized=true")
    if lock.get("judge_calls_authorized") is not False:
        raise GateError("Stage 12 requires judge_calls_authorized=false")

    states = load_jsonl(episode_states_path)
    lineage_rows = load_jsonl(packet_lineage_path)
    eligible_pair_rows = [
        row
        for row in _read_csv(pair_census_path)
        if _csv_true(row.get("execution_valid_pair"))
        and _csv_true(row.get("pair_complete"))
    ]
    eligible_episode_rows = [
        row
        for row in _read_csv(episode_census_path)
        if _csv_true(row.get("paired_analysis_eligible"))
    ]
    semantic_by_episode: dict[str, dict[str, Any]] = {}
    if semantic_matches_path is not None:
        semantic_rows = load_jsonl(semantic_matches_path)
        for row in semantic_rows:
            episode_pseudonym = str(row.get("episode_pseudonym") or "")
            final = row.get("final")
            if (
                not episode_pseudonym
                or episode_pseudonym in semantic_by_episode
                or not isinstance(final, dict)
            ):
                raise GateError("Stage 12 invalid or duplicate semantic match row")
            semantic_by_episode[episode_pseudonym] = final
    if dry_run:
        return {
            "schema_version": "atobench.pair_profile_plan.v1",
            "status": "dry_run",
            "episode_state_count": len(states),
            "private_lineage_count": len(lineage_rows),
            "eligible_pair_count": len(eligible_pair_rows),
            "eligible_episode_count": len(eligible_episode_rows),
            "model_calls_made": 0,
        }
    ensure_output_available(output_dir, new_version)

    if len(states) != expected_episodes:
        raise GateError(
            f"Stage 12 found {len(states)} episode states, expected {expected_episodes}"
        )
    if len(eligible_pair_rows) != expected_pairs:
        raise GateError(
            f"Stage 12 found {len(eligible_pair_rows)} eligible pairs, "
            f"expected {expected_pairs}"
        )
    if len(eligible_episode_rows) != expected_episodes:
        raise GateError(
            f"Stage 12 found {len(eligible_episode_rows)} eligible episodes, "
            f"expected {expected_episodes}"
        )

    lineage_by_packet = _unique_by(
        lineage_rows,
        "packet_id",
        label="packet lineage",
    )
    pair_census = _unique_by(eligible_pair_rows, "pair_id", label="pair census")
    episode_census = _unique_by(
        eligible_episode_rows,
        "episode_id",
        label="episode census",
    )
    if len(lineage_by_packet) != expected_episodes * len(DIMENSIONS):
        raise GateError(
            f"Stage 12 found {len(lineage_by_packet)} packet lineages, "
            f"expected {expected_episodes * len(DIMENSIONS)}"
        )

    unblinded_episodes: list[dict[str, Any]] = []
    by_pair: dict[str, dict[str, dict[str, Any]]] = collections.defaultdict(dict)
    seen_episode_ids: set[str] = set()
    for state in states:
        if state.get("schema_version") != "atobench.episode_state.v1":
            raise GateError(
                f"Stage 12 unexpected episode state schema: "
                f"{state.get('schema_version')!r}"
            )
        if state.get("complete") is not True or state.get("identity_blinded") is not True:
            raise GateError(
                f"Stage 12 requires complete blinded state: "
                f"{state.get('episode_pseudonym')}"
            )
        identity = _lineage_identity(state, lineage_by_packet)
        if identity["condition"] not in {"C0", "C1"}:
            raise GateError(
                f"Stage 12 invalid condition for {identity['episode_id']}: "
                f"{identity['condition']}"
            )
        if identity["episode_id"] in seen_episode_ids:
            raise GateError(f"Stage 12 duplicate episode identity: {identity['episode_id']}")
        seen_episode_ids.add(identity["episode_id"])
        census = episode_census.get(identity["episode_id"])
        if census is None:
            raise GateError(
                f"Stage 12 episode not in frozen eligible census: "
                f"{identity['episode_id']}"
            )
        expected_identity = {
            "pair_id": census.get("pair_id"),
            "campaign_id": census.get("campaign_id"),
            "condition": census.get("condition"),
            "model": census.get("model"),
        }
        for field, expected in expected_identity.items():
            if identity[field] != expected:
                raise GateError(
                    f"Stage 12 {field} mismatch for {identity['episode_id']}: "
                    f"{identity[field]!r} != {expected!r}"
                )
        if state.get("aou") != census.get("aou"):
            raise GateError(
                f"Stage 12 AOU mismatch for {identity['episode_id']}: "
                f"{state.get('aou')!r} != {census.get('aou')!r}"
            )
        semantic_match = semantic_by_episode.get(str(state["episode_pseudonym"]))
        if semantic_matches_path is not None and semantic_match is None:
            raise GateError(
                f"Stage 12 missing semantic match for {state['episode_pseudonym']}"
            )
        projection = _episode_projection(state, identity, census, semantic_match)
        unblinded_episodes.append(projection)
        conditions = by_pair[identity["pair_id"]]
        if identity["condition"] in conditions:
            raise GateError(
                f"Stage 12 duplicate {identity['condition']} for pair "
                f"{identity['pair_id']}"
            )
        conditions[identity["condition"]] = projection

    if seen_episode_ids != set(episode_census):
        missing = sorted(set(episode_census) - seen_episode_ids)
        extra = sorted(seen_episode_ids - set(episode_census))
        raise GateError(
            f"Stage 12 episode census mismatch: missing={missing[:3]}, extra={extra[:3]}"
        )
    if set(by_pair) != set(pair_census):
        missing = sorted(set(pair_census) - set(by_pair))
        extra = sorted(set(by_pair) - set(pair_census))
        raise GateError(
            f"Stage 12 pair census mismatch: missing={missing[:3]}, extra={extra[:3]}"
        )

    pair_profiles: list[dict[str, Any]] = []
    score_transitions: list[dict[str, Any]] = []
    state_transitions: list[dict[str, Any]] = []
    descriptor_transitions: list[dict[str, Any]] = []
    for pair_id in sorted(pair_census):
        conditions = by_pair[pair_id]
        if set(conditions) != {"C0", "C1"}:
            raise GateError(
                f"Stage 12 pair {pair_id} conditions are {sorted(conditions)}"
            )
        c0 = conditions["C0"]
        c1 = conditions["C1"]
        census = pair_census[pair_id]
        for field in ("campaign_id", "model", "aou", "unit_id", "block_id"):
            if str(c0.get(field) or "") != str(c1.get(field) or ""):
                raise GateError(f"Stage 12 pair {pair_id} differs on {field}")
            if str(c0.get(field) or "") != str(census.get(field) or ""):
                raise GateError(f"Stage 12 pair census mismatch on {field}: {pair_id}")

        pair_scores = {
            dimension: _dimension_transition(pair_id, dimension, c0, c1)
            for dimension in sorted(DIMENSIONS)
        }
        score_transitions.extend(pair_scores.values())
        c0_descriptor = c0["dimensions"]["stop_decision"]["descriptor"][
            "derived_descriptors"
        ]
        c1_descriptor = c1["dimensions"]["stop_decision"]["descriptor"][
            "derived_descriptors"
        ]
        descriptor_transition = {
            "schema_version": "atobench.pair_stop_descriptor_transition.v1",
            "pair_id": pair_id,
            "c0": c0_descriptor,
            "c1": c1_descriptor,
            "readiness_transition": {
                "from": c0_descriptor["readiness_state"],
                "to": c1_descriptor["readiness_state"],
            },
            "stop_fit_transition": {
                "from": c0_descriptor["stop_fit"],
                "to": c1_descriptor["stop_fit"],
            },
            "descriptor_primary": True,
        }
        descriptor_transitions.append(descriptor_transition)
        transition_ready = (
            c0["verification_resolution_state"] != "state_unavailable"
            and c1["verification_resolution_state"] != "state_unavailable"
        )
        unavailable_sides = [
            f"{condition}:{episode['verification_resolution_state_reason']}"
            for condition, episode in (("C0", c0), ("C1", c1))
            if episode["verification_resolution_state"] == "state_unavailable"
        ]
        state_transition = {
            "schema_version": "atobench.pair_verification_state_transition.v1",
            "pair_id": pair_id,
            "c0_state": c0["verification_resolution_state"],
            "c1_state": c1["verification_resolution_state"],
            "transition_ready": transition_ready,
            "reason": (
                "complete_semantic_and_task_endpoint_measurement"
                if transition_ready
                else ";".join(unavailable_sides)
            ),
            "must_not_be_interpreted_as_unresolved_verification": (
                not transition_ready
            ),
        }
        state_transitions.append(state_transition)
        pair_profiles.append(
            {
                "schema_version": "atobench.pair_resilience_profile.v1",
                "pair_id": pair_id,
                "campaign_id": c0["campaign_id"],
                "model": c0["model"],
                "aou": c0["aou"],
                "unit_id": c0["unit_id"],
                "block_id": c0["block_id"],
                "episodes": {
                    "C0": {
                        "episode_id": c0["episode_id"],
                        "episode_pseudonym": c0["episode_pseudonym"],
                        "task_evidence_endpoint": c0["task_evidence_endpoint"],
                        "verification_resolution_state": c0[
                            "verification_resolution_state"
                        ],
                    },
                    "C1": {
                        "episode_id": c1["episode_id"],
                        "episode_pseudonym": c1["episode_pseudonym"],
                        "task_evidence_endpoint": c1["task_evidence_endpoint"],
                        "verification_resolution_state": c1[
                            "verification_resolution_state"
                        ],
                    },
                },
                "diagnostic_score_transitions": pair_scores,
                "stop_descriptor_transition": descriptor_transition,
                "verification_state_transition": state_transition,
                "pairing_basis": "frozen_pair_census_exact_join",
                "rematching_performed": False,
                "model_calls_made": 0,
                "statistical_analysis_performed": False,
                "paper_facing_interpretation_performed": False,
                "must_never_be_sent_to_judge": True,
            }
        )

    unblinded_path = output_dir / "unblinded_episode_states.jsonl"
    profiles_path = output_dir / "pair_resilience_profiles.jsonl"
    score_path = output_dir / "pair_score_transitions.jsonl"
    state_path = output_dir / "pair_state_transitions.jsonl"
    descriptor_path = output_dir / "pair_stop_descriptor_transitions.jsonl"
    summary_path = output_dir / "pair_profile_summary.json"
    write_jsonl(
        unblinded_path,
        sorted(unblinded_episodes, key=lambda row: row["episode_id"]),
    )
    write_jsonl(profiles_path, pair_profiles)
    write_jsonl(score_path, score_transitions)
    write_jsonl(state_path, state_transitions)
    write_jsonl(descriptor_path, descriptor_transitions)
    unavailable_reason_counts = collections.Counter(
        row["verification_resolution_state_reason"]
        for row in unblinded_episodes
        if row["verification_resolution_state"] == "state_unavailable"
    )
    if not unavailable_reason_counts:
        primary_state_blocker = None
    elif len(unavailable_reason_counts) == 1:
        primary_state_blocker = next(iter(unavailable_reason_counts))
    else:
        primary_state_blocker = "multiple_unavailability_causes"
    summary = {
        "schema_version": "atobench.pair_profile_summary.v1",
        "status": "PASS",
        "created_at": utc_now(),
        "episode_count": len(unblinded_episodes),
        "pair_count": len(pair_profiles),
        "score_transition_count": len(score_transitions),
        "verification_state_transition_count": len(state_transitions),
        "stop_descriptor_transition_count": len(descriptor_transitions),
        "aou_pair_counts": dict(
            sorted(collections.Counter(row["aou"] for row in pair_profiles).items())
        ),
        "model_pair_counts": dict(
            sorted(collections.Counter(row["model"] for row in pair_profiles).items())
        ),
        "pairing_basis": "frozen_pair_census_exact_join",
        "rematching_performed": False,
        "private_identity_joined": True,
        "primary_verification_state_transitions_ready": all(
            row["transition_ready"] for row in state_transitions
        ),
        "primary_verification_state_blocker": (
            primary_state_blocker
        ),
        "state_unavailable_reason_counts": dict(
            sorted(unavailable_reason_counts.items())
        ),
        "state_unavailable_episode_count": sum(
            row["verification_resolution_state"] == "state_unavailable"
            for row in unblinded_episodes
        ),
        "model_calls_made": 0,
        "network_accessed": False,
        "statistical_analysis_performed": False,
        "paper_facing_analysis_performed": False,
    }
    write_json(summary_path, summary)
    outputs = [
        unblinded_path,
        profiles_path,
        score_path,
        state_path,
        descriptor_path,
        summary_path,
    ]
    manifest = stage_manifest(
        "12_build_pair_profiles",
        inputs,
        outputs,
        "complete",
        dry_run=False,
        details=summary,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Join private lineage and build exact frozen-census pair profiles"
    )
    parser.add_argument("--episode-states", type=Path, required=True)
    parser.add_argument("--packet-lineage", type=Path, required=True)
    parser.add_argument("--pair-census", type=Path, required=True)
    parser.add_argument("--episode-census", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--semantic-matches", type=Path)
    parser.add_argument("--expected-pairs", type=int, default=215)
    parser.add_argument("--expected-episodes", type=int, default=430)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(
            episode_states_path=args.episode_states,
            packet_lineage_path=args.packet_lineage,
            pair_census_path=args.pair_census,
            episode_census_path=args.episode_census,
            output_dir=args.output,
            lock_path=args.lock,
            semantic_matches_path=args.semantic_matches,
            expected_pairs=args.expected_pairs,
            expected_episodes=args.expected_episodes,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
