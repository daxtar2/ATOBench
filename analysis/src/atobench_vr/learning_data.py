from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .common import (
    GateError,
    load_jsonl,
    require_real_data_authorization,
    sha256_file,
    sha256_json,
    write_json,
    write_jsonl,
)
from .redaction import residual_sensitive_kinds

SCHEMA_VERSION = "atobench.learning_record.v1"
DATASET_VERSION = "atobench.learning_dataset.v1"
AUDIT_VERSION = "atobench.learning_dataset_audit.v1"

NON_GROUNDED_OBSERVED_STATES = {
    "unresolved_verification",
    "unreported_verification",
    "unsupported_closure",
}
LOWER_STOP_STATES = {
    "ready_not_supported",
    "not_ready",
    "internally_conflicted",
}


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{sha256_json(value)[:20]}"


def _group_key(
    pair: dict[str, Any], target_id: str, target_snapshot_id: str
) -> str:
    return "::".join(
        [
            target_id,
            target_snapshot_id,
            str(pair["aou"]),
            str(pair["unit_id"]),
            str(pair["block_id"]),
        ]
    )


def _split_assignments(
    pairs: list[dict[str, Any]], target_id: str, target_snapshot_id: str
) -> dict[str, str]:
    """Assign complete target/AOU/unit/block groups to deterministic splits."""

    groups_by_aou: dict[str, set[str]] = defaultdict(set)
    for pair in pairs:
        groups_by_aou[str(pair["aou"])].add(
            _group_key(pair, target_id, target_snapshot_id)
        )
    assignments: dict[str, str] = {}
    for aou, groups in sorted(groups_by_aou.items()):
        ordered = sorted(groups, key=lambda value: sha256_json(["split-v1", value]))
        count = len(ordered)
        train_count = int(count * 0.8)
        validation_count = int(count * 0.1)
        if count >= 3 and validation_count == 0:
            validation_count = 1
        if count >= 2 and train_count == count:
            train_count -= 1
        for index, group in enumerate(ordered):
            if index < train_count:
                split = "train"
            elif index < train_count + validation_count:
                split = "validation"
            else:
                split = "test"
            assignments[group] = split
    return assignments


def _split_object(group: str, split: str) -> dict[str, Any]:
    return {
        "name": split,
        "group_id": _stable_id("sg", group),
        "group_basis": "target_snapshot+aou+unit_id+block_id",
        "scope": "within_target_block_holdout",
        "cross_target_generalization": False,
    }


def _label_tier(fact: dict[str, Any]) -> str:
    source = fact.get("source_kind")
    confidence = fact.get("confidence")
    if source == "deterministic_http" and confidence == "exact":
        return "environment_verified"
    if source == "deterministic_http":
        return "environment_derived_uncertain"
    if source == "recorded_rationale" and confidence == "exact":
        return "explicit_agent_text"
    if source == "recorded_rationale":
        return "explicit_agent_text_uncertain"
    return "unknown"


def _fact_training_status(fact: dict[str, Any]) -> dict[str, Any]:
    tier = _label_tier(fact)
    status = fact.get("measurement_status")
    eligible = tier == "environment_verified" and status in {"positive", "negative"}
    return {
        "readiness": (
            "verifiable_process_label_candidate"
            if eligible
            else "analysis_only_not_reward_ready"
        ),
        "process_supervision_candidate": eligible,
        "numeric_reward_assigned": False,
        "reward_mapping": "aou_specific_mapping_required",
        "trajectory_content_included": False,
    }


def _dimension_summary(value: dict[str, Any]) -> dict[str, Any]:
    final = value.get("final") or {}
    result: dict[str, Any] = {
        "measurement_role": value.get("measurement_role"),
        "numeric_interpretation": value.get("numeric_interpretation"),
        "final": {
            "score": final.get("score"),
            "score_low": final.get("score_low"),
            "score_high": final.get("score_high"),
            "band": final.get("band"),
            "insufficient_evidence": final.get("insufficient_evidence"),
            "method": final.get("method"),
        },
        "adjudication_triggered": value.get("adjudication_triggered"),
        "reviewer_score_difference": value.get("reviewer_score_difference"),
        "runner_warning_count": value.get("runner_warning_count"),
        "source_bundle_sha256": (value.get("source_bundle") or {}).get("sha256"),
    }
    descriptor = value.get("descriptor")
    if descriptor:
        result["descriptor"] = {
            "derived_descriptors": descriptor.get("derived_descriptors") or {},
            "primary_evidence": descriptor.get("primary_evidence") or {},
            "verification_status_counts": descriptor.get(
                "verification_status_counts"
            )
            or {},
            "supporting_fact_ids": descriptor.get("supporting_fact_ids") or [],
        }
    return result


