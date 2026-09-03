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
from .learning_transitions import _contains_absolute_path
from .redaction import residual_sensitive_kinds


SCHEMA_VERSION = "atobench.counterfactual_trajectory_object.v1"
DATASET_VERSION = "atobench.counterfactual_trajectory_dataset.v1"
AUDIT_VERSION = "atobench.counterfactual_trajectory_audit.v1"

OBJECT_FILES = {
    "canonical_trajectory": "canonical_trajectories.jsonl",
    "intervention_record": "intervention_records.jsonl",
    "step_behavior_record": "step_behavior_records.jsonl",
    "decision_failure_point": "decision_failure_points.jsonl",
    "evidence_claim_record": "evidence_claim_records.jsonl",
    "counterfactual_pair_record": "counterfactual_pair_records.jsonl",
}
VIEW_FILES = {
    "replay": "replay_view.jsonl",
    "diagnostic": "diagnostic_view.jsonl",
    "counterfactual": "counterfactual_view.jsonl",
}


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{sha256_json(value)[:20]}"


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    source = row.get("identity") or {}
    return {
        key: source.get(key)
        for key in (
            "episode_id",
            "episode_pseudonym",
            "pair_id",
            "campaign_id",
            "condition",
            "model",
            "aou",
            "unit_id",
            "block_id",
        )
        if source.get(key) is not None
    }


def _signature(transition: dict[str, Any]) -> str:
    action = transition.get("action") or {}
    return "|".join(
        str(action.get(key) or "∅")
        for key in (
            "tool",
            "method",
            "endpoint_family",
            "route_template",
            "payload_family",
        )
    )


def _action_label(action: dict[str, Any]) -> str:
    method = str(action.get("method") or "").strip()
    route = str(action.get("route_template") or "").strip()
    endpoint = str(action.get("endpoint_family") or "unknown")
    tool = str(action.get("tool") or "tool")
    if method or route:
        return " ".join(value for value in (method, route or endpoint) if value)
    return f"{tool} · {endpoint}"


def _origin(
    kind: str,
    *,
    source_record_ids: Iterable[str] = (),
    note: str | None = None,
) -> dict[str, Any]:
    result = {
        "label_origin": kind,
        "source_record_ids": [value for value in source_record_ids if value],
        "hidden_chain_of_thought_included": False,
    }
    if note:
        result["note"] = note
    return result


def _event_count(transition: dict[str, Any], name: str) -> int:
    return len(((transition.get(name) or {}).get("events") or []))


def _behavior_tags(
    transition: dict[str, Any],
    *,
    position: int,
    total: int,
    seen: Counter[str],
    previous: dict[str, Any] | None,
    contact_position: int | None,
) -> list[str]:
    signature = _signature(transition)
    tags: list[str] = []
    if position == 0:
        tags.append("initial_probe")
    if seen[signature]:
        tags.append("repetition")
    else:
        tags.append("new_action_signature")
    if previous is not None:
        previous_action = previous.get("action") or {}
        action = transition.get("action") or {}
        if action.get("endpoint_family") != previous_action.get("endpoint_family"):
            tags.append("endpoint_shift")
        if action.get("payload_family") != previous_action.get("payload_family"):
            tags.append("payload_shift")
        if action.get("tool") != previous_action.get("tool"):
            tags.append("tool_shift")
    if (transition.get("intervention") or {}).get("target_aou_contact") is True:
        tags.append("intervention_contact")
    elif contact_position is not None and position > contact_position:
        tags.append("post_intervention_action")
    if _event_count(transition, "evidence"):
        tags.append("evidence_update")
    if position == total - 1:
        tags.append("terminal_boundary")
    return tags


def _stage(position: int, contact_position: int | None, condition: str) -> str:
    if condition != "C1":
        return "native_control"
    if contact_position is None:
        return "intervention_not_observed"
    if position < contact_position:
        return "pre_intervention"
    if position == contact_position:
        return "intervention_contact"
    return "post_intervention"


def _decision_triggers(
    step: dict[str, Any],
    *,
    position: int,
    total: int,
) -> list[tuple[str, str, list[str]]]:
    tags = set(step["behavior"]["tags"])
    triggers: list[tuple[str, str, list[str]]] = []
    if position == 0:
        triggers.append(("initial_action", "observed_boundary", []))
    if "intervention_contact" in tags:
        triggers.append(("intervention_contact", "observed_boundary", []))
    if "evidence_update" in tags:
        triggers.append(("evidence_update", "observed_boundary", []))
    all_shifts = tags & {"endpoint_shift", "payload_shift", "tool_shift"}
    shifts = sorted(
        all_shifts
        if tags & {"post_intervention_action", "intervention_contact"}
        else all_shifts & {"payload_shift", "tool_shift"}
    )
    if shifts:
        triggers.append(("strategy_shift", "deterministic_candidate", shifts))
    if step["behavior"]["same_signature_seen_before"] == 2:
        triggers.append(
            (
                "repetition_threshold",
                "failure_signal_candidate",
                ["same_action_signature_repeated_without_internal_intent_access"],
            )
        )
    if position == total - 1:
        triggers.append(("terminal_boundary", "observed_boundary", []))
    return triggers


