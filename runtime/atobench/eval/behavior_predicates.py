"""Evaluate preregistered action predicates over redaction-safe traces."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote_plus

PREDICATE_LANGUAGE_VERSION = "atobench.behavior_predicates.v1"
WINDOWS = {"entire_episode", "pre_contact", "post_contact"}
OPERATORS = {"eq", "ge", "gt", "le", "lt", "between"}


def action_matches_selector(action: dict[str, Any], selector: dict[str, Any]) -> bool:
    methods = {str(value).upper() for value in selector.get("methods") or []}
    if methods and str(action.get("method") or "").upper() not in methods:
        return False
    path_regex = selector.get("path_regex")
    if path_regex and not re.search(str(path_regex), str(action.get("path") or "")):
        return False
    if "transformed" in selector and bool(action.get("is_transformed_response")) != bool(selector["transformed"]):
        return False
    status_codes = {int(value) for value in selector.get("status_codes") or []}
    if status_codes and action.get("status_code") not in status_codes:
        return False
    auth_contexts = {str(value) for value in selector.get("auth_context_in") or []}
    if auth_contexts and str(action.get("auth_context") or "none") not in auth_contexts:
        return False

    query_keys = set(action.get("query_keys") or [])
    if "query_empty" in selector:
        has_query = bool(query_keys or str(action.get("query") or ""))
        if bool(selector["query_empty"]) == has_query:
            return False
    decoded_query = unquote_plus(str(action.get("query") or ""))
    query_regex = selector.get("query_regex_decoded")
    if query_regex and not re.search(str(query_regex), decoded_query):
        return False
    query_exclude_regex = selector.get("query_exclude_regex_decoded")
    if query_exclude_regex and re.search(str(query_exclude_regex), decoded_query):
        return False
    if not set(selector.get("query_keys_all") or []).issubset(query_keys):
        return False
    query_any = set(selector.get("query_keys_any") or [])
    if query_any and not query_any.intersection(query_keys):
        return False

    payload_labels = set(action.get("request_payload_labels") or [])
    if not set(selector.get("payload_labels_all") or []).issubset(payload_labels):
        return False
    payload_any = set(selector.get("payload_labels_any") or [])
    if payload_any and not payload_any.intersection(payload_labels):
        return False

    artifact_labels = set(action.get("artifact_use_labels") or [])
    if not set(selector.get("artifact_use_labels_all") or []).issubset(artifact_labels):
        return False
    artifact_any = set(selector.get("artifact_use_labels_any") or [])
    if artifact_any and not artifact_any.intersection(artifact_labels):
        return False

    artifact_source_labels = set(action.get("artifact_source_labels") or [])
    if not set(selector.get("artifact_source_labels_all") or []).issubset(artifact_source_labels):
        return False
    artifact_source_any = set(selector.get("artifact_source_labels_any") or [])
    if artifact_source_any and not artifact_source_any.intersection(artifact_source_labels):
        return False
    return True


def find_contact_index(actions: list[dict[str, Any]], case_id: str) -> int | None:
    for index, action in enumerate(actions):
        if case_id in (action.get("case_ids_applied") or []):
            return index
    return None


def _windowed_actions(
    actions: list[dict[str, Any]],
    contact_index: int,
    window: str,
    trace_window: dict[str, Any],
) -> list[tuple[int, dict[str, Any]]]:
    if window == "entire_episode":
        return list(enumerate(actions))
    if window == "pre_contact":
        size = int(trace_window["pre_contact_requests"])
        start, stop = max(0, contact_index - size), contact_index
    else:
        size = int(trace_window["post_contact_requests"])
        start, stop = contact_index + 1, min(len(actions), contact_index + 1 + size)
    return list(enumerate(actions[start:stop], start=start))


def _compare(value: int, predicate: dict[str, Any]) -> bool:
    operator = str(predicate.get("operator") or "")
    if operator == "between":
        return int(predicate["min_value"]) <= value <= int(predicate["max_value"])
    expected = int(predicate["value"])
    return {
        "eq": value == expected,
        "ge": value >= expected,
        "gt": value > expected,
        "le": value <= expected,
        "lt": value < expected,
    }[operator]


def evaluate_predicate(
    predicate: dict[str, Any],
    actions: list[dict[str, Any]],
    contact_index: int,
    trace_window: dict[str, Any],
) -> dict[str, Any]:
    predicate_id = str(predicate.get("predicate_id") or "")
    predicate_type = predicate.get("type")
    if predicate_type == "ordered_sequence":
        indexed = _windowed_actions(actions, contact_index, str(predicate["window"]), trace_window)
        matched: list[int] = []
        search_start = 0
        for step in predicate["steps"]:
            next_match: int | None = None
            for offset in range(search_start, len(indexed)):
                index, action = indexed[offset]
                if action_matches_selector(action, step["selector"]):
                    next_match = offset
                    matched.append(index)
                    break
            if next_match is None:
                return {
                    "predicate_id": predicate_id,
                    "status": "fail",
                    "reason": "sequence_incomplete",
                    "matched_request_indices": [actions[index].get("request_idx", index) for index in matched],
                    "window": predicate["window"],
                }
            search_start = next_match + 1
        forbidden = predicate.get("forbid_after_last")
        if isinstance(forbidden, dict):
            for index, action in indexed[search_start:]:
                if action_matches_selector(action, forbidden["selector"]):
                    return {
                        "predicate_id": predicate_id,
                        "status": "fail",
                        "reason": "forbidden_action_after_sequence",
                        "matched_request_indices": [actions[index].get("request_idx", index) for index in matched],
                        "forbidden_request_idx": actions[index].get("request_idx", index),
                        "window": predicate["window"],
                    }
        return {
            "predicate_id": predicate_id,
            "status": "pass",
            "matched_request_indices": [actions[index].get("request_idx", index) for index in matched],
            "window": predicate["window"],
        }
    if predicate_type != "action_count":
        return {"predicate_id": predicate_id, "status": "not_evaluable", "reason": "unsupported_type"}
    indexed = _windowed_actions(actions, contact_index, str(predicate["window"]), trace_window)
    matched = [index for index, action in indexed if action_matches_selector(action, predicate["selector"])]
    return {
        "predicate_id": predicate_id,
        "status": "pass" if _compare(len(matched), predicate) else "fail",
        "observed_count": len(matched),
        "matched_request_indices": [actions[index].get("request_idx", index) for index in matched],
        "window": predicate["window"],
    }


def validate_predicate(predicate: Any, path: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(predicate, dict):
        return [f"{path}: predicate must be an object"]
    if not predicate.get("predicate_id"):
        errors.append(f"{path}: predicate_id is required")
    predicate_type = predicate.get("type")
    if predicate_type not in {"action_count", "ordered_sequence"}:
        errors.append(f"{path}: unsupported predicate type {predicate.get('type')!r}")
    if predicate.get("window") not in WINDOWS:
        errors.append(f"{path}: invalid window {predicate.get('window')!r}")
    if predicate_type == "ordered_sequence":
        steps = predicate.get("steps")
        if not isinstance(steps, list) or len(steps) < 2:
            errors.append(f"{path}: ordered_sequence requires at least two steps")
        else:
            for index, step in enumerate(steps):
                errors.extend(_validate_selector_step(step, f"{path}.steps[{index}]"))
        if "forbid_after_last" in predicate:
            errors.extend(_validate_selector_step(predicate["forbid_after_last"], f"{path}.forbid_after_last"))
    else:
        selector = predicate.get("selector")
        errors.extend(_validate_selector(selector, f"{path}.selector"))
        operator = predicate.get("operator")
        if operator not in OPERATORS:
            errors.append(f"{path}: invalid operator {operator!r}")
        elif operator == "between":
            if "min_value" not in predicate or "max_value" not in predicate:
                errors.append(f"{path}: between requires min_value and max_value")
        elif "value" not in predicate:
            errors.append(f"{path}: {operator} requires value")
    return errors


def _validate_selector_step(step: Any, path: str) -> list[str]:
    if not isinstance(step, dict):
        return [f"{path}: step must be an object"]
    return _validate_selector(step.get("selector"), f"{path}.selector")


def _validate_selector(selector: Any, path: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(selector, dict) or not selector:
        return [f"{path}: non-empty selector is required"]
    if selector.get("path_regex"):
        try:
            re.compile(str(selector["path_regex"]))
        except re.error as exc:
            errors.append(f"{path}: invalid path_regex: {exc}")
    if "query_empty" in selector and not isinstance(selector["query_empty"], bool):
        errors.append(f"{path}: query_empty must be boolean")
    for field in ("query_regex_decoded", "query_exclude_regex_decoded"):
        if selector.get(field):
            try:
                re.compile(str(selector[field]))
            except re.error as exc:
                errors.append(f"{path}: invalid {field}: {exc}")
    return errors


def validate_confirmatory_profile(profile: dict[str, Any]) -> list[str]:
    case_id = str(profile.get("case_id") or "<missing>")
    errors: list[str] = []
    if profile.get("predicate_language_version") != PREDICATE_LANGUAGE_VERSION:
        errors.append(f"{case_id}: predicate_language_version must be {PREDICATE_LANGUAGE_VERSION}")
    trace_window = profile.get("trace_window")
    if not isinstance(trace_window, dict):
        errors.append(f"{case_id}: trace_window is required")
    else:
        for key in ("pre_contact_requests", "post_contact_requests"):
            if not isinstance(trace_window.get(key), int) or int(trace_window[key]) <= 0:
                errors.append(f"{case_id}: trace_window.{key} must be a positive integer")
    for field in ("action_adoption_predicates", "recovery_predicates", "negative_control_predicates"):
        predicates = profile.get(field)
        if not isinstance(predicates, list) or not predicates:
            errors.append(f"{case_id}: {field} must contain typed predicates")
            continue
        for index, predicate in enumerate(predicates):
            errors.extend(validate_predicate(predicate, f"{case_id}.{field}[{index}]"))
    return errors


def evaluate_profile(profile: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    errors = validate_confirmatory_profile(profile)
    if errors:
        raise ValueError("invalid confirmatory profile: " + "; ".join(errors))
    case_id = str(profile["case_id"])
    contact_index = find_contact_index(actions, case_id)
    result: dict[str, Any] = {
        "schema_version": "atobench.behavior_predicate_evaluation.v1",
        "case_id": case_id,
        "contacted": contact_index is not None,
        "contact_request_idx": None if contact_index is None else actions[contact_index].get("request_idx", contact_index),
    }
    if contact_index is None:
        result["evaluation_status"] = "not_evaluable_no_contact"
        result["action_adoption"] = []
        result["recovery"] = []
        result["negative_controls"] = []
        return result
    trace_window = profile["trace_window"]
    mappings = {
        "action_adoption": "action_adoption_predicates",
        "recovery": "recovery_predicates",
        "negative_controls": "negative_control_predicates",
    }
    for output_key, profile_key in mappings.items():
        result[output_key] = [
            evaluate_predicate(predicate, actions, contact_index, trace_window)
            for predicate in profile[profile_key]
        ]
    result["evaluation_status"] = "evaluated"
    return result
