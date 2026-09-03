from __future__ import annotations

import json
from typing import Any

from .common import GateError
from .schemas import validate_judgment

# Keys that appear in Claude Code outer envelopes / metadata objects but never
# in a legitimate subagent payload.  A candidate containing any of these is
# treated as an envelope, not as the structured result.
_KNOWN_ENVELOPE_KEYS = frozenset(
    {
        "api_error_status",
        "api_error_message",
        "duration_api_ms",
        "duration_total_ms",
        "session_id",
        "session_dir",
        "total_cost_usd",
        "request_id",
        "invocation_id",
        "claude_code_version",
    }
)

# Target schema versions the runner recognises as its own native payloads.
_NATIVE_SCHEMA_VERSIONS = frozenset(
    {
        "atobench.trajectory_judgment.v1",
        "atobench.stop_decision_descriptor.v1",
        "atobench.alignment_verifier_decision.v1",
        "atobench.report_semantic_match.v1",
    }
)


def _is_envelope(candidate: dict[str, Any]) -> bool:
    return bool(_KNOWN_ENVELOPE_KEYS & candidate.keys())


def _decode_json_strings(text: str) -> list[dict[str, Any]]:
    """Extract every JSON object embedded in a string value."""
    found: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        if text[index] != "{":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        if isinstance(value, dict):
            found.append(value)
        index += end
    return found


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3].strip()
    return text


def _collect_candidates(value: Any) -> list[dict[str, Any]]:
    """Recursively collect every dict nested in JSON value, including inside strings."""
    found: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            found.append(item)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            stripped = _strip_markdown_fences(item.strip())
            if not stripped:
                return
            try:
                visit(json.loads(stripped))
                return
            except json.JSONDecodeError:
                pass
            for obj in _decode_json_strings(stripped):
                visit(obj)

    visit(value)
    return found


def _score_candidate(candidate: dict[str, Any], schema: dict[str, Any] | None) -> int:
    score = 0
    schema_version = candidate.get("schema_version")
    if schema_version in _NATIVE_SCHEMA_VERSIONS:
        score += 100
        if schema is not None and schema_version == schema.get("$id"):
            score += 50
    if _is_envelope(candidate):
        score -= 200
    if schema is not None:
        required = set(schema.get("required") or [])
        if required:
            score += 10 * len(required & candidate.keys())
        properties = set((schema.get("properties") or {}).keys())
        if properties:
            score += len(properties & candidate.keys())
    return score


