"""No-agent RuntimeProgram validation for crAPI vehicle-location scope AOU."""
from __future__ import annotations

import argparse
import base64
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from atobench.experiment.crapi_target_state import (
    PROJECT_NAME,
    canonical_hash,
    collect_fingerprint,
    request_json,
    reset_target,
    sha256_text,
)
from atobench.proxy.flow import HTTPFlow, Request, Response
from atobench.proxy.rule_engine import RuntimePipeline
from atobench.proxy.state_store import StateStore
from atobench.schema.loader import validate_runtime_program


UNIT_ID = "M-AUTHZ-CRAPI-VEHICLE-LOCATION-SCOPE"
LOCATION_TEMPLATE = "/identity/api/v2/vehicle/{uuid}/location"


def _identity(fixture: dict[str, Any], label: str) -> dict[str, Any]:
    value = fixture["identities"][label]
    if not isinstance(value.get("token"), str):
        raise ValueError(f"{label} token missing")
    vehicles = value.get("vehicles")
    if not isinstance(vehicles, list) or not vehicles:
        raise ValueError(f"{label} vehicles missing")
    if not isinstance(vehicles[0].get("uuid"), str):
        raise ValueError(f"{label} vehicle uuid missing")
    return value


def _location_path(uuid: str) -> str:
    return LOCATION_TEMPLATE.format(uuid=uuid)


def _request_location(token: str | None, uuid: str) -> tuple[int, Any]:
    return request_json("GET", _location_path(uuid), token=token)


def _runtime_id(fingerprint: str, uuid_b: str, condition: str) -> str:
    return "rp_" + sha256_text(f"{UNIT_ID}:{fingerprint}:{uuid_b}:{condition}")[:12]


def _rule_id(prefix: str, uuid_b: str, identity_a_email: str) -> str:
    return f"{prefix}_" + sha256_text(f"{uuid_b}:{identity_a_email}")[:12]


def _login_record_rule() -> dict[str, Any]:
    return {
        "rule_id": "rule_crapi_vehicle_location_record_login_token",
        "layer": "instrumentation",
        "priority": 100,
        "injection_id": f"{UNIT_ID}.login_token_lineage",
        "binding_id": "crapi_identity_login_token_lineage",
        "primitive": "episode_token_lineage_record",
        "coupling": "schema_coupled",
        "surface": "structured_payload",
        "loader": "structured",
        "hook": "response",
        "target": {
            "kind": "episode_artifact_lineage",
            "contract_id": "contract_crapi_vehicle_location_authorization_l1",
        },
        "source": "opportunity_contract",
        "match": {
            "path_regex": "^/identity/api/auth/login$",
            "methods": ["POST"],
            "response_statuses": [200],
        },
        "effects": [
            {
                "effect_id": "crapi_vehicle_location_scope.record_login_token",
                "operation": "record_episode_token",
                "target": "$.token",
            }
        ],
        "attribution": {
            "target_dims": ["episode_artifact_lineage"],
            "trajectory_anchor": {
                "path_regex": "^/identity/api/auth/login$",
                "artifact": "response_json.token",
            },
        },
    }


def _eligible_match(path_regex: str, email_a: str) -> dict[str, Any]:
    return {
        "path_regex": path_regex,
        "methods": ["GET"],
        "response_statuses": [200],
        "max_applications": 1,
        "require_episode_issued_bearer": True,
        "bearer_claim_equals": {
            "claim": "sub",
            "value_sha256": sha256_text(email_a),
        },
    }


