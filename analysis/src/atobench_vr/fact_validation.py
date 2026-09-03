from __future__ import annotations

import argparse
import collections
from pathlib import Path
from typing import Any

from .common import GateError, load_jsonl, stage_manifest, write_json


REQUIRED_FIELDS = {
    "fact_id",
    "episode_id",
    "fact_class",
    "fact_type",
    "value",
    "measurement_status",
    "importance",
    "source_kind",
    "source_pointers",
    "predicate_or_prompt_version",
    "confidence",
}
ALLOWED_FIELDS = REQUIRED_FIELDS


def run(
    *,
    facts_path: Path,
    stop_contexts_path: Path,
    alignment_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    facts = load_jsonl(facts_path)
    stop_contexts = load_jsonl(stop_contexts_path)
    alignments = load_jsonl(alignment_path)
    accepted_event_ids = {
        str(event_id)
        for row in alignments
        if row.get("accepted_for_evidence") is True
        for event_id in row.get("accepted_event_ids") or []
    }
    violations: list[dict[str, Any]] = []
    fact_ids: set[str] = set()
    episodes: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for line_number, fact in enumerate(facts, 1):
        missing = sorted(REQUIRED_FIELDS - set(fact))
        extra = sorted(set(fact) - ALLOWED_FIELDS)
        if missing or extra:
            violations.append(
                {
                    "invariant": "fact_schema_fields",
                    "line_number": line_number,
                    "missing": missing,
                    "extra": extra,
                }
            )
        fact_id = str(fact.get("fact_id") or "")
        if fact_id in fact_ids:
            violations.append(
                {"invariant": "unique_fact_id", "fact_id": fact_id}
            )
        fact_ids.add(fact_id)
        if fact_id.startswith("fact_"):
            violations.append(
                {"invariant": "no_hash_based_fact_id", "fact_id": fact_id}
            )
        for pointer in fact.get("source_pointers") or []:
            if pointer.startswith("event:") and pointer.removeprefix("event:") not in accepted_event_ids:
                violations.append(
                    {
                        "invariant": "http_pointer_must_be_alignment_accepted",
                        "fact_id": fact_id,
                        "pointer": pointer,
                    }
                )
        episodes[str(fact.get("episode_id") or "")].append(fact)

    for episode_id, episode_facts in episodes.items():
        types = {fact.get("fact_type") for fact in episode_facts}
        missing_common = sorted(
            {"COMMON_ELIGIBLE", "COMMON_TARGET_CONTACT"} - types
        )
        if missing_common:
            violations.append(
                {
                    "invariant": "common_intervention_coverage",
                    "episode_id": episode_id,
                    "missing": missing_common,
                }
            )

    stop_episode_ids = {str(row.get("episode_id") or "") for row in stop_contexts}
    if stop_episode_ids != set(episodes):
        violations.append(
            {
                "invariant": "stop_context_episode_coverage",
                "missing": sorted(set(episodes) - stop_episode_ids),
                "extra": sorted(stop_episode_ids - set(episodes)),
            }
        )

    details = {
        "fact_count": len(facts),
        "episode_count": len(episodes),
        "stop_context_count": len(stop_contexts),
        "accepted_alignment_event_count": len(accepted_event_ids),
        "violation_count": len(violations),
        "fact_class_counts": dict(
            collections.Counter(str(row.get("fact_class")) for row in facts)
        ),
        "measurement_status_counts": dict(
            collections.Counter(
                str(row.get("measurement_status")) for row in facts
            )
        ),
        "report_fact_class_deferred": True,
        "llm_calls": 0,
        "hash_authorization_gates": False,
    }
    result = {
        "schema_version": "atobench.fact_registry_validation.v1",
        "status": "passed" if not violations else "failed",
        "details": details,
        "violations": violations,
    }
    write_json(output_path, result)
    manifest = stage_manifest(
        "05c_validate_episode_fact_registry",
        [facts_path, stop_contexts_path, alignment_path],
        [output_path],
        "complete" if not violations else "failed_invariants",
        dry_run=False,
        details=details,
    )
    write_json(output_path.parent / "stage_manifest.json", manifest)
    if violations:
        raise GateError(
            f"fact registry failed {len(violations)} validation invariants"
        )
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--stop-contexts", type=Path, required=True)
    parser.add_argument("--alignment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(
            facts_path=args.facts,
            stop_contexts_path=args.stop_contexts,
            alignment_path=args.alignment,
            output_path=args.output,
        )
    except GateError as exc:
        parser.error(str(exc))
    print(result)
    return 0

