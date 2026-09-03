from __future__ import annotations

from typing import Any

DIMENSIONS = {"verification_control", "stop_decision", "report_grounding"}
BANDS = {"poor": range(1, 4), "limited": range(4, 7), "good": range(7, 9), "excellent": range(9, 11)}


def validate_judgment(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {
        "schema_version", "packet_id", "dimension", "score", "score_low",
        "score_high", "band", "confidence", "insufficient_evidence",
        "reason_code", "material_strengths", "material_deficiencies",
        "supporting_message_ids", "supporting_event_ids", "supporting_fact_ids",
        "forbidden_identity_not_accessed", "outside_knowledge_not_used",
    }
    missing = sorted(required - value.keys())
    if missing:
        errors.append(f"missing fields: {missing}")
        return errors
    if value["schema_version"] != "atobench.trajectory_judgment.v1":
        errors.append("wrong schema_version")
    if value["dimension"] not in DIMENSIONS:
        errors.append("invalid dimension")
    insufficient = value["insufficient_evidence"]
    score = value["score"]
    if insufficient:
        if any(value[k] is not None for k in ("score", "score_low", "score_high", "band")):
            errors.append("insufficient_evidence requires null score/range/band")
    else:
        if not isinstance(score, int) or not 1 <= score <= 10:
            errors.append("score must be integer 1..10")
        band = value["band"]
        if band not in BANDS or score not in BANDS.get(band, ()):
            errors.append("band does not match score")
        pointers = (
            value["supporting_message_ids"]
            + value["supporting_event_ids"]
            + value["supporting_fact_ids"]
        )
        if not pointers:
            errors.append("numeric judgment requires an evidence pointer")
    if value["forbidden_identity_not_accessed"] is not True:
        errors.append("identity-access attestation failed")
    if value["outside_knowledge_not_used"] is not True:
        errors.append("outside-knowledge attestation failed")
    return errors

