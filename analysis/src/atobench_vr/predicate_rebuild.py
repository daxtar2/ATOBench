from __future__ import annotations

import argparse
import collections
import importlib
import sys
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    require_real_data_authorization,
    stage_manifest,
    write_json,
    write_jsonl,
)


def _event_id(event: dict[str, Any]) -> str:
    integrity = event.get("integrity") or {}
    file_sha = str(integrity.get("raw_file_sha256") or "")
    line_number = integrity.get("raw_line_number")
    if not file_sha or not isinstance(line_number, int):
        raise GateError("normalized event missing raw_file_sha256/raw_line_number")
    return f"http:{file_sha[:16]}:line:{line_number}"


def _load_engine(engine_root: Path) -> tuple[Any, Any, str]:
    package_dir = engine_root.resolve()
    if not (package_dir / "graph.py").is_file() or not (
        package_dir / "predicates.py"
    ).is_file():
        raise GateError(f"deterministic engine package is incomplete: {package_dir}")
    sys.path.insert(0, str(package_dir.parent))
    graph = importlib.import_module(f"{package_dir.name}.graph")
    predicates = importlib.import_module(f"{package_dir.name}.predicates")
    return graph, predicates, str(getattr(predicates, "REGISTRY_VERSION", "unknown"))


def _value(status: str) -> bool | None:
    if status == "positive":
        return True
    if status == "negative":
        return False
    return None


def run(
    *,
    normalized_paths: list[Path],
    alignment_path: Path,
    engine_root: Path,
    output_dir: Path,
    dry_run: bool,
    new_version: bool,
    allow_real_data: bool,
) -> dict[str, Any]:
    inputs = [*normalized_paths, alignment_path, engine_root / "graph.py", engine_root / "predicates.py"]
    require_real_data_authorization(inputs, allow_real_data)
    if dry_run:
        return {
            "stage": "05a_rebuild_predicate_decisions",
            "status": "dry_run",
            "normalized_sources": len(normalized_paths),
            "engine_root": str(engine_root.resolve()),
        }

    ensure_output_available(output_dir, new_version)
    graph, predicates, registry_version = _load_engine(engine_root)

    events_by_episode: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    event_ids: set[str] = set()
    for path in normalized_paths:
        for event in load_jsonl(path):
            identity = event.get("identity") or {}
            episode_id = str(identity.get("episode_id") or "")
            if not episode_id:
                raise GateError(f"normalized event missing episode_id: {path}")
            eid = _event_id(event)
            if eid in event_ids:
                raise GateError(f"duplicate normalized event id across inputs: {eid}")
            event_ids.add(eid)
            event["_atobench_event_id"] = eid
            events_by_episode[episode_id].append(event)

    alignment = load_jsonl(alignment_path)
    accepted_event_ids = {
        str(event_id)
        for row in alignment
        if row.get("accepted_for_evidence") is True
        for event_id in row.get("accepted_event_ids") or []
    }
    unknown_alignment_ids = sorted(accepted_event_ids - event_ids)
    if unknown_alignment_ids:
        raise GateError(
            f"alignment references {len(unknown_alignment_ids)} absent normalized events"
        )

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    evidence_id_count = 0
    accepted_evidence_id_count = 0
    for episode_id in sorted(events_by_episode):
        events = events_by_episode[episode_id]
        episode_nodes, episode_edges = graph._build_episode(events)
        nodes.extend(episode_nodes)
        edges.extend(episode_edges)
        by_turn = {
            int(event["identity"]["turn_idx"]): event["_atobench_event_id"]
            for event in events
        }
        for decision in predicates._evaluate_episode(events, episode_edges):
            source_event_ids = sorted(
                {
                    by_turn[turn]
                    for turn in decision.get("evidence_turns") or []
                    if turn in by_turn
                }
            )
            accepted_source_ids = sorted(set(source_event_ids) & accepted_event_ids)
            status = str(decision.get("machine_decision") or "unknown")
            decision.pop("code_hash", None)
            decision.update(
                {
                    "measurement_status": status,
                    "value": _value(status),
                    "source_event_ids": source_event_ids,
                    "accepted_source_event_ids": accepted_source_ids,
                    "all_source_events_alignment_accepted": bool(source_event_ids)
                    and len(accepted_source_ids) == len(source_event_ids),
                    "fact_class": (
                        "intervention"
                        if decision["predicate_id"]
                        in {"COMMON_ELIGIBLE", "COMMON_TARGET_CONTACT"}
                        else "verification"
                    ),
                    "importance": "high",
                    "predicate_version": f"trajectory_analysis_v3.{registry_version}",
                    "engine_rebuilt_from_current_inputs": True,
                }
            )
            evidence_id_count += len(source_event_ids)
            accepted_evidence_id_count += len(accepted_source_ids)
            decisions.append(decision)

    violations = graph.validate_graph(nodes, edges)
    if violations:
        write_jsonl(output_dir / "graph_violations.jsonl", violations)
        raise GateError(
            f"fresh evidence graph failed {len(violations)} deterministic invariants"
        )

    node_path = output_dir / "graph_nodes.jsonl"
    edge_path = output_dir / "graph_edges.jsonl"
    decision_path = output_dir / "predicate_decisions.jsonl"
    summary_path = output_dir / "predicate_rebuild_summary.json"
    write_jsonl(node_path, nodes)
    write_jsonl(edge_path, edges)
    write_jsonl(decision_path, decisions)
    details = {
        "normalized_source_count": len(normalized_paths),
        "episode_count": len(events_by_episode),
        "normalized_event_count": len(event_ids),
        "accepted_alignment_event_count": len(accepted_event_ids),
        "graph_node_count": len(nodes),
        "graph_edge_count": len(edges),
        "graph_violation_count": 0,
        "predicate_decision_count": len(decisions),
        "predicate_evidence_event_reference_count": evidence_id_count,
        "accepted_predicate_evidence_event_reference_count": accepted_evidence_id_count,
        "registry_version": registry_version,
        "legacy_derived_outputs_read": False,
        "llm_calls": 0,
        "hash_authorization_gates": False,
    }
    write_json(summary_path, details)
    outputs = [node_path, edge_path, decision_path, summary_path]
    manifest = stage_manifest(
        "05a_rebuild_predicate_decisions",
        inputs,
        outputs,
        "complete",
        dry_run=False,
        details=details,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--normalized-events", type=Path, action="append", required=True)
    parser.add_argument("--alignment", type=Path, required=True)
    parser.add_argument("--engine-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    parser.add_argument("--allow-real-data", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            normalized_paths=args.normalized_events,
            alignment_path=args.alignment,
            engine_root=args.engine_root,
            output_dir=args.output,
            dry_run=args.dry_run,
            new_version=args.new_version,
            allow_real_data=args.allow_real_data,
        )
    except GateError as exc:
        parser.error(str(exc))
    print(result)
    return 0

