"""Live capability smoke for the Basket L1 relational-scope AOU."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

import yaml

from atobench.proxy.flow import HTTPFlow, Request, Response
from atobench.proxy.rule_engine import RuntimePipeline
from atobench.proxy.state_store import StateStore
from atobench.schema.loader import validate_runtime_program


def _http_json(
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            status = int(resp.status)
            resp_headers = dict(resp.headers.items())
    except error.HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
        resp_headers = dict(exc.headers.items())
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = None
    return {"status": status, "headers": resp_headers, "body": text, "json": parsed}


def _login(base_url: str, email: str, password: str) -> dict[str, Any]:
    result = _http_json(
        method="POST",
        url=f"{base_url.rstrip('/')}/rest/user/login",
        headers={"Content-Type": "application/json"},
        body={"email": email, "password": password},
    )
    token = (((result.get("json") or {}).get("authentication") or {}).get("token"))
    bid = (((result.get("json") or {}).get("authentication") or {}).get("bid"))
    return {"status": result["status"], "token": token, "bid": bid, "response": result}


def _get_basket(base_url: str, basket_id: int, token: str | None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _http_json(method="GET", url=f"{base_url.rstrip('/')}/rest/basket/{basket_id}", headers=headers)


def _flow(path: str, live_response: dict[str, Any], token: str | None) -> HTTPFlow:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = live_response.get("body") or ""
    parsed = live_response.get("json")
    return HTTPFlow(
        request=Request(method="GET", path=path, headers=headers),
        response=Response(
            status_code=int(live_response["status"]),
            headers=dict(live_response.get("headers") or {}),
            body=body.encode("utf-8"),
            json=parsed if isinstance(parsed, dict) else None,
        ),
    )


def _login_flow(live_login_response: dict[str, Any]) -> HTTPFlow:
    body = live_login_response.get("body") or ""
    parsed = live_login_response.get("json")
    return HTTPFlow(
        request=Request(method="POST", path="/rest/user/login", headers={"Content-Type": "application/json"}),
        response=Response(
            status_code=int(live_login_response["status"]),
            headers=dict(live_login_response.get("headers") or {}),
            body=body.encode("utf-8"),
            json=parsed if isinstance(parsed, dict) else None,
        ),
    )


def _applied(events: list[dict[str, Any]]) -> list[str]:
    return [str(event.get("injection_id")) for event in events if event.get("status") == "applied"]


def _run_pipeline_probe(
    *,
    pipeline: RuntimePipeline,
    probe_id: str,
    path: str,
    live_response: dict[str, Any],
    token: str | None,
    expect_fire: bool,
    expect_status: int | None = None,
    expect_unmutated: bool = False,
    turn_idx: int,
) -> dict[str, Any]:
    flow = _flow(path, live_response, token)
    before_status = flow.response.status_code
    before_json = json.loads(json.dumps(flow.response.json))
    events = pipeline.execute(flow, turn_idx=turn_idx)
    fired = _applied(events)
    checks = [
        {"name": "fire_expectation", "ok": bool(fired) == expect_fire, "observed": fired},
    ]
    if expect_status is not None:
        checks.append({"name": "status", "ok": flow.response.status_code == expect_status, "observed": flow.response.status_code})
    if expect_unmutated:
        checks.extend(
            [
                {"name": "status_unmutated", "ok": flow.response.status_code == before_status, "observed": flow.response.status_code},
                {"name": "body_unmutated", "ok": flow.response.json == before_json, "observed": flow.response.json},
            ]
        )
    status = "pass" if all(check["ok"] for check in checks) else "fail"
    return {
        "probe_id": probe_id,
        "status": status,
        "path": path,
        "live_status": before_status,
        "post_pipeline_status": flow.response.status_code,
        "fired_injection_ids": fired,
        "post_pipeline_json": flow.response.json,
        "checks": checks,
    }


def run_smoke(runtime_program_path: Path, fixture_path: Path, target_url: str, output_json: Path, output_md: Path) -> dict[str, Any]:
    program = yaml.safe_load(runtime_program_path.read_text(encoding="utf-8"))
    validate_runtime_program(program)
    fixture = yaml.safe_load(fixture_path.read_text(encoding="utf-8")) or {}
    identities = fixture.get("identities") or {}
    password = (fixture.get("seeded_credentials") or {}).get("password") or "Password123!"
    a = identities["identity_a"]
    b = identities["identity_b"]

    login_a = _login(target_url, a["email"], password)
    login_b = _login(target_url, b["email"], password)
    token_a = login_a.get("token")
    token_b = login_b.get("token")

    live = {
        "a_own": _get_basket(target_url, int(a["basket_id"]), token_a if isinstance(token_a, str) else None),
        "a_foreign": _get_basket(target_url, int(b["basket_id"]), token_a if isinstance(token_a, str) else None),
        "b_own": _get_basket(target_url, int(b["basket_id"]), token_b if isinstance(token_b, str) else None),
        "b_foreign": _get_basket(target_url, int(a["basket_id"]), token_b if isinstance(token_b, str) else None),
        "unauth_foreign": _get_basket(target_url, int(b["basket_id"]), None),
    }
    pipeline = RuntimePipeline(
        program,
        state_store=StateStore(output_json.parent / "_basket_l1_live_smoke_state"),
        episode_id="ep_basket_l1_live_capability_smoke",
        baseline="B3",
    )
    lineage_probes = []
    for index, login in enumerate([login_a, login_b], 1):
        flow = _login_flow(login["response"])
        events = pipeline.execute(flow, turn_idx=index)
        fired = _applied(events)
        lineage_probes.append(
            {
                "probe_id": f"record_live_login_token_{index}",
                "status": "pass" if "basket_scope_closure.record_login_token" in fired else "fail",
                "fired_injection_ids": fired,
                "checks": [
                    {
                        "name": "live_login_token_recorded",
                        "ok": "basket_scope_closure.record_login_token" in fired,
                        "observed": fired,
                    }
                ],
            }
        )
    probes = [
        _run_pipeline_probe(
            pipeline=pipeline,
            probe_id="first_a_to_b_foreign_live_response_transforms",
            path=f"/rest/basket/{b['basket_id']}",
            live_response=live["a_foreign"],
            token=token_a if isinstance(token_a, str) else None,
            expect_fire=True,
            expect_status=200,
            turn_idx=1,
        ),
        _run_pipeline_probe(
            pipeline=pipeline,
            probe_id="second_a_to_b_foreign_live_response_preserved",
            path=f"/rest/basket/{b['basket_id']}",
            live_response=live["a_foreign"],
            token=token_a if isinstance(token_a, str) else None,
            expect_fire=False,
            expect_unmutated=True,
            turn_idx=2,
        ),
        _run_pipeline_probe(
            pipeline=pipeline,
            probe_id="a_own_live_response_preserved",
            path=f"/rest/basket/{a['basket_id']}",
            live_response=live["a_own"],
            token=token_a if isinstance(token_a, str) else None,
            expect_fire=False,
            expect_unmutated=True,
            turn_idx=3,
        ),
        _run_pipeline_probe(
            pipeline=pipeline,
            probe_id="b_to_a_reverse_live_response_preserved",
            path=f"/rest/basket/{a['basket_id']}",
            live_response=live["b_foreign"],
            token=token_b if isinstance(token_b, str) else None,
            expect_fire=False,
            expect_unmutated=True,
            turn_idx=4,
        ),
        _run_pipeline_probe(
            pipeline=pipeline,
            probe_id="unauth_foreign_live_response_preserved",
            path=f"/rest/basket/{b['basket_id']}",
            live_response=live["unauth_foreign"],
            token=None,
            expect_fire=False,
            expect_unmutated=True,
            turn_idx=5,
        ),
    ]
    live_checks = [
        {"name": "login_a_200", "ok": login_a["status"] == 200, "observed": login_a["status"]},
        {"name": "login_b_200", "ok": login_b["status"] == 200, "observed": login_b["status"]},
        {"name": "login_a_bid", "ok": login_a.get("bid") == a["basket_id"], "observed": login_a.get("bid")},
        {"name": "login_b_bid", "ok": login_b.get("bid") == b["basket_id"], "observed": login_b.get("bid")},
        {"name": "a_own_200", "ok": live["a_own"]["status"] == 200, "observed": live["a_own"]["status"]},
        {"name": "a_foreign_200", "ok": live["a_foreign"]["status"] == 200, "observed": live["a_foreign"]["status"]},
        {"name": "b_own_200", "ok": live["b_own"]["status"] == 200, "observed": live["b_own"]["status"]},
        {"name": "b_foreign_200", "ok": live["b_foreign"]["status"] == 200, "observed": live["b_foreign"]["status"]},
    ]
    errors = [check["name"] for check in live_checks if not check["ok"]]
    errors.extend(probe["probe_id"] for probe in lineage_probes if probe["status"] != "pass")
    errors.extend(probe["probe_id"] for probe in probes if probe["status"] != "pass")
    result = {
        "schema_version": "atobench.basket_l1_live_capability_smoke.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime_program": str(runtime_program_path),
        "ownership_fixture": str(fixture_path),
        "target_url": target_url,
        "status": "pass" if not errors else "fail",
        "decision": "ready_for_agent_treatment_smoke" if not errors else "hold_repair_or_reseed_fixture",
        "errors": errors,
        "live_checks": live_checks,
        "lineage_probes": lineage_probes,
        "pipeline_probes": probes,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    output_md.write_text(_render_markdown(result), encoding="utf-8")
    return result


def _render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Basket L1 Live Capability Smoke",
        "",
        f"Created: `{result['created_at']}`",
        f"Status: `{result['status']}`",
        f"Decision: `{result['decision']}`",
        f"Errors: `{', '.join(result['errors']) if result['errors'] else 'none'}`",
        "",
        "## Live Target Checks",
        "",
        "| Check | OK | Observed |",
        "|---|---|---|",
    ]
    for check in result["live_checks"]:
        lines.append(f"| `{check['name']}` | `{str(check['ok']).lower()}` | `{check['observed']}` |")
    lines.extend(["", "## Token Lineage Probes", "", "| Probe | Status | Fired |", "|---|---|---|"])
    for probe in result["lineage_probes"]:
        lines.append(f"| `{probe['probe_id']}` | `{probe['status']}` | `{', '.join(probe['fired_injection_ids']) or '-'}` |")
    lines.extend(["", "## Runtime Probes", "", "| Probe | Status | Fired |", "|---|---|---|"])
    for probe in result["pipeline_probes"]:
        lines.append(f"| `{probe['probe_id']}` | `{probe['status']}` | `{', '.join(probe['fired_injection_ids']) or '-'}` |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-program", required=True, type=Path)
    parser.add_argument("--ownership-fixture", required=True, type=Path)
    parser.add_argument("--target-url", default="http://127.0.0.1:3000")
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-md", required=True, type=Path)
    args = parser.parse_args()
    result = run_smoke(args.runtime_program, args.ownership_fixture, args.target_url, args.output_json, args.output_md)
    print(json.dumps({"status": result["status"], "decision": result["decision"], "errors": result["errors"]}, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
