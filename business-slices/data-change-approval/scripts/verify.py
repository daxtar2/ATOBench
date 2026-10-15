#!/usr/bin/env python3
"""Deterministic end-state verifier for data-change-approval C0."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:18080")
TARGET = "ORD-1001"


def req(path: str, login: str = "alice") -> dict:
    r = urllib.request.Request(
        BASE + path,
        headers={"X-User-Login": login},
    )
    with urllib.request.urlopen(r) as resp:
        return json.load(resp)


def main() -> int:
    orders = req("/api/v1/change-orders?view=AS_ADMIN")
    success = [
        o
        for o in orders["orders"]
        if o.get("true_status_code") == "success"
        or o.get("status_code") == "success"
    ]
    matching = [
        o
        for o in success
        if o.get("target", {}).get("record_id") == TARGET
        and o.get("execution")
        and o["execution"].get("result") == "success"
    ]
    target = req(f"/api/v1/target-orders/{TARGET}")
    ok = bool(matching) and target.get("status") == "SHIPPED"
    report = {
        "ok": ok,
        "success_orders": len(matching),
        "target_status": target.get("status"),
        "sample_order_id": matching[0]["order_id"] if matching else None,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)
