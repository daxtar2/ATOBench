from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    read_json,
    sha256_file,
    stage_manifest,
    utc_now,
    write_json,
)
from .judge_runner import (
    AGENT_FILES,
    RunnerConfig,
    _adjudication_schema,
    _canonicalize_verifier_pointer_partition,
    _check_authorization,
    _check_packet,
    _consensus,
    _make_workspace_read_only,
    _materialize_verifier_workspace,
    _materialize_workspace,
    _needs_adjudication,
    _pointer_ids,
    _retrieve_cited_evidence,
    _run_invocation,
    _validate_review_pointers,
    _validate_adjudication_output,
    _validate_verifier_output,
)
from .schemas import validate_judgment


def run(
    *,
    packet_dir: Path,
    prior_bundle_path: Path,
    output_dir: Path,
    config_path: Path,
    lock_path: Path,
    agents_dir: Path,
    allow_judge_calls: bool,
    new_version: bool,
) -> dict[str, Any]:
    packet = _check_packet(packet_dir)
    _check_authorization(lock_path, allow_judge_calls, synthetic_smoke=False)
    config = RunnerConfig.load(config_path)
    ensure_output_available(output_dir, new_version)
    prior = read_json(prior_bundle_path)
    if prior.get("packet_id") != packet["packet_id"]:
        raise GateError("prior judgment bundle packet identity mismatch")
    reviews = prior.get("reviewer_outputs") or {}
    if set(reviews) != set(config.reviewer_ids):
        raise GateError("prior bundle does not contain both configured frozen reviews")
    for review in reviews.values():
        errors = validate_judgment(review)
        if errors:
            raise GateError(f"prior frozen review is invalid: {errors}")
        _validate_review_pointers(review, packet)

    packet_hash = sha256_file(packet_dir / "packet.json")
    verifier_spec = agents_dir / AGENT_FILES["verifier"]
    verifier_hash = sha256_file(verifier_spec)
    verifier_outputs: dict[str, dict[str, Any]] = {}
    receipts: list[dict[str, Any]] = []
    deterministic_retrieval: dict[str, dict[str, Any]] = {}
    for reviewer_id in config.reviewer_ids:
        review = reviews[reviewer_id]
        pointer_ids = _pointer_ids(review)
        request = {
            "packet_id": packet["packet_id"],
            "dimension": packet["dimension"],
            "review_claim": review["reason_code"],
            "material_strengths": review["material_strengths"],
            "material_deficiencies": review["material_deficiencies"],
            "pointer_ids": pointer_ids,
            "numeric_score_withheld": True,
            "replay_of_frozen_review": reviewer_id,
        }
        cited_evidence = _retrieve_cited_evidence(packet_dir, pointer_ids)
        deterministic_retrieval[reviewer_id] = {
            "requested_pointer_count": len(pointer_ids),
            "retrieved_pointer_count": sum(bool(value) for value in cited_evidence.values()),
            "deterministically_missing_pointer_ids": sorted(
                key for key, value in cited_evidence.items() if not value
            ),
        }
        temporary, workspace = _materialize_verifier_workspace(
            packet_dir, verifier_spec, request, cited_evidence
        )
        try:
            verifier, receipt, _ = _run_invocation(
                config=config,
                workspace=workspace,
                agent_name="atobench-evidence-verifier",
                prompt=(
                    "Read the exact files verification_request.json, cited_evidence.json, "
                    "rubric.md, and output_schema.json. Verify semantic entailment of "
                    "the reason and every material strength and deficiency using only "
                    "cited_evidence.json, without assigning or inferring a score. "
                    "Pointer presence is not semantic entailment. A report fact or "
                    "final-report excerpt proves only what the report says and cannot "
                    "by itself prove trace grounding. Return JSON only."
                ),
                schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "status",
                        "reason",
                        "verified_pointer_ids",
                        "missing_pointer_ids",
                    ],
                    "properties": {
                        "status": {
                            "enum": [
                                "entailed",
                                "partially_entailed",
                                "not_entailed",
                                "unavailable",
                            ]
                        },
                        "reason": {"type": "string"},
                        "verified_pointer_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "missing_pointer_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                invocation_id=(
                    f"{packet['packet_id']}:{packet['dimension']}:"
                    f"{reviewer_id}:evidence_replay"
                ),
                spec_hash=verifier_hash,
                packet_hash=packet_hash,
            )
        finally:
            temporary.cleanup()
        warnings = _validate_verifier_output(verifier)
        if warnings:
            raise GateError(f"invalid raw verifier output in replay: {warnings}")
        receipt["model_reported_pointer_partition"] = {
            "verified_pointer_ids": verifier["verified_pointer_ids"],
            "missing_pointer_ids": verifier["missing_pointer_ids"],
        }
        verifier = _canonicalize_verifier_pointer_partition(
            verifier, cited_evidence
        )
        warnings = _validate_verifier_output(verifier, pointer_ids)
        if warnings:
            raise GateError(f"invalid canonical verifier output in replay: {warnings}")
        verifier_outputs[reviewer_id] = verifier
        receipts.append(receipt)

    a = reviews[config.reviewer_ids[0]]
    b = reviews[config.reviewer_ids[1]]
    adjudicate, trigger = _needs_adjudication(a, b, verifier_outputs)
    adjudication = None
    if adjudicate:
        adjudicator_spec = agents_dir / AGENT_FILES["adjudicator"]
        request = {
            "packet_id": packet["packet_id"],
            "dimension": packet["dimension"],
            "trigger": trigger,
            "reviews": reviews,
            "evidence_verifier_outputs": verifier_outputs,
            "replay_reason": "prior evidence verifier inputs were not read",
        }
        temporary, workspace = _materialize_workspace(
            packet_dir,
            adjudicator_spec,
            "atobench-judge-adjudicator",
            {"adjudication_request.json": request},
        )
        try:
            adjudication, receipt, _ = _run_invocation(
                config=config,
                workspace=workspace,
                agent_name="atobench-judge-adjudicator",
                prompt=(
                    "Read adjudication_request.json and the packet files. Re-adjudicate "
                    "the frozen reviews using the corrected evidence-verifier outputs. "
                    "Return schema-valid JSON only."
                ),
                schema=_adjudication_schema(read_json(packet_dir / "output_schema.json")),
                invocation_id=(
                    f"{packet['packet_id']}:{packet['dimension']}:"
                    "adjudication_after_evidence_replay"
                ),
                spec_hash=sha256_file(adjudicator_spec),
                packet_hash=packet_hash,
            )
        finally:
            temporary.cleanup()
        base = _validate_adjudication_output(adjudication, packet)
        _validate_review_pointers(base, packet)
        receipts.append(receipt)
        final = {
            "method": "adjudicated_after_evidence_replay",
            "score": base["score"],
            "score_low": base["score_low"],
            "score_high": base["score_high"],
            "band": base["band"],
            "insufficient_evidence": base["insufficient_evidence"],
        }
    else:
        final = _consensus(a, b)

    bundle = {
        "schema_version": "atobench.trajectory_judgment_bundle.v1",
        "packet_id": packet["packet_id"],
        "dimension": packet["dimension"],
        "reviewer_outputs": reviews,
        "evidence_verifier_outputs": verifier_outputs,
        "deterministic_evidence_retrieval": deterministic_retrieval,
        "adjudication_triggered": adjudicate,
        "adjudication_trigger": trigger,
        "adjudication_output": adjudication,
        "final": final,
        "repair_of": str(prior_bundle_path.resolve()),
        "frozen_reviews_reused": True,
        "repair_invocation_count": len(receipts),
        "prior_invocation_count": prior.get("invocation_count"),
        "created_at": utc_now(),
    }
    receipt_dir = output_dir / "receipts"
    verifier_dir = output_dir / "evidence_verifiers"
    receipt_dir.mkdir(parents=True)
    verifier_dir.mkdir(parents=True)
    for reviewer_id, value in verifier_outputs.items():
        write_json(verifier_dir / f"{reviewer_id}.json", value)
    for index, receipt in enumerate(receipts, 1):
        write_json(receipt_dir / f"{index:02d}_{receipt['agent_name']}.json", receipt)
    if adjudication:
        write_json(output_dir / "adjudication.json", adjudication)
    write_json(output_dir / "judgment_bundle.json", bundle)
    manifest = stage_manifest(
        "08c_replay_evidence_verifiers",
        [
            packet_dir / "packet.json",
            prior_bundle_path,
            config_path,
            lock_path,
            verifier_spec,
            agents_dir / AGENT_FILES["adjudicator"],
        ],
        [output_dir / "judgment_bundle.json", *sorted(receipt_dir.glob("*.json"))],
        "complete",
        dry_run=False,
        details={
            "packet_id": packet["packet_id"],
            "frozen_reviews_reused": True,
            "repair_invocation_count": len(receipts),
            "adjudication_replayed": adjudicate,
        },
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return bundle


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--prior-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--agents-dir", type=Path, required=True)
    parser.add_argument("--allow-judge-calls", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            packet_dir=args.packet_dir,
            prior_bundle_path=args.prior_bundle,
            output_dir=args.output,
            config_path=args.config,
            lock_path=args.lock,
            agents_dir=args.agents_dir,
            allow_judge_calls=args.allow_judge_calls,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
