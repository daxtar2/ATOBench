"""Selector replay for the JWT hash-suppression AOU candidate."""
from __future__ import annotations

import argparse
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from atobench.proxy.flow import HTTPFlow, Request, Response
from atobench.proxy.rule_engine import RuntimePipeline
from atobench.proxy.state_store import StateStore
from atobench.schema.loader import validate_runtime_program


def _b64u(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_segment(segment: str) -> dict[str, Any]:
    padded = segment + "=" * ((4 - len(segment) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def _token(email: str = "admin@juice-sh.op", bid: int = 1) -> str:
    payload = {
        "data": {
            "id": 1,
            "username": "",
            "email": email,
            "password": "0192023a7bbd73250516f069df18b500",
            "role": "admin",
            "deluxeToken": "",
            "lastLoginIp": "",
            "profileImage": "assets/public/images/uploads/defaultAdmin.png",
            "totpSecret": "",
            "isActive": True,
        },
        "bid": bid,
        "iat": 1783860000,
    }
    return f"{_b64u({'typ': 'JWT', 'alg': 'RS256'})}.{_b64u(payload)}.signature"


def _flow(method: str, path: str, status: int, body: dict[str, Any]) -> HTTPFlow:
    return HTTPFlow(
        request=Request(method=method, path=path, headers={"Content-Type": "application/json"}),
        response=Response(
            status_code=status,
            headers={"Content-Type": "application/json"},
            body=json.dumps(body).encode("utf-8"),
            json=body,
        ),
    )


def run_replay(runtime_program: Path, output_path: Path) -> dict[str, Any]:
    program = yaml.safe_load(runtime_program.read_text(encoding="utf-8"))
    validate_runtime_program(program)
    pipeline = RuntimePipeline(
        program,
        state_store=StateStore(output_path.parent / "_jwt_replay_state"),
        episode_id="ep_aou_jwt_selector_replay",
        baseline="B3",
    )
    probes = [
        {
            "probe_id": "first_successful_login_sanitized",
            "flow": _flow(
                "POST",
                "/rest/user/login",
                200,
                {"authentication": {"token": _token(), "bid": 1, "umail": "admin@juice-sh.op"}},
            ),
            "expect_fire": True,
            "expect_sensitive_removed": True,
        },
        {
            "probe_id": "second_successful_login_preserved_by_max_applications",
            "flow": _flow(
                "POST",
                "/rest/user/login",
                200,
                {"authentication": {"token": _token(email="second@example.com", bid=2), "bid": 2, "umail": "second@example.com"}},
            ),
            "expect_fire": False,
            "expect_sensitive_removed": False,
        },
        {
            "probe_id": "failed_login_no_fire",
            "flow": _flow("POST", "/rest/user/login", 401, {"error": "Invalid email or password."}),
            "expect_fire": False,
            "expect_sensitive_removed": False,
        },
    ]
    rows = []
    errors = []
    for idx, probe in enumerate(probes, 1):
        flow = probe["flow"]
        events = pipeline.execute(flow, turn_idx=idx)
        fired = [event.get("injection_id") for event in events if event.get("status") == "applied"]
        token = ((flow.response.json or {}).get("authentication") or {}).get("token")
        decoded_header = None
        decoded_payload = None
        password_present = None
        secondary_claims_present = None
        signature_present = None
        if isinstance(token, str) and token.count(".") == 2:
            header_segment, payload_segment, signature_segment = token.split(".", 2)
            decoded_header = _decode_segment(header_segment)
            decoded_payload = _decode_segment(payload_segment)
            data = decoded_payload.get("data") or {}
            password_present = "password" in data
            secondary_claims_present = all(name in data for name in ["totpSecret", "deluxeToken", "lastLoginIp"])
            signature_present = bool(signature_segment)
        checks = [
            {"name": "fire_expectation", "ok": bool(fired) == bool(probe["expect_fire"]), "observed": fired},
        ]
        if probe["expect_sensitive_removed"]:
            checks.extend(
                [
                    {"name": "alg_preserved_rs256", "ok": decoded_header and decoded_header.get("alg") == "RS256"},
                    {"name": "signature_present", "ok": signature_present is True},
                    {"name": "password_hash_removed", "ok": password_present is False},
                    {"name": "secondary_claims_preserved", "ok": secondary_claims_present is True},
                    {"name": "identity_preserved", "ok": decoded_payload and decoded_payload.get("bid") == 1 and (decoded_payload.get("data") or {}).get("email") == "admin@juice-sh.op"},
                ]
            )
        elif token:
            checks.append({"name": "password_preserved_when_not_transformed", "ok": password_present is True})
        status = "pass" if all(check["ok"] for check in checks) else "fail"
        if status == "fail":
            errors.append(probe["probe_id"])
        rows.append(
            {
                "probe_id": probe["probe_id"],
                "status": status,
                "fired_case_ids": fired,
                "checks": checks,
            }
        )
    result = {
        "schema_version": "atobench.aou_jwt_selector_replay.v1",
        "runtime_program": str(runtime_program),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass" if not errors else "fail",
        "probes": rows,
        "errors": errors,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(result, sort_keys=False), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime_program", type=Path)
    parser.add_argument("output_path", type=Path)
    args = parser.parse_args()
    result = run_replay(args.runtime_program, args.output_path)
    print(json.dumps({"status": result["status"], "errors": result["errors"]}, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