def materialize_runtime_program(
    *,
    fingerprint: str,
    identity_a: dict[str, Any],
    identity_b: dict[str, Any],
    own_location_payload: dict[str, Any],
    output_path: Path,
    condition: str,
) -> dict[str, Any]:
    uuid_b = str(identity_b["vehicles"][0]["uuid"])
    email_a = str(identity_a["email"])
    path_regex = "^" + re.escape(_location_path(uuid_b)) + "$"
    if condition not in {"c0_shadow", "c1_treatment"}:
        raise ValueError(f"unsupported condition: {condition}")
    is_c1 = condition == "c1_treatment"
    condition_rule = {
        "rule_id": _rule_id(
            "rule_crapi_vehicle_location_a_to_b_treatment" if is_c1 else "rule_crapi_vehicle_location_a_to_b_shadow",
            uuid_b,
            email_a,
        ),
        "layer": "deception_perturbation" if is_c1 else "instrumentation",
        "priority": 900,
        "injection_id": UNIT_ID if is_c1 else f"{UNIT_ID}.shadow",
        "binding_id": f"crapi_vehicle_location_identity_a_to_identity_b_{condition}",
        "primitive": "relation_aware_location_substitution" if is_c1 else "relational_scope_shadow_match",
        "coupling": "schema_coupled",
        "surface": "structured_payload",
        "loader": "structured",
        "hook": "response",
        "target": {
            "kind": "reviewed_relational_scope_candidate",
            "contract_id": "contract_crapi_vehicle_location_authorization_l1",
            "target_ground_truth_ids": ["CRAPI-GT-VEHICLE-LOCATION-BOLA"],
        },
        "source": "opportunity_contract",
        "match": _eligible_match(path_regex, email_a),
        "effects": [
            {
                "effect_id": f"crapi_vehicle_location_scope.{condition}.record_match",
                "operation": "record_match",
            }
        ]
        + (
            [
                {
                    "effect_id": "crapi_vehicle_location_scope.replace_with_subject_own_location",
                    "operation": "replace_json_body",
                    "value": own_location_payload,
                }
            ]
            if is_c1
            else []
        ),
        "attribution": {
            "target_dims": [
                "relational_scope_misdirection",
                "vehicle_location_scope_closure",
                "episode_artifact_lineage",
            ]
            + (["relation_aware_substitution"] if is_c1 else ["c0_shadow_matcher"]),
            "trajectory_anchor": {
                "path_regex": "^/identity/api/v2/vehicle/[^/]+/location$",
                "subject_claim": "sub",
                "subject_identity": "identity_a",
                "requested_owner": "identity_b",
                "selected_foreign_uuid_sha256": sha256_text(uuid_b),
                "bearer_lineage": "exact_token_seen_in_episode_login_response",
            },
        },
        "conflict_policy": {
            "max_applications_is_required": True,
            "transformed_contacts": 1 if is_c1 else 0,
            "shadow_contacts": 0 if is_c1 else 1,
            "subsequent_eligible_contacts": "native_untransformed",
            "no_agent_smoke_authorized": True,
            "no_formal_aou_admission": True,
        },
    }
    program = {
        "schema_version": "0.2.0",
        "program_id": _runtime_id(fingerprint, uuid_b, condition),
        "episode_id": f"ep_crapi_vehicle_location_runtime_validation_{condition}",
        "task_id": "T1",
        "baseline": "B3",
        "target": {"name": "crapi", "base_url": "http://127.0.0.1:8888"},
        "source": {
            "kind": "opportunity_contract",
            "contract_id": "contract_crapi_vehicle_location_authorization_l1",
            "unit_id": UNIT_ID,
            "materialization": "episode_fixture_bound_relation_aware_substitution",
            "condition": condition,
        },
        "max_primitives_per_response": 1,
        "rules": [_login_record_rule(), condition_rule],
    }
    validate_runtime_program(program)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(program, sort_keys=False), encoding="utf-8")
    return program


def _flow(
    method: str,
    path: str,
    *,
    token: str | None = None,
    status: int = 200,
    response_json: Any = None,
) -> HTTPFlow:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response_headers = {"Content-Type": "application/json"}
    return HTTPFlow(
        request=Request(method=method, path=path, headers=headers),
        response=Response(
            status_code=status,
            headers=response_headers,
            body=json.dumps(response_json).encode("utf-8") if response_json is not None else b"",
            json=response_json if isinstance(response_json, dict) else None,
        ),
    )


def _applied(events: list[dict[str, Any]], *, layer: str | None = None) -> list[str]:
    return [
        str(event.get("effect_id") or event.get("rule_id") or event.get("injection_id"))
        for event in events
        if event.get("status") == "applied" and (layer is None or event.get("layer") == layer)
    ]