def _common_identity(
    episode: dict[str, Any], pair: dict[str, Any]
) -> dict[str, Any]:
    return {
        "episode_id": episode["episode_id"],
        "episode_pseudonym": episode.get("episode_pseudonym"),
        "pair_id": episode["pair_id"],
        "campaign_id": episode.get("campaign_id"),
        "condition": episode["condition"],
        "model": episode.get("model"),
        "aou": episode["aou"],
        "unit_id": pair["unit_id"],
        "block_id": pair["block_id"],
    }


def _episode_record(
    episode: dict[str, Any],
    pair: dict[str, Any],
    episode_facts: list[dict[str, Any]],
    *,
    target_id: str,
    target_snapshot_id: str,
    split: dict[str, Any],
) -> dict[str, Any]:
    tier_counts = Counter(_label_tier(fact) for fact in episode_facts)
    status_counts = Counter(
        str(fact.get("measurement_status")) for fact in episode_facts
    )
    class_counts = Counter(str(fact.get("fact_class")) for fact in episode_facts)
    high_importance = [
        fact["fact_id"]
        for fact in episode_facts
        if fact.get("importance") == "high"
    ]
    task_evidence = episode.get("task_evidence_endpoint") or {}
    stop_descriptor = (
        episode.get("dimensions", {})
        .get("stop_decision", {})
        .get("descriptor", {})
        .get("derived_descriptors", {})
    )
    semantic = episode.get("semantic_match") or {}
    base = {
        "schema_version": SCHEMA_VERSION,
        "view": "episode_summary",
        "identity": _common_identity(episode, pair),
        "environment": {
            "target_id": target_id,
            "target_snapshot_id": target_snapshot_id,
            "target_snapshot_is_cryptographic": False,
        },
        "split": split,
        "condition": {
            "name": episode["condition"],
            "intervention_condition": episode["condition"] == "C1",
            "intervention_contact": next(
                (
                    {
                        "measurement_status": fact.get("measurement_status"),
                        "value": fact.get("value"),
                        "fact_id": fact.get("fact_id"),
                    }
                    for fact in episode_facts
                    if fact.get("fact_type") == "COMMON_TARGET_CONTACT"
                ),
                {
                    "measurement_status": "unavailable",
                    "value": None,
                    "fact_id": None,
                },
            ),
        },
        "outcomes": {
            "verification_resolution_state": episode.get(
                "verification_resolution_state"
            ),
            "verification_resolution_reason": episode.get(
                "verification_resolution_state_reason"
            ),
            "task_evidence_endpoint": task_evidence,
            "report_closure": episode.get("report_closure") or {},
            "registered_claim_trace_status": episode.get(
                "registered_claim_trace_status"
            )
            or {},
            "stop_readiness": stop_descriptor,
        },
        "measurements": {
            name: _dimension_summary(value)
            for name, value in sorted((episode.get("dimensions") or {}).items())
        },
        "process_labels": {
            "fact_count": len(episode_facts),
            "fact_class_counts": dict(sorted(class_counts.items())),
            "measurement_status_counts": dict(sorted(status_counts.items())),
            "provenance_tier_counts": dict(sorted(tier_counts.items())),
            "high_importance_fact_ids": high_importance,
        },
        "semantic_label": {
            "matcher_id": semantic.get("matcher_id"),
            "confidence": semantic.get("confidence"),
            "reason_code": semantic.get("reason_code"),
            "insufficient_evidence": semantic.get("insufficient_evidence"),
            "supporting_fact_ids": semantic.get("supporting_fact_ids") or [],
            "contradicting_fact_ids": semantic.get("contradicting_fact_ids")
            or [],
            "label_provenance": "isolated_model_matcher_consensus_or_adjudication",
        },
        "provenance": {
            "fact_registry_record_ids": [fact["fact_id"] for fact in episode_facts],
            "source_packet_ids": episode.get("source_packet_ids") or {},
            "raw_paths_removed": True,
            "hidden_chain_of_thought_included": False,
        },
        "training": {
            "readiness": "episode_level_learning_candidate",
            "trajectory_sft_ready": False,
            "preference_ready": False,
            "offline_transition_ready": False,
            "process_reward_ready": False,
            "reason": (
                "The lightweight cohort contains audited labels but not the "
                "action-observation trajectory required for direct training."
            ),
        },
    }
    base["record_id"] = _stable_id(
        "lr_ep", [episode["episode_id"], episode["condition"], "episode_summary"]
    )
    return base