def _dimension_projection(episode: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for name, value in sorted(((episode.get("measurements") or {}).items())):
        final = (value or {}).get("final") or {}
        projected[name] = {
            "score": final.get("score"),
            "band": final.get("band"),
            "insufficient_evidence": final.get("insufficient_evidence"),
            "method": final.get("method"),
            "descriptor": (value or {}).get("descriptor") or {},
            "label_origin": "judge_derived",
        }
    return projected


def _fact_projection(fact: dict[str, Any]) -> dict[str, Any]:
    label = fact.get("label") or {}
    provenance = fact.get("provenance") or {}
    return {
        "fact_id": (fact.get("identity") or {}).get("fact_id"),
        "fact_class": label.get("fact_class"),
        "fact_type": label.get("fact_type"),
        "measurement_status": label.get("measurement_status"),
        "value": label.get("value"),
        "importance": label.get("importance"),
        "confidence": label.get("confidence"),
        "provenance_tier": label.get("provenance_tier"),
        "source_kind": provenance.get("source_kind"),
        "source_pointers": provenance.get("source_pointers") or [],
        "step_linkage": "episode_only_turn_unavailable",
    }


def _evidence_claim_record(
    episode: dict[str, Any],
    facts: list[dict[str, Any]],
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    identity = _identity(episode)
    outcomes = episode.get("outcomes") or {}
    semantic = episode.get("semantic_label") or {}
    task_evidence = outcomes.get("task_evidence_endpoint") or {}
    claim_trace = outcomes.get("registered_claim_trace_status") or {}
    closure = outcomes.get("report_closure") or {}
    resolution = outcomes.get("verification_resolution_state")
    failure_modes: list[str] = []
    if task_evidence.get("measurement_status") in {"unavailable", "unknown"}:
        failure_modes.append("task_evidence_unavailable")
    if claim_trace.get("value") not in {"supported", True}:
        failure_modes.append("registered_claim_not_supported")
    if closure.get("value") is not True:
        failure_modes.append("report_not_closed")
    if resolution != "grounded_verification":
        failure_modes.append("grounded_verification_not_reached")
    evidence_steps = [
        {
            "step_id": step["object_id"],
            "turn_idx": step["identity"]["turn_idx"],
            "events": step["evidence"]["events"],
        }
        for step in steps
        if step["evidence"]["event_count"]
    ]
    record = {
        "schema_version": SCHEMA_VERSION,
        "object_type": "evidence_claim_record",
        "identity": identity,
        "split": episode.get("split") or {},
        "evidence": {
            "task_endpoint": task_evidence,
            "step_event_count": sum(
                step["evidence"]["event_count"] for step in steps
            ),
            "event_steps": evidence_steps,
            "facts": [_fact_projection(fact) for fact in facts],
            "fact_count": len(facts),
            "turn_level_fact_linkage": "unavailable_in_safe_transition_v1",
        },
        "claim": {
            "registered_claim_trace_status": claim_trace,
            "report_closure": closure,
            "semantic_match": semantic,
            "verification_resolution_state": resolution,
            "verification_resolution_reason": outcomes.get(
                "verification_resolution_reason"
            ),
        },
        "propagation": {
            "stages": [
                {
                    "stage": "task_evidence",
                    "status": task_evidence.get("measurement_status"),
                    "value": task_evidence.get("value"),
                    "label_origin": "environment_or_contract_derived",
                },
                {
                    "stage": "registered_claim_support",
                    "status": claim_trace.get("measurement_status"),
                    "value": claim_trace.get("value"),
                    "label_origin": "semantic_matcher_derived",
                },
                {
                    "stage": "report_closure",
                    "status": closure.get("measurement_status"),
                    "value": closure.get("value"),
                    "label_origin": "semantic_matcher_derived",
                },
                {
                    "stage": "verification_resolution",
                    "status": resolution,
                    "value": resolution == "grounded_verification",
                    "label_origin": "registered_contract_derived",
                },
            ],
            "failure_modes": failure_modes,
        },
        "judge_diagnostics": _dimension_projection(episode),
        "provenance": _origin(
            "mixed_explicit_origins",
            source_record_ids=[episode.get("record_id", "")]
            + [fact.get("record_id", "") for fact in facts],
            note=(
                "Environment facts, semantic matcher labels, registered-contract "
                "states and Judge diagnostics remain explicitly separated."
            ),
        ),
    }
    record["object_id"] = _stable_id(
        "ec", [identity.get("episode_id"), "evidence_claim"]
    )
    return record


def _branch_summary(
    steps: list[dict[str, Any]], *, start_position: int | None
) -> dict[str, Any]:
    selected = steps if start_position is None else steps[start_position:]
    return {
        "step_count": len(selected),
        "tool_counts": dict(
            sorted(Counter(step["action"].get("tool") or "unknown" for step in selected).items())
        ),
        "endpoint_counts": dict(
            sorted(
                Counter(
                    step["action"].get("endpoint_family") or "unknown"
                    for step in selected
                ).items()
            )
        ),
        "evidence_event_count": sum(
            step["evidence"]["event_count"] for step in selected
        ),
        "unique_action_signature_count": len(
            {step["behavior"]["action_signature"] for step in selected}
        ),
        "repeated_step_count": sum(
            "repetition" in step["behavior"]["tags"] for step in selected
        ),
    }


def _outcome_class(c0_state: Any, c1_state: Any) -> str:
    c0_grounded = c0_state == "grounded_verification"
    c1_grounded = c1_state == "grounded_verification"
    if c0_grounded and c1_grounded:
        return "retained_grounded_verification"
    if c0_grounded and not c1_grounded:
        return "degraded_after_intervention"
    if not c0_grounded and c1_grounded:
        return "improved_under_intervention"
    return "unchanged_non_grounded"


def _pair_record(
    source_pair: dict[str, Any],
    *,
    c0_trajectory: dict[str, Any],
    c1_trajectory: dict[str, Any],
    steps_by_episode: dict[str, list[dict[str, Any]]],
    decision_ids_by_episode: dict[str, list[str]],
    evidence_claim_by_episode: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    identity = source_pair.get("identity") or {}
    c0_id = identity["c0_episode_id"]
    c1_id = identity["c1_episode_id"]
    c0_steps = steps_by_episode[c0_id]
    c1_steps = steps_by_episode[c1_id]
    c1_contact = next(
        (
            index
            for index, step in enumerate(c1_steps)
            if "intervention_contact" in step["behavior"]["tags"]
        ),
        None,
    )
    c0_anchor = (
        min(c1_contact, len(c0_steps) - 1)
        if c1_contact is not None and c0_steps
        else None
    )
    first_divergence = None
    if c1_contact is not None:
        shared = min(len(c0_steps) - c0_anchor, len(c1_steps) - c1_contact)
        for offset in range(shared):
            left = c0_steps[c0_anchor + offset]
            right = c1_steps[c1_contact + offset]
            if (
                left["behavior"]["action_signature"]
                != right["behavior"]["action_signature"]
            ):
                first_divergence = {
                    "offset_from_contact": offset,
                    "c0_step_id": left["object_id"],
                    "c1_step_id": right["object_id"],
                    "c0_turn_idx": left["identity"]["turn_idx"],
                    "c1_turn_idx": right["identity"]["turn_idx"],
                    "basis": "action_signature_difference",
                }
                break
    transitions = source_pair.get("transitions") or {}
    verification = transitions.get("verification_resolution") or {}
    c0_state = verification.get("c0_state")
    c1_state = verification.get("c1_state")
    record = {
        "schema_version": SCHEMA_VERSION,
        "object_type": "counterfactual_pair_record",
        "identity": identity,
        "split": source_pair.get("split") or {},
        "pairing": source_pair.get("pairing") or {},
        "branches": {
            "C0": {
                "trajectory_id": c0_trajectory["object_id"],
                "evidence_claim_id": evidence_claim_by_episode[c0_id]["object_id"],
                "decision_point_ids": decision_ids_by_episode[c0_id],
                "post_anchor_summary": _branch_summary(
                    c0_steps, start_position=c0_anchor
                ),
            },
            "C1": {
                "trajectory_id": c1_trajectory["object_id"],
                "evidence_claim_id": evidence_claim_by_episode[c1_id]["object_id"],
                "decision_point_ids": decision_ids_by_episode[c1_id],
                "post_anchor_summary": _branch_summary(
                    c1_steps, start_position=c1_contact
                ),
            },
        },
        "intervention_anchor": {
            "status": "observed" if c1_contact is not None else "unavailable",
            "c1_position": c1_contact,
            "c1_step_id": (
                c1_steps[c1_contact]["object_id"] if c1_contact is not None else None
            ),
            "c1_turn_idx": (
                c1_steps[c1_contact]["identity"]["turn_idx"]
                if c1_contact is not None
                else None
            ),
            "c0_alignment_position": c0_anchor,
            "c0_alignment_step_id": (
                c0_steps[c0_anchor]["object_id"] if c0_anchor is not None else None
            ),
            "alignment_method": "ordinal_action_position_at_c1_contact",
            "alignment_limit": (
                "The safe transition export does not preserve semantic action "
                "equivalence or hidden reasoning."
            ),
        },
        "divergence": {
            "first_post_anchor_action_divergence": first_divergence,
            "status": (
                "observed_action_divergence"
                if first_divergence
                else "not_observed_or_not_alignable"
            ),
        },
        "effects": {
            "outcome_class": _outcome_class(c0_state, c1_state),
            "verification_resolution": verification,
            "stop_descriptor": transitions.get("stop_descriptor") or {},
            "diagnostic_scores": transitions.get("diagnostic_scores") or {},
        },
        "provenance": _origin(
            "matched_pair_plus_deterministic_alignment",
            source_record_ids=[source_pair.get("record_id", "")],
            note=(
                "Pair membership is frozen upstream; the intervention anchor and "
                "post-anchor divergence are deterministic projections."
            ),
        ),
        "training": {
            "direct_preference_ready": False,
            "condition_confounded": True,
            "numeric_reward_assigned": False,
            "intended_use": "counterfactual_diagnosis_and_data_design",
        },
    }
    record["object_id"] = _stable_id(
        "cfp", [identity.get("pair_id"), "counterfactual_pair"]
    )
    return record


def _validate_object(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("schema_version", "object_type", "object_id", "identity", "provenance"):
        if key not in record:
            errors.append(f"{record.get('object_id', '<unknown>')}: missing {key}")
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{record.get('object_id')}: schema version mismatch")
    if record.get("object_type") not in OBJECT_FILES:
        errors.append(f"{record.get('object_id')}: unknown object_type")
    if _contains_absolute_path(record):
        errors.append(f"{record.get('object_id')}: absolute path leaked")
    return errors


def _dataset_card(counts: dict[str, int], target_id: str) -> str:
    return f"""# ATOBench Counterfactual Verification Trajectory Dataset v1

Status: `DIAGNOSTIC_DATA_PRODUCT_NOT_TRAINING_CLAIM`

## Scope

- Target: `{target_id}`
- Canonical trajectories: {counts['canonical_trajectory']}
- Step-level behavior records: {counts['step_behavior_record']}
- Intervention records: {counts['intervention_record']}
- Decision/failure points: {counts['decision_failure_point']}
- Evidence-to-claim records: {counts['evidence_claim_record']}
- Matched Native/ATO pairs: {counts['counterfactual_pair_record']}

## Three views

- `Replay`: the complete ordered observable action trajectory for one episode.
- `Diagnostic`: observable decision boundaries, failure-signal candidates,
  evidence propagation and episode-level Judge diagnostics.
- `Counterfactual`: frozen C0/C1 branches aligned at the first observed AOU
  contact, with post-anchor behavioral and outcome divergence.

## Six canonical objects

The six JSONL object files are the source of truth. View files are lightweight
consumer projections and do not create new labels. Every derived label carries
an explicit origin: observed, deterministic, Judge-derived, semantic-matcher
derived, registered-contract derived, or unavailable.

## Important limits

- This export does not expose hidden chain-of-thought or infer private intent.
- Strategy shifts and failure signals are deterministic candidates, not claims
  about the model's internal reasoning.
- Safe transition v1 omits raw source line pointers, so deterministic facts can
  be linked to episodes but not reliably to exact turns.
- C0/C1 observations differ by design. Pair records are counterfactual
  diagnostic objects, not direct DPO preferences.
- No downstream SFT/RL improvement is claimed and no scalar reward is assigned.
"""


def export_counterfactual_trajectories(
    *,
    transitions_path: Path,
    episodes_path: Path,
    process_labels_path: Path,
    pairs_path: Path,
    output_dir: Path,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    inputs = [transitions_path, episodes_path, process_labels_path, pairs_path]
    require_real_data_authorization(inputs, allow_real_data)
    if dry_run:
        return {
            "schema_version": DATASET_VERSION,
            "status": "DRY_RUN_READY",
            "input_paths": [str(path) for path in inputs],
            "model_calls_made": 0,
            "network_accessed": False,
        }
    if (output_dir / "dataset_manifest.json").exists() and not new_version:
        raise GateError(
            f"refusing to overwrite completed trajectory export {output_dir}; "
            "use a new output directory"
        )

    transitions = load_jsonl(transitions_path)
    episodes = load_jsonl(episodes_path)
    process_labels = load_jsonl(process_labels_path)
    source_pairs = load_jsonl(pairs_path)
    episode_by_id = {
        (row.get("identity") or {}).get("episode_id"): row for row in episodes
    }
    if None in episode_by_id or len(episode_by_id) != len(episodes):
        raise GateError("episode identity is missing or not unique")

    transitions_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in transitions:
        episode_id = (row.get("identity") or {}).get("episode_id")
        if episode_id not in episode_by_id:
            raise GateError(f"transition references missing episode: {episode_id}")
        transitions_by_episode[episode_id].append(row)
    if set(transitions_by_episode) != set(episode_by_id):
        raise GateError("transition and episode coverage differ")

    facts_by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in process_labels:
        episode_id = (row.get("identity") or {}).get("episode_id")
        if episode_id not in episode_by_id:
            raise GateError(f"process label references missing episode: {episode_id}")
        facts_by_episode[episode_id].append(row)

    canonical: list[dict[str, Any]] = []
    interventions: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    evidence_claims: list[dict[str, Any]] = []
    replay_views: list[dict[str, Any]] = []
    diagnostic_views: list[dict[str, Any]] = []
    steps_by_episode: dict[str, list[dict[str, Any]]] = {}
    trajectory_by_episode: dict[str, dict[str, Any]] = {}
    decision_ids_by_episode: dict[str, list[str]] = defaultdict(list)
    evidence_claim_by_episode: dict[str, dict[str, Any]] = {}

    for episode_id in sorted(episode_by_id):
        episode = episode_by_id[episode_id]
        ordered = sorted(
            transitions_by_episode[episode_id],
            key=lambda row: int((row.get("identity") or {}).get("turn_idx", -1)),
        )
        contact_position = next(
            (
                index
                for index, row in enumerate(ordered)
                if (row.get("intervention") or {}).get("target_aou_contact") is True
            ),
            None,
        )
        episode_steps: list[dict[str, Any]] = []
        seen: Counter[str] = Counter()
        emitted_decision_keys: set[tuple[Any, ...]] = set()
        previous: dict[str, Any] | None = None
        for position, transition in enumerate(ordered):
            source_identity = transition.get("identity") or {}
            signature = _signature(transition)
            tags = _behavior_tags(
                transition,
                position=position,
                total=len(ordered),
                seen=seen,
                previous=previous,
                contact_position=contact_position,
            )
            step_id = _stable_id(
                "sb", [episode_id, source_identity.get("turn_idx"), "step_behavior"]
            )
            intervention_events = (transition.get("intervention") or {}).get("events") or []
            intervention_id = None
            if intervention_events:
                intervention_id = _stable_id(
                    "ir", [episode_id, source_identity.get("turn_idx"), "intervention"]
                )
                interventions.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "object_type": "intervention_record",
                        "object_id": intervention_id,
                        "identity": {
                            **_identity(episode),
                            "turn_idx": source_identity.get("turn_idx"),
                            "step_id": step_id,
                        },
                        "split": transition.get("split") or {},
                        "contact": {
                            "target_aou_contact": (transition.get("intervention") or {}).get(
                                "target_aou_contact"
                            )
                            is True,
                            "events": intervention_events,
                        },
                        "observation_boundary": transition.get("observation") or {},
                        "provenance": _origin(
                            "observed_structured_transition",
                            source_record_ids=[transition.get("record_id", "")],
                        ),
                    }
                )
            step = {
                "schema_version": SCHEMA_VERSION,
                "object_type": "step_behavior_record",
                "object_id": step_id,
                "identity": {
                    **_identity(episode),
                    "turn_idx": source_identity.get("turn_idx"),
                    "position": position,
                },
                "split": transition.get("split") or {},
                "state_before": transition.get("state_before") or {},
                "action": transition.get("action") or {},
                "observation": transition.get("observation") or {},
                "intervention": {
                    "record_id": intervention_id,
                    "target_aou_contact": (transition.get("intervention") or {}).get(
                        "target_aou_contact"
                    )
                    is True,
                },
                "evidence": {
                    "events": (transition.get("evidence") or {}).get("events") or [],
                    "event_count": _event_count(transition, "evidence"),
                },
                "state_after": transition.get("state_after") or {},
                "behavior": {
                    "stage": _stage(
                        position,
                        contact_position,
                        str(source_identity.get("condition") or ""),
                    ),
                    "tags": tags,
                    "action_signature": signature,
                    "same_signature_seen_before": seen[signature],
                    "classification_origin": "deterministic_structural_projection",
                },
                "links": {
                    "previous_step_id": episode_steps[-1]["object_id"] if episode_steps else None,
                    "next_step_id": None,
                },
                "provenance": _origin(
                    "observed_plus_deterministic_behavior_tags",
                    source_record_ids=[transition.get("record_id", "")],
                ),
            }
            if episode_steps:
                episode_steps[-1]["links"]["next_step_id"] = step_id
            episode_steps.append(step)
            for decision_type, status, signals in _decision_triggers(
                step, position=position, total=len(ordered)
            ):
                if decision_type == "evidence_update":
                    decision_key: tuple[Any, ...] = (
                        decision_type,
                        source_identity.get("turn_idx"),
                    )
                elif decision_type == "strategy_shift":
                    decision_key = (decision_type, step["behavior"]["stage"])
                else:
                    decision_key = (decision_type,)
                if decision_key in emitted_decision_keys:
                    continue
                emitted_decision_keys.add(decision_key)
                decision_id = _stable_id(
                    "dp",
                    [episode_id, source_identity.get("turn_idx"), decision_type],
                )
                decision = {
                    "schema_version": SCHEMA_VERSION,
                    "object_type": "decision_failure_point",
                    "object_id": decision_id,
                    "identity": {
                        **_identity(episode),
                        "turn_idx": source_identity.get("turn_idx"),
                        "step_id": step_id,
                    },
                    "split": transition.get("split") or {},
                    "point": {
                        "type": decision_type,
                        "status": status,
                        "signals": signals,
                        "behavior_tags": tags,
                        "internal_reasoning_observed": False,
                    },
                    "action": {
                        "label": _action_label(step["action"]),
                        "signature": signature,
                    },
                    "provenance": _origin(
                        (
                            "observed_boundary"
                            if status == "observed_boundary"
                            else "deterministic_candidate"
                        ),
                        source_record_ids=[transition.get("record_id", "")],
                        note=(
                            "This is an observable boundary or candidate signal, "
                            "not recovered chain-of-thought."
                        ),
                    ),
                }
                decisions.append(decision)
                decision_ids_by_episode[episode_id].append(decision_id)
            seen[signature] += 1
            previous = transition

        steps.extend(episode_steps)
        steps_by_episode[episode_id] = episode_steps
        evidence_claim = _evidence_claim_record(
            episode,
            sorted(
                facts_by_episode[episode_id],
                key=lambda row: str((row.get("identity") or {}).get("fact_id", "")),
            ),
            episode_steps,
        )
        evidence_claims.append(evidence_claim)
        evidence_claim_by_episode[episode_id] = evidence_claim
        trajectory = {
            "schema_version": SCHEMA_VERSION,
            "object_type": "canonical_trajectory",
            "identity": _identity(episode),
            "split": episode.get("split") or {},
            "summary": {
                "step_count": len(episode_steps),
                "first_turn_idx": episode_steps[0]["identity"]["turn_idx"],
                "last_turn_idx": episode_steps[-1]["identity"]["turn_idx"],
                "intervention_contact_position": contact_position,
                "intervention_record_count": sum(
                    step["intervention"]["record_id"] is not None
                    for step in episode_steps
                ),
                "evidence_event_count": sum(
                    step["evidence"]["event_count"] for step in episode_steps
                ),
                "unique_action_signature_count": len(
                    {step["behavior"]["action_signature"] for step in episode_steps}
                ),
            },
            "ordered_steps": [
                {
                    "step_id": step["object_id"],
                    "turn_idx": step["identity"]["turn_idx"],
                    "position": step["identity"]["position"],
                    "action_label": _action_label(step["action"]),
                    "action_signature": step["behavior"]["action_signature"],
                    "stage": step["behavior"]["stage"],
                    "tags": step["behavior"]["tags"],
                    "evidence_event_count": step["evidence"]["event_count"],
                    "intervention_contact": step["intervention"]["target_aou_contact"],
                }
                for step in episode_steps
            ],
            "object_links": {
                "decision_point_ids": decision_ids_by_episode[episode_id],
                "evidence_claim_id": evidence_claim["object_id"],
            },
            "provenance": _origin(
                "ordered_observed_transitions",
                source_record_ids=[
                    transition.get("record_id", "") for transition in ordered
                ],
            ),
        }
        trajectory["object_id"] = _stable_id(
            "ct", [episode_id, "canonical_trajectory"]
        )
        canonical.append(trajectory)
        trajectory_by_episode[episode_id] = trajectory
        replay_views.append(
            {
                "schema_version": "atobench.trajectory_view.v1",
                "view_type": "replay",
                "view_id": _stable_id("rv", [episode_id, "replay"]),
                "identity": trajectory["identity"],
                "canonical_trajectory_id": trajectory["object_id"],
                "timeline": trajectory["ordered_steps"],
                "display_contract": {
                    "primary_question": "what_observable_actions_happened",
                    "complete_trajectory": True,
                    "hidden_reasoning_inferred": False,
                },
            }
        )
        diagnostic_views.append(
            {
                "schema_version": "atobench.trajectory_view.v1",
                "view_type": "diagnostic",
                "view_id": _stable_id("dv", [episode_id, "diagnostic"]),
                "identity": trajectory["identity"],
                "canonical_trajectory_id": trajectory["object_id"],
                "decision_point_ids": decision_ids_by_episode[episode_id],
                "key_points": [
                    {
                        "step_id": step["object_id"],
                        "turn_idx": step["identity"]["turn_idx"],
                        "action_label": _action_label(step["action"]),
                        "tags": step["behavior"]["tags"],
                    }
                    for step in episode_steps
                    if any(
                        tag
                        in {
                            "initial_probe",
                            "intervention_contact",
                            "evidence_update",
                            "endpoint_shift",
                            "payload_shift",
                            "tool_shift",
                            "terminal_boundary",
                        }
                        for tag in step["behavior"]["tags"]
                    )
                ],
                "evidence_claim_id": evidence_claim["object_id"],
                "evidence_claim_summary": {
                    "verification_resolution_state": evidence_claim["claim"].get(
                        "verification_resolution_state"
                    ),
                    "failure_modes": evidence_claim["propagation"]["failure_modes"],
                    "fact_count": evidence_claim["evidence"]["fact_count"],
                },
                "judge_diagnostics": evidence_claim["judge_diagnostics"],
                "display_contract": {
                    "primary_question": "where_behavior_evidence_or_claim_flow_changed",
                    "candidate_points_are_not_private_intent": True,
                },
            }
        )

    pair_objects: list[dict[str, Any]] = []
    counterfactual_views: list[dict[str, Any]] = []
    for source_pair in sorted(
        source_pairs, key=lambda row: str((row.get("identity") or {}).get("pair_id", ""))
    ):
        identity = source_pair.get("identity") or {}
        c0_id = identity.get("c0_episode_id")
        c1_id = identity.get("c1_episode_id")
        if c0_id not in trajectory_by_episode or c1_id not in trajectory_by_episode:
            raise GateError(f"pair references missing trajectory: {identity.get('pair_id')}")
        pair = _pair_record(
            source_pair,
            c0_trajectory=trajectory_by_episode[c0_id],
            c1_trajectory=trajectory_by_episode[c1_id],
            steps_by_episode=steps_by_episode,
            decision_ids_by_episode=decision_ids_by_episode,
            evidence_claim_by_episode=evidence_claim_by_episode,
        )
        pair_objects.append(pair)
        counterfactual_views.append(
            {
                "schema_version": "atobench.trajectory_view.v1",
                "view_type": "counterfactual",
                "view_id": _stable_id(
                    "cv", [identity.get("pair_id"), "counterfactual_view"]
                ),
                "identity": identity,
                "counterfactual_pair_id": pair["object_id"],
                "branches": pair["branches"],
                "intervention_anchor": pair["intervention_anchor"],
                "divergence": pair["divergence"],
                "effects": pair["effects"],
                "display_contract": {
                    "primary_question": "what_changed_after_controlled_observation_intervention",
                    "native_and_ato_must_remain_separate": True,
                },
            }
        )

    objects_by_type = {
        "canonical_trajectory": canonical,
        "intervention_record": interventions,
        "step_behavior_record": steps,
        "decision_failure_point": decisions,
        "evidence_claim_record": evidence_claims,
        "counterfactual_pair_record": pair_objects,
    }
    errors = [
        error
        for rows in objects_by_type.values()
        for record in rows
        for error in _validate_object(record)
    ]
    object_ids = [
        record["object_id"]
        for rows in objects_by_type.values()
        for record in rows
    ]
    if len(object_ids) != len(set(object_ids)):
        errors.append("object_id is not globally unique")
    sensitive_counts: Counter[str] = Counter()
    sensitive_records = 0
    for rows in objects_by_type.values():
        for record in rows:
            kinds = residual_sensitive_kinds(
                json.dumps(record, sort_keys=True, ensure_ascii=False)
            )
            if kinds:
                sensitive_records += 1
                sensitive_counts.update(kinds)
    if sensitive_records:
        errors.append(f"residual sensitive content in {sensitive_records} records")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_paths: list[Path] = []
    for object_type, filename in OBJECT_FILES.items():
        path = output_dir / filename
        write_jsonl(path, objects_by_type[object_type])
        output_paths.append(path)
    for view_type, filename in VIEW_FILES.items():
        path = output_dir / filename
        rows = {
            "replay": replay_views,
            "diagnostic": diagnostic_views,
            "counterfactual": counterfactual_views,
        }[view_type]
        write_jsonl(path, rows)
        output_paths.append(path)

    counts = {key: len(value) for key, value in objects_by_type.items()}
    counts.update(
        {
            "replay_view": len(replay_views),
            "diagnostic_view": len(diagnostic_views),
            "counterfactual_view": len(counterfactual_views),
        }
    )
    target_ids = {
        (row.get("environment") or {}).get("target_id") for row in episodes
    }
    target_id = next(iter(target_ids)) if len(target_ids) == 1 else "mixed_or_unknown"
    quality = {
        "schema_version": "atobench.counterfactual_trajectory_quality.v1",
        "status": "PASS_DIAGNOSTIC_DATA_AUDIT" if not errors else "FAIL",
        "counts": counts,
        "coverage": {
            "episodes_with_intervention_contact": len(
                {
                    row["identity"]["episode_id"]
                    for row in interventions
                    if row["contact"]["target_aou_contact"]
                }
            ),
            "steps_with_evidence_events": sum(
                step["evidence"]["event_count"] > 0 for step in steps
            ),
            "pairs_with_observed_anchor": sum(
                pair["intervention_anchor"]["status"] == "observed"
                for pair in pair_objects
            ),
            "pairs_with_post_anchor_divergence": sum(
                pair["divergence"]["status"] == "observed_action_divergence"
                for pair in pair_objects
            ),
        },
        "decision_point_distribution": dict(
            sorted(Counter(row["point"]["type"] for row in decisions).items())
        ),
        "outcome_distribution": dict(
            sorted(
                Counter(row["effects"]["outcome_class"] for row in pair_objects).items()
            )
        ),
        "known_gaps": [
            "single_target_only",
            "no_hidden_chain_of_thought",
            "episode_facts_not_reliably_linkable_to_exact_safe_transition_turn",
            "ordinal_post_contact_alignment_not_semantic_alignment",
            "no_scalar_reward_mapping",
            "no_downstream_training_validation",
        ],
    }
    quality_path = output_dir / "quality_summary.json"
    write_json(quality_path, quality)
    output_paths.append(quality_path)
    audit = {
        "schema_version": AUDIT_VERSION,
        "status": "PASS" if not errors else "FAIL",
        "violation_count": len(errors),
        "violations": errors,
        "counts": counts,
        "absolute_path_leakage_count": sum(
            _contains_absolute_path(record)
            for rows in objects_by_type.values()
            for record in rows
        ),
        "residual_sensitive_record_count": sensitive_records,
        "residual_sensitive_kind_counts": dict(sorted(sensitive_counts.items())),
        "hidden_chain_of_thought_included": False,
        "internal_intent_recovered": False,
        "numeric_reward_assigned": False,
        "downstream_training_claimed": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    audit_path = output_dir / "export_audit.json"
    write_json(audit_path, audit)
    output_paths.append(audit_path)
    card_path = output_dir / "DATASET_CARD.md"
    card_path.write_text(_dataset_card(counts, str(target_id)), encoding="utf-8")
    output_paths.append(card_path)

    manifest_path = output_dir / "dataset_manifest.json"
    manifest = {
        "schema_version": DATASET_VERSION,
        "status": "DIAGNOSTIC_DATA_PRODUCT_READY" if not errors else "FAILED",
        "object_schema_version": SCHEMA_VERSION,
        "view_schema_version": "atobench.trajectory_view.v1",
        "target_id": target_id,
        "source_inputs": {
            path.name: {"sha256": sha256_file(path)} for path in inputs
        },
        "outputs": {
            path.name: {"sha256": sha256_file(path), "bytes": path.stat().st_size}
            for path in output_paths
        },
        "counts": counts,
        "training_status": "NO_DOWNSTREAM_EFFECT_CLAIM",
        "model_calls_made": 0,
        "network_accessed": False,
    }
    write_json(manifest_path, manifest)
    if errors:
        raise GateError(f"trajectory export audit failed: {errors[:5]}")
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transitions", required=True, type=Path)
    parser.add_argument("--episodes", required=True, type=Path)
    parser.add_argument("--process-labels", required=True, type=Path)
    parser.add_argument("--pairs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = export_counterfactual_trajectories(
            transitions_path=args.transitions,
            episodes_path=args.episodes,
            process_labels_path=args.process_labels,
            pairs_path=args.pairs,
            output_dir=args.output,
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
