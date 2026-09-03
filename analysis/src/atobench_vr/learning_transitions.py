from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    load_jsonl,
    require_real_data_authorization,
    sha256_file,
    sha256_json,
    write_json,
    write_jsonl,
)
from .learning_data import _group_key, _split_assignments, _split_object
from .redaction import residual_sensitive_kinds

SCHEMA_VERSION = "atobench.learning_transition.v1"
DATASET_VERSION = "atobench.learning_transitions.v1"
AUDIT_VERSION = "atobench.learning_transition_audit.v1"

# This exporter is deliberately a structural projection.  In particular, it
# never emits request/response values, resource or identity hashes, free-form
# agent text, or raw source pointers.
ACTION_FIELDS = (
    "tool",
    "method",
    "endpoint_family",
    "route_template",
    "payload_family",
    "query_keys",
    "request_body_type",
    "request_body_schema",
)
NATIVE_OBSERVATION_FIELDS = (
    "native_body_available",
    "native_body_type",
    "native_body_schema",
    "native_status_code",
    "status_code",
)
VISIBLE_OBSERVATION_FIELDS = (
    "content_type",
    "visible_body_type",
    "visible_body_schema",
    "status_code",
    "transformed_status_code",
    "transformed_after_ref_type",
    "transformed_after_ref_schema",
    "sqli_native_support",
    "sqli_visible_support",
)
INTERVENTION_FIELDS = (
    "is_target_aou_contact",
    "layer",
    "operation",
    "primitive",
    "status",
    "target_dims",
    "target_kind",
)
EVIDENCE_FIELDS = ("polarity", "role", "scope")


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{sha256_json(value)[:20]}"


def _contains_absolute_path(value: Any) -> bool:
    if isinstance(value, str):
        # Route templates are intentional API metadata (for example
        # `/rest/user/login`), whereas host filesystem paths are forbidden.
        return value.startswith(("/Users/", "/private/", "/tmp/", "/var/", "/home/"))
    if isinstance(value, dict):
        return any(_contains_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_absolute_path(item) for item in value)
    return False


def _safe_fields(data: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: data.get(field) for field in fields if field in data}


def _compact_schema(value: Any) -> Any:
    """Project a schema into field-name-free structural statistics.

    Graph normalization can receive malformed form payloads whose apparent JSON
    property names contain response fragments.  Retaining property names would
    therefore reintroduce body text.  Shape statistics preserve useful policy
    context (object versus array, complexity) without retaining any names.
    """

    if not isinstance(value, dict):
        return None
    type_counts: Counter[str] = Counter()
    property_count = 0
    max_depth = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal property_count, max_depth
        if not isinstance(node, dict):
            return
        max_depth = max(max_depth, depth)
        node_type = node.get("type")
        if isinstance(node_type, str):
            type_counts[node_type] += 1
        properties = node.get("properties")
        if isinstance(properties, dict):
            property_count += len(properties)
            for child in properties.values():
                walk(child, depth + 1)
        walk(node.get("items"), depth + 1)

    walk(value, 0)
    return {
        "root_type": value.get("type") if isinstance(value.get("type"), str) else "unknown",
        "node_type_counts": dict(sorted(type_counts.items())),
        "property_count": property_count,
        "max_depth": max_depth,
    }


def _project_action(data: dict[str, Any]) -> dict[str, Any]:
    projected = _safe_fields(data, ACTION_FIELDS)
    if "request_body_schema" in projected:
        projected["request_body_schema"] = _compact_schema(
            projected["request_body_schema"]
        )
    return projected


def _project_observation(data: dict[str, Any], *, native: bool) -> dict[str, Any]:
    fields = NATIVE_OBSERVATION_FIELDS if native else VISIBLE_OBSERVATION_FIELDS
    projected = _safe_fields(data, fields)
    for schema_key in (
        "native_body_schema",
        "visible_body_schema",
        "transformed_after_ref_schema",
    ):
        if schema_key in projected:
            projected[schema_key] = _compact_schema(projected[schema_key])
    return projected


