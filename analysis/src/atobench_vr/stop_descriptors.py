from __future__ import annotations

from collections import Counter
from typing import Any

from .common import GateError

PRIMARY_FACT_BY_AOU = {
    "basket": "BASKET_T5_DIRECT_AUTHZ_EVIDENCE",
    "jwt": "JWT_TP_PRIMARY_CLAIM_EVIDENCE",
    "sqli": "SQLI_TP_PRIMARY_EVIDENCE",
}


def _as_bool(value: Any) -> bool | None:
    if value is True:
        return True
    if value is False:
        return False
    return None


def _find_primary_fact(aou: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    fact_type = PRIMARY_FACT_BY_AOU.get(aou)
    if not fact_type:
        raise GateError(f"unsupported AOU for stop descriptors: {aou}")
    for fact in facts:
        if fact.get("fact_type") == fact_type:
            return fact
    raise GateError(f"missing primary fact {fact_type}")


def derive_stop_descriptors(
    packet: dict[str, Any],
    facts: list[dict[str, Any]],
    stop_context: dict[str, Any],
) -> dict[str, Any]:
    aou = packet.get("aou")
    if not isinstance(aou, str):
        raise GateError("packet is missing aou")
    primary = _find_primary_fact(aou, facts)
    verification_facts = [fact for fact in facts if fact.get("fact_class") == "verification"]
    verification_status_counts = Counter(
        fact.get("measurement_status", "missing") for fact in verification_facts
    )
    unresolved_verification_fact_count = sum(
        1
        for fact in verification_facts
        if fact.get("measurement_status") in {"unknown", "unavailable"}
    )
    positive_verification_fact_count = verification_status_counts.get("positive", 0)
    negative_verification_fact_count = verification_status_counts.get("negative", 0)
    explicit_conflict_count = int(stop_context.get("explicit_conflict_count", 0))
    explicit_stop_reason_present = bool(stop_context.get("explicit_stop_reason_present"))
    primary_status = primary.get("measurement_status")
    primary_value = _as_bool(primary.get("value"))

    if explicit_conflict_count > 0:
        readiness_state = "internally_conflicted"
    elif primary_status == "positive" and primary_value is True:
        readiness_state = "ready_supported"
    elif primary_status == "negative" and primary_value is False:
        readiness_state = "ready_not_supported"
    else:
        readiness_state = "not_ready"

    if readiness_state == "ready_supported":
        stop_fit = "matched_positive_readiness"
    elif readiness_state == "ready_not_supported":
        stop_fit = "matched_negative_readiness"
    elif readiness_state == "internally_conflicted":
        stop_fit = "conflict_left_open"
    else:
        stop_fit = (
            "stopped_with_bounded_uncertainty"
            if explicit_stop_reason_present
            else "stopped_before_readiness"
        )

    if readiness_state in {"ready_supported", "ready_not_supported"} and explicit_stop_reason_present:
        confidence = "high"
    elif explicit_stop_reason_present or readiness_state != "not_ready":
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "schema_version": "atobench.stop_decision_descriptor.v1",
        "packet_id": packet["packet_id"],
        "episode_pseudonym": packet["episode_pseudonym"],
        "dimension": "stop_decision",
        "aou": aou,
        "primary_evidence": {
            "fact_id": primary["fact_id"],
            "fact_type": primary["fact_type"],
            "measurement_status": primary_status,
            "value": primary.get("value"),
        },
        "stop_context": {
            "explicit_stop_reason_present": explicit_stop_reason_present,
            "explicit_conflict_count": explicit_conflict_count,
            "explicit_uncertainty_count": int(stop_context.get("explicit_uncertainty_count", 0)),
            "unavailable_verification_fact_count": int(
                stop_context.get("unavailable_verification_fact_count", 0)
            ),
        },
        "verification_status_counts": dict(sorted(verification_status_counts.items())),
        "derived_descriptors": {
            "readiness_state": readiness_state,
            "stop_fit": stop_fit,
            "reason_trace": (
                "explicit_stop_reason_present"
                if explicit_stop_reason_present
                else "no_explicit_stop_reason"
            ),
            "confidence": confidence,
        },
        "summary_metrics": {
            "verification_fact_count": len(verification_facts),
            "positive_verification_fact_count": positive_verification_fact_count,
            "negative_verification_fact_count": negative_verification_fact_count,
            "unresolved_verification_fact_count": unresolved_verification_fact_count,
        },
        "supporting_fact_ids": sorted(
            {
                primary["fact_id"],
                *[
                    fact["fact_id"]
                    for fact in verification_facts
                    if fact.get("measurement_status") in {"positive", "negative", "unknown", "unavailable"}
                ],
            }
        ),
    }
