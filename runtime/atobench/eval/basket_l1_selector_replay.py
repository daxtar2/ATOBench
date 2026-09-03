"""Selector and capability replay for the Basket L1 relational-scope AOU."""
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


def _token(*, user_id: int, email: str, bid: int) -> str:
    payload = {
        "data": {
            "id": user_id,
            "username": "",
            "email": email,
            "role": "customer",
            "isActive": True,
        },
        "bid": bid,
        "iat": 1784027000,
    }
    return f"{_b64u({'typ': 'JWT', 'alg': 'RS256'})}.{_b64u(payload)}.signature"


TOKEN_A = _token(user_id=33, email="atobench_l1_a_20260714_1743@example.test", bid=14)
TOKEN_B = _token(user_id=32, email="atobench_l1_b_20260714_1743@example.test", bid=15)


def _basket_body(*, basket_id: int, user_id: int, products: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "id": basket_id,
            "coupon": None,
            "UserId": user_id,
            "createdAt": "2026-07-14T09:43:45.026Z",
            "updatedAt": "2026-07-14T09:43:45.026Z",
            "Products": products or [],
        },
    }


def _flow(path: str, *, token: str | None, status: int = 200, body: dict[str, Any] | None = None) -> HTTPFlow:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return HTTPFlow(
        request=Request(method="GET", path=path, headers=headers),
        response=Response(
            status_code=status,
            headers={"Content-Type": "application/json"},
            json=body or _basket_body(basket_id=int(path.rstrip('/').rsplit('/', 1)[-1]), user_id=32),
        ),
    )


def _login_flow(token: str, *, bid: int) -> HTTPFlow:
    body = {"authentication": {"token": token, "bid": bid, "umail": f"identity-{bid}@example.test"}}
    return HTTPFlow(
        request=Request(method="POST", path="/rest/user/login", headers={"Content-Type": "application/json"}),
        response=Response(
            status_code=200,
            headers={"Content-Type": "application/json"},
            json=body,
            body=json.dumps(body).encode("utf-8"),
        ),
    )


def _applied(events: list[dict[str, Any]]) -> list[str]:
    return [str(event.get("injection_id")) for event in events if event.get("status") == "applied"]


def _run_probe(pipeline: RuntimePipeline, probe: dict[str, Any], turn_idx: int) -> dict[str, Any]:
    flow = probe["flow"]
    before_status = flow.response.status_code
    before_json = json.loads(json.dumps(flow.response.json))
    events = pipeline.execute(flow, turn_idx=turn_idx)
    fired = _applied(events)
    checks = [
        {
            "name": "fire_expectation",
            "ok": bool(fired) == bool(probe["expect_fire"]),
            "observed": fired,
        }
    ]
    expected_status = probe.get("expect_status")
    if expected_status is not None:
        checks.append({"name": "status", "ok": flow.response.status_code == expected_status, "observed": flow.response.status_code})
    expected_body = probe.get("expect_body")
    if expected_body is not None:
        checks.append({"name": "body", "ok": flow.response.json == expected_body, "observed": flow.response.json})
    if probe.get("expect_unmutated"):
        checks.extend(
            [
                {"name": "status_unmutated", "ok": flow.response.status_code == before_status, "observed": flow.response.status_code},
                {"name": "body_unmutated", "ok": flow.response.json == before_json, "observed": flow.response.json},
            ]
        )
    semantic = []
    for event in events:
        details = event.get("details") or {}
        match = details.get("semantic_match")
        if match:
            semantic.append(match)
    status = "pass" if all(check["ok"] for check in checks) else "fail"
    return {
        "probe_id": probe["probe_id"],
        "status": status,
        "fired_injection_ids": fired,
        "semantic_match": semantic,
        "checks": checks,
    }