def _project_intervention(data: dict[str, Any]) -> dict[str, Any]:
    return _safe_fields(data, INTERVENTION_FIELDS)


def _project_evidence(data: dict[str, Any]) -> dict[str, Any]:
    return _safe_fields(data, EVIDENCE_FIELDS)


def _index_nodes(nodes: list[dict[str, Any]]) -> tuple[dict[str, dict[int, list[dict[str, Any]]]], list[str]]:
    indexed: dict[str, dict[int, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    errors: list[str] = []
    for node in nodes:
        episode_id = node.get("episode_id")
        turn_idx = node.get("turn_idx")
        if not isinstance(episode_id, str) or not isinstance(turn_idx, int):
            errors.append("graph node missing episode_id or integer turn_idx")
            continue
        indexed[episode_id][turn_idx].append(node)
    return indexed, errors


def _state_snapshot(counts: Counter[str], *, turn_idx: int) -> dict[str, Any]:
    return {
        "turn_idx": turn_idx,
        "action_count": counts["action"],
        "native_observation_count": counts["native_observation"],
        "visible_observation_count": counts["visible_observation"],
        "artifact_count": counts["artifact"],
        "resource_count": counts["resource"],
        "identity_count": counts["identity"],
        "evidence_count": counts["evidence"],
        "intervention_count": counts["intervention"],
        "target_aou_contact_count": counts["target_aou_contact"],
    }


def _turn_nodes(nodes: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return sorted(
        (node for node in nodes if node.get("node_type") == kind),
        key=lambda node: str(node.get("node_id", "")),
    )


def _transition_record(
    *,
    action: dict[str, Any],
    turn_nodes: list[dict[str, Any]],
    pair: dict[str, Any],
    split: dict[str, Any],
    target_id: str,
    target_snapshot_id: str,
    state_before: dict[str, Any],
    state_after: dict[str, Any],
) -> dict[str, Any]:
    action_data = action.get("data") or {}
    native = [_project_observation(node.get("data") or {}, native=True) for node in _turn_nodes(turn_nodes, "native_observation")]
    visible = [_project_observation(node.get("data") or {}, native=False) for node in _turn_nodes(turn_nodes, "visible_observation")]
    interventions = [_project_intervention(node.get("data") or {}) for node in _turn_nodes(turn_nodes, "intervention")]
    evidence = [_project_evidence(node.get("data") or {}) for node in _turn_nodes(turn_nodes, "evidence")]
    source = action.get("source") or {}
    identity = {
        "episode_id": action["episode_id"],
        "pair_id": action.get("pair_id"),
        "campaign_id": pair.get("campaign_id"),
        "condition": action.get("condition"),
        "model": action.get("model"),
        "aou": action.get("aou"),
        "unit_id": pair["unit_id"],
        "block_id": pair["block_id"],
        "turn_idx": action["turn_idx"],
    }
    record = {
        "schema_version": SCHEMA_VERSION,
        "view": "structured_transition",
        "identity": identity,
        "environment": {
            "target_id": target_id,
            "target_snapshot_id": target_snapshot_id,
            "target_snapshot_is_cryptographic": False,
            "graph_version": action.get("graph_version"),
        },
        "split": split,
        "state_before": state_before,
        "action": _project_action(action_data),
        "observation": {
            "native": native,
            "visible": visible,
            "native_available": bool(native),
            "visible_available": bool(visible),
        },
        "intervention": {
            "events": interventions,
            "target_aou_contact": any(
                item.get("is_target_aou_contact") is True for item in interventions
            ),
        },
        "evidence": {
            "events": evidence,
            "count": len(evidence),
        },
        "state_after": state_after,
        "provenance": {
            "source_parse_status": source.get("parse_status"),
            "source_parser_version": source.get("parser_version"),
            "source_raw_file_sha256": source.get("raw_file_sha256"),
            "raw_paths_removed": True,
            "request_and_response_values_removed": True,
            "identity_and_resource_values_removed": True,
            "hidden_chain_of_thought_included": False,
        },
        "training": {
            "readiness": "structured_transition_candidate",
            "structured_transition_candidate": True,
            "language_sft_ready": False,
            "direct_offline_rl_ready": False,
            "generic_reward_assigned": False,
            "reason": (
                "The record has structured action-observation state transitions, "
                "but omits raw language context and has no frozen AOU-specific reward map."
            ),
        },
    }
    record["record_id"] = _stable_id(
        "lt", [identity["episode_id"], identity["turn_idx"], "structured_transition"]
    )
    return record


def _validate_record(record: dict[str, Any]) -> list[str]:
    required = (
        "schema_version",
        "record_id",
        "view",
        "identity",
        "environment",
        "split",
        "state_before",
        "action",
        "observation",
        "state_after",
        "provenance",
        "training",
    )
    errors = [
        f"{record.get('record_id', '<unknown>')}: missing {key}"
        for key in required
        if key not in record
    ]
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{record.get('record_id')}: schema version mismatch")
    if _contains_absolute_path(record):
        errors.append(f"{record.get('record_id')}: absolute path leaked")
    return errors


def _dataset_card(*, count: int, episode_count: int, target_id: str, target_snapshot_id: str) -> str:
    return f"""# ATOBench Structured Learning Transitions v1

Status: `CANDIDATE_EXPORT_NOT_DIRECT_RL_READY`

## Scope

- Target: `{target_id}`
- Target snapshot label: `{target_snapshot_id}` (not a cryptographic digest)
- Episodes with action transitions: {episode_count}
- Structured action transitions: {count}

## Contents

Each record is one observable tool-call boundary with a compact state-before,
an action class, native/visible observation metadata, intervention and evidence
events, and a state-after. It is a deterministic projection of the frozen
graph; node-level request values, response bodies, typed values, identity and
resource values, raw source paths, and free-form agent text are excluded.

## Intended uses

- Tool-policy and trajectory-state analysis.
- Process-supervision design after AOU-specific label mapping.
- Offline/agentic RL dataset research with explicit safety and reward gates.
- Defender intervention and recovery analysis.

## Not ready for

- Direct language SFT: no natural-language prompts, thoughts, reports or body text.
- Direct offline RL: no frozen AOU-specific reward map or downstream validation.
- Direct DPO/RM: Native/ATO conditions have different observations.
- Public release or production training without a dedicated data/safety review.

## Split policy

All models sharing one target snapshot, AOU, unit and block remain in one split.
This is a within-target block holdout only; it makes no cross-target
generalization claim.
"""


def _quality_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_action = Counter(
        (row["action"].get("tool") or "unknown") for row in records
    )
    by_endpoint = Counter(
        (row["action"].get("endpoint_family") or "unknown") for row in records
    )
    groups = {row["split"]["group_id"]: row["split"]["name"] for row in records}
    return {
        "schema_version": "atobench.learning_transition_quality.v1",
        "status": "PASS_CANDIDATE_QUALITY_AUDIT",
        "transition_distribution": {
            "by_aou": dict(sorted(Counter(row["identity"]["aou"] for row in records).items())),
            "by_condition": dict(sorted(Counter(row["identity"]["condition"] for row in records).items())),
            "by_model": dict(sorted(Counter(row["identity"]["model"] for row in records).items())),
            "by_split": dict(sorted(Counter(row["split"]["name"] for row in records).items())),
            "by_tool": dict(sorted(by_action.items())),
            "by_endpoint_family": dict(sorted(by_endpoint.items())),
        },
        "boundary_coverage": {
            "with_native_observation": sum(row["observation"]["native_available"] for row in records),
            "with_visible_observation": sum(row["observation"]["visible_available"] for row in records),
            "with_evidence_event": sum(bool(row["evidence"]["events"]) for row in records),
            "with_intervention_event": sum(bool(row["intervention"]["events"]) for row in records),
            "with_target_aou_contact": sum(row["intervention"]["target_aou_contact"] for row in records),
        },
        "split_distribution": {
            "group_count": len(groups),
            "group_counts": dict(sorted(Counter(groups.values()).items())),
        },
        "known_gaps": [
            "single_target_only",
            "target_snapshot_label_is_not_cryptographic",
            "raw_language_and_body_content_removed",
            "no_aou_specific_reward_mapping",
            "no_human_audit_of_transition_semantics",
            "native_ato_pairs_are_condition_confounded",
            "no_downstream_training_validation",
        ],
        "next_quality_gates": [
            "human-audit transition projections",
            "link deterministic facts at compatible turn boundaries",
            "freeze AOU-specific reward maps",
            "construct within-condition matched-observation preferences",
            "add target-level held-out evaluation",
            "run bounded downstream learning experiment",
        ],
    }


def export_learning_transitions(
    *,
    graph_nodes_path: Path,
    pair_profiles_path: Path,
    episode_states_path: Path,
    output_dir: Path,
    target_id: str,
    target_snapshot_id: str,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    inputs = [graph_nodes_path, pair_profiles_path, episode_states_path]
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
            f"refusing to overwrite completed transition export {output_dir}; use a new output directory"
        )
    if not target_id.strip() or not target_snapshot_id.strip():
        raise GateError("target-id and target-snapshot-id must be non-empty")

    pairs = load_jsonl(pair_profiles_path)
    episodes = load_jsonl(episode_states_path)
    nodes = load_jsonl(graph_nodes_path)
    pair_by_id = {row["pair_id"]: row for row in pairs}
    episode_by_id = {row["episode_id"]: row for row in episodes}
    if len(pair_by_id) != len(pairs) or len(episode_by_id) != len(episodes):
        raise GateError("pair_id or episode_id is not unique")
    indexed, errors = _index_nodes(nodes)
    if set(indexed) != set(episode_by_id):
        missing = sorted(set(episode_by_id) - set(indexed))
        extra = sorted(set(indexed) - set(episode_by_id))
        raise GateError(
            f"graph and episode-state coverage differ: missing={len(missing)} extra={len(extra)}"
        )

    split_by_group = _split_assignments(pairs, target_id, target_snapshot_id)
    split_by_pair = {
        pair["pair_id"]: _split_object(
            _group_key(pair, target_id, target_snapshot_id),
            split_by_group[_group_key(pair, target_id, target_snapshot_id)],
        )
        for pair in pairs
    }
    records: list[dict[str, Any]] = []
    for episode_id in sorted(indexed):
        episode = episode_by_id[episode_id]
        pair = pair_by_id.get(episode["pair_id"])
        if pair is None:
            raise GateError(f"episode references missing pair: {episode_id}")
        counts: Counter[str] = Counter()
        for turn_idx in sorted(indexed[episode_id]):
            turn_nodes = indexed[episode_id][turn_idx]
            state_before = _state_snapshot(counts, turn_idx=turn_idx)
            for node in turn_nodes:
                kind = str(node.get("node_type"))
                counts[kind] += 1
                if kind == "intervention" and (node.get("data") or {}).get("is_target_aou_contact") is True:
                    counts["target_aou_contact"] += 1
            state_after = _state_snapshot(counts, turn_idx=turn_idx)
            actions = _turn_nodes(turn_nodes, "action")
            for action in actions:
                records.append(
                    _transition_record(
                        action=action,
                        turn_nodes=turn_nodes,
                        pair=pair,
                        split=split_by_pair[pair["pair_id"]],
                        target_id=target_id,
                        target_snapshot_id=target_snapshot_id,
                        state_before=state_before,
                        state_after=state_after,
                    )
                )

    record_ids = [row["record_id"] for row in records]
    if len(record_ids) != len(set(record_ids)):
        errors.append("record_id is not unique")
    transition_keys = [(row["identity"]["episode_id"], row["identity"]["turn_idx"]) for row in records]
    if len(transition_keys) != len(set(transition_keys)):
        errors.append("episode_id/turn_idx is not unique among action transitions")
    errors.extend(error for record in records for error in _validate_record(record))
    sensitive_record_count = 0
    sensitive_kind_counts: Counter[str] = Counter()
    for record in records:
        kinds = residual_sensitive_kinds(json.dumps(record, sort_keys=True, ensure_ascii=False))
        if kinds:
            sensitive_record_count += 1
            sensitive_kind_counts.update(kinds)
    if sensitive_record_count:
        errors.append(f"residual sensitive content in {sensitive_record_count} records")
    group_splits: dict[str, set[str]] = defaultdict(set)
    for record in records:
        group_splits[record["split"]["group_id"]].add(record["split"]["name"])
    leaking_groups = [group for group, values in group_splits.items() if len(values) != 1]
    if leaking_groups:
        errors.append(f"split group leakage: {len(leaking_groups)} groups")

    output_dir.mkdir(parents=True, exist_ok=True)
    transition_path = output_dir / "transition_records.jsonl"
    card_path = output_dir / "DATASET_CARD.md"
    quality_path = output_dir / "quality_summary.json"
    audit_path = output_dir / "export_audit.json"
    manifest_path = output_dir / "dataset_manifest.json"
    write_jsonl(transition_path, records)
    card_path.write_text(
        _dataset_card(
            count=len(records),
            episode_count=len({row["identity"]["episode_id"] for row in records}),
            target_id=target_id,
            target_snapshot_id=target_snapshot_id,
        ),
        encoding="utf-8",
    )
    write_json(quality_path, _quality_summary(records))
    audit = {
        "schema_version": AUDIT_VERSION,
        "status": "PASS" if not errors else "FAIL",
        "violation_count": len(errors),
        "violations": errors,
        "counts": {
            "transition_records": len(records),
            "episodes_with_transitions": len({row["identity"]["episode_id"] for row in records}),
            "graph_nodes_processed": len(nodes),
        },
        "split_group_count": len(group_splits),
        "split_group_leakage_count": len(leaking_groups),
        "absolute_path_leakage_count": sum(_contains_absolute_path(record) for record in records),
        "residual_sensitive_record_count": sensitive_record_count,
        "residual_sensitive_kind_counts": dict(sorted(sensitive_kind_counts.items())),
        "request_and_response_values_included": False,
        "identity_and_resource_values_included": False,
        "hidden_chain_of_thought_included": False,
        "generic_reward_assigned": False,
        "direct_offline_rl_ready": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    write_json(audit_path, audit)
    outputs = [transition_path, card_path, quality_path, audit_path]
    manifest = {
        "schema_version": DATASET_VERSION,
        "status": "CANDIDATE_EXPORT_NOT_DIRECT_RL_READY" if not errors else "FAILED",
        "learning_transition_schema_version": SCHEMA_VERSION,
        "target_id": target_id,
        "target_snapshot_id": target_snapshot_id,
        "source_inputs": {path.name: {"sha256": sha256_file(path)} for path in inputs},
        "outputs": {
            path.name: {"sha256": sha256_file(path), "bytes": path.stat().st_size}
            for path in outputs
        },
        "counts": audit["counts"],
        "release_status": "internal_research_only_pending_dataset_review",
        "direct_offline_rl_ready": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    write_json(manifest_path, manifest)
    if errors:
        raise GateError(f"transition export audit failed: {errors[:5]}")
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--graph-nodes", required=True, type=Path)
    parser.add_argument("--pair-profiles", required=True, type=Path)
    parser.add_argument("--episode-states", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--target-snapshot-id", required=True)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = export_learning_transitions(
            graph_nodes_path=args.graph_nodes,
            pair_profiles_path=args.pair_profiles,
            episode_states_path=args.episode_states,
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
