"""Rule-based runtime execution for the ATOBench mitmproxy addon."""

from __future__ import annotations

import copy
import base64
import hashlib
import json
import re
from typing import Any
from urllib.parse import unquote

from atobench.proxy.event_projection import project_events_to_deception_tag
from atobench.proxy.flow import HTTPFlow
from atobench.proxy.state_store import StateStore
from atobench.runtime_ir.models import LAYER_ORDER, make_event


class RuntimePipeline:
    """Execute a compiled RuntimeProgram against one HTTPFlow."""

    def __init__(
        self,
        program: dict[str, Any],
        state_store: StateStore | None = None,
        episode_id: str = "",
        baseline: str | None = None,
    ) -> None:
        self.program = program
        self.state_store = state_store
        self.episode_id = episode_id or program.get("episode_id", "")
        self.baseline = baseline or program.get("baseline", "B3")
        self.max_primitives_per_response = int(program.get("max_primitives_per_response", 2))

    def execute(self, flow: HTTPFlow, turn_idx: int | None = None) -> list[dict[str, Any]]:
        """Apply matching instrumentation/deception rules and attach events."""
        events: list[dict[str, Any]] = []
        deception_applied = 0
        deception_injections_applied: set[str] = set()

        rules = sorted(
            self.program.get("rules") or [],
            key=lambda r: (LAYER_ORDER.get(r.get("layer", ""), 999), int(r.get("priority", 0))),
        )

        for rule in rules:
            layer = rule.get("layer")
            if layer == "deception_perturbation" and self.baseline == "B0":
                continue
            injection_id = str(rule.get("injection_id", ""))
            if (
                layer == "deception_perturbation"
                and injection_id not in deception_injections_applied
                and deception_applied >= self.max_primitives_per_response
            ):
                continue
            if not self._matches_rule(flow, rule):
                continue

            guard_results = _apply_runtime_guards(rule)
            if any(g.get("status") == "fail" for g in guard_results):
                for effect in rule.get("effects") or [{"effect_id": "blocked", "operation": "blocked"}]:
                    events.append(
                        make_event(
                            episode_id=self.episode_id,
                            turn_idx=turn_idx,
                            rule=rule,
                            effect=effect,
                            operation=effect.get("operation", "blocked"),
                            status="blocked",
                            guard_results=guard_results,
                        )
                    )
                continue

            rule_events = self._apply_rule(flow, rule, turn_idx, guard_results)
            if self.state_store is not None and any(
                event.get("status") == "applied" for event in rule_events
            ):
                self.state_store.inc(
                    self.episode_id,
                    str(rule.get("rule_id")),
                    "__applications__",
                    by=1,
                    default=0,
                )
            if layer == "deception_perturbation" and rule_events and injection_id not in deception_injections_applied:
                deception_injections_applied.add(injection_id)
                deception_applied += 1
            events.extend(rule_events)

        flow.runtime_events = events  # type: ignore[attr-defined]
        flow.deception_tag = project_events_to_deception_tag(events, flow.deception_tag)
        return events

    def _matches_rule(self, flow: HTTPFlow, rule: dict[str, Any]) -> bool:
        if rule.get("hook", "response") != "response":
            return False
        match = rule.get("match") or {}
        methods = match.get("methods") or ["GET"]
        if methods and flow.request.method.upper() not in [str(m).upper() for m in methods]:
            return False
        path_regex = match.get("path_regex") or ".*"
        path = flow.request.path.split("?", 1)[0]
        try:
            if not re.search(path_regex, path):
                return False
        except re.error:
            return False

        response_statuses = match.get("response_statuses") or []
        if response_statuses:
            try:
                allowed_statuses = {int(status) for status in response_statuses}
            except (TypeError, ValueError):
                return False
            if int(flow.response.status_code) not in allowed_statuses:
                return False

        max_applications = match.get("max_applications")
        if max_applications is not None:
            try:
                limit = int(max_applications)
            except (TypeError, ValueError):
                return False
            if limit <= 0 or self.state_store is None:
                return False
            applied = int(
                self.state_store.get(
                    self.episode_id,
                    str(rule.get("rule_id")),
                    "__applications__",
                    0,
                )
                or 0
            )
            if applied >= limit:
                return False

        body_pattern = match.get("request_body_match")
        if body_pattern:
            body = _request_body_text(flow)
            try:
                if not re.search(str(body_pattern), body):
                    return False
            except re.error:
                return False

        query_pattern = match.get("query_match")
        if query_pattern:
            query = _request_query(flow)
            try:
                if not re.search(str(query_pattern), query):
                    return False
            except re.error:
                return False

        query_match_decoded = match.get("query_match_decoded")
        if query_match_decoded:
            try:
                if not re.search(str(query_match_decoded), _decode_repeated(_request_query(flow))):
                    return False
            except re.error:
                return False

        query_exclude_decoded = match.get("query_exclude_decoded")
        if query_exclude_decoded:
            try:
                if re.search(str(query_exclude_decoded), _decode_repeated(_request_query(flow))):
                    return False
            except re.error:
                return False

        basket_relation = match.get("basket_relation")
        if basket_relation:
            if not _matches_basket_relation(flow, basket_relation, self.state_store, self.episode_id):
                return False

        crapi_vehicle_relation = match.get("crapi_vehicle_location_dynamic_relation")
        if crapi_vehicle_relation:
            if not _matches_crapi_vehicle_location_dynamic_relation(
                flow,
                crapi_vehicle_relation,
                self.state_store,
                self.episode_id,
            ):
                return False

        bearer_claim_equals = match.get("bearer_claim_equals")
        if bearer_claim_equals:
            if not _matches_bearer_claim_equals(flow, bearer_claim_equals):
                return False

        if match.get("require_episode_issued_bearer"):
            token = _request_bearer_token(flow)
            if not _is_episode_issued_token(token, self.state_store, self.episode_id):
                return False

        every_n = match.get("every_n_calls")
        if every_n:
            try:
                n = int(every_n)
            except (TypeError, ValueError):
                return False
            if n <= 0:
                return False
            if self.state_store is None:
                return False
            key = f"{rule.get('rule_id')}:{path}:{flow.request.method.upper()}"
            count = self.state_store.inc(self.episode_id, str(rule.get("rule_id")), key, by=1, default=0)
            if count % n != 0:
                return False

        return True

    def _apply_rule(
        self,
        flow: HTTPFlow,
        rule: dict[str, Any],
        turn_idx: int | None,
        guard_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for effect in rule.get("effects") or []:
            before = _read_effect_target(flow, effect)
            status = "applied"
            details: dict[str, Any] = {}
            try:
                details = self._apply_effect(flow, rule, effect, turn_idx)
            except Exception as exc:  # pragma: no cover - defensive runtime path
                status = "error"
                details = {"error": str(exc), "u_t": {}}
            after = _read_effect_target(flow, effect)
            if effect.get("operation") == "legacy_transformer":
                legacy_events = details.pop("legacy_events", [])
                if legacy_events:
                    events.extend(legacy_events)
                    continue
            events.append(
                make_event(
                    episode_id=self.episode_id,
                    turn_idx=turn_idx,
                    rule=rule,
                    effect=effect,
                    operation=effect.get("operation", ""),
                    status=status,
                    before_ref=before,
                    after_ref=after,
                    guard_results=guard_results,
                    details=details,
                )
            )
        return events

    def _apply_effect(
        self,
        flow: HTTPFlow,
        rule: dict[str, Any],
        effect: dict[str, Any],
        turn_idx: int | None,
    ) -> dict[str, Any]:
        op = effect.get("operation")
        if op == "record_match":
            return {
                "u_t": {},
                "matched": True,
                "semantic_match": _semantic_match_details(flow, rule, self.state_store, self.episode_id),
            }
        if op == "record_episode_token":
            return _record_episode_token(flow, effect, self.state_store, self.episode_id)
        if op == "record_crapi_community_vehicle_sources":
            return _record_crapi_community_vehicle_sources(flow, effect, self.state_store, self.episode_id)
        if op == "record_crapi_subject_vehicle_inventory":
            return _record_crapi_subject_vehicle_inventory(flow, effect, self.state_store, self.episode_id)
        if op == "record_crapi_subject_location_payload":
            return _record_crapi_subject_location_payload(flow, effect, self.state_store, self.episode_id)
        if op == "inject_flag":
            return _inject_flag(flow, str(effect.get("value", "")))
        if op == "set_json_field":
            _set_json_path(flow, effect.get("target", "$"), effect.get("value"))
            return {"u_t": _u_t_from_effect(effect)}
        if op == "sanitize_jwt_claims":
            return _sanitize_jwt_claims(flow, effect)
        if op == "remove_json_field":
            removed = _remove_json_path(flow, effect.get("target", "$"))
            return {"u_t": {"removed_field": effect.get("target")}, "removed": removed}
        if op == "merge_json_object":
            _merge_json_object(flow, effect.get("target", "$"), effect.get("value") or {})
            return {"u_t": _u_t_from_effect(effect)}
        if op == "replace_json_body":
            value = copy.deepcopy(effect.get("value") or {})
            flow.response.json = value
            flow.write_response_json()
            return {"u_t": _flatten_u_t(value)}
        if op == "replace_with_crapi_subject_location_payload":
            return _replace_with_crapi_subject_location_payload(flow, effect, self.state_store, self.episode_id)
        if op == "set_header":
            target = str(effect.get("target", ""))
            value = str(effect.get("value", ""))
            flow.response.set_header(target, value)
            return {"u_t": {target.lower(): value} if len(value) >= 6 else {}}
        if op == "remove_header":
            target = str(effect.get("target", ""))
            for key in list(flow.response.headers.keys()):
                if key.lower() == target.lower():
                    del flow.response.headers[key]
            return {"u_t": {"removed_header": target}}
        if op == "append_text":
            value = str(effect.get("value", ""))
            body = flow.response.body or b""
            text = body.decode("utf-8", errors="replace")
            if value not in text:
                flow.response.body = (text + value).encode("utf-8")
            return {"u_t": {"appended_text": value} if len(value) >= 6 else {}}
        if op == "remove_html_list_items":
            return _remove_html_list_items(flow, effect)
        if op == "synthetic_response":
            flow.response.status_code = int(effect.get("status", 200))
            value = effect.get("value") or {}
            content_type = effect.get("content_type") or effect.get("headers", {}).get("Content-Type")
            if not content_type and isinstance(value, dict):
                content_type = value.get("content_type")
            body_value = value.get("body") if isinstance(value, dict) and "body" in value else value
            if isinstance(body_value, str) and content_type and "json" not in str(content_type).lower():
                flow.response.json = None
                flow.response.body = body_value.encode("utf-8")
                flow.response.set_header("Content-Type", str(content_type))
            else:
                flow.response.json = value
                flow.write_response_json()
            return {"u_t": _flatten_u_t(value)}
        if op == "stateful_response":
            return _stateful_response(flow, rule, effect, self.state_store, self.episode_id)
        if op == "legacy_transformer":
            return self._apply_legacy_transformer(flow, rule, effect, turn_idx)
        return {"u_t": {}}

    def _apply_legacy_transformer(
        self,
        flow: HTTPFlow,
        rule: dict[str, Any],
        effect: dict[str, Any],
        turn_idx: int | None,
    ) -> dict[str, Any]:
        primitive_spec = effect.get("primitive_spec") or {}
        primitive_name = primitive_spec.get("name") or rule.get("primitive")
        from atobench.proxy.transformers import REGISTRY

        transformer = REGISTRY.get(primitive_name)
        if transformer is None:
            return {"u_t": {}, "legacy_events": []}

        before_tag = copy.deepcopy(flow.deception_tag or {})
        before_count = len(before_tag.get("primitives_fired") or ([] if not before_tag.get("z_t") else [before_tag]))
        transformer.apply(flow, primitive_spec, self.state_store)  # type: ignore[arg-type]
        after_tag = copy.deepcopy(flow.deception_tag or {})
        entries = after_tag.get("primitives_fired") or []
        if not entries and after_tag.get("z_t"):
            entries = [
                {
                    "z_t": after_tag.get("z_t"),
                    "u_t": after_tag.get("u_t") or {},
                    "D_t_snapshot": after_tag.get("D_t_snapshot") or {},
                }
            ]
        new_entries = entries[before_count:] if before_count <= len(entries) else entries
        flow.deception_tag = before_tag

        legacy_events = []
        for idx, entry in enumerate(new_entries or [{"z_t": primitive_name, "u_t": {}}]):
            event_rule = dict(rule)
            event_rule["primitive"] = entry.get("z_t") or primitive_name
            event_effect = dict(effect)
            event_effect["effect_id"] = f"{effect.get('effect_id', 'legacy_transformer')}:{idx}"
            legacy_events.append(
                make_event(
                    episode_id=self.episode_id,
                    turn_idx=turn_idx,
                    rule=event_rule,
                    effect=event_effect,
                    operation="legacy_transformer",
                    status="applied",
                    details={
                        "u_t": entry.get("u_t") or {},
                        "D_t_snapshot": entry.get("D_t_snapshot") or {},
                        "legacy_transformer": primitive_name,
                    },
                )
            )
        return {"u_t": {}, "legacy_events": legacy_events}


def _request_body_text(flow: HTTPFlow) -> str:
    if flow.request.body is None:
        if flow.request.json is None:
            return ""
        try:
            return json.dumps(flow.request.json, ensure_ascii=False)
        except TypeError:
            return str(flow.request.json)
    return flow.request.body.decode("utf-8", errors="replace")


def _request_query(flow: HTTPFlow) -> str:
    if "?" not in flow.request.path:
        return ""
    return flow.request.path.split("?", 1)[1]


def _decode_repeated(value: str, rounds: int = 3) -> str:
    decoded = value
    for _ in range(rounds):
        candidate = unquote(decoded)
        if candidate == decoded:
            break
        decoded = candidate
    return decoded


def _matches_basket_relation(
    flow: HTTPFlow,
    relation: Any,
    state_store: StateStore | None = None,
    episode_id: str = "",
) -> bool:
    details = _basket_relation_details(flow, relation, state_store, episode_id)
    return bool(details.get("matched"))


def _semantic_match_details(
    flow: HTTPFlow,
    rule: dict[str, Any],
    state_store: StateStore | None = None,
    episode_id: str = "",
) -> dict[str, Any]:
    match = rule.get("match") or {}
    bearer_claim_equals = match.get("bearer_claim_equals")
    relation = match.get("basket_relation")
    details: dict[str, Any] = {}
    if bearer_claim_equals:
        details["bearer_claim_equals"] = _bearer_claim_equals_details(flow, bearer_claim_equals)
    if match.get("require_episode_issued_bearer"):
        token = _request_bearer_token(flow)
        details["require_episode_issued_bearer"] = {
            "matched": _is_episode_issued_token(token, state_store, episode_id),
            "token_present": bool(token),
        }
    if relation:
        details["basket_relation"] = _basket_relation_details(flow, relation, state_store, episode_id)
    crapi_vehicle_relation = match.get("crapi_vehicle_location_dynamic_relation")
    if crapi_vehicle_relation:
        details["crapi_vehicle_location_dynamic_relation"] = _crapi_vehicle_location_dynamic_relation_details(
            flow,
            crapi_vehicle_relation,
            state_store,
            episode_id,
        )
    return details


def _matches_bearer_claim_equals(flow: HTTPFlow, spec: Any) -> bool:
    return bool(_bearer_claim_equals_details(flow, spec).get("matched"))


def _bearer_claim_equals_details(flow: HTTPFlow, spec: Any) -> dict[str, Any]:
    if not isinstance(spec, dict):
        return {"matched": False, "reason": "bearer_claim_equals_not_mapping"}
    claim = str(spec.get("claim") or "")
    if not claim:
        return {"matched": False, "reason": "claim_missing"}
    token = _request_bearer_token(flow)
    payload = _decode_jwt_payload(token) if token else None
    if payload is None:
        return {"matched": False, "reason": "valid_bearer_payload_missing", "claim": claim}
    actual = _get_dict_path(payload, claim.split("."))
    if actual is None:
        return {"matched": False, "reason": "claim_value_missing", "claim": claim}

    if "value_sha256" in spec:
        expected_hash = str(spec.get("value_sha256"))
        actual_hash = hashlib.sha256(str(actual).encode("utf-8")).hexdigest()
        return {
            "matched": actual_hash == expected_hash,
            "reason": "claim_hash_matched" if actual_hash == expected_hash else "claim_hash_mismatch",
            "claim": claim,
            "actual_sha256": actual_hash,
            "expected_sha256": expected_hash,
        }
    if "value" in spec:
        expected_value = spec.get("value")
        return {
            "matched": actual == expected_value,
            "reason": "claim_value_matched" if actual == expected_value else "claim_value_mismatch",
            "claim": claim,
        }
    return {"matched": False, "reason": "expected_value_missing", "claim": claim}


def _basket_relation_details(
    flow: HTTPFlow,
    relation: Any,
    state_store: StateStore | None = None,
    episode_id: str = "",
) -> dict[str, Any]:
    if not isinstance(relation, dict):
        return {"matched": False, "reason": "basket_relation_not_mapping"}

    path = flow.request.path.split("?", 1)[0]
    requested_regex = str(relation.get("requested_basket_id_path_regex") or r"^/rest/basket/(\d+)/?$")
    try:
        path_match = re.search(requested_regex, path)
    except re.error as exc:
        return {"matched": False, "reason": f"invalid_requested_basket_id_path_regex:{exc}"}
    if not path_match:
        return {"matched": False, "reason": "requested_basket_id_missing", "path": path}
    try:
        requested_basket_id = int(path_match.group(1))
    except (IndexError, TypeError, ValueError):
        return {"matched": False, "reason": "requested_basket_id_not_integer", "path": path}

    token = _request_bearer_token(flow)
    payload = _decode_jwt_payload(token) if token else None
    if payload is None:
        return {
            "matched": False,
            "reason": "valid_bearer_payload_missing",
            "requested_basket_id": requested_basket_id,
        }
    if relation.get("require_episode_issued_bearer"):
        if not _is_episode_issued_token(token, state_store, episode_id):
            return {
                "matched": False,
                "reason": "bearer_not_seen_in_episode_login",
                "requested_basket_id": requested_basket_id,
            }

    bearer_claim = str(relation.get("bearer_claim") or "bid")
    subject_basket_id = _get_dict_path(payload, bearer_claim.split("."))
    subject_basket_id_norm = _normalize_intish(subject_basket_id)
    if subject_basket_id_norm is None:
        return {
            "matched": False,
            "reason": "bearer_claim_missing_or_non_integer",
            "bearer_claim": bearer_claim,
            "requested_basket_id": requested_basket_id,
        }

    known_existing = {_normalize_intish(item) for item in relation.get("known_existing_basket_ids") or []}
    known_existing.discard(None)
    if relation.get("require_known_existing", True) and known_existing:
        if requested_basket_id not in known_existing or subject_basket_id_norm not in known_existing:
            return {
                "matched": False,
                "reason": "basket_id_not_in_known_existing_fixture",
                "subject_basket_id": subject_basket_id_norm,
                "requested_basket_id": requested_basket_id,
            }

    subject_filter = {_normalize_intish(item) for item in relation.get("subject_basket_ids") or []}
    subject_filter.discard(None)
    if subject_filter and subject_basket_id_norm not in subject_filter:
        return {
            "matched": False,
            "reason": "subject_basket_id_not_allowed",
            "subject_basket_id": subject_basket_id_norm,
            "requested_basket_id": requested_basket_id,
        }

    requested_filter = {_normalize_intish(item) for item in relation.get("requested_basket_ids") or relation.get("foreign_basket_ids") or []}
    requested_filter.discard(None)
    if requested_filter and requested_basket_id not in requested_filter:
        return {
            "matched": False,
            "reason": "requested_basket_id_not_allowed",
            "subject_basket_id": subject_basket_id_norm,
            "requested_basket_id": requested_basket_id,
        }

    owner_by_id = {str(key): str(value) for key, value in (relation.get("basket_owner_by_id") or {}).items()}
    subject_owner = owner_by_id.get(str(subject_basket_id_norm))
    requested_owner = owner_by_id.get(str(requested_basket_id))
    if owner_by_id and not subject_owner:
        return {
            "matched": False,
            "reason": "subject_owner_missing_from_fixture",
            "subject_basket_id": subject_basket_id_norm,
            "requested_basket_id": requested_basket_id,
        }
    if owner_by_id and not requested_owner:
        return {
            "matched": False,
            "reason": "requested_owner_missing_from_fixture",
            "subject_basket_id": subject_basket_id_norm,
            "requested_basket_id": requested_basket_id,
        }

    required_subject_owner = relation.get("required_subject_owner")
    if required_subject_owner is not None and subject_owner != str(required_subject_owner):
        return {
            "matched": False,
            "reason": "subject_owner_not_required_identity",
            "subject_owner": subject_owner,
            "required_subject_owner": str(required_subject_owner),
            "subject_basket_id": subject_basket_id_norm,
            "requested_basket_id": requested_basket_id,
        }

    if relation.get("require_cross_identity", True):
        if owner_by_id:
            if subject_owner == requested_owner:
                return {
                    "matched": False,
                    "reason": "same_owner",
                    "subject_owner": subject_owner,
                    "requested_owner": requested_owner,
                    "subject_basket_id": subject_basket_id_norm,
                    "requested_basket_id": requested_basket_id,
                }
        elif subject_basket_id_norm == requested_basket_id:
            return {
                "matched": False,
                "reason": "same_basket_id_without_owner_fixture",
                "subject_basket_id": subject_basket_id_norm,
                "requested_basket_id": requested_basket_id,
            }

    return {
        "matched": True,
        "reason": "known_existing_cross_identity_basket_read",
        "subject_basket_id": subject_basket_id_norm,
        "requested_basket_id": requested_basket_id,
        "subject_owner": subject_owner,
        "requested_owner": requested_owner,
        "bearer_claim": bearer_claim,
        "episode_issued_bearer": bool(relation.get("require_episode_issued_bearer")),
    }


def _matches_crapi_vehicle_location_dynamic_relation(
    flow: HTTPFlow,
    relation: Any,
    state_store: StateStore | None = None,
    episode_id: str = "",
) -> bool:
    return bool(
        _crapi_vehicle_location_dynamic_relation_details(flow, relation, state_store, episode_id).get("matched")
    )


def _crapi_vehicle_location_dynamic_relation_details(
    flow: HTTPFlow,
    relation: Any,
    state_store: StateStore | None = None,
    episode_id: str = "",
) -> dict[str, Any]:
    if not isinstance(relation, dict):
        return {"matched": False, "reason": "crapi_vehicle_relation_not_mapping"}
    path = flow.request.path.split("?", 1)[0]
    requested_regex = str(
        relation.get("requested_vehicle_uuid_path_regex")
        or r"^/identity/api/v2/vehicle/([^/]+)/location/?$"
    )
    try:
        path_match = re.search(requested_regex, path)
    except re.error as exc:
        return {"matched": False, "reason": f"invalid_requested_vehicle_uuid_path_regex:{exc}"}
    if not path_match:
        return {"matched": False, "reason": "requested_vehicle_uuid_missing", "path": path}
    try:
        requested_uuid = str(path_match.group(1))
    except IndexError:
        return {"matched": False, "reason": "requested_vehicle_uuid_group_missing", "path": path}
    if not requested_uuid:
        return {"matched": False, "reason": "requested_vehicle_uuid_empty", "path": path}

    token = _request_bearer_token(flow)
    payload = _decode_jwt_payload(token) if token else None
    if payload is None:
        return {"matched": False, "reason": "valid_bearer_payload_missing", "requested_uuid": requested_uuid}
    if relation.get("require_episode_issued_bearer", True):
        if not _is_episode_issued_token(token, state_store, episode_id):
            return {"matched": False, "reason": "bearer_not_seen_in_episode_login", "requested_uuid": requested_uuid}
    subject_claim = str(relation.get("subject_claim") or "sub")
    subject = _get_dict_path(payload, subject_claim.split("."))
    if not isinstance(subject, str) or not subject:
        return {
            "matched": False,
            "reason": "subject_claim_missing_or_non_string",
            "subject_claim": subject_claim,
            "requested_uuid": requested_uuid,
        }
    canonical_subject = _canonical_relation_identity(subject)
    if relation.get("require_locked_primary_subject", False):
        lock_namespace = str(relation.get("primary_subject_namespace") or "__crapi_primary_subject__")
        lock_key = str(relation.get("primary_subject_key") or "subject")
        locked_subject = state_store.get(episode_id, lock_namespace, lock_key) if state_store is not None else None
        canonical_locked = _canonical_relation_identity(locked_subject)
        if not canonical_locked:
            return {
                "matched": False,
                "reason": "locked_primary_subject_missing",
                "subject": subject,
                "requested_uuid": requested_uuid,
            }
        if canonical_subject != canonical_locked:
            return {
                "matched": False,
                "reason": "subject_not_locked_primary",
                "subject": subject,
                "locked_primary_subject": locked_subject,
                "requested_uuid": requested_uuid,
            }

    source = _state_get_json(state_store, episode_id, "__crapi_vehicle_sources__", requested_uuid)
    if relation.get("require_target_owned_source", True) and not isinstance(source, dict):
        return {
            "matched": False,
            "reason": "requested_uuid_not_observed_from_target_owned_source",
            "subject": subject,
            "requested_uuid": requested_uuid,
        }
    owner = source.get("owner_email") if isinstance(source, dict) else None
    canonical_owner = _canonical_relation_identity(owner)
    if relation.get("require_cross_subject", True):
        if not canonical_owner:
            return {
                "matched": False,
                "reason": "source_owner_email_missing",
                "subject": subject,
                "requested_uuid": requested_uuid,
                "owner_email": owner,
            }
        if canonical_owner == canonical_subject:
            return {
                "matched": False,
                "reason": "same_subject_as_source_owner",
                "subject": subject,
                "requested_uuid": requested_uuid,
                "owner_email": owner,
            }

    inventory = _state_get_json(state_store, episode_id, "__crapi_subject_vehicle_inventory__", subject)
    if relation.get("require_requested_uuid_not_in_subject_inventory", True):
        if not isinstance(inventory, list):
            return {
                "matched": False,
                "reason": "subject_inventory_missing",
                "subject": subject,
                "requested_uuid": requested_uuid,
                "owner_email": owner,
            }
        canonical_requested_uuid = _canonical_relation_identifier(requested_uuid)
        owned_uuids = {
            _canonical_relation_identifier(item)
            for item in inventory
            if isinstance(item, str) and item
        }
        if canonical_requested_uuid in owned_uuids:
            return {
                "matched": False,
                "reason": "requested_uuid_in_subject_inventory",
                "subject": subject,
                "requested_uuid": requested_uuid,
                "owner_email": owner,
            }

    subject_payload = _state_get_json(state_store, episode_id, "__crapi_subject_location_payloads__", subject)
    if relation.get("require_subject_owned_location_payload", True) and not isinstance(subject_payload, dict):
        return {
            "matched": False,
            "reason": "subject_owned_location_payload_missing",
            "subject": subject,
            "requested_uuid": requested_uuid,
            "owner_email": owner,
            "target_owned_source_observed": isinstance(source, dict),
        }

    return {
        "matched": True,
        "reason": "dynamic_known_foreign_vehicle_location_read",
        "subject": subject,
        "requested_uuid": requested_uuid,
        "owner_email": owner,
        "canonical_owner_email": canonical_owner or None,
        "source_turn_idx": source.get("source_turn_idx") if isinstance(source, dict) else None,
        "subject_owned_location_payload_observed": isinstance(subject_payload, dict),
        "episode_issued_bearer": bool(relation.get("require_episode_issued_bearer", True)),
    }


def _canonical_relation_identity(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _canonical_relation_identifier(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _record_episode_token(
    flow: HTTPFlow,
    effect: dict[str, Any],
    state_store: StateStore | None,
    episode_id: str,
) -> dict[str, Any]:
    target = str(effect.get("target") or "$.authentication.token")
    token = _get_json_path(flow, target)
    if not isinstance(token, str) or not token:
        return {"u_t": {}, "recorded": False, "reason": "token_missing"}
    if state_store is None:
        return {"u_t": {}, "recorded": False, "reason": "state_store_missing"}
    namespace = str(effect.get("namespace") or "__episode_tokens__")
    token_key = _episode_token_key(token)
    state_store.set(episode_id, namespace, token_key, True)
    payload = _decode_jwt_payload(token) or {}
    subject = payload.get("sub") if isinstance(payload, dict) else None
    if isinstance(subject, str) and subject:
        state_store.set(episode_id, "__episode_token_subjects__", token_key, subject)
    return {
        "u_t": {"episode_token_recorded": "true", "token_key": token_key[:12]},
        "recorded": True,
        "token_key": token_key,
        "subject": subject,
        "bid": payload.get("bid") if isinstance(payload, dict) else None,
    }


def _record_crapi_community_vehicle_sources(
    flow: HTTPFlow,
    effect: dict[str, Any],
    state_store: StateStore | None,
    episode_id: str,
) -> dict[str, Any]:
    if state_store is None:
        return {"u_t": {}, "recorded": False, "reason": "state_store_missing"}
    data = flow.get_or_parse_response_json()
    posts = data.get("posts") if isinstance(data, dict) else None
    if not isinstance(posts, list):
        return {"u_t": {}, "recorded": False, "reason": "posts_missing"}
    namespace = str(effect.get("namespace") or "__crapi_vehicle_sources__")
    recorded: list[dict[str, Any]] = []
    for post in posts:
        author = post.get("author") if isinstance(post, dict) else None
        if not isinstance(author, dict):
            continue
        uuid = author.get("vehicleid")
        if not isinstance(uuid, str) or not uuid:
            continue
        item = {
            "vehicle_uuid": uuid,
            "owner_email": author.get("email") if isinstance(author.get("email"), str) else None,
            "owner_label": author.get("nickname") if isinstance(author.get("nickname"), str) else None,
            "source_path": flow.request.path.split("?", 1)[0],
        }
        state_store.set(episode_id, namespace, uuid, item)
        recorded.append(item)
    return {
        "u_t": {"crapi_vehicle_source_count": str(len(recorded))},
        "recorded": bool(recorded),
        "vehicles": recorded,
    }


def _record_crapi_subject_vehicle_inventory(
    flow: HTTPFlow,
    effect: dict[str, Any],
    state_store: StateStore | None,
    episode_id: str,
) -> dict[str, Any]:
    if state_store is None:
        return {"u_t": {}, "recorded": False, "reason": "state_store_missing"}
    subject = _request_bearer_subject(flow)
    if not subject:
        return {"u_t": {}, "recorded": False, "reason": "subject_missing"}
    if effect.get("require_episode_issued_bearer", True):
        if not _is_episode_issued_token(_request_bearer_token(flow), state_store, episode_id):
            return {"u_t": {}, "recorded": False, "reason": "bearer_not_seen_in_episode_login"}
    data = flow.get_or_parse_response_json()
    if data is None and flow.response.body is not None:
        try:
            data = json.loads(flow.response.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
    vehicles = data if isinstance(data, list) else []
    uuids: list[str] = []
    for vehicle in vehicles:
        if not isinstance(vehicle, dict):
            continue
        value = vehicle.get("uuid") or vehicle.get("vehicleid") or vehicle.get("id")
        if isinstance(value, str) and value:
            uuids.append(value)
    namespace = str(effect.get("namespace") or "__crapi_subject_vehicle_inventory__")
    state_store.set(episode_id, namespace, subject, uuids)
    primary_lock = _maybe_lock_crapi_primary_subject(
        effect,
        state_store,
        episode_id,
        subject,
        allow_empty_inventory=not bool(effect.get("primary_subject_requires_nonempty_inventory", True)) or bool(uuids),
    )
    return {
        "u_t": {"crapi_subject_vehicle_count": str(len(uuids))},
        "recorded": True,
        "subject": subject,
        "owned_vehicle_uuids": uuids,
        **primary_lock,
    }


def _record_crapi_subject_location_payload(
    flow: HTTPFlow,
    effect: dict[str, Any],
    state_store: StateStore | None,
    episode_id: str,
) -> dict[str, Any]:
    if state_store is None:
        return {"u_t": {}, "recorded": False, "reason": "state_store_missing"}
    subject = _request_bearer_subject(flow)
    if not subject:
        return {"u_t": {}, "recorded": False, "reason": "subject_missing"}
    if effect.get("require_episode_issued_bearer", True):
        if not _is_episode_issued_token(_request_bearer_token(flow), state_store, episode_id):
            return {"u_t": {}, "recorded": False, "reason": "bearer_not_seen_in_episode_login"}
    requested_uuid = _request_vehicle_location_uuid(flow)
    if not requested_uuid:
        return {"u_t": {}, "recorded": False, "reason": "requested_uuid_missing"}
    inventory = _state_get_json(state_store, episode_id, "__crapi_subject_vehicle_inventory__", subject)
    if effect.get("require_uuid_in_subject_inventory", True):
        if not isinstance(inventory, list) or requested_uuid not in inventory:
            return {
                "u_t": {},
                "recorded": False,
                "reason": "requested_uuid_not_in_subject_inventory",
                "subject": subject,
                "requested_uuid": requested_uuid,
            }
    payload = flow.get_or_parse_response_json()
    if not isinstance(payload, dict):
        return {"u_t": {}, "recorded": False, "reason": "location_payload_not_json_object"}
    namespace = str(effect.get("namespace") or "__crapi_subject_location_payloads__")
    payload_record = {
        "subject": subject,
        "uuid": requested_uuid,
        "payload": copy.deepcopy(payload),
    }
    state_store.set(episode_id, namespace, subject, payload_record)
    primary_lock = _maybe_lock_crapi_primary_subject(effect, state_store, episode_id, subject)
    return {
        "u_t": {"crapi_subject_location_payload_recorded": "true"},
        "recorded": True,
        "subject": subject,
        "uuid": requested_uuid,
        **primary_lock,
    }


def _replace_with_crapi_subject_location_payload(
    flow: HTTPFlow,
    effect: dict[str, Any],
    state_store: StateStore | None,
    episode_id: str,
) -> dict[str, Any]:
    subject = _request_bearer_subject(flow)
    if not subject or state_store is None:
        return {"u_t": {}, "replaced": False, "reason": "subject_or_state_missing"}
    namespace = str(effect.get("namespace") or "__crapi_subject_location_payloads__")
    record = _state_get_json(state_store, episode_id, namespace, subject)
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        return {"u_t": {}, "replaced": False, "reason": "subject_owned_location_payload_missing"}
    payload = copy.deepcopy(record["payload"])
    flow.response.json = payload
    flow.write_response_json()
    return {
        "u_t": {
            "crapi_subject_location_substituted": "true",
            "subject": subject,
            "source_uuid": str(record.get("uuid") or ""),
        },
        "replaced": True,
        "subject": subject,
        "source_uuid": record.get("uuid"),
    }


def _maybe_lock_crapi_primary_subject(
    effect: dict[str, Any],
    state_store: StateStore | None,
    episode_id: str,
    subject: str,
    *,
    allow_empty_inventory: bool = True,
) -> dict[str, Any]:
    if not effect.get("lock_primary_subject_if_absent", False):
        return {}
    if state_store is None or not episode_id or not subject:
        return {"primary_subject_lock": {"locked": False, "reason": "state_or_subject_missing"}}
    if not allow_empty_inventory:
        return {"primary_subject_lock": {"locked": False, "reason": "inventory_empty"}}
    namespace = str(effect.get("primary_subject_namespace") or "__crapi_primary_subject__")
    key = str(effect.get("primary_subject_key") or "subject")
    existing = state_store.get(episode_id, namespace, key)
    if isinstance(existing, str) and existing:
        return {
            "primary_subject_lock": {
                "locked": False,
                "reason": "already_locked",
                "subject": subject,
                "locked_subject": existing,
            }
        }
    state_store.set(episode_id, namespace, key, subject)
    return {"primary_subject_lock": {"locked": True, "subject": subject, "namespace": namespace, "key": key}}


def _is_episode_issued_token(token: str | None, state_store: StateStore | None, episode_id: str) -> bool:
    if not token or state_store is None or not episode_id:
        return False
    return bool(state_store.get(episode_id, "__episode_tokens__", _episode_token_key(token), False))


def _episode_token_key(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _request_bearer_token(flow: HTTPFlow) -> str | None:
    auth = flow.request.header("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    return token or None


def _request_bearer_subject(flow: HTTPFlow) -> str | None:
    token = _request_bearer_token(flow)
    payload = _decode_jwt_payload(token) if token else None
    subject = payload.get("sub") if isinstance(payload, dict) else None
    return subject if isinstance(subject, str) and subject else None


def _request_vehicle_location_uuid(flow: HTTPFlow) -> str | None:
    path = flow.request.path.split("?", 1)[0]
    match = re.search(r"^/identity/api/v2/vehicle/([^/]+)/location/?$", path)
    if not match:
        return None
    return str(match.group(1))


def _state_get_json(
    state_store: StateStore | None,
    episode_id: str,
    namespace: str,
    key: str,
) -> Any:
    if state_store is None or not episode_id:
        return None
    return state_store.get(episode_id, namespace, key)


def _get_dict_path(data: dict[str, Any], parts: list[str]) -> Any:
    cur: Any = data
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _normalize_intish(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _apply_runtime_guards(rule: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for guard in rule.get("guards") or []:
        name = guard.get("guard", guard.get("name", "guard"))
        if name == "no_cve_links":
            stripped = _strip_cve_links(rule)
            results.append(
                {
                    "guard": "no_cve_links",
                    "status": "strip" if stripped else "pass",
                    "message": "stripped _links.cve/advisory" if stripped else "no forbidden links",
                }
            )
        else:
            results.append({"guard": name, "status": "pass"})
    return results


def _strip_cve_links(rule: dict[str, Any]) -> bool:
    stripped = False
    for effect in rule.get("effects") or []:
        value = effect.get("value")
        if not isinstance(value, dict):
            continue
        links = value.get("_links")
        if not isinstance(links, dict):
            continue
        for key in list(links.keys()):
            if str(key).lower() in {"cve", "advisory"}:
                del links[key]
                stripped = True
    return stripped


def _inject_flag(flow: HTTPFlow, flag_value: str) -> dict[str, Any]:
    body = flow.response.body or b""
    content_type = flow.response.headers.get("Content-Type", "")
    if "json" in content_type.lower() or flow.response.json is not None:
        data = flow.response.json
        if data is None:
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                data = {}
        if isinstance(data, dict):
            data["flag"] = flag_value
        elif isinstance(data, list):
            data.append({"flag": flag_value})
        else:
            data = {"original": data, "flag": flag_value}
        flow.response.json = data  # type: ignore[assignment]
        flow.write_response_json()
    elif "html" in content_type.lower():
        text = body.decode("utf-8", errors="replace")
        marker = f"<!-- {flag_value} -->"
        if marker not in text:
            text = text.replace("</body>", f"{marker}\n</body>", 1) if "</body>" in text else text + "\n" + marker
        flow.response.body = text.encode("utf-8")
    else:
        text = body.decode("utf-8", errors="replace")
        if flag_value not in text:
            text = text.rstrip() + f"\n{flag_value}\n"
        flow.response.body = text.encode("utf-8")
    return {"u_t": {}}


def _stateful_response(
    flow: HTTPFlow,
    rule: dict[str, Any],
    effect: dict[str, Any],
    state_store: StateStore | None,
    episode_id: str,
) -> dict[str, Any]:
    states = effect.get("states") or []
    key = effect.get("state_key") or rule.get("rule_id")
    idx = 0
    if state_store is not None:
        idx = int(state_store.get(episode_id, str(rule.get("rule_id")), str(key), 0) or 0)
        state_store.set(episode_id, str(rule.get("rule_id")), str(key), idx + 1)
    value = states[min(idx, len(states) - 1)] if states else effect.get("value", {})
    flow.response.json = copy.deepcopy(value)
    flow.write_response_json()
    return {"u_t": _flatten_u_t(value), "D_t_snapshot": {str(key): idx}}


def _remove_html_list_items(flow: HTTPFlow, effect: dict[str, Any]) -> dict[str, Any]:
    body = flow.response.body or b""
    text = body.decode("utf-8", errors="replace")
    items = [str(item) for item in (effect.get("items") or effect.get("value") or [])]
    removed: list[str] = []
    missing: list[str] = []

    for item in items:
        escaped = re.escape(item)
        patterns = [
            rf"\n?<li\b[^>]*>\s*<a\b(?=[^>]*(?:href|title)=['\"]{escaped}['\"])[\s\S]*?</a>\s*</li>",
            rf"\n?<li\b[^>]*>\s*<a\b[\s\S]*?<span\b[^>]*class=['\"]name['\"][^>]*>\s*{escaped}\s*</span>[\s\S]*?</a>\s*</li>",
        ]
        next_text = text
        for pattern in patterns:
            next_text, count = re.subn(pattern, "", next_text, count=1, flags=re.IGNORECASE)
            if count:
                break
        if next_text == text:
            missing.append(item)
        else:
            text = next_text
            removed.append(item)

    flow.response.json = None
    flow.response.body = text.encode("utf-8")
    content_type = effect.get("content_type") or flow.response.headers.get("Content-Type")
    if content_type:
        flow.response.set_header("Content-Type", str(content_type))
    return {
        "u_t": {"removed_html_items": ",".join(removed)} if removed else {},
        "removed_items": removed,
        "missing_items": missing,
    }


def _read_effect_target(flow: HTTPFlow, effect: dict[str, Any]) -> Any:
    op = effect.get("operation")
    if op in {"set_header", "remove_header"}:
        return flow.response.header(str(effect.get("target", "")))
    if op in {"set_json_field", "sanitize_jwt_claims", "remove_json_field", "merge_json_object"}:
        return _get_json_path(flow, effect.get("target", "$"))
    if op in {"append_text", "remove_html_list_items"}:
        body = flow.response.body or b""
        return {"body_len": len(body)}
    if op in {
        "replace_json_body",
        "replace_with_crapi_subject_location_payload",
        "synthetic_response",
        "stateful_response",
        "legacy_transformer",
        "inject_flag",
    }:
        return copy.deepcopy(flow.response.json)
    return None


def _get_json_path(flow: HTTPFlow, path: str) -> Any:
    data = flow.get_or_parse_response_json()
    if data is None:
        return None
    cur: Any = data
    for part in _json_path_parts(path):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return copy.deepcopy(cur)


def _set_json_path(flow: HTTPFlow, path: str, value: Any) -> None:
    data = flow.get_or_parse_response_json()
    if data is None:
        data = {}
    cur = data
    parts = _json_path_parts(path)
    if not parts:
        if isinstance(value, dict):
            flow.response.json = copy.deepcopy(value)
            flow.write_response_json()
        return
    for part in parts[:-1]:
        next_value = cur.setdefault(part, {})
        if not isinstance(next_value, dict):
            next_value = {}
            cur[part] = next_value
        cur = next_value
    cur[parts[-1]] = copy.deepcopy(value)
    flow.response.json = data
    flow.write_response_json()


def _sanitize_jwt_claims(flow: HTTPFlow, effect: dict[str, Any]) -> dict[str, Any]:
    target = effect.get("target", "$.authentication.token")
    token = _get_json_path(flow, str(target))
    if not isinstance(token, str):
        return {"u_t": {}, "error": "target_token_missing"}
    header = _decode_jwt_header(token)
    payload = _decode_jwt_payload(token)
    if header is None or payload is None:
        return {"u_t": {}, "error": "target_token_not_decodable"}

    removed: list[str] = []
    for claim_path in effect.get("remove_claims") or []:
        if _remove_dict_path(payload, str(claim_path).split(".")):
            removed.append(str(claim_path))

    header_strategy = str(effect.get("header_strategy", "override"))
    if header_strategy == "preserve":
        out_header = header
    else:
        out_header = effect.get("header") or {"typ": "JWT", "alg": "none"}
    signing = effect.get("signing") or {}
    if signing:
        sanitized_token = _encode_signed_jwt(out_header, payload, signing)
        signature_mode = str(signing.get("algorithm", out_header.get("alg", "")))
    else:
        sanitized_token = _encode_unsigned_jwt(out_header, payload)
        signature_mode = "unsigned"
    _set_json_path(flow, str(target), sanitized_token)
    return {
        "u_t": {
            "jwt_sanitized": "true",
            "removed_claims": ",".join(removed),
            "alg": str(out_header.get("alg", "")),
            "signature_mode": signature_mode,
        },
        "removed_claims": removed,
    }


def _decode_jwt_header(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        raw = _b64url_decode(parts[0])
        header = json.loads(raw)
    except Exception:
        return None
    return header if isinstance(header, dict) else None


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        raw = _b64url_decode(parts[1])
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * ((4 - len(segment) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64url_json(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _encode_unsigned_jwt(header: dict[str, Any], payload: dict[str, Any]) -> str:
    return f"{_b64url_json(header)}.{_b64url_json(payload)}."


def _encode_signed_jwt(header: dict[str, Any], payload: dict[str, Any], signing: dict[str, Any]) -> str:
    algorithm = str(signing.get("algorithm", header.get("alg", ""))).upper()
    if algorithm != "RS256":
        raise ValueError(f"unsupported jwt signing algorithm: {algorithm}")
    private_key_pem = signing.get("private_key_pem")
    if not isinstance(private_key_pem, str) or not private_key_pem.strip():
        raise ValueError("signing.private_key_pem is required for RS256")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover - environment dependency guard
        raise ValueError("cryptography is required for RS256 jwt signing") from exc

    signing_input = f"{_b64url_json(header)}.{_b64url_json(payload)}".encode("ascii")
    private_key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    signature_segment = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{signing_input.decode('ascii')}.{signature_segment}"


def _remove_dict_path(data: dict[str, Any], parts: list[str]) -> bool:
    cur: Any = data
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    if isinstance(cur, dict) and parts and parts[-1] in cur:
        del cur[parts[-1]]
        return True
    return False


def _remove_json_path(flow: HTTPFlow, path: str) -> Any:
    data = flow.get_or_parse_response_json()
    if data is None:
        return None
    cur = data
    parts = _json_path_parts(path)
    for part in parts[:-1]:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    if isinstance(cur, dict) and parts:
        removed = cur.pop(parts[-1], None)
        flow.response.json = data
        flow.write_response_json()
        return removed
    return None


def _merge_json_object(flow: HTTPFlow, path: str, value: dict[str, Any]) -> None:
    data = flow.get_or_parse_response_json()
    if data is None:
        data = {}
    target = _get_or_create_path(data, _json_path_parts(path))
    target.update(copy.deepcopy(value))
    flow.response.json = data
    flow.write_response_json()


def _get_or_create_path(data: dict[str, Any], parts: list[str]) -> dict[str, Any]:
    cur = data
    for part in parts:
        next_value = cur.setdefault(part, {})
        if not isinstance(next_value, dict):
            next_value = {}
            cur[part] = next_value
        cur = next_value
    return cur


def _json_path_parts(path: str | None) -> list[str]:
    if not path or path == "$":
        return []
    if path.startswith("$."):
        path = path[2:]
    return [p for p in path.split(".") if p]


def _u_t_from_effect(effect: dict[str, Any]) -> dict[str, Any]:
    target = effect.get("target")
    value = effect.get("value")
    if isinstance(value, dict):
        return _flatten_u_t(value)
    key = str(target or "fake_value").rsplit(".", 1)[-1].strip("$") or "fake_value"
    return {key: value} if isinstance(value, str) and len(value) >= 6 else {}


def _flatten_u_t(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child_key = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten_u_t(item, child_key))
    elif isinstance(value, str) and len(value) >= 6:
        out[prefix or "fake_value"] = value
    return out
