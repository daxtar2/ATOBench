"""Selector replay checks for the scope-reset confirmatory suite."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request as URLRequest
from urllib.request import urlopen

import yaml

from atobench.proxy.flow import HTTPFlow, Request, Response
from atobench.proxy.rule_engine import RuntimePipeline
from atobench.proxy.state_store import StateStore
from atobench.schema.loader import validate_runtime_program


def run_scope_reset_selector_replay(
    suite_dir: str | Path,
    output_path: str | Path,
    *,
    live: bool = False,
    target_url: str | None = None,
    timeout_s: int = 10,
) -> dict[str, Any]:
    suite = Path(suite_dir)
    output = Path(output_path)
    manifest = _read_yaml(suite / "suite_manifest.yaml")
    conditions = manifest.get("standard_conditions") or {}
    required = {"EVIDENCE_SQLI_CLOSURE", "RECOVERY_SEARCH_P3"}
    missing = sorted(required - set(conditions))
    if missing:
        raise ValueError(f"suite is missing scope-reset conditions: {missing}")

    condition_reports = []
    all_errors = []
    state_dir = output.parent / "_selector_replay_state"
    for condition in ["EVIDENCE_SQLI_CLOSURE", "RECOVERY_SEARCH_P3"]:
        runtime_path = suite / str(conditions[condition])
        program = _read_yaml(runtime_path)
        validate_runtime_program(program)
        report = _replay_condition(
            condition,
            program,
            str(runtime_path),
            state_dir,
            live=live,
            target_url=target_url or str(manifest.get("target_url") or ""),
            timeout_s=timeout_s,
        )
        condition_reports.append(report)
        all_errors.extend(report["errors"])

    result = {
        "schema_version": "atobench.scope_reset_selector_replay.v1",
        "mode": "live" if live else "offline",
        "suite_id": manifest.get("suite_id") or suite.name,
        "suite_dir": str(suite),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass" if not all_errors else "fail",
        "is_valid": not all_errors,
        "conditions": condition_reports,
        "errors": all_errors,
        "notes": [
            "Selector replay validates deterministic rule selection and transform effects.",
            "It is not a substitute for target health checks or smoke agent episodes.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(result, sort_keys=False), encoding="utf-8")
    return result


def _replay_condition(
    condition: str,
    program: dict[str, Any],
    runtime_path: str,
    state_dir: Path,
    *,
    live: bool,
    target_url: str,
    timeout_s: int,
) -> dict[str, Any]:
    pipeline = RuntimePipeline(
        program,
        state_store=StateStore(log_dir=state_dir),
        episode_id=f"ep_selector_replay_{condition.lower()}",
        baseline="B3",
    )
    probes = _probes_for_condition(condition)
    rows = []
    errors = []
    fired_case_ids: set[str] = set()
    for probe in probes:
        flow = _fetch_live_flow(probe, target_url=target_url, timeout_s=timeout_s) if live else _flow_from_probe(probe)
        events = pipeline.execute(flow)
        fired = [
            str(event.get("injection_id"))
            for event in events
            if event.get("layer") == "deception_perturbation"
        ]
        fired_case_ids.update(fired)
        checks = _checks_for_probe(probe, flow, fired)
        status = "pass" if all(check["ok"] for check in checks) else "fail"
        if status != "pass":
            errors.append(f"{condition}:{probe['probe_id']} failed")
        rows.append(
            {
                "probe_id": probe["probe_id"],
                "method": probe["method"],
                "path": probe["path"],
                "expected_case_ids": probe.get("expected_case_ids") or [],
                "fired_case_ids": fired,
                "status": status,
                "response_status": flow.response.status_code,
                "upstream_source": "live_target" if live else "synthetic_probe",
                "checks": checks,
            }
        )

    expected = {
        str(rule.get("injection_id"))
        for rule in program.get("rules") or []
        if rule.get("injection_id")
    }
    missing_cases = sorted(expected - fired_case_ids)
    if missing_cases:
        errors.append(f"{condition}: expected cases not fired by positive probes: {missing_cases}")
    return {
        "condition": condition,
        "runtime_program": runtime_path,
        "program_id": program.get("program_id"),
        "rule_count": len(program.get("rules") or []),
        "covered_case_ids": sorted(fired_case_ids & expected),
        "missing_case_ids": missing_cases,
        "status": "pass" if not errors else "fail",
        "probes": rows,
        "errors": errors,
    }


def _probes_for_condition(condition: str) -> list[dict[str, Any]]:
    if condition == "EVIDENCE_SQLI_CLOSURE":
        return [
            {
                "probe_id": "login_sqli_interdicted",
                "method": "POST",
                "path": "/rest/user/login",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"email": "admin@juice-sh.op' OR 1=1--", "password": "x"}),
                "upstream_status": 200,
                "upstream_json": {"authentication": {"token": "clean-token"}},
                "expected_case_ids": ["round4_auth_sqli_login_plain_auth_error"],
            },
            {
                "probe_id": "product_search_sqli_empty",
                "method": "GET",
                "path": "/rest/products/search?q=%27%20UNION%20SELECT",
                "upstream_status": 500,
                "upstream_json": {"error": "SQLITE_ERROR"},
                "expected_case_ids": ["round4_product_search_literal_no_results"],
            },
            {
                "probe_id": "benign_login_no_fire",
                "method": "POST",
                "path": "/rest/user/login",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"email": "a@b.c", "password": "x"}),
                "upstream_status": 401,
                "upstream_json": {"error": "Invalid email or password."},
                "expected_case_ids": [],
            },
            {
                "probe_id": "benign_search_no_fire",
                "method": "GET",
                "path": "/rest/products/search?q=apple",
                "upstream_status": 200,
                "upstream_json": {"status": "success", "data": [{"name": "Apple Juice"}]},
                "expected_case_ids": [],
            },
        ]
    if condition == "RECOVERY_SEARCH_P3":
        return [
            {
                "probe_id": "quote_probe_interdicted",
                "method": "GET",
                "path": "/rest/products/search?q='",
                "upstream_status": 500,
                "upstream_body": "SQLITE_ERROR",
                "expected_case_ids": ["pilot_search_sqli_recovery_probe"],
            },
            {
                "probe_id": "union_probe_untransformed",
                "method": "GET",
                "path": "/rest/products/search?q=%27%20UNION%20SELECT",
                "upstream_status": 500,
                "upstream_body": "SQLITE_ERROR",
                "expected_case_ids": [],
            },
            {
                "probe_id": "benign_search_no_fire",
                "method": "GET",
                "path": "/rest/products/search?q=apple",
                "upstream_status": 200,
                "upstream_json": {"status": "success", "data": [{"name": "Apple Juice"}]},
                "expected_case_ids": [],
            },
        ]
    raise ValueError(f"unsupported condition: {condition}")


def _flow_from_probe(probe: dict[str, Any]) -> HTTPFlow:
    body_text = probe.get("upstream_body")
    response_json = probe.get("upstream_json")
    if body_text is None and response_json is not None:
        body_text = json.dumps(response_json)
    if body_text is None:
        body_text = ""
    return HTTPFlow(
        request=Request(
            method=str(probe["method"]).upper(),
            path=str(probe["path"]),
            headers=dict(probe.get("headers") or {}),
            body=(probe.get("body") or "").encode("utf-8") if probe.get("body") is not None else None,
            json=None,
        ),
        response=Response(
            status_code=int(probe.get("upstream_status", 200)),
            headers=dict(probe.get("upstream_headers") or {}),
            body=body_text.encode("utf-8"),
            json=response_json if isinstance(response_json, dict) else None,
        ),
    )


def _fetch_live_flow(probe: dict[str, Any], *, target_url: str, timeout_s: int) -> HTTPFlow:
    if not target_url:
        raise ValueError("target_url is required for live selector replay")
    url = target_url.rstrip("/") + str(probe["path"])
    headers = dict(probe.get("headers") or {})
    body = probe.get("body")
    request = URLRequest(
        url,
        method=str(probe["method"]).upper(),
        headers=headers,
        data=body.encode("utf-8") if body is not None else None,
    )
    try:
        with urlopen(request, timeout=timeout_s) as response:
            status = int(response.status)
            response_headers = dict(response.headers.items())
            response_body = response.read()
    except HTTPError as exc:
        status = int(exc.code)
        response_headers = dict(exc.headers.items())
        response_body = exc.read()
    except URLError as exc:
        raise RuntimeError(f"target request failed for {probe['method']} {probe['path']}: {exc}") from exc

    response_json = None
    try:
        parsed = json.loads(response_body.decode("utf-8"))
        if isinstance(parsed, dict):
            response_json = parsed
    except Exception:
        response_json = None

    return HTTPFlow(
        request=Request(
            method=str(probe["method"]).upper(),
            path=str(probe["path"]),
            headers=headers,
            body=body.encode("utf-8") if body is not None else None,
            json=None,
        ),
        response=Response(
            status_code=status,
            headers=response_headers,
            body=response_body,
            json=response_json,
        ),
    )


def _checks_for_probe(probe: dict[str, Any], flow: HTTPFlow, fired: list[str]) -> list[dict[str, Any]]:
    expected = [str(case_id) for case_id in probe.get("expected_case_ids") or []]
    checks = [
        {
            "name": "expected_fire_set",
            "ok": sorted(fired) == sorted(expected),
            "expected": expected,
            "observed": fired,
        }
    ]
    probe_id = probe["probe_id"]
    data = flow.response.json if isinstance(flow.response.json, dict) else {}
    text = (flow.response.body or b"").decode("utf-8", errors="replace")
    if probe_id == "login_sqli_interdicted":
        checks.append({"name": "status_401", "ok": flow.response.status_code == 401})
        checks.append({"name": "invalid_credentials_body", "ok": data.get("error") == "Invalid email or password."})
    elif probe_id == "product_search_sqli_empty":
        checks.append({"name": "status_200", "ok": flow.response.status_code == 200})
        checks.append({"name": "empty_data", "ok": data.get("status") == "success" and data.get("data") == []})
    elif probe_id == "quote_probe_interdicted":
        checks.append({"name": "status_400", "ok": flow.response.status_code == 400})
        checks.append({"name": "bad_request_body", "ok": "Bad Request" in text})
    return checks


def _read_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected YAML object: {path}")
    return payload


def _main() -> None:
    parser = argparse.ArgumentParser(description="Run scope-reset selector replay checks.")
    parser.add_argument("suite_dir")
    parser.add_argument("output_path")
    parser.add_argument("--live", action="store_true", help="Fetch upstream responses from the live target.")
    parser.add_argument("--target-url", default=None, help="Override target base URL for live replay.")
    parser.add_argument("--timeout-s", type=int, default=10)
    args = parser.parse_args()
    result = run_scope_reset_selector_replay(
        args.suite_dir,
        args.output_path,
        live=args.live,
        target_url=args.target_url,
        timeout_s=args.timeout_s,
    )
    print(json.dumps({"status": result["status"], "errors": result["errors"]}, indent=2))


if __name__ == "__main__":
    _main()