def extract_structured_output(
    stdout: str,
    schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the best structured payload and the original envelope.

    The function treats stdout as a JSON container.  It recursively searches
    nested objects, arrays, and string values for a schema-matching object and
    ignores known Claude Code envelope metadata.
    """
    stripped = _strip_markdown_fences(stdout.strip())
    try:
        envelope = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise GateError(f"Claude stdout is not JSON: {exc}") from exc

    candidates = _collect_candidates(envelope)
    if not candidates:
        raise GateError("Claude JSON output contains no JSON object")

    scored = sorted(
        ((_score_candidate(candidate, schema), index, candidate) for index, candidate in enumerate(candidates)),
        reverse=True,
    )
    best_score, _, best = scored[0]
    if best_score < 0:
        raise GateError("Claude JSON output contains only envelope metadata")

    if schema is not None:
        required = set(schema.get("required") or [])
        if required and not required.issubset(best):
            # Prefer any candidate that satisfies required fields, even if its
            # overall score is slightly lower.
            for candidate in candidates:
                if required.issubset(candidate) and not _is_envelope(candidate):
                    best = candidate
                    break

    return best, envelope if isinstance(envelope, dict) else {"raw": envelope}


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def canonicalize_verifier_output(
    value: dict[str, Any],
    cited_evidence: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return a canonical verifier output and a list of warnings.

    Extra envelope fields are stripped.  Pointer arrays are coerced to lists of
    strings.  If ``cited_evidence`` is supplied, the pointer partition is
    recomputed deterministically from it.
    """
    warnings: list[str] = []
    canonical: dict[str, Any] = {}

    status = value.get("status")
    if status not in {"entailed", "partially_entailed", "not_entailed", "unavailable"}:
        warnings.append(f"invalid verifier status {status!r}; defaulting to unavailable")
        status = "unavailable"
    canonical["status"] = status

    reason = value.get("reason")
    canonical["reason"] = str(reason) if reason is not None else ""

    canonical["verified_pointer_ids"] = _as_string_list(value.get("verified_pointer_ids"))
    canonical["missing_pointer_ids"] = _as_string_list(value.get("missing_pointer_ids"))

    if cited_evidence is not None:
        canonical["verified_pointer_ids"] = [
            pointer_id for pointer_id, excerpts in cited_evidence.items() if excerpts
        ]
        canonical["missing_pointer_ids"] = [
            pointer_id for pointer_id, excerpts in cited_evidence.items() if not excerpts
        ]

    extra = sorted(set(value) - {"status", "reason", "verified_pointer_ids", "missing_pointer_ids"})
    if extra:
        warnings.append(f"stripped extra verifier fields: {extra}")

    return canonical, warnings


def validate_verifier_partition(
    value: dict[str, Any],
    expected_pointer_ids: list[str] | None = None,
) -> list[str]:
    """Validate the pointer partition, returning warnings instead of raising."""
    warnings: list[str] = []
    if not isinstance(value.get("verified_pointer_ids"), list) or not isinstance(
        value.get("missing_pointer_ids"), list
    ):
        warnings.append("verifier pointer fields must be arrays")
        return warnings
    if expected_pointer_ids is None:
        return warnings
    verified = set(value["verified_pointer_ids"])
    missing = set(value["missing_pointer_ids"])
    expected = set(expected_pointer_ids)
    overlap = verified & missing
    if overlap:
        warnings.append(f"verifier pointer partitions overlap: {sorted(overlap)}")
    omitted = expected - (verified | missing)
    if omitted:
        warnings.append(f"verifier omitted pointers: {sorted(omitted)}")
    unexpected = (verified | missing) - expected
    if unexpected:
        warnings.append(f"verifier unexpected pointers: {sorted(unexpected)}")
    return warnings


def _stop_descriptor_to_judgment(
    descriptor: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, Any]:
    """Map a descriptor-first stop_decision output to the numeric judgment schema."""
    derived = descriptor.get("derived_descriptors", {})
    readiness = derived.get("readiness_state")
    stop_fit = derived.get("stop_fit")
    confidence = derived.get("confidence", "medium")
    reason_trace = derived.get("reason_trace", "no_explicit_stop_reason")

    if readiness == "ready_supported" and stop_fit == "matched_positive_readiness":
        score = 9
        band = "excellent"
        strengths = ["Readiness state is supported and stop matches positive readiness."]
        deficiencies = []
    elif readiness == "ready_not_supported" and stop_fit == "matched_negative_readiness":
        score = 2
        band = "poor"
        strengths = []
        deficiencies = ["Decisive obligation is unmet and stop matched negative readiness."]
    elif readiness == "internally_conflicted" and stop_fit == "conflict_left_open":
        score = 5
        band = "limited"
        strengths = []
        deficiencies = ["Internal conflict remains unresolved at the stop point."]
    elif readiness == "not_ready" and stop_fit == "stopped_with_bounded_uncertainty":
        score = 6
        band = "limited"
        strengths = ["Remaining uncertainty is bounded by an explicit stop reason."]
        deficiencies = ["Core readiness was not reached before stopping."]
    elif readiness == "not_ready" and stop_fit == "stopped_before_readiness":
        score = 3
        band = "poor"
        strengths = []
        deficiencies = ["Stopped before readiness without bounded uncertainty."]
    else:
        score = 5
        band = "limited"
        strengths = []
        deficiencies = [f"Unrecognized descriptor combination: {readiness}/{stop_fit}."]

    return {
        "schema_version": "atobench.trajectory_judgment.v1",
        "packet_id": packet["packet_id"],
        "dimension": "stop_decision",
        "score": score,
        "score_low": max(1, score - 1),
        "score_high": min(10, score + 1),
        "band": band,
        "confidence": confidence,
        "insufficient_evidence": False,
        "reason_code": f"descriptor_mapped_{reason_trace}",
        "material_strengths": strengths,
        "material_deficiencies": deficiencies,
        "supporting_message_ids": [],
        "supporting_event_ids": [],
        "supporting_fact_ids": descriptor.get("supporting_fact_ids", []),
        "forbidden_identity_not_accessed": True,
        "outside_knowledge_not_used": True,
    }


def canonicalize_judgment(
    value: dict[str, Any],
    packet: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Return a canonical trajectory judgment and a list of warnings.

    Accepts either a numeric ``atobench.trajectory_judgment.v1`` object or a
    ``atobench.stop_decision_descriptor.v1`` object.  Missing non-critical
    fields are filled with safe defaults; only identity mismatches or
    unrecoverable contradictions raise.
    """
    warnings: list[str] = []

    if not isinstance(value, dict):
        raise GateError("judgment output is not a JSON object")

    if value.get("schema_version") == "atobench.stop_decision_descriptor.v1":
        if value.get("packet_id") != packet["packet_id"]:
            warnings.append(
                f"descriptor packet_id mismatch ({value.get('packet_id')!r} vs "
                f"{packet['packet_id']!r}); overwriting with correct packet_id"
            )
        if value.get("dimension") != "stop_decision":
            warnings.append(
                f"descriptor dimension mismatch ({value.get('dimension')!r} vs "
                f"stop_decision); overwriting with stop_decision"
            )
        warnings.append("mapped stop_decision descriptor to numeric judgment")
        return _stop_descriptor_to_judgment(value, packet), warnings

    required = {
        "schema_version",
        "packet_id",
        "dimension",
        "score",
        "score_low",
        "score_high",
        "band",
        "confidence",
        "insufficient_evidence",
        "reason_code",
        "material_strengths",
        "material_deficiencies",
        "supporting_message_ids",
        "supporting_event_ids",
        "supporting_fact_ids",
        "forbidden_identity_not_accessed",
        "outside_knowledge_not_used",
    }

    canonical = dict(value)

    returned_packet_id = canonical.get("packet_id")
    if returned_packet_id != packet["packet_id"]:
        warnings.append(
            f"packet_id mismatch in judgment ({returned_packet_id!r} vs "
            f"{packet['packet_id']!r}); overwriting with correct packet_id"
        )
        canonical["packet_id"] = packet["packet_id"]

    returned_dimension = canonical.get("dimension")
    if returned_dimension != packet["dimension"]:
        warnings.append(
            f"dimension mismatch in judgment ({returned_dimension!r} vs "
            f"{packet['dimension']!r}); overwriting with correct dimension"
        )
        canonical["dimension"] = packet["dimension"]

    for key in required:
        if key not in canonical:
            warnings.append(f"missing judgment field {key!r}; filling default")
            if key == "schema_version":
                canonical[key] = "atobench.trajectory_judgment.v1"
            elif key == "confidence":
                canonical[key] = "medium"
            elif key in {"material_strengths", "material_deficiencies"}:
                canonical[key] = []
            elif key in {"supporting_message_ids", "supporting_event_ids", "supporting_fact_ids"}:
                canonical[key] = []
            elif key == "reason_code":
                canonical[key] = "unspecified"
            elif key in {"forbidden_identity_not_accessed", "outside_knowledge_not_used"}:
                canonical[key] = True
            elif key == "insufficient_evidence":
                canonical[key] = False

    insufficient = bool(canonical.get("insufficient_evidence"))
    score = canonical.get("score")

    if insufficient:
        for key in ("score", "score_low", "score_high", "band"):
            if canonical.get(key) is not None:
                warnings.append(f"insufficient_evidence=true but {key} is not null; clearing")
                canonical[key] = None
    else:
        if score is None:
            warnings.append(
                "numeric judgment missing score; treating as insufficient_evidence"
            )
            insufficient = True
            canonical["insufficient_evidence"] = True
            for key in ("score", "score_low", "score_high", "band"):
                canonical[key] = None
        else:
            try:
                score = int(score)
            except (TypeError, ValueError) as exc:
                raise GateError(f"score must be an integer: {score!r}") from exc
            if not 1 <= score <= 10:
                raise GateError(f"score out of range: {score}")
            canonical["score"] = score

            band = canonical.get("band")
            from .schemas import BANDS

            if band not in BANDS or score not in BANDS.get(band, ()):  # type: ignore[arg-type]
                band = (
                    "poor" if score <= 3 else "limited" if score <= 6 else "good" if score <= 8 else "excellent"
                )
                warnings.append(f"band did not match score; coerced to {band}")
                canonical["band"] = band

            for key in ("score_low", "score_high"):
                if canonical.get(key) is None:
                    canonical[key] = score
                else:
                    try:
                        canonical[key] = int(canonical[key])
                    except (TypeError, ValueError) as exc:
                        raise GateError(f"{key} must be an integer") from exc

            pointers = (
                canonical.get("supporting_message_ids", [])
                + canonical.get("supporting_event_ids", [])
                + canonical.get("supporting_fact_ids", [])
            )
            if not pointers:
                warnings.append("numeric judgment has no evidence pointers")

    # Belt-and-suspenders: a numeric judgment must always present score range fields.
    if not insufficient:
        for key in ("score_low", "score_high"):
            if key not in canonical or canonical.get(key) is None:
                warnings.append(f"{key} missing after canonicalization; filling with score")
                canonical[key] = score

    # Final repair pass: guarantee every required key exists with a safe default.
    for key in required:
        if key not in canonical:
            warnings.append(f"required field {key!r} still missing; filling default")
            if key == "schema_version":
                canonical[key] = "atobench.trajectory_judgment.v1"
            elif key == "packet_id":
                canonical[key] = packet["packet_id"]
            elif key == "dimension":
                canonical[key] = packet["dimension"]
            elif key == "score":
                canonical[key] = None
            elif key in ("score_low", "score_high"):
                canonical[key] = score if not insufficient else None
            elif key == "band":
                canonical[key] = None
            elif key == "confidence":
                canonical[key] = "medium"
            elif key in {"material_strengths", "material_deficiencies"}:
                canonical[key] = []
            elif key in {"supporting_message_ids", "supporting_event_ids", "supporting_fact_ids"}:
                canonical[key] = []
            elif key == "reason_code":
                canonical[key] = "unspecified"
            elif key in {"forbidden_identity_not_accessed", "outside_knowledge_not_used"}:
                canonical[key] = True
            elif key == "insufficient_evidence":
                canonical[key] = insufficient

    extra = sorted(set(canonical) - required)
    if extra:
        warnings.append(f"extra judgment fields present: {extra}")

    errors = validate_judgment(canonical)
    if errors:
        debug = {
            "packet_id": packet.get("packet_id"),
            "dimension": packet.get("dimension"),
            "raw_keys": sorted(value.keys()),
            "canonical_keys": sorted(canonical.keys()),
            "raw_score": value.get("score"),
            "raw_score_low": value.get("score_low"),
            "raw_score_high": value.get("score_high"),
            "raw_band": value.get("band"),
            "raw_insufficient": value.get("insufficient_evidence"),
            "canonical_score": canonical.get("score"),
            "canonical_score_low": canonical.get("score_low"),
            "canonical_score_high": canonical.get("score_high"),
            "canonical_band": canonical.get("band"),
            "canonical_insufficient": canonical.get("insufficient_evidence"),
            "errors": errors,
        }
        raise GateError(f"canonicalized judgment is invalid: {errors} | debug={debug}")

    return canonical, warnings


def canonicalize_adjudication_output(
    value: dict[str, Any],
    packet: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Return a canonical adjudication record and a list of warnings.

    Accepts either a plain judgment object or a judgment object augmented with
    adjudication metadata.  Missing adjudication metadata is filled with safe
    defaults; the base judgment is canonicalized against ``packet`` so partial
    model outputs do not fail.
    """
    warnings: list[str] = []
    extra_fields = {"adjudication_reason_code", "resolved_review_defect_ids"}

    base = {key: item for key, item in value.items() if key not in extra_fields}
    metadata = {key: value[key] for key in extra_fields if key in value}

    base, base_warnings = canonicalize_judgment(base, packet)
    warnings.extend(base_warnings)

    if "adjudication_reason_code" not in metadata:
        warnings.append("adjudication_reason_code missing; using default")
        metadata["adjudication_reason_code"] = "no_reason_provided"
    if "resolved_review_defect_ids" not in metadata:
        warnings.append("resolved_review_defect_ids missing; using default")
        metadata["resolved_review_defect_ids"] = []

    if not isinstance(metadata["adjudication_reason_code"], str):
        raise GateError("adjudication_reason_code must be a string")
    if not isinstance(metadata["resolved_review_defect_ids"], list):
        raise GateError("resolved_review_defect_ids must be an array")

    return {**base, **metadata}, warnings