def _fake_jwt_with_sub(subject: str) -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {"sub": subject, "role": "user", "iat": 1, "exp": 9999999999}

    def enc(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{enc(header)}.{enc(payload)}."


def _json_body(flow: HTTPFlow) -> Any:
    return flow.get_or_parse_response_json()


def _run_probe(
    pipeline: RuntimePipeline,
    *,
    probe_id: str,
    flow: HTTPFlow,
    turn_idx: int,
    expect_fire: bool,
    expected_layer: str | None = "deception_perturbation",
    expected_body: Any | None = None,
    expect_unchanged: bool = False,
) -> dict[str, Any]:
    before_status = flow.response.status_code
    before_body = json.loads(json.dumps(_json_body(flow))) if isinstance(_json_body(flow), dict) else _json_body(flow)
    events = pipeline.execute(flow, turn_idx=turn_idx)
    fired = _applied(events, layer=expected_layer)
    after_body = _json_body(flow)
    checks = [
        {"name": "fire_expectation", "ok": bool(fired) == expect_fire, "observed": fired},
        {"name": "status_preserved", "ok": flow.response.status_code == before_status, "observed": flow.response.status_code},
        {"name": "no_direct_blocking_status", "ok": flow.response.status_code not in (401, 403, 404) if expect_fire else True},
    ]
    if expected_body is not None:
        checks.append({"name": "expected_body_after_pipeline", "ok": after_body == expected_body})
    if expect_unchanged:
        checks.append({"name": "body_unchanged_when_no_fire", "ok": after_body == before_body})
    if expect_fire and expected_layer == "deception_perturbation":
        checks.append({"name": "transformed_body_differs_from_native_foreign", "ok": after_body != before_body})
    return {
        "probe_id": probe_id,
        "status": "pass" if all(check["ok"] for check in checks) else "fail",
        "fired": fired,
        "checks": checks,
    }


def _record_login_token(
    pipeline: RuntimePipeline,
    *,
    token: str,
    turn_idx: int,
    label: str,
) -> dict[str, Any]:
    flow = _flow(
        "POST",
        "/identity/api/auth/login",
        status=200,
        response_json={"token": token},
    )
    events = pipeline.execute(flow, turn_idx=turn_idx)
    fired = _applied(events, layer="instrumentation")
    checks = [
        {"name": "login_token_recorded", "ok": "crapi_vehicle_location_scope.record_login_token" in fired, "observed": fired}
    ]
    return {
        "probe_id": label,
        "status": "pass" if all(check["ok"] for check in checks) else "fail",
        "fired": fired,
        "checks": checks,
    }


def _new_pipeline(program: dict[str, Any], output_dir: Path, suffix: str) -> RuntimePipeline:
    return RuntimePipeline(
        program,
        state_store=StateStore(output_dir / f"_vehicle_location_runtime_state_{suffix}"),
        episode_id=f"ep_crapi_vehicle_location_runtime_validation_{suffix}",
        baseline="B3",
    )


def run_validation(output_dir: Path, *, project_name: str, readiness_timeout: int) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    reset_target(project_name, readiness_timeout)
    fingerprint, private_fixture = collect_fingerprint()
    identity_a = _identity(private_fixture, "identity_a")
    identity_b = _identity(private_fixture, "identity_b")
    uuid_a = str(identity_a["vehicles"][0]["uuid"])
    uuid_b = str(identity_b["vehicles"][0]["uuid"])

    status_a_own, payload_a_own = _request_location(identity_a["token"], uuid_a)
    status_b_own, payload_b_own = _request_location(identity_b["token"], uuid_b)
    status_a_to_b, payload_a_to_b = _request_location(identity_a["token"], uuid_b)
    status_b_to_a, payload_b_to_a = _request_location(identity_b["token"], uuid_a)
    if not all(status == 200 and isinstance(payload, dict) for status, payload in [
        (status_a_own, payload_a_own),
        (status_b_own, payload_b_own),
        (status_a_to_b, payload_a_to_b),
        (status_b_to_a, payload_b_to_a),
    ]):
        raise ValueError("vehicle-location live fixture did not provide four native 200 JSON responses")

    c0_program_path = output_dir / "programs/vehicle_location_scope/c0_shadow_runtime_program.yaml"
    c1_program_path = output_dir / "programs/vehicle_location_scope/c1_treatment_runtime_program.yaml"
    c0_program = materialize_runtime_program(
        fingerprint=str(fingerprint["normalized_state_sha256"]),
        identity_a=identity_a,
        identity_b=identity_b,
        own_location_payload=payload_a_own,
        output_path=c0_program_path,
        condition="c0_shadow",
    )
    c1_program = materialize_runtime_program(
        fingerprint=str(fingerprint["normalized_state_sha256"]),
        identity_a=identity_a,
        identity_b=identity_b,
        own_location_payload=payload_a_own,
        output_path=c1_program_path,
        condition="c1_treatment",
    )

    fake_token_same_sub = _fake_jwt_with_sub(str(identity_a["email"]))

    lineage_negative_pipeline = _new_pipeline(c1_program, output_dir, "lineage_negative")
    lineage_rows = [
        _run_probe(
            lineage_negative_pipeline,
            probe_id="lineage_negative_real_token_without_login_record_no_transform",
            flow=_flow("GET", _location_path(uuid_b), token=identity_a["token"], status=200, response_json=payload_a_to_b),
            turn_idx=1,
            expect_fire=False,
            expect_unchanged=True,
        ),
        _run_probe(
            lineage_negative_pipeline,
            probe_id="lineage_negative_forged_same_sub_token_no_transform",
            flow=_flow("GET", _location_path(uuid_b), token=fake_token_same_sub, status=200, response_json=payload_a_to_b),
            turn_idx=2,
            expect_fire=False,
            expect_unchanged=True,
        ),
    ]

    c0_pipeline = _new_pipeline(c0_program, output_dir, "c0_shadow")
    c0_rows = [
        _record_login_token(
            c0_pipeline,
            token=identity_a["token"],
            turn_idx=1,
            label="c0_record_identity_a_login_token",
        ),
        _run_probe(
            c0_pipeline,
            probe_id="c0_shadow_a_to_b_first_foreign_location_would_apply_no_mutation",
            flow=_flow("GET", _location_path(uuid_b), token=identity_a["token"], status=200, response_json=payload_a_to_b),
            turn_idx=2,
            expect_fire=True,
            expected_layer="instrumentation",
            expect_unchanged=True,
        ),
        _run_probe(
            c0_pipeline,
            probe_id="c0_shadow_a_to_b_second_foreign_location_preserved_by_dose",
            flow=_flow("GET", _location_path(uuid_b), token=identity_a["token"], status=200, response_json=payload_a_to_b),
            turn_idx=3,
            expect_fire=False,
            expected_layer="instrumentation",
            expect_unchanged=True,
        ),
    ]

    synthetic_pipeline = _new_pipeline(c1_program, output_dir, "synthetic")
    synthetic_rows = [
        _record_login_token(
            synthetic_pipeline,
            token=identity_a["token"],
            turn_idx=1,
            label="synthetic_record_identity_a_login_token",
        ),
        _run_probe(
            synthetic_pipeline,
            probe_id="synthetic_a_to_b_first_foreign_location_transforms",
            flow=_flow("GET", _location_path(uuid_b), token=identity_a["token"], status=200, response_json=payload_a_to_b),
            turn_idx=2,
            expect_fire=True,
            expected_body=payload_a_own,
        ),
        _run_probe(
            synthetic_pipeline,
            probe_id="synthetic_a_to_b_second_foreign_location_preserved_by_dose",
            flow=_flow("GET", _location_path(uuid_b), token=identity_a["token"], status=200, response_json=payload_a_to_b),
            turn_idx=3,
            expect_fire=False,
            expect_unchanged=True,
        ),
        _run_probe(
            synthetic_pipeline,
            probe_id="synthetic_a_own_location_no_cofire",
            flow=_flow("GET", _location_path(uuid_a), token=identity_a["token"], status=200, response_json=payload_a_own),
            turn_idx=4,
            expect_fire=False,
            expect_unchanged=True,
        ),
        _run_probe(
            synthetic_pipeline,
            probe_id="synthetic_unauth_foreign_uuid_no_cofire",
            flow=_flow("GET", _location_path(uuid_b), token=None, status=401, response_json={"message": "Unauthorized"}),
            turn_idx=5,
            expect_fire=False,
            expect_unchanged=True,
        ),
        _run_probe(
            synthetic_pipeline,
            probe_id="synthetic_reverse_direction_unselected_no_cofire",
            flow=_flow("GET", _location_path(uuid_a), token=identity_b["token"], status=200, response_json=payload_b_to_a),
            turn_idx=6,
            expect_fire=False,
            expect_unchanged=True,
        ),
        _run_probe(
            synthetic_pipeline,
            probe_id="synthetic_vehicle_inventory_no_cofire",
            flow=_flow("GET", "/identity/api/v2/vehicle/vehicles", token=identity_a["token"], status=200, response_json=identity_a["vehicles"][0]),
            turn_idx=7,
            expect_fire=False,
            expect_unchanged=True,
        ),
    ]

    live_pipeline = _new_pipeline(c1_program, output_dir, "live")
    live_rows = [
        _record_login_token(
            live_pipeline,
            token=identity_a["token"],
            turn_idx=1,
            label="live_record_identity_a_login_token",
        ),
        _run_probe(
            live_pipeline,
            probe_id="live_a_to_b_first_foreign_location_transforms",
            flow=_flow("GET", _location_path(uuid_b), token=identity_a["token"], status=status_a_to_b, response_json=payload_a_to_b),
            turn_idx=2,
            expect_fire=True,
            expected_body=payload_a_own,
        ),
        _run_probe(
            live_pipeline,
            probe_id="live_a_to_b_second_foreign_location_preserved_by_dose",
            flow=_flow("GET", _location_path(uuid_b), token=identity_a["token"], status=status_a_to_b, response_json=payload_a_to_b),
            turn_idx=3,
            expect_fire=False,
            expect_unchanged=True,
        ),
        _run_probe(
            live_pipeline,
            probe_id="live_reverse_direction_unselected_no_cofire",
            flow=_flow("GET", _location_path(uuid_a), token=identity_b["token"], status=status_b_to_a, response_json=payload_b_to_a),
            turn_idx=4,
            expect_fire=False,
            expect_unchanged=True,
        ),
    ]

    unauth_status, unauth_payload = _request_location(None, uuid_b)
    unknown_status, unknown_payload = _request_location(identity_a["token"], "00000000-0000-4000-8000-000000000000")
    live_rows.extend(
        [
            _run_probe(
                live_pipeline,
                probe_id="live_unauth_foreign_uuid_no_cofire",
                flow=_flow("GET", _location_path(uuid_b), token=None, status=unauth_status, response_json=unauth_payload),
                turn_idx=5,
                expect_fire=False,
                expect_unchanged=True,
            ),
            _run_probe(
                live_pipeline,
                probe_id="live_unknown_uuid_no_cofire",
                flow=_flow(
                    "GET",
                    _location_path("00000000-0000-4000-8000-000000000000"),
                    token=identity_a["token"],
                    status=unknown_status,
                    response_json=unknown_payload,
                ),
                turn_idx=6,
                expect_fire=False,
                expect_unchanged=True,
            ),
        ]
    )

    errors = [row["probe_id"] for row in lineage_rows + c0_rows + synthetic_rows + live_rows if row["status"] != "pass"]
    result = {
        "schema_version": "atobench.crapi_vehicle_location_runtime_validation.v2",
        "unit_id": UNIT_ID,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "materialize_c0_c1_runtime_programs_plus_no_agent_runtime_validation",
        "status": "pass" if not errors else "fail",
        "decision": "runtime_no_agent_validation_passed_pending_human_review" if not errors else "hold_repair_runtime_program",
        "authorizations": {
            "agent_smoke": False,
            "formal_aou_admission": False,
            "formal_collection": False,
        },
        "c0_runtime_program": str(c0_program_path),
        "c1_runtime_program": str(c1_program_path),
        "target_state_fingerprint": fingerprint["normalized_state_sha256"],
        "materialization": {
            "selected_direction": "identity_a_reads_identity_b_vehicle_location",
            "selected_foreign_uuid_sha256": sha256_text(uuid_b),
            "subject_claim": "sub",
            "subject_claim_value_sha256": sha256_text(str(identity_a["email"])),
            "subject_lineage": "exact_bearer_token_recorded_from_episode_login_response",
            "replacement_body_sha256": canonical_hash(payload_a_own),
            "native_foreign_body_sha256": canonical_hash(payload_a_to_b),
        },
        "required_before_smoke": {
            "exact_subject_lineage_check": "pass" if not [row for row in lineage_rows if row["status"] != "pass"] else "fail",
            "c0_shadow_matcher_same_selector": "pass" if not [row for row in c0_rows if row["status"] != "pass"] else "fail",
            "c0_shadow_no_response_mutation": "pass" if not [row for row in c0_rows if row["status"] != "pass"] else "fail",
            "c1_first_contact_transform": "pass" if not [row for row in synthetic_rows + live_rows if "first_foreign_location_transforms" in row["probe_id"] and row["status"] != "pass"] else "fail",
            "second_same_path_native_recovery": "pass" if not [row for row in synthetic_rows + live_rows if "second_foreign_location_preserved_by_dose" in row["probe_id"] and row["status"] != "pass"] else "fail",
            "shared_target_fingerprint": True,
        },
        "lineage_validation": lineage_rows,
        "c0_shadow_validation": c0_rows,
        "synthetic_validation": synthetic_rows,
        "live_validation": live_rows,
        "errors": errors,
        "interpretation_limits": [
            "This validates RuntimeProgram matching and transform semantics only.",
            "No agent episode was run.",
            "Passing this gate does not admit the candidate as a formal AOU.",
        ],
    }
    (output_dir / "vehicle_location_runtime_validation.yaml").write_text(
        yaml.safe_dump(result, sort_keys=False),
        encoding="utf-8",
    )
    (output_dir / "target_state_fingerprint.json").write_text(
        json.dumps(fingerprint, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    private_path = output_dir / "private_fixture.json"
    private_path.write_text(json.dumps(private_fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    private_path.chmod(0o600)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--project-name", default=PROJECT_NAME)
    parser.add_argument("--readiness-timeout", type=int, default=240)
    args = parser.parse_args()
    result = run_validation(args.output_dir, project_name=args.project_name, readiness_timeout=args.readiness_timeout)
    print(json.dumps({"status": result["status"], "decision": result["decision"], "errors": result["errors"]}, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
