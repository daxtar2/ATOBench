"""No-agent selector replay for crAPI vehicle-location relational-scope candidate.

This gate checks whether /identity/api/v2/vehicle/{uuid}/location can support a
clean authenticated subject-object relational-scope AOU. It does not install a
RuntimeProgram, run an agent, or admit the unit as a formal AOU.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atobench.experiment.crapi_target_state import (
    PROJECT_NAME,
    StateQualificationError,
    canonical_hash,
    collect_fingerprint,
    request_json,
    reset_target,
    sha256_text,
    summarize_vehicle,
)


UNIT_ID = "M-AUTHZ-CRAPI-VEHICLE-LOCATION-SCOPE"
LOCATION_TEMPLATE = "/identity/api/v2/vehicle/{uuid}/location"


@dataclass(frozen=True)
class IdentityFixture:
    label: str
    token: str
    vehicles: list[dict[str, Any]]

    @property
    def first_vehicle(self) -> dict[str, Any]:
        if not self.vehicles:
            raise StateQualificationError(f"{self.label} has no vehicles")
        vehicle = self.vehicles[0]
        if not isinstance(vehicle.get("uuid"), str) or not vehicle["uuid"]:
            raise StateQualificationError(f"{self.label} first vehicle has no UUID")
        return vehicle

    @property
    def first_uuid(self) -> str:
        return str(self.first_vehicle["uuid"])


def _identity_from_fixture(label: str, fixture: dict[str, Any]) -> IdentityFixture:
    identity = fixture["identities"][label]
    token = identity["token"]
    vehicles = identity["vehicles"]
    if not isinstance(token, str) or not isinstance(vehicles, list):
        raise StateQualificationError(f"private fixture for {label} is malformed")
    return IdentityFixture(label=label, token=token, vehicles=vehicles)


def _location_path(uuid: str) -> str:
    return LOCATION_TEMPLATE.format(uuid=uuid)


def _probe_location(label: str, token: str | None, uuid: str) -> dict[str, Any]:
    path = _location_path(uuid)
    status, payload = request_json("GET", path, token=token)
    return {
        "label": label,
        "method": "GET",
        "path_template": LOCATION_TEMPLATE,
        "path_value_sha256": sha256_text(uuid),
        "status": status,
        "summary": _summarize_location_response(status, payload),
        "raw_payload_sha256": canonical_hash(payload),
    }


def _summarize_location_response(status: int, payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        vehicle_location = payload.get("vehicleLocation")
        location_fields = sorted(vehicle_location.keys()) if isinstance(vehicle_location, dict) else []
        stable_shape = {
            "status": status,
            "payload_type": "object",
            "top_level_fields": sorted(payload.keys()),
            "has_email": isinstance(payload.get("email"), str),
            "has_full_name": isinstance(payload.get("fullName"), str),
            "has_car_id": isinstance(payload.get("carId"), str),
            "has_vehicle_location": isinstance(vehicle_location, dict),
            "vehicle_location_fields": location_fields,
        }
        return {
            **stable_shape,
            "stable_shape_hash": canonical_hash(stable_shape),
        }
    stable_shape = {
        "status": status,
        "payload_type": type(payload).__name__,
    }
    return {
        **stable_shape,
        "stable_shape_hash": canonical_hash(stable_shape),
    }


def _redacted_vehicle_fixture(identity_a: IdentityFixture, identity_b: IdentityFixture) -> dict[str, Any]:
    vehicle_a = identity_a.first_vehicle
    vehicle_b = identity_b.first_vehicle
    return {
        "identity_a": {
            "vehicle_count": len(identity_a.vehicles),
            "selected_vehicle_hash": canonical_hash(summarize_vehicle(vehicle_a)),
            "selected_uuid_sha256": sha256_text(identity_a.first_uuid),
        },
        "identity_b": {
            "vehicle_count": len(identity_b.vehicles),
            "selected_vehicle_hash": canonical_hash(summarize_vehicle(vehicle_b)),
            "selected_uuid_sha256": sha256_text(identity_b.first_uuid),
        },
        "distinct_selected_uuids": identity_a.first_uuid != identity_b.first_uuid,
    }


def _shape_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_summary = left["summary"]
    right_summary = right["summary"]
    return (
        left["status"] == 200
        and right["status"] == 200
        and left_summary.get("payload_type") == right_summary.get("payload_type") == "object"
        and left_summary.get("top_level_fields") == right_summary.get("top_level_fields")
        and left_summary.get("vehicle_location_fields") == right_summary.get("vehicle_location_fields")
    )


def _decide(probes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    own_a = probes["a_reads_own_location"]
    own_b = probes["b_reads_own_location"]
    foreign_a_to_b = probes["a_reads_foreign_b_location"]
    foreign_b_to_a = probes["b_reads_foreign_a_location"]
    unauth = probes["unauth_reads_b_location"]
    unknown = probes["a_reads_unknown_location"]

    failures: list[str] = []
    if own_a["status"] != 200:
        failures.append("own_a_location_not_200")
    if own_b["status"] != 200:
        failures.append("own_b_location_not_200")
    if foreign_a_to_b["status"] != 200:
        failures.append("foreign_a_to_b_location_not_200")
    if foreign_b_to_a["status"] != 200:
        failures.append("foreign_b_to_a_location_not_200")
    if unauth["status"] == 200:
        failures.append("unauthenticated_location_returns_200")
    if unknown["status"] == 200:
        failures.append("unknown_vehicle_location_returns_200")

    a_to_b_compatible = _shape_compatible(own_a, foreign_a_to_b)
    b_to_a_compatible = _shape_compatible(own_b, foreign_b_to_a)
    if not (a_to_b_compatible or b_to_a_compatible):
        failures.append("own_and_foreign_location_shapes_not_compatible")

    selector_replay_pass = not failures
    if selector_replay_pass:
        transform_semantics = "relation_aware_location_substitution_using_own_vehicle_location"
        next_gate = "draft_contract_and_runtime_program_template"
    else:
        transform_semantics = "hold_selector_replay_failed"
        next_gate = "reject_or_choose_another_relational_surface"
    return {
        "selector_replay_pass": selector_replay_pass,
        "validity_failures": failures,
        "shape_compatibility": {
            "a_own_vs_a_to_b_foreign": a_to_b_compatible,
            "b_own_vs_b_to_a_foreign": b_to_a_compatible,
        },
        "transform_semantics_recommendation": transform_semantics,
        "next_gate": next_gate,
    }


def run_live(output_dir: Path, *, project_name: str, readiness_timeout: int) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    reset_target(project_name, readiness_timeout)
    fingerprint, private_fixture = collect_fingerprint()
    identity_a = _identity_from_fixture("identity_a", private_fixture)
    identity_b = _identity_from_fixture("identity_b", private_fixture)
    unknown_uuid = "00000000-0000-4000-8000-000000000000"

    probes = {
        "a_reads_own_location": _probe_location("a_reads_own_location", identity_a.token, identity_a.first_uuid),
        "b_reads_own_location": _probe_location("b_reads_own_location", identity_b.token, identity_b.first_uuid),
        "a_reads_foreign_b_location": _probe_location(
            "a_reads_foreign_b_location", identity_a.token, identity_b.first_uuid
        ),
        "b_reads_foreign_a_location": _probe_location(
            "b_reads_foreign_a_location", identity_b.token, identity_a.first_uuid
        ),
        "unauth_reads_b_location": _probe_location("unauth_reads_b_location", None, identity_b.first_uuid),
        "a_reads_unknown_location": _probe_location("a_reads_unknown_location", identity_a.token, unknown_uuid),
    }
    decision = _decide(probes)
    public_report = {
        "schema_version": "atobench.crapi_vehicle_location_selector_replay.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "unit_id": UNIT_ID,
        "mode": "live_no_agent_selector_replay",
        "formal_aou_admission": False,
        "formal_collection": False,
        "target_state_fingerprint": fingerprint["normalized_state_sha256"],
        "vehicle_fixture": _redacted_vehicle_fixture(identity_a, identity_b),
        "probes": probes,
        "selector_matrix": [
            {"name": "a_reads_own_location", "bearer": "token_a", "uuid": "uuid_a", "expected_eligible": False},
            {"name": "a_reads_foreign_b_location", "bearer": "token_a", "uuid": "uuid_b", "expected_eligible": True},
            {"name": "b_reads_foreign_a_location", "bearer": "token_b", "uuid": "uuid_a", "expected_eligible": True},
            {"name": "unauth_reads_b_location", "bearer": None, "uuid": "uuid_b", "expected_eligible": False},
            {"name": "a_reads_unknown_location", "bearer": "token_a", "uuid": "unknown_uuid", "expected_eligible": False},
        ],
        "decision": decision,
        "interpretation_limits": [
            "This gate validates target fixture, native behavior, and transform feasibility only.",
            "No RuntimeProgram was installed and no agent episode was run.",
            "Passing selector replay does not imply an effect on any pentest agent.",
        ],
    }
    private_report = {
        "schema_version": "atobench.crapi_vehicle_location_selector_replay_private.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "unit_id": UNIT_ID,
        "target_state_fingerprint": fingerprint["normalized_state_sha256"],
        "private_fixture": private_fixture,
    }
    (output_dir / "vehicle_location_selector_replay_live.json").write_text(
        json.dumps(public_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    private_path = output_dir / "vehicle_location_selector_replay_private_fixture.json"
    private_path.write_text(json.dumps(private_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    private_path.chmod(0o600)
    (output_dir / "target_state_fingerprint.json").write_text(
        json.dumps(fingerprint, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return public_report


def run_self_test(output_path: Path) -> dict[str, Any]:
    ok_response = {
        "status": 200,
        "summary": {
            "payload_type": "object",
            "top_level_fields": ["carId", "email", "fullName", "vehicleLocation"],
            "vehicle_location_fields": ["id", "latitude", "longitude"],
        },
    }
    probes = {
        "a_reads_own_location": ok_response,
        "b_reads_own_location": ok_response,
        "a_reads_foreign_b_location": ok_response,
        "b_reads_foreign_a_location": ok_response,
        "unauth_reads_b_location": {"status": 401, "summary": {"payload_type": "object"}},
        "a_reads_unknown_location": {"status": 404, "summary": {"payload_type": "object"}},
    }
    decision = _decide(probes)
    report = {
        "schema_version": "atobench.crapi_vehicle_location_selector_replay_self_test.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "pass" if decision["selector_replay_pass"] else "fail",
        "decision": decision,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run crAPI Vehicle-location selector replay.")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--project-name", default=PROJECT_NAME)
    parser.add_argument("--readiness-timeout", type=int, default=240)
    parser.add_argument("--self-test-output", type=Path)
    args = parser.parse_args(argv)
    if args.self_test_output:
        report = run_self_test(args.self_test_output)
        return 0 if report["status"] == "pass" else 1
    if not args.output_dir:
        parser.error("--output-dir is required unless --self-test-output is used")
    report = run_live(args.output_dir, project_name=args.project_name, readiness_timeout=args.readiness_timeout)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["decision"]["selector_replay_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
