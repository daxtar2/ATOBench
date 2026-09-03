"""Selector replay for the Basket L1 persistent K=2 relational-scope AOU."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from atobench.eval.basket_l1_selector_replay import (
    TOKEN_A,
    TOKEN_B,
    _applied,
    _basket_body,
    _flow,
    _login_flow,
    _probe_status,
    _run_probe,
    _token,
)
from atobench.proxy.rule_engine import RuntimePipeline
from atobench.proxy.state_store import StateStore
from atobench.schema.loader import validate_runtime_program


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
        state_store=StateStore(output_path.parent / "_basket_l1_persistent_k2_c0_replay_state"),
        episode_id="ep_basket_l1_persistent_k2_selector_replay_c0",
        baseline="B0",
    )
    c1_pipeline = RuntimePipeline(
        c1_program,
        state_store=StateStore(output_path.parent / "_basket_l1_persistent_k2_c1_replay_state"),
        episode_id="ep_basket_l1_persistent_k2_selector_replay_c1",
        baseline="B3",
    )

    rows: list[dict[str, Any]] = []
    for pipeline, condition in [(c0_pipeline, "C0_shadow"), (c1_pipeline, "C1_persistent_k2")]:
        for index, (token, bid) in enumerate([(TOKEN_A, 14), (TOKEN_B, 15)], 1):
            flow = _login_flow(token, bid=bid)
            events = pipeline.execute(flow, turn_idx=index)
            fired = _applied(events)
            expected = (
                "basket_scope_closure_persistent_k2.record_login_token"
                if condition == "C0_shadow"
                else "basket_scope_closure_persistent_k2.record_login_token"
            )
            rows.append(
                {
                    "condition": condition,
                    "probe_id": f"{condition.lower()}_record_login_token_bid_{bid}",
                    "status": "pass" if expected in fired else "fail",
                    "fired_injection_ids": fired,
                    "checks": [{"name": "login_token_recorded", "ok": expected in fired, "observed": fired}],
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
            "flow": _flow("/rest/basket/15", token=_token(user_id=33, email="forged-a@example.test", bid=14), body=foreign_body),
            "expect_fire": False,
            "expect_unmutated": True,
        },
    ]
    c1_probes = [
        {
            "probe_id": "c1_forged_decodable_token_preserved",
            "flow": _flow("/rest/basket/15", token=_token(user_id=33, email="forged-a@example.test", bid=14), body=foreign_body),
            "expect_fire": False,
            "expect_unmutated": True,
        },
        {
            "probe_id": "c1_first_a_to_b_foreign_get_transforms",
            "flow": _flow("/rest/basket/15", token=TOKEN_A, body=foreign_body),
            "expect_fire": True,
            "expect_status": 200,
            "expect_body": subject_scope_body,
        },
        {
            "probe_id": "c1_second_a_to_b_foreign_get_transforms",
            "flow": _flow("/rest/basket/15", token=TOKEN_A, body=foreign_body),
            "expect_fire": True,
            "expect_status": 200,
            "expect_body": subject_scope_body,
        },
        {
            "probe_id": "c1_third_a_to_b_foreign_get_preserved_as_recovery_path",
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

    for index, probe in enumerate(c0_probes, 1):
        row = _run_probe(c0_pipeline, probe, index)
        row["condition"] = "C0_shadow"
        rows.append(row)
    for index, probe in enumerate(c1_probes, 1):
        row = _run_probe(c1_pipeline, probe, index)
        row["condition"] = "C1_persistent_k2"
        rows.append(row)

    errors = [row["probe_id"] for row in rows if row["status"] != "pass"]
    result = {
        "schema_version": "atobench.basket_l1_persistent_selector_replay.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "c0_runtime_program": str(c0_program_path),
        "c1_runtime_program": str(c1_program_path),
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "probes": rows,
        "dose": {
            "transformed_eligible_contacts": 2,
            "third_eligible_contact_native_recovery_path": _probe_status(
                rows, "c1_third_a_to_b_foreign_get_preserved_as_recovery_path"
            ),
        },
        "capability_preservation": {
            "own_resource_read_preserved": _probe_status(rows, "c1_own_resource_preserved"),
            "unauthenticated_request_preserved": _probe_status(rows, "c1_unauthenticated_foreign_preserved"),
            "reverse_direction_preserved": _probe_status(rows, "c1_reverse_direction_b_to_a_preserved_by_subject_owner_gate"),
            "forged_token_preserved": _probe_status(rows, "c1_forged_decodable_token_preserved"),
            "unknown_basket_preserved": _probe_status(rows, "c1_unknown_basket_preserved"),
        },
        "decision": "selector_replay_passed_ready_for_live_smoke" if not errors else "hold_repair_runtime_program",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(result, sort_keys=False), encoding="utf-8")
    return result


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
