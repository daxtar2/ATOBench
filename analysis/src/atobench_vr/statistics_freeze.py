from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    read_json_or_yaml,
    require_real_data_authorization,
    stage_manifest,
    utc_now,
    write_json,
    write_jsonl,
)


def _unique(rows: list[dict[str, Any]], key: str, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = str(row.get(key) or "")
        if not value:
            raise GateError(f"statistics freeze {label} row has no {key}")
        if value in result:
            raise GateError(f"statistics freeze duplicate {label} {key}: {value}")
        result[value] = row
    return result


def _task_observed(episode: dict[str, Any]) -> bool:
    endpoint = episode.get("task_evidence_endpoint") or {}
    status = endpoint.get("measurement_status")
    value = endpoint.get("value")
    return (status == "positive" and value is True) or (
        status == "negative" and value is False
    )


def _contact_positive(fact: dict[str, Any]) -> bool:
    return (
        fact.get("measurement_status") == "positive"
        and fact.get("value") is True
    )


def _expected(lock: dict[str, Any], key: str) -> int:
    counts = lock.get("expected_structural_counts")
    if not isinstance(counts, dict) or not isinstance(counts.get(key), int):
        raise GateError(f"statistics lock has no integer expected count: {key}")
    return int(counts[key])


def _assert_count(lock: dict[str, Any], key: str, actual: int) -> None:
    expected = _expected(lock, key)
    if actual != expected:
        raise GateError(
            f"statistics freeze count mismatch for {key}: "
            f"expected {expected}, observed {actual}"
        )


def run(
    *,
    pair_profiles_path: Path,
    episode_states_path: Path,
    fact_registry_path: Path,
    output_dir: Path,
    lock_path: Path,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    require_real_data_authorization(
        [
            pair_profiles_path,
            episode_states_path,
            fact_registry_path,
            lock_path,
        ],
        allow_real_data,
    )
    lock = read_json_or_yaml(lock_path)
    if lock.get("freeze_status") != "frozen_authorized":
        raise GateError("statistics lock is not frozen_authorized")
    if lock.get("statistics_authorized") is not True:
        raise GateError("statistics execution is not authorized")
    if lock.get("paper_integration_authorized") is not False:
        raise GateError("statistics lock must keep paper integration disabled")
    if lock.get("hash_gate_enabled") is not False:
        raise GateError("statistics lock unexpectedly enables a hash gate")

    pairs = load_jsonl(pair_profiles_path)
    episodes = load_jsonl(episode_states_path)
    facts = load_jsonl(fact_registry_path)
    pair_by_id = _unique(pairs, "pair_id", "pair")
    episode_by_id = _unique(episodes, "episode_id", "episode")

    contact_by_episode: dict[str, dict[str, Any]] = {}
    for fact in facts:
        if fact.get("fact_type") != "COMMON_TARGET_CONTACT":
            continue
        episode_id = str(fact.get("episode_id") or "")
        if not episode_id or episode_id in contact_by_episode:
            raise GateError(
                f"statistics freeze duplicate or missing contact fact: {episode_id}"
            )
        contact_by_episode[episode_id] = fact
    if set(contact_by_episode) != set(episode_by_id):
        raise GateError("statistics freeze contact-fact episode coverage mismatch")

    memberships: list[dict[str, Any]] = []
    referenced_episodes: set[str] = set()
    for pair_id in sorted(pair_by_id):
        pair = pair_by_id[pair_id]
        pair_episodes = pair.get("episodes")
        if not isinstance(pair_episodes, dict) or set(pair_episodes) != {"C0", "C1"}:
            raise GateError(f"statistics freeze invalid episode map for {pair_id}")
        resolved: dict[str, dict[str, Any]] = {}
        for condition in ("C0", "C1"):
            episode_id = str(pair_episodes[condition].get("episode_id") or "")
            episode = episode_by_id.get(episode_id)
            if episode is None or episode.get("pair_id") != pair_id:
                raise GateError(
                    f"statistics freeze episode identity mismatch for {pair_id} {condition}"
                )
            if episode.get("condition") != condition:
                raise GateError(
                    f"statistics freeze condition mismatch for {pair_id} {condition}"
                )
            resolved[condition] = episode
            referenced_episodes.add(episode_id)

        c0 = resolved["C0"]
        c1 = resolved["C1"]
        c0_state_observed = c0.get("verification_resolution_state") != "state_unavailable"
        c1_state_observed = c1.get("verification_resolution_state") != "state_unavailable"
        if c0_state_observed != _task_observed(c0):
            raise GateError(f"statistics freeze C0 task/state availability mismatch: {pair_id}")
        if c1_state_observed != _task_observed(c1):
            raise GateError(f"statistics freeze C1 task/state availability mismatch: {pair_id}")

        c1_contact = contact_by_episode[c1["episode_id"]]
        c1_contact_positive = _contact_positive(c1_contact)
        c0_grounded = c0.get("verification_resolution_state") == "grounded_verification"
        capability_target = c0_grounded and c1_contact_positive
        capability_observed = capability_target and c1_state_observed
        memberships.append(
            {
                "schema_version": "atobench.statistics_population_membership.v1",
                "pair_id": pair_id,
                "aou": pair.get("aou"),
                "model": pair.get("model"),
                "c0_episode_id": c0["episode_id"],
                "c1_episode_id": c1["episode_id"],
                "c0_state": c0.get("verification_resolution_state"),
                "c1_state": c1.get("verification_resolution_state"),
                "c0_state_observed": c0_state_observed,
                "c1_state_observed": c1_state_observed,
                "complete_state_pair": c0_state_observed and c1_state_observed,
                "c0_grounded": c0_grounded,
                "c1_anchor_contact_status": c1_contact.get("measurement_status"),
                "c1_anchor_contact_value": c1_contact.get("value"),
                "c1_anchor_contact_positive": c1_contact_positive,
                "c0_capability_target": capability_target,
                "c0_capability_outcome_observed": capability_observed,
                "c0_capability_outcome_missing": (
                    capability_target and not c1_state_observed
                ),
                "effect_estimate_computed": False,
                "paper_facing_interpretation_performed": False,
            }
        )

    if referenced_episodes != set(episode_by_id):
        raise GateError("statistics freeze pair profiles do not cover every episode")

    count_map = {
        "pair_count": len(memberships),
        "episode_count": len(episode_by_id),
        "complete_state_pair_count": sum(
            row["complete_state_pair"] for row in memberships
        ),
        "incomplete_state_pair_count": sum(
            not row["complete_state_pair"] for row in memberships
        ),
        "c0_grounded_pair_count": sum(row["c0_grounded"] for row in memberships),
        "c1_anchor_contact_positive_pair_count": sum(
            row["c1_anchor_contact_positive"] for row in memberships
        ),
        "c1_anchor_contact_unavailable_pair_count": sum(
            not row["c1_anchor_contact_positive"] for row in memberships
        ),
        "capability_target_pair_count": sum(
            row["c0_capability_target"] for row in memberships
        ),
        "capability_outcome_observed_pair_count": sum(
            row["c0_capability_outcome_observed"] for row in memberships
        ),
        "capability_outcome_missing_pair_count": sum(
            row["c0_capability_outcome_missing"] for row in memberships
        ),
    }
    for key, actual in count_map.items():
        _assert_count(lock, key, actual)

    availability_patterns = collections.Counter(
        (
            "observed" if row["c0_state_observed"] else "unavailable",
            "observed" if row["c1_state_observed"] else "unavailable",
        )
        for row in memberships
    )
    registry = {
        "schema_version": "atobench.statistics_population_registry.v1",
        "freeze_status": "frozen",
        "created_at": utc_now(),
        "source_lock": str(lock_path.resolve()),
        "population_counts": count_map,
        "availability_pattern_counts": {
            f"C0_{c0}__C1_{c1}": count
            for (c0, c1), count in sorted(availability_patterns.items())
        },
        "analysis_populations": lock.get("analysis_populations"),
        "primary_estimands": lock.get("primary_estimands"),
        "diagnostic_score_estimands": lock.get("diagnostic_score_estimands"),
        "stratification": lock.get("stratification"),
        "uncertainty": lock.get("uncertainty"),
        "judge_sensitivity_definitions": lock.get(
            "judge_sensitivity_definitions"
        ),
        "prohibited_actions": lock.get("prohibited_actions"),
        "hash_gate_enabled": False,
        "statistics_performed": False,
        "effect_estimates_opened": False,
        "paper_facing_analysis_performed": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    summary = {
        "schema_version": "atobench.statistics_freeze_summary.v1",
        "status": "PASS_FROZEN",
        "created_at": utc_now(),
        **count_map,
        "availability_pattern_counts": registry["availability_pattern_counts"],
        "statistics_performed": False,
        "effect_estimates_opened": False,
        "paper_facing_analysis_performed": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    if dry_run:
        return summary

    ensure_output_available(output_dir, new_version)
    membership_path = output_dir / "pair_analysis_population_membership.jsonl"
    registry_path = output_dir / "analysis_population_registry.json"
    summary_path = output_dir / "statistics_freeze_summary.json"
    write_jsonl(membership_path, memberships)
    write_json(registry_path, registry)
    write_json(summary_path, summary)
    manifest = stage_manifest(
        "13a_freeze_statistics_populations",
        [
            pair_profiles_path,
            episode_states_path,
            fact_registry_path,
            lock_path,
        ],
        [membership_path, registry_path, summary_path],
        "complete",
        dry_run=False,
        details=summary,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair-profiles", type=Path, required=True)
    parser.add_argument("--episode-states", type=Path, required=True)
    parser.add_argument("--fact-registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(
            pair_profiles_path=args.pair_profiles,
            episode_states_path=args.episode_states,
            fact_registry_path=args.fact_registry,
            output_dir=args.output,
            lock_path=args.lock,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
