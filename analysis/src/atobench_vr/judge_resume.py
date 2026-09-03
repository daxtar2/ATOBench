from __future__ import annotations

import argparse
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
    _claude_version,
    _consensus,
    _materialize_workspace,
    _needs_adjudication,
    _pointer_ids,
    _retrieve_cited_evidence,
    _run_invocation,
    _validate_adjudication_output,
    _validate_review_pointers,
    _validate_verifier_output,
)
from .schemas import validate_judgment


def run(
    *,
    packet_dir: Path,
    incomplete_dir: Path,
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

    marker_path = incomplete_dir / "RUN_INCOMPLETE.json"
    if not marker_path.is_file():
        raise GateError("resume source has no RUN_INCOMPLETE.json marker")
    marker = read_json(marker_path)
    if (
        marker.get("packet_id") != packet["packet_id"]
        or marker.get("dimension") != packet["dimension"]
    ):
        raise GateError("resume source packet identity mismatch")

    reviews = {
        reviewer_id: read_json(incomplete_dir / "reviews" / f"{reviewer_id}.json")
        for reviewer_id in config.reviewer_ids
    }
    for review in reviews.values():
        errors = validate_judgment(review)
        if errors:
            raise GateError(f"invalid frozen review in resume source: {errors}")
        _validate_review_pointers(review, packet)

    verifier_outputs: dict[str, dict[str, Any]] = {}
    for reviewer_id, review in reviews.items():
        raw = read_json(
            incomplete_dir / "raw_evidence_verifiers" / f"{reviewer_id}.json"
        )
        warnings = _validate_verifier_output(raw)
        if warnings:
            raise GateError(f"invalid raw verifier output in resume source: {warnings}")
        pointer_ids = _pointer_ids(review)
        cited_evidence = _retrieve_cited_evidence(packet_dir, pointer_ids)
        canonical = _canonicalize_verifier_pointer_partition(raw, cited_evidence)
        warnings = _validate_verifier_output(canonical, pointer_ids)
        if warnings:
            raise GateError(f"invalid canonical verifier output in resume source: {warnings}")
        verifier_outputs[reviewer_id] = canonical

    prior_receipt_paths = sorted((incomplete_dir / "receipts").glob("*.json"))
    prior_receipts = [read_json(path) for path in prior_receipt_paths]
    if len(prior_receipts) != 4:
        raise GateError(
            f"adjudication resume requires four frozen prior receipts, found "
            f"{len(prior_receipts)}"
        )

    a = reviews[config.reviewer_ids[0]]
    b = reviews[config.reviewer_ids[1]]
    adjudicate, trigger = _needs_adjudication(a, b, verifier_outputs)
    adjudication = None
    new_receipts: list[dict[str, Any]] = []
    stderr = ""
    if adjudicate:
        judgment_schema = read_json(packet_dir / "output_schema.json")
        adjudicator_spec = agents_dir / AGENT_FILES["adjudicator"]
        request = {
            "packet_id": packet["packet_id"],
            "dimension": packet["dimension"],
            "trigger": trigger,
            "reviews": reviews,
            "evidence_verifier_outputs": verifier_outputs,
            "resume_reason": "prior adjudicator output wrapper was not parsed",
        }
        temporary, workspace = _materialize_workspace(
            packet_dir,
            adjudicator_spec,
            "atobench-judge-adjudicator",
            {"adjudication_request.json": request},
        )
        try:
            adjudication, receipt, stderr = _run_invocation(
                config=config,
                workspace=workspace,
                agent_name="atobench-judge-adjudicator",
                prompt=(
                    "Resolve the trigger in adjudication_request.json using only the "
                    "packet, frozen reviews, and verifier outputs. Correct all "
                    "non-entailed assertions. Return schema-valid JSON only."
                ),
                schema=_adjudication_schema(judgment_schema),
                invocation_id=(
                    f"{packet['packet_id']}:{packet['dimension']}:"
                    "adjudication_resume"
                ),
                spec_hash=sha256_file(adjudicator_spec),
                packet_hash=sha256_file(packet_dir / "packet.json"),
            )
        finally:
            temporary.cleanup()
        base = _validate_adjudication_output(adjudication, packet)
        if (
            base["packet_id"] != packet["packet_id"]
            or base["dimension"] != packet["dimension"]
        ):
            raise GateError("identity mismatch in resumed adjudication")
        _validate_review_pointers(base, packet)
        new_receipts.append(receipt)
        final = {
            "method": "adjudicated",
            "score": base["score"],
            "score_low": base["score_low"],
            "score_high": base["score_high"],
            "band": base["band"],
            "insufficient_evidence": base["insufficient_evidence"],
        }
    else:
        final = _consensus(a, b)

    output_dir.mkdir(parents=True, exist_ok=True)
    for reviewer_id, review in reviews.items():
        write_json(output_dir / "reviews" / f"{reviewer_id}.json", review)
        write_json(
            output_dir / "evidence_verifiers" / f"{reviewer_id}.json",
            verifier_outputs[reviewer_id],
        )
    if adjudication is not None:
        write_json(output_dir / "adjudication.json", adjudication)
    all_receipts = [*prior_receipts, *new_receipts]
    for index, receipt in enumerate(all_receipts, 1):
        write_json(
            output_dir / "receipts" / f"{index:02d}_{receipt['agent_name']}.json",
            receipt,
        )
    bundle = {
        "schema_version": "atobench.trajectory_judgment_bundle.v1",
        "packet_id": packet["packet_id"],
        "dimension": packet["dimension"],
        "reviewer_outputs": reviews,
        "evidence_verifier_outputs": verifier_outputs,
        "adjudication_triggered": adjudicate,
        "adjudication_trigger": trigger,
        "adjudication_output": adjudication,
        "final": final,
        "claude_code_version": _claude_version(config.claude_executable),
        "invocation_count": len(all_receipts),
        "resumed_from_incomplete_run": str(incomplete_dir.resolve()),
        "new_invocation_count": len(new_receipts),
        "created_at": utc_now(),
    }
    write_json(output_dir / "judgment_bundle.json", bundle)
    if stderr:
        write_json(output_dir / "stderr_records.json", [{"stderr": stderr}])
    manifest = stage_manifest(
        "08h_resume_incomplete_adjudication",
        [
            packet_dir / "packet.json",
            packet_dir / "rubric.md",
            packet_dir / "output_schema.json",
            marker_path,
            *sorted((incomplete_dir / "reviews").glob("*.json")),
            *sorted((incomplete_dir / "raw_evidence_verifiers").glob("*.json")),
            *prior_receipt_paths,
            config_path,
            lock_path,
            *[agents_dir / name for name in AGENT_FILES.values()],
        ],
        [
            output_dir / "judgment_bundle.json",
            *sorted((output_dir / "receipts").glob("*.json")),
        ],
        "complete",
        dry_run=False,
        details={
            "packet_id": packet["packet_id"],
            "dimension": packet["dimension"],
            "prior_invocation_count": len(prior_receipts),
            "new_invocation_count": len(new_receipts),
            "adjudication_triggered": adjudicate,
        },
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    if output_dir.resolve() == incomplete_dir.resolve() and marker_path.exists():
        marker_path.unlink()
    return bundle


def cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--incomplete-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--agents-dir", type=Path, required=True)
    parser.add_argument("--allow-judge-calls", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args()
    try:
        result = run(
            packet_dir=args.packet_dir,
            incomplete_dir=args.incomplete_dir,
            output_dir=args.output,
            config_path=args.config,
            lock_path=args.lock,
            agents_dir=args.agents_dir,
            allow_judge_calls=args.allow_judge_calls,
            new_version=args.new_version,
        )
    except GateError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    print(result)
    return 0
