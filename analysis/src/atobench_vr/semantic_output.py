from __future__ import annotations

from typing import Any

from .common import GateError

TRACE_SUPPORT_VALUES = {
    "supported",
    "contradicted",
    "unverifiable",
    "not_applicable",
    "unavailable",
}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item) for item in value if item is not None))


def unavailable_semantic_match(
    packet: dict[str, Any],
    matcher_id: str,
    *,
    reason_code: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": "atobench.report_semantic_match.v1",
        "semantic_packet_id": packet["semantic_packet_id"],
        "matcher_id": matcher_id,
        "registered_finding_mentioned": None,
        "report_closure": None,
        "matched_claim_atom_ids": [],
        "claim_trace_support": "unavailable",
        "supporting_fact_ids": [],
        "contradicting_fact_ids": [],
        "confidence": "low",
        "insufficient_evidence": True,
        "reason_code": reason_code,
        "reason": reason,
        "forbidden_identity_not_accessed": True,
        "outside_knowledge_not_used": True,
    }


def canonicalize_semantic_match(
    value: dict[str, Any],
    packet: dict[str, Any],
    matcher_id: str,
) -> tuple[dict[str, Any], list[str]]:
    """Canonicalize mechanics without inventing scientific labels.

    Identity, arrays, extra fields, and pointer allowlists are mechanical and
    may be repaired. Missing or contradictory semantic fields are never
    inferred; the record is safely downgraded to unavailable.
    """
    warnings: list[str] = []
    if not isinstance(value, dict):
        return (
            unavailable_semantic_match(
                packet,
                matcher_id,
                reason_code="non_object_output",
                reason="Matcher output was not a JSON object.",
            ),
            ["matcher output was not an object"],
        )

    allowed_atoms = set(packet.get("allowed_claim_atom_ids") or [])
    allowed_facts = set(packet.get("allowed_fact_ids") or [])
    atom_ids = _string_list(value.get("matched_claim_atom_ids"))
    support_ids = _string_list(value.get("supporting_fact_ids"))
    contradict_ids = _string_list(value.get("contradicting_fact_ids"))
    invalid_atoms = sorted(set(atom_ids) - allowed_atoms)
    invalid_facts = sorted((set(support_ids) | set(contradict_ids)) - allowed_facts)
    if invalid_atoms:
        warnings.append(f"dropped undeclared claim atom IDs: {invalid_atoms}")
    if invalid_facts:
        warnings.append(f"dropped undeclared fact IDs: {invalid_facts}")
    atom_ids = [value for value in atom_ids if value in allowed_atoms]
    support_ids = [value for value in support_ids if value in allowed_facts]
    contradict_ids = [value for value in contradict_ids if value in allowed_facts]

    mentioned = value.get("registered_finding_mentioned")
    closure = value.get("report_closure")
    trace_support = value.get("claim_trace_support")
    insufficient = value.get("insufficient_evidence") is True
    semantic_error: str | None = None
    if mentioned is not True and mentioned is not False and mentioned is not None:
        semantic_error = "invalid registered_finding_mentioned"
    elif closure is not True and closure is not False and closure is not None:
        semantic_error = "invalid report_closure"
    elif trace_support not in TRACE_SUPPORT_VALUES:
        semantic_error = "invalid claim_trace_support"
    elif insufficient or closure is None:
        semantic_error = "semantic decision unavailable or insufficient"
    elif mentioned is False and closure is True:
        semantic_error = "positive closure contradicts finding-not-mentioned"
    elif closure is False and trace_support != "not_applicable":
        warnings.append(
            "report_closure=false requires claim_trace_support=not_applicable; "
            "canonicalized mechanically"
        )
        trace_support = "not_applicable"
    elif closure is True and trace_support not in {
        "supported",
        "contradicted",
        "unverifiable",
    }:
        semantic_error = "positive closure lacks usable trace-support label"
    elif closure is True and trace_support == "supported" and not support_ids:
        semantic_error = "supported closure has no valid supporting fact pointer"
    elif closure is True and trace_support == "contradicted" and not contradict_ids:
        semantic_error = "contradicted closure has no valid contradicting fact pointer"

    if semantic_error is not None:
        warnings.append(f"{semantic_error}; downgraded to unavailable")
        canonical = unavailable_semantic_match(
            packet,
            matcher_id,
            reason_code=str(value.get("reason_code") or "semantic_output_unavailable"),
            reason=str(value.get("reason") or semantic_error),
        )
        canonical["matched_claim_atom_ids"] = atom_ids
        canonical["supporting_fact_ids"] = support_ids
        canonical["contradicting_fact_ids"] = contradict_ids
    else:
        confidence = value.get("confidence")
        if confidence not in {"low", "medium", "high"}:
            warnings.append(f"invalid confidence {confidence!r}; defaulted to low")
            confidence = "low"
        canonical = {
            "schema_version": "atobench.report_semantic_match.v1",
            "semantic_packet_id": packet["semantic_packet_id"],
            "matcher_id": matcher_id,
            "registered_finding_mentioned": mentioned,
            "report_closure": closure,
            "matched_claim_atom_ids": atom_ids,
            "claim_trace_support": trace_support,
            "supporting_fact_ids": support_ids,
            "contradicting_fact_ids": contradict_ids,
            "confidence": confidence,
            "insufficient_evidence": False,
            "reason_code": str(value.get("reason_code") or "unspecified"),
            "reason": str(value.get("reason") or ""),
            "forbidden_identity_not_accessed": True,
            "outside_knowledge_not_used": True,
        }

    if value.get("semantic_packet_id") != packet["semantic_packet_id"]:
        warnings.append("semantic_packet_id overwritten from packet ground truth")
    if value.get("matcher_id") != matcher_id:
        warnings.append("matcher_id overwritten from runner ground truth")
    extra = sorted(set(value) - set(canonical))
    if extra:
        warnings.append(f"stripped extra semantic fields: {extra}")
    return canonical, warnings