def _process_record(
    fact: dict[str, Any],
    episode: dict[str, Any],
    pair: dict[str, Any],
    *,
    target_id: str,
    target_snapshot_id: str,
    split: dict[str, Any],
) -> dict[str, Any]:
    base = {
        "schema_version": SCHEMA_VERSION,
        "view": "process_label",
        "identity": {
            **_common_identity(episode, pair),
            "fact_id": fact["fact_id"],
        },
        "environment": {
            "target_id": target_id,
            "target_snapshot_id": target_snapshot_id,
            "target_snapshot_is_cryptographic": False,
        },
        "split": split,
        "label": {
            "fact_class": fact.get("fact_class"),
            "fact_type": fact.get("fact_type"),
            "measurement_status": fact.get("measurement_status"),
            "value": fact.get("value"),
            "importance": fact.get("importance"),
            "confidence": fact.get("confidence"),
            "provenance_tier": _label_tier(fact),
        },
        "provenance": {
            "source_kind": fact.get("source_kind"),
            "source_pointers": fact.get("source_pointers") or [],
            "predicate_or_prompt_version": fact.get(
                "predicate_or_prompt_version"
            ),
            "raw_paths_removed": True,
            "hidden_chain_of_thought_included": False,
        },
        "training": _fact_training_status(fact),
    }
    base["record_id"] = _stable_id(
        "lr_pl", [episode["episode_id"], fact["fact_id"], "process_label"]
    )
    return base


