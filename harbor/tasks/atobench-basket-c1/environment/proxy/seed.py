#!/usr/bin/env python3
"""Seed sidecar for the basket AOU (replaces the legacy host-side gate).

Runs once per trial, after juice-shop is healthy and before the proxy starts:

1. wait for semantic HTTP readiness (version / search schema / login API)
2. reseed the A/B ownership fixture (register identities, canary basket item,
   ownership probes) — the exact routine the legacy runner used
3. materialize the condition's RuntimeProgram from its frozen template against
   the live fixture (concrete basket ids, owner map, native responses)
4. write /program/runtime_program.yaml + /program/fixture.json for the proxy

Fails closed: any contract violation exits non-zero, which fails the trial's
environment startup instead of running against an unseeded target.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from atobench.protocol.juice_shop_state import (
    TargetStateError,
    _load_contract,
    materialize_basket_runtime_program,
    reseed_basket_fixture,
    wait_for_readiness,
)

CONTRACT_PATH = Path("/seed/target_state_contract.yaml")
PROGRAM_DIR = Path("/program")


def main() -> int:
    template = os.environ.get("SEED_TEMPLATE")
    episode_id = os.environ.get("SEED_EPISODE_ID", "ep_basket_harbor")
    if not template:
        print("SEED_TEMPLATE is required", file=sys.stderr)
        return 2

    contract = _load_contract(CONTRACT_PATH)
    readiness = wait_for_readiness(contract)
    seeded = reseed_basket_fixture(contract, contract_path=CONTRACT_PATH)
    fixture = seeded["fixture"]

    PROGRAM_DIR.mkdir(parents=True, exist_ok=True)
    fixture_path = PROGRAM_DIR / "fixture.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = materialize_basket_runtime_program(
        template,
        fixture_path=fixture_path,
        episode_id=episode_id,
        output_path=PROGRAM_DIR / "runtime_program.yaml",
    )

    status = {
        "status": "pass",
        "readiness": readiness["checks"],
        "fixture_id": fixture["fixture_id"],
        "basket_ids": {
            name: fixture["identities"][name]["basket_id"] for name in ("identity_a", "identity_b")
        },
        "runtime_program": result,
    }
    (PROGRAM_DIR / "seed_status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TargetStateError as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)