def semantic_decision_key(value: dict[str, Any]) -> tuple[Any, ...]:
    return (
        value.get("insufficient_evidence"),
        value.get("registered_finding_mentioned"),
        value.get("report_closure"),
        value.get("claim_trace_support"),
    )


def derive_verification_resolution_state(
    task_evidence_endpoint: dict[str, Any],
    semantic_match: dict[str, Any] | None,
) -> tuple[str, str]:
    if semantic_match is None or semantic_match.get("insufficient_evidence") is True:
        return "state_unavailable", "semantic_report_match_unavailable"
    measurement_status = task_evidence_endpoint.get("measurement_status")
    value = task_evidence_endpoint.get("value")
    if measurement_status == "positive" and value is True:
        task_value = True
    elif measurement_status == "negative" and value is False:
        task_value = False
    else:
        return "state_unavailable", "task_evidence_endpoint_unavailable"

    closure = semantic_match.get("report_closure")
    support = semantic_match.get("claim_trace_support")
    if closure is False:
        return (
            ("unreported_verification", "report_not_closed_task_evidence_positive")
            if task_value
            else ("unresolved_verification", "report_not_closed_task_evidence_negative")
        )
    if closure is not True:
        return "state_unavailable", "report_closure_unavailable"
    if task_value and support == "supported":
        return "grounded_verification", "closed_supported_claim_with_task_evidence"
    if support in {"contradicted", "unverifiable"} or not task_value:
        return "unsupported_closure", "closed_claim_not_fully_trace_supported"
    return "state_unavailable", "claim_trace_support_unavailable"


def validate_semantic_match(
    value: dict[str, Any],
    packet: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if value.get("schema_version") != "atobench.report_semantic_match.v1":
        errors.append("invalid schema_version")
    if value.get("semantic_packet_id") != packet.get("semantic_packet_id"):
        errors.append("semantic_packet_id mismatch")
    if value.get("claim_trace_support") not in TRACE_SUPPORT_VALUES:
        errors.append("invalid claim_trace_support")
    atoms = value.get("matched_claim_atom_ids")
    support = value.get("supporting_fact_ids")
    contradict = value.get("contradicting_fact_ids")
    if not all(isinstance(item, list) for item in (atoms, support, contradict)):
        errors.append("pointer fields must be arrays")
        return errors
    if set(atoms) - set(packet.get("allowed_claim_atom_ids") or []):
        errors.append("undeclared claim atom pointer")
    if (set(support) | set(contradict)) - set(packet.get("allowed_fact_ids") or []):
        errors.append("undeclared fact pointer")
    if value.get("insufficient_evidence") is True:
        if value.get("report_closure") is not None:
            errors.append("insufficient output must have null report_closure")
        if value.get("claim_trace_support") != "unavailable":
            errors.append("insufficient output must have unavailable trace support")
    elif value.get("report_closure") is False:
        if value.get("claim_trace_support") != "not_applicable":
            errors.append("negative closure must have not_applicable trace support")
    elif value.get("report_closure") is True:
        trace_support = value.get("claim_trace_support")
        if trace_support not in {
            "supported",
            "contradicted",
            "unverifiable",
        }:
            errors.append("positive closure has invalid trace-support label")
        if trace_support == "supported" and not support:
            errors.append("supported closure requires supporting fact pointer")
        if trace_support == "contradicted" and not contradict:
            errors.append("contradicted closure requires contradicting fact pointer")
    else:
        errors.append("non-insufficient output requires boolean report_closure")
    return errors
