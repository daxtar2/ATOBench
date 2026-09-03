from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    load_jsonl,
    read_json,
    read_json_or_yaml,
    stage_manifest,
    utc_now,
    write_json,
)
from .judge_episode import DIMENSIONS
from .judge_runner import run_packet
from .stop_descriptors import derive_stop_descriptors


EXPECTED_EPISODES = 12
EXPECTED_PACKETS = EXPECTED_EPISODES * len(DIMENSIONS)
CALLS_PER_PACKET = 4


def _load_frozen_plan(
    call_plan_path: Path,
    packet_root: Path,
) -> list[dict[str, Any]]:
    rows = load_jsonl(call_plan_path)
    if len(rows) != EXPECTED_PACKETS:
        raise GateError(
            f"engineering pilot requires {EXPECTED_PACKETS} packets, got {len(rows)}"
        )
    by_episode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    packet_ids: set[str] = set()
    for row in rows:
        packet_id = str(row.get("packet_id") or "")
        episode = str(row.get("episode_pseudonym") or "")
        dimension = str(row.get("dimension") or "")
        relative_path = str(row.get("relative_path") or "")
        if (
            not packet_id
            or not episode
            or dimension not in DIMENSIONS
            or not relative_path
        ):
            raise GateError(f"invalid engineering-pilot call-plan row: {row}")
        if packet_id in packet_ids:
            raise GateError(f"duplicate packet in call plan: {packet_id}")
        packet_ids.add(packet_id)
        by_episode[episode].append(row)
        packet = read_json(packet_root / relative_path / "packet.json")
        if (
            packet.get("packet_id") != packet_id
            or packet.get("episode_pseudonym") != episode
            or packet.get("dimension") != dimension
        ):
            raise GateError(f"call-plan packet mismatch: {relative_path}")
    if len(by_episode) != EXPECTED_EPISODES:
        raise GateError(
            f"engineering pilot requires {EXPECTED_EPISODES} episodes, "
            f"got {len(by_episode)}"
        )
    for episode, episode_rows in by_episode.items():
        dimensions = [str(row["dimension"]) for row in episode_rows]
        if len(dimensions) != len(DIMENSIONS) or set(dimensions) != set(DIMENSIONS):
            raise GateError(f"incomplete dimension set for {episode}")
    dimension_order = {dimension: index for index, dimension in enumerate(DIMENSIONS)}
    return sorted(
        rows,
        key=lambda row: (
            str(row["episode_pseudonym"]),
            dimension_order[str(row["dimension"])],
        ),
    )


def _receipt_summary(output_dir: Path) -> dict[str, Any]:
    receipts = [
        read_json(path)
        for path in sorted((output_dir / "receipts").glob("*.json"))
    ]
    return {
        "receipt_count": len(receipts),
        "cost_usd": sum(float(row.get("cost_usd") or 0) for row in receipts),
        "call_duration_seconds": sum(
            float(row.get("duration_seconds") or 0) for row in receipts
        ),
    }