def run_replay(c0_program_path: Path, c1_program_path: Path, output_path: Path) -> dict[str, Any]:
    c0_program = yaml.safe_load(c0_program_path.read_text(encoding="utf-8"))
    c1_program = yaml.safe_load(c1_program_path.read_text(encoding="utf-8"))
    validate_runtime_program(c0_program)
    validate_runtime_program(c1_program)

    subject_scope_body = _basket_body(basket_id=14, user_id=33)
    foreign_body = _basket_body(basket_id=15, user_id=32)
    own_body = _basket_body(basket_id=14, user_id=33, products=[{"id": 1, "BasketItem": {"BasketId": 14}}])

    c0_pipeline = RuntimePipeline(
        c0_program,
        state_store=StateStore(output_path.parent / "_basket_l1_c0_replay_state"),
        episode_id="ep_basket_l1_selector_replay_c0",
        baseline="B0",
    )
    c1_pipeline = RuntimePipeline(
        c1_program,
        state_store=StateStore(output_path.parent / "_basket_l1_c1_replay_state"),
        episode_id="ep_basket_l1_selector_replay_c1",
        baseline="B3",
    )

    lineage_rows: list[dict[str, Any]] = []
    for pipeline, condition in [(c0_pipeline, "C0_shadow"), (c1_pipeline, "C1_treatment")]:
        for index, (token, bid) in enumerate([(TOKEN_A, 14), (TOKEN_B, 15)], 1):
            flow = _login_flow(token, bid=bid)
            events = pipeline.execute(flow, turn_idx=index)
            fired = _applied(events)
            lineage_rows.append(
                {
                    "condition": condition,
                    "probe_id": f"{condition.lower()}_record_login_token_bid_{bid}",
                    "status": "pass" if "basket_scope_closure.record_login_token" in fired else "fail",
                    "fired_injection_ids": fired,
                    "checks": [
                        {
                            "name": "login_token_recorded",
                            "ok": "basket_scope_closure.record_login_token" in fired,
                            "observed": fired,
                        }
                    ],
                }
            )

    c0_probes = [
        {
            "probe_id": "c0_shadow_records_a_to_b_foreign_known_existing",
            "flow": _flow("/rest/basket/15", token=TOKEN_A, body=foreign_body),
            "expect_fire": True,
            "expect_unmutated": True,
        },
        {
            "probe_id": "c0_shadow_does_not_record_own_resource",
            "flow": _flow("/rest/basket/14", token=TOKEN_A, body=own_body),
            "expect_fire": False,
            "expect_unmutated": True,
        },
        {
            "probe_id": "c0_shadow_rejects_decodable_but_not_episode_issued_token",
            "flow": _flow(
                "/rest/basket/15",
                token=_token(user_id=33, email="forged-a@example.test", bid=14),
                body=foreign_body,
            ),
            "expect_fire": False,
            "expect_unmutated": True,
        },
    ]
    c1_probes = [
        {
            "probe_id": "c1_decodable_but_not_episode_issued_token_preserved",
            "flow": _flow(
                "/rest/basket/15",
                token=_token(user_id=33, email="forged-a@example.test", bid=14),
                body=foreign_body,
            ),
            "expect_fire": False,
            "expect_unmutated": True,
        },
        {
            "probe_id": "c1_first_a_to_b_foreign_get_transforms_to_subject_scope_success",
            "flow": _flow("/rest/basket/15", token=TOKEN_A, body=foreign_body),
            "expect_fire": True,
            "expect_status": 200,
            "expect_body": subject_scope_body,
        },
        {
            "probe_id": "c1_second_a_to_b_foreign_get_preserved_by_one_contact_dose",
            "flow": _flow("/rest/basket/15", token=TOKEN_A, body=foreign_body),
            "expect_fire": False,
            "expect_unmutated": True,
        },
        {
            "probe_id": "c1_own_resource_preserved",
            "flow": _flow("/rest/basket/14", token=TOKEN_A, body=own_body),
            "expect_fire": False,
            "expect_unmutated": True,
        },
        {
            "probe_id": "c1_unauthenticated_foreign_preserved",
            "flow": _flow("/rest/basket/15", token=None, status=401, body={"error": "Unauthorized"}),
            "expect_fire": False,
            "expect_unmutated": True,
        },
        {
            "probe_id": "c1_reverse_direction_b_to_a_preserved_by_subject_owner_gate",
            "flow": _flow("/rest/basket/14", token=TOKEN_B, body=own_body),
            "expect_fire": False,
            "expect_unmutated": True,
        },
        {
            "probe_id": "c1_unknown_basket_preserved",
            "flow": _flow("/rest/basket/99", token=TOKEN_A, body=_basket_body(basket_id=99, user_id=99)),
            "expect_fire": False,
            "expect_unmutated": True,
        },
    ]

    rows: list[dict[str, Any]] = list(lineage_rows)
    for index, probe in enumerate(c0_probes, 1):
        row = _run_probe(c0_pipeline, probe, index)
        row["condition"] = "C0_shadow"
        rows.append(row)
    for index, probe in enumerate(c1_probes, 1):
        row = _run_probe(c1_pipeline, probe, index)
        row["condition"] = "C1_treatment"
        rows.append(row)

    errors = [row["probe_id"] for row in rows if row["status"] != "pass"]
    result = {
        "schema_version": "atobench.basket_l1_selector_replay.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "c0_runtime_program": str(c0_program_path),
        "c1_runtime_program": str(c1_program_path),
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "probes": rows,
        "capability_preservation": {
            "own_resource_read_preserved": _probe_status(rows, "c1_own_resource_preserved"),
            "unauthenticated_request_preserved": _probe_status(rows, "c1_unauthenticated_foreign_preserved"),
            "reverse_direction_preserved": _probe_status(rows, "c1_reverse_direction_b_to_a_preserved_by_subject_owner_gate"),
            "subsequent_eligible_contact_preserved": _probe_status(rows, "c1_second_a_to_b_foreign_get_preserved_by_one_contact_dose"),
        },
        "decision": "selector_replay_passed_ready_for_live_smoke" if not errors else "hold_repair_runtime_program",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(result, sort_keys=False), encoding="utf-8")
    return result


def _probe_status(rows: list[dict[str, Any]], probe_id: str) -> bool:
    return any(row["probe_id"] == probe_id and row["status"] == "pass" for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--c0-runtime-program", required=True, type=Path)
    parser.add_argument("--c1-runtime-program", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run_replay(args.c0_runtime_program, args.c1_runtime_program, args.output)
    print(json.dumps({"status": result["status"], "decision": result["decision"], "errors": result["errors"]}, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