def _preference_candidate(
    c0: dict[str, Any], c1: dict[str, Any]
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    c0_state = c0.get("verification_resolution_state")
    c1_state = c1.get("verification_resolution_state")
    if c0_state == "grounded_verification" and c1_state in NON_GROUNDED_OBSERVED_STATES:
        preferred = "C0"
    elif c1_state == "grounded_verification" and c0_state in NON_GROUNDED_OBSERVED_STATES:
        preferred = "C1"
    else:
        preferred = None
    if preferred:
        candidates.append(
            {
                "axis": "verification_resolution",
                "preferred_condition": preferred,
                "label_status": "derived_candidate_not_training_ready",
                "basis": "registered_finding_evidence_closure_contract",
                "requires_trajectory_join": True,
            }
        )

    c0_stop = (
        c0.get("dimensions", {})
        .get("stop_decision", {})
        .get("descriptor", {})
        .get("derived_descriptors", {})
        .get("readiness_state")
    )
    c1_stop = (
        c1.get("dimensions", {})
        .get("stop_decision", {})
        .get("descriptor", {})
        .get("derived_descriptors", {})
        .get("readiness_state")
    )
    if c0_stop == "ready_supported" and c1_stop in LOWER_STOP_STATES:
        stop_preferred = "C0"
    elif c1_stop == "ready_supported" and c0_stop in LOWER_STOP_STATES:
        stop_preferred = "C1"
    else:
        stop_preferred = None
    if stop_preferred:
        candidates.append(
            {
                "axis": "stop_readiness",
                "preferred_condition": stop_preferred,
                "label_status": "derived_candidate_not_training_ready",
                "basis": "descriptor_primary_stop_contract",
                "requires_trajectory_join": True,
            }
        )
    return candidates


def _pair_record(
    pair: dict[str, Any],
    c0: dict[str, Any],
    c1: dict[str, Any],
    *,
    target_id: str,
    target_snapshot_id: str,
    split: dict[str, Any],
) -> dict[str, Any]:
    candidates = _preference_candidate(c0, c1)
    base = {
        "schema_version": SCHEMA_VERSION,
        "view": "counterfactual_pair",
        "identity": {
            "pair_id": pair["pair_id"],
            "campaign_id": pair.get("campaign_id"),
            "model": pair.get("model"),
            "aou": pair["aou"],
            "unit_id": pair["unit_id"],
            "block_id": pair["block_id"],
            "c0_episode_id": c0["episode_id"],
            "c1_episode_id": c1["episode_id"],
        },
        "environment": {
            "target_id": target_id,
            "target_snapshot_id": target_snapshot_id,
            "target_snapshot_is_cryptographic": False,
        },
        "split": split,
        "pairing": {
            "basis": pair.get("pairing_basis"),
            "rematching_performed": pair.get("rematching_performed"),
            "native_condition": "C0",
            "intervention_condition": "C1",
        },
        "transitions": {
            "verification_resolution": pair.get("verification_state_transition")
            or {},
            "stop_descriptor": pair.get("stop_descriptor_transition") or {},
            "diagnostic_scores": pair.get("diagnostic_score_transitions") or {},
        },
        "preference_candidates": candidates,
        "provenance": {
            "raw_paths_removed": True,
            "hidden_chain_of_thought_included": False,
            "counterfactual_strength": "frozen_matched_pair_not_causal_identity",
        },
        "training": {
            "readiness": (
                "preference_candidate_requires_trajectory_join"
                if candidates
                else "counterfactual_analysis_only"
            ),
            "preference_ready": False,
            "direct_dpo_ready": False,
            "condition_confounded": True,
            "numeric_reward_assigned": False,
            "judge_scores_used_as_preference": False,
        },
    }
    base["record_id"] = _stable_id("lr_pair", [pair["pair_id"], "counterfactual_pair"])
    return base


def _contains_absolute_path(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("/")
    if isinstance(value, dict):
        return any(_contains_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_absolute_path(item) for item in value)
    return False


def _validate_record(record: dict[str, Any]) -> list[str]:
    errors = []
    for key in (
        "schema_version",
        "record_id",
        "view",
        "identity",
        "environment",
        "split",
        "provenance",
        "training",
    ):
        if key not in record:
            errors.append(f"{record.get('record_id', '<unknown>')}: missing {key}")
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{record.get('record_id')}: schema version mismatch")
    if _contains_absolute_path(record):
        errors.append(f"{record.get('record_id')}: absolute path leaked")
    return errors


def _dataset_card(
    *,
    episode_count: int,
    pair_count: int,
    process_count: int,
    verifiable_process_count: int,
    preference_candidate_count: int,
    split_counts: Counter[str],
    target_id: str,
    target_snapshot_id: str,
) -> str:
    return f"""# ATOBench Learning Data v1

Status: `CANDIDATE_EXPORT_NOT_RL_READY`

## Scope

- Target: `{target_id}`
- Target snapshot label: `{target_snapshot_id}` (not a cryptographic digest)
- Episodes: {episode_count}
- Native/ATO pairs: {pair_count}
- Process-label records: {process_count}
- Environment-verified process candidates: {verifiable_process_count}
- Counterfactual pairs with at least one preference candidate: {preference_candidate_count}
- Split counts by episode: {dict(sorted(split_counts.items()))}

## Views

- `episode_summary`: audited evidence, stop, semantic and outcome state.
- `process_label`: one typed fact with evidence tier and source pointers.
- `counterfactual_pair`: frozen C0/C1 transitions and conservative preference candidates.

## Intended uses

- Dataset research and quality auditing.
- AOU-specific process-reward design.
- Preference-data construction after action-observation trajectory join.
- Defender intervention analysis.

## Not ready for

- Direct SFT: the lightweight cohort does not contain action-observation text.
- Direct offline RL: no step-level next-state transitions are included.
- Direct DPO/RM from C0 versus C1: the paired conditions contain different
  observations, so they are counterfactual analysis pairs rather than
  same-input preference pairs.
- Automatic scalar rewards: fact polarity is AOU-specific.
- Cross-target generalization claims: this snapshot contains one target.
- Public release or production training without a separate data and safety review.

## Label provenance

`environment_verified` labels come from exact deterministic target-side HTTP
predicates. Explicit agent text is stored under separate provenance tiers.
Judge and semantic-matcher results remain model-derived measurements and are
never represented as environment truth. Hidden chain-of-thought is not included.

## Split policy

All models sharing the same target snapshot, AOU, unit and block remain in one
split. This prevents same-block model variants from crossing train/validation/test,
but it is only a within-target holdout. A future multi-target release must add a
target-level held-out split.
"""


def _quality_summary(
    episode_records: list[dict[str, Any]],
    process_records: list[dict[str, Any]],
    pair_records: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_process = [
        row
        for row in process_records
        if row["training"]["process_supervision_candidate"]
    ]
    preference_candidates = [
        candidate
        for row in pair_records
        for candidate in row["preference_candidates"]
    ]
    groups = {
        row["split"]["group_id"]: row["split"]["name"] for row in episode_records
    }
    return {
        "schema_version": "atobench.learning_dataset_quality.v1",
        "status": "PASS_CANDIDATE_QUALITY_AUDIT",
        "episode_distribution": {
            "by_aou": dict(
                sorted(Counter(row["identity"]["aou"] for row in episode_records).items())
            ),
            "by_condition": dict(
                sorted(
                    Counter(
                        row["identity"]["condition"] for row in episode_records
                    ).items()
                )
            ),
            "by_model": dict(
                sorted(
                    Counter(row["identity"]["model"] for row in episode_records).items()
                )
            ),
            "by_verification_state": dict(
                sorted(
                    Counter(
                        row["outcomes"]["verification_resolution_state"]
                        for row in episode_records
                    ).items()
                )
            ),
        },
        "process_label_distribution": {
            "by_provenance_tier": dict(
                sorted(
                    Counter(
                        row["label"]["provenance_tier"] for row in process_records
                    ).items()
                )
            ),
            "by_measurement_status": dict(
                sorted(
                    Counter(
                        row["label"]["measurement_status"] for row in process_records
                    ).items()
                )
            ),
            "verifiable_candidates_by_aou": dict(
                sorted(
                    Counter(
                        row["identity"]["aou"] for row in candidate_process
                    ).items()
                )
            ),
            "verifiable_candidates_by_fact_type": dict(
                sorted(
                    Counter(
                        row["label"]["fact_type"] for row in candidate_process
                    ).items()
                )
            ),
        },
        "preference_candidate_distribution": {
            "pair_count": sum(
                bool(row["preference_candidates"]) for row in pair_records
            ),
            "candidate_count": len(preference_candidates),
            "by_axis": dict(
                sorted(Counter(row["axis"] for row in preference_candidates).items())
            ),
            "by_preferred_condition": dict(
                sorted(
                    Counter(
                        row["preferred_condition"]
                        for row in preference_candidates
                    ).items()
                )
            ),
        },
        "split_distribution": {
            "episode_counts": dict(
                sorted(
                    Counter(row["split"]["name"] for row in episode_records).items()
                )
            ),
            "pair_counts": dict(
                sorted(Counter(row["split"]["name"] for row in pair_records).items())
            ),
            "group_counts": dict(sorted(Counter(groups.values()).items())),
        },
        "known_gaps": [
            "single_target_only",
            "target_snapshot_label_is_not_cryptographic",
            "action_observation_content_not_joined",
            "no_generic_numeric_reward_mapping",
            "preference_candidates_not_human_audited",
            "native_ato_preference_direction_is_condition_confounded",
            "preference_candidate_direction_is_imbalanced",
            "no_downstream_training_validation",
        ],
        "next_quality_gates": [
            "join redacted tool-call action-observation transitions",
            "freeze AOU-specific reward maps",
            "human-audit preference candidates",
            "construct within-condition or matched-observation preferences",
            "add target-level held-out evaluation",
            "run bounded downstream learning experiment",
        ],
    }


def export_learning_data(
    *,
    pair_profiles_path: Path,
    episode_states_path: Path,
    fact_registry_path: Path,
    output_dir: Path,
    target_id: str,
    target_snapshot_id: str,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    inputs = [pair_profiles_path, episode_states_path, fact_registry_path]
    require_real_data_authorization(inputs, allow_real_data)
    plan = {
        "schema_version": DATASET_VERSION,
        "status": "DRY_RUN_READY",
        "target_id": target_id,
        "target_snapshot_id": target_snapshot_id,
        "input_paths": [str(path) for path in inputs],
        "model_calls_made": 0,
        "network_accessed": False,
    }
    if dry_run:
        return plan
    if (output_dir / "dataset_manifest.json").exists() and not new_version:
        raise GateError(
            f"refusing to overwrite completed learning-data export {output_dir}; "
            "use a new output directory"
        )
    if not target_id.strip() or not target_snapshot_id.strip():
        raise GateError("target-id and target-snapshot-id must be non-empty")

    pairs = load_jsonl(pair_profiles_path)
    episodes = load_jsonl(episode_states_path)
    facts = load_jsonl(fact_registry_path)
    pair_by_id = {row["pair_id"]: row for row in pairs}
    episode_by_id = {row["episode_id"]: row for row in episodes}
    if len(pair_by_id) != len(pairs):
        raise GateError("pair_id is not unique")
    if len(episode_by_id) != len(episodes):
        raise GateError("episode_id is not unique")

    facts_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        facts_by_episode[fact["episode_id"]].append(fact)
    if set(facts_by_episode) != set(episode_by_id):
        raise GateError("fact registry and episode-state coverage differ")

    split_by_group = _split_assignments(pairs, target_id, target_snapshot_id)
    split_by_pair: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        group = _group_key(pair, target_id, target_snapshot_id)
        split_by_pair[pair["pair_id"]] = _split_object(
            group, split_by_group[group]
        )

    episode_records = []
    process_records = []
    for episode in sorted(episodes, key=lambda row: row["episode_id"]):
        pair = pair_by_id.get(episode["pair_id"])
        if pair is None:
            raise GateError(f"episode references missing pair: {episode['episode_id']}")
        split = split_by_pair[pair["pair_id"]]
        selected_facts = sorted(
            facts_by_episode[episode["episode_id"]],
            key=lambda row: row["fact_id"],
        )
        episode_records.append(
            _episode_record(
                episode,
                pair,
                selected_facts,
                target_id=target_id,
                target_snapshot_id=target_snapshot_id,
                split=split,
            )
        )
        process_records.extend(
            _process_record(
                fact,
                episode,
                pair,
                target_id=target_id,
                target_snapshot_id=target_snapshot_id,
                split=split,
            )
            for fact in selected_facts
        )

    pair_records = []
    for pair in sorted(pairs, key=lambda row: row["pair_id"]):
        c0_id = pair["episodes"]["C0"]["episode_id"]
        c1_id = pair["episodes"]["C1"]["episode_id"]
        if c0_id not in episode_by_id or c1_id not in episode_by_id:
            raise GateError(f"pair references missing episode: {pair['pair_id']}")
        pair_records.append(
            _pair_record(
                pair,
                episode_by_id[c0_id],
                episode_by_id[c1_id],
                target_id=target_id,
                target_snapshot_id=target_snapshot_id,
                split=split_by_pair[pair["pair_id"]],
            )
        )

    all_records = [*episode_records, *process_records, *pair_records]
    errors = [
        error
        for record in all_records
        for error in _validate_record(record)
    ]
    sensitive_record_count = 0
    sensitive_kind_counts: Counter[str] = Counter()
    for record in all_records:
        kinds = residual_sensitive_kinds(
            json.dumps(record, sort_keys=True, ensure_ascii=False)
        )
        if kinds:
            sensitive_record_count += 1
            sensitive_kind_counts.update(kinds)
    if sensitive_record_count:
        errors.append(
            f"residual sensitive content in {sensitive_record_count} records"
        )
    record_ids = [record["record_id"] for record in all_records]
    if len(record_ids) != len(set(record_ids)):
        errors.append("record_id is not unique")
    group_splits: dict[str, set[str]] = defaultdict(set)
    for record in all_records:
        group_splits[record["split"]["group_id"]].add(record["split"]["name"])
    leaking_groups = [
        group for group, values in group_splits.items() if len(values) != 1
    ]
    if leaking_groups:
        errors.append(f"split group leakage: {len(leaking_groups)} groups")

    verifiable_process_count = sum(
        row["training"]["process_supervision_candidate"]
        for row in process_records
    )
    preference_candidate_count = sum(
        bool(row["preference_candidates"]) for row in pair_records
    )
    episode_split_counts = Counter(
        row["split"]["name"] for row in episode_records
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    episode_path = output_dir / "episode_records.jsonl"
    process_path = output_dir / "process_label_records.jsonl"
    pair_path = output_dir / "counterfactual_pair_records.jsonl"
    card_path = output_dir / "DATASET_CARD.md"
    quality_path = output_dir / "quality_summary.json"
    audit_path = output_dir / "export_audit.json"
    manifest_path = output_dir / "dataset_manifest.json"
    write_jsonl(episode_path, episode_records)
    write_jsonl(process_path, process_records)
    write_jsonl(pair_path, pair_records)
    card_path.write_text(
        _dataset_card(
            episode_count=len(episode_records),
            pair_count=len(pair_records),
            process_count=len(process_records),
            verifiable_process_count=verifiable_process_count,
            preference_candidate_count=preference_candidate_count,
            split_counts=episode_split_counts,
            target_id=target_id,
            target_snapshot_id=target_snapshot_id,
        ),
        encoding="utf-8",
    )
    write_json(
        quality_path,
        _quality_summary(episode_records, process_records, pair_records),
    )

    audit = {
        "schema_version": AUDIT_VERSION,
        "status": "PASS" if not errors else "FAIL",
        "violation_count": len(errors),
        "violations": errors,
        "counts": {
            "episode_records": len(episode_records),
            "process_label_records": len(process_records),
            "counterfactual_pair_records": len(pair_records),
            "verifiable_process_label_candidates": verifiable_process_count,
            "pairs_with_preference_candidates": preference_candidate_count,
            "all_records": len(all_records),
        },
        "episode_split_counts": dict(sorted(episode_split_counts.items())),
        "split_group_count": len(group_splits),
        "split_group_leakage_count": len(leaking_groups),
        "absolute_path_leakage_count": sum(
            _contains_absolute_path(record) for record in all_records
        ),
        "residual_sensitive_record_count": sensitive_record_count,
        "residual_sensitive_kind_counts": dict(
            sorted(sensitive_kind_counts.items())
        ),
        "hidden_chain_of_thought_included": False,
        "numeric_reward_assigned": False,
        "rl_ready": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    write_json(audit_path, audit)
    outputs = [
        episode_path,
        process_path,
        pair_path,
        card_path,
        quality_path,
        audit_path,
    ]
    manifest = {
        "schema_version": DATASET_VERSION,
        "status": "CANDIDATE_EXPORT_NOT_RL_READY" if not errors else "FAILED",
        "learning_record_schema_version": SCHEMA_VERSION,
        "target_id": target_id,
        "target_snapshot_id": target_snapshot_id,
        "source_inputs": {
            path.name: {"sha256": sha256_file(path)} for path in inputs
        },
        "outputs": {
            path.name: {"sha256": sha256_file(path), "bytes": path.stat().st_size}
            for path in outputs
        },
        "counts": audit["counts"],
        "episode_split_counts": audit["episode_split_counts"],
        "release_status": "internal_research_only_pending_dataset_review",
        "rl_ready": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    write_json(manifest_path, manifest)
    if errors:
        raise GateError(f"learning-data export audit failed: {errors[:5]}")
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair-profiles", required=True, type=Path)
    parser.add_argument("--episode-states", required=True, type=Path)
    parser.add_argument("--fact-registry", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--target-snapshot-id", required=True)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = export_learning_data(
            pair_profiles_path=args.pair_profiles,
            episode_states_path=args.episode_states,
            fact_registry_path=args.fact_registry,
            output_dir=args.output,
            target_id=args.target_id,
            target_snapshot_id=args.target_snapshot_id,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