def _result_row(
    row: dict[str, Any],
    bundle: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    reviews = list(bundle["reviewer_outputs"].values())
    verifiers = list(bundle["evidence_verifier_outputs"].values())
    score_a = reviews[0].get("score")
    score_b = reviews[1].get("score")
    both_scores = score_a is not None and score_b is not None
    receipt = _receipt_summary(output_dir)
    return {
        "episode_pseudonym": row["episode_pseudonym"],
        "dimension": row["dimension"],
        "packet_id": row["packet_id"],
        "output_dir": str(output_dir.resolve()),
        "invocation_count": int(bundle["invocation_count"]),
        "cost_usd": receipt["cost_usd"],
        "call_duration_seconds": receipt["call_duration_seconds"],
        "score_a": score_a,
        "score_b": score_b,
        "score_difference": abs(float(score_a) - float(score_b))
        if both_scores
        else None,
        "exact_score_agreement": score_a == score_b if both_scores else None,
        "within_one_point": abs(float(score_a) - float(score_b)) <= 1
        if both_scores
        else None,
        "band_agreement": reviews[0].get("band") == reviews[1].get("band"),
        "verifier_statuses": [value.get("status") for value in verifiers],
        "adjudication_trigger": bundle.get("adjudication_trigger"),
        "final": bundle["final"],
    }


def _rate(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [row[key] for row in rows if row.get(key) is not None]
    if not values:
        return None
    return sum(bool(value) for value in values) / len(values)


def _agreement_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    verifier_counts = Counter(
        status for row in rows for status in row["verifier_statuses"]
    )
    trigger_counts = Counter(str(row["adjudication_trigger"]) for row in rows)
    return {
        "packet_count": len(rows),
        "exact_score_agreement_rate": _rate(rows, "exact_score_agreement"),
        "within_one_point_rate": _rate(rows, "within_one_point"),
        "band_agreement_rate": _rate(rows, "band_agreement"),
        "verifier_status_counts": dict(sorted(verifier_counts.items())),
        "trigger_counts": dict(sorted(trigger_counts.items())),
    }


def _stop_decision_descriptor_projection(
    call_plan_rows: list[dict[str, Any]],
    packet_root: Path,
) -> dict[str, Any]:
    descriptor_rows: list[dict[str, Any]] = []
    for row in call_plan_rows:
        if row["dimension"] != "stop_decision":
            continue
        packet_dir = packet_root / str(row["relative_path"])
        packet = read_json(packet_dir / "packet.json")
        facts = read_json(packet_dir / "packet_evidence" / "facts.json")
        stop_context = read_json(packet_dir / "packet_evidence" / "stop_context.json")
        descriptor = derive_stop_descriptors(packet, facts, stop_context)
        descriptor_rows.append(
            {
                "packet_id": descriptor["packet_id"],
                "episode_pseudonym": descriptor["episode_pseudonym"],
                "aou": descriptor["aou"],
                "readiness_state": descriptor["derived_descriptors"]["readiness_state"],
                "stop_fit": descriptor["derived_descriptors"]["stop_fit"],
                "confidence": descriptor["derived_descriptors"]["confidence"],
                "primary_measurement_status": descriptor["primary_evidence"]["measurement_status"],
                "explicit_conflict_count": descriptor["stop_context"]["explicit_conflict_count"],
                "unavailable_verification_fact_count": descriptor["stop_context"][
                    "unavailable_verification_fact_count"
                ],
            }
        )
    readiness_counts = Counter(row["readiness_state"] for row in descriptor_rows)
    stop_fit_counts = Counter(row["stop_fit"] for row in descriptor_rows)
    confidence_counts = Counter(row["confidence"] for row in descriptor_rows)
    by_aou: dict[str, dict[str, Any]] = {}
    for aou in sorted({row["aou"] for row in descriptor_rows}):
        subset = [row for row in descriptor_rows if row["aou"] == aou]
        by_aou[aou] = {
            "packet_count": len(subset),
            "readiness_state_counts": dict(
                sorted(Counter(row["readiness_state"] for row in subset).items())
            ),
            "stop_fit_counts": dict(
                sorted(Counter(row["stop_fit"] for row in subset).items())
            ),
        }
    return {
        "schema_version": "atobench.stop_decision_descriptor_projection.v1",
        "packet_count": len(descriptor_rows),
        "readiness_state_counts": dict(sorted(readiness_counts.items())),
        "stop_fit_counts": dict(sorted(stop_fit_counts.items())),
        "confidence_counts": dict(sorted(confidence_counts.items())),
        "by_aou": by_aou,
        "results": descriptor_rows,
    }


def _summary(
    rows: list[dict[str, Any]],
    *,
    started_at: str,
    complete: bool,
    elapsed_seconds: float,
    call_plan_rows: list[dict[str, Any]],
    packet_root: Path,
) -> dict[str, Any]:
    by_dimension = {
        dimension: _agreement_summary(
            [row for row in rows if row["dimension"] == dimension]
        )
        for dimension in DIMENSIONS
    }
    return {
        "schema_version": "atobench.engineering_pilot_summary.v1",
        "started_at": started_at,
        "updated_at": utc_now(),
        "complete": complete,
        "planned_episode_count": EXPECTED_EPISODES,
        "planned_packet_count": EXPECTED_PACKETS,
        "planned_invocation_count": EXPECTED_PACKETS * CALLS_PER_PACKET,
        "completed_packet_count": len(rows),
        "invocation_count": sum(int(row["invocation_count"]) for row in rows),
        "known_cost_usd": sum(float(row["cost_usd"]) for row in rows),
        "elapsed_seconds_this_process": elapsed_seconds,
        "agreement": _agreement_summary(rows),
        "agreement_by_dimension": by_dimension,
        "stop_decision_descriptor_projection": _stop_decision_descriptor_projection(
            call_plan_rows, packet_root
        ),
        "results": rows,
    }


def run(
    *,
    packet_root: Path,
    call_plan_path: Path,
    output_root: Path,
    config_path: Path,
    lock_path: Path,
    agents_dir: Path,
    allow_judge_calls: bool,
    dry_run: bool,
) -> dict[str, Any]:
    rows = _load_frozen_plan(call_plan_path, packet_root)
    packet_manifest = read_json(packet_root / "packet_manifest.json")
    if (
        int(packet_manifest.get("packet_count", -1)) != EXPECTED_PACKETS
        or int(packet_manifest.get("leakage_hit_count", -1)) != 0
        or int(packet_manifest.get("secret_hit_count", -1)) != 0
    ):
        raise GateError("packet manifest does not pass engineering-pilot QA")
    lock = read_json_or_yaml(lock_path)
    if lock.get("engineering_pilot_authorized") is not True:
        raise GateError("engineering pilot is not authorized by the selected lock")
    if dry_run:
        return {
            "schema_version": "atobench.engineering_pilot_plan.v1",
            "status": "dry_run",
            "episode_count": EXPECTED_EPISODES,
            "packet_count": EXPECTED_PACKETS,
            "calls_per_packet": CALLS_PER_PACKET,
            "planned_invocation_count": EXPECTED_PACKETS * CALLS_PER_PACKET,
            "adjudicator_calls": 0,
            "claude_calls_made": 0,
        }

    output_root.mkdir(parents=True, exist_ok=True)
    progress_path = output_root / "engineering_pilot_progress.json"
    started_at = utc_now()
    process_start = time.monotonic()
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        output_dir = (
            output_root
            / str(row["episode_pseudonym"])
            / str(row["dimension"])
        )
        completed = output_dir / "judgment_bundle.json"
        incomplete = output_dir / "RUN_INCOMPLETE.json"
        if completed.is_file():
            bundle = read_json(completed)
            if (
                bundle.get("packet_id") != row["packet_id"]
                or bundle.get("dimension") != row["dimension"]
            ):
                raise GateError(f"completed output mismatch: {output_dir}")
        elif incomplete.is_file():
            raise GateError(
                "current packet stopped mid-run; preserve it for receipt recovery "
                f"before retrying: {output_dir}"
            )
        else:
            bundle = run_packet(
                packet_dir=packet_root / str(row["relative_path"]),
                output_dir=output_dir,
                config_path=config_path,
                lock_path=lock_path,
                agents_dir=agents_dir,
                allow_judge_calls=allow_judge_calls,
                synthetic_smoke=False,
                allow_adjudication=False,
            )
        if int(bundle.get("invocation_count", -1)) != CALLS_PER_PACKET:
            raise GateError(f"packet did not use exactly four calls: {row['packet_id']}")
        results.append(_result_row(row, bundle, output_dir))
        progress = _summary(
            results,
            started_at=started_at,
            complete=False,
            elapsed_seconds=time.monotonic() - process_start,
            call_plan_rows=rows,
            packet_root=packet_root,
        )
        write_json(progress_path, progress)
        print(
            json.dumps(
                {
                    "completed": index,
                    "total": EXPECTED_PACKETS,
                    "packet_id": row["packet_id"],
                    "dimension": row["dimension"],
                    "known_cost_usd": progress["known_cost_usd"],
                },
                sort_keys=True,
            ),
            flush=True,
        )

    summary = _summary(
        results,
        started_at=started_at,
        complete=True,
        elapsed_seconds=time.monotonic() - process_start,
        call_plan_rows=rows,
        packet_root=packet_root,
    )
    summary_path = output_root / "engineering_pilot_summary.json"
    write_json(progress_path, summary)
    write_json(summary_path, summary)
    manifest = stage_manifest(
        "08l_run_engineering_pilot",
        [
            call_plan_path,
            packet_root / "packet_manifest.json",
            config_path,
            lock_path,
            *[
                packet_root / str(row["relative_path"]) / "packet.json"
                for row in rows
            ],
        ],
        [
            summary_path,
            progress_path,
            *[
                Path(row["output_dir"]) / "judgment_bundle.json"
                for row in results
            ],
        ],
        "complete",
        dry_run=False,
        details={
            "episode_count": EXPECTED_EPISODES,
            "packet_count": EXPECTED_PACKETS,
            "invocation_count": EXPECTED_PACKETS * CALLS_PER_PACKET,
            "known_cost_usd": summary["known_cost_usd"],
        },
    )
    write_json(output_root / "stage_manifest.json", manifest)
    return summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the resumable 12-episode ATOBench engineering pilot"
    )
    parser.add_argument("--packet-root", type=Path, required=True)
    parser.add_argument("--call-plan", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--agents-dir", type=Path, required=True)
    parser.add_argument("--allow-judge-calls", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            packet_root=args.packet_root,
            call_plan_path=args.call_plan,
            output_root=args.output_root,
            config_path=args.config,
            lock_path=args.lock,
            agents_dir=args.agents_dir,
            allow_judge_calls=args.allow_judge_calls,
            dry_run=args.dry_run,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
