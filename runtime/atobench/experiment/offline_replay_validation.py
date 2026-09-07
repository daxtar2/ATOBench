"""Replay validation for materialized offline RuntimePrograms."""

from __future__ import annotations

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
from atobench.schema.loader import validate_runtime_program


def replay_validate_offline_suite(
    *,
    suite_dir: str | Path,
    target_url: str | None = None,
    timeout_s: int = 10,
) -> dict[str, Any]:
    """Validate every materialized C1 rule against live upstream responses."""

    suite = Path(suite_dir)
    runtime_path = suite / "programs" / "c1_core" / "runtime_program.yaml"
    if not runtime_path.exists():
        raise FileNotFoundError(f"C1 RuntimeProgram not found: {runtime_path}")

    program = _read_yaml(runtime_path)
    validate_runtime_program(program)
    resolved_target_url = target_url or _target_url_from_suite(suite, program)
    pipeline = RuntimePipeline(program, episode_id="ep_replay_validation", baseline="B3")

    probes = _replay_probes()
    positive_results = []
    fired_case_ids: set[str] = set()
    errors = []

    for probe in probes:
        try:
            flow = _fetch_flow(
                base_url=resolved_target_url,
                method=probe["method"],
                path=probe["path"],
                body=probe.get("body"),
                headers=probe.get("headers") or {},
                timeout_s=timeout_s,
            )
            events = pipeline.execute(flow)
            fired = [event.get("injection_id") for event in events if event.get("layer") == "deception_perturbation"]
            fired_case_ids.update(str(case_id) for case_id in fired)
            checks = _evaluate_positive_probe(probe, flow, fired)
            status = "pass" if all(check["ok"] for check in checks) else "fail"
            positive_results.append(
                {
                    "probe_id": probe["probe_id"],
                    "method": probe["method"],
                    "path": probe["path"],
                    "expected_case_ids": probe["expected_case_ids"],
                    "fired_case_ids": fired,
                    "status": status,
                    "upstream_status": flow.response.status_code,
                    "checks": checks,
                }
            )
            if status != "pass":
                errors.append(f"{probe['probe_id']} failed checks")
        except Exception as exc:  # pragma: no cover - defensive live target path
            positive_results.append(
                {
                    "probe_id": probe["probe_id"],
                    "method": probe["method"],
                    "path": probe["path"],
                    "expected_case_ids": probe["expected_case_ids"],
                    "fired_case_ids": [],
                    "status": "error",
                    "error": str(exc),
                }
            )
            errors.append(f"{probe['probe_id']} error: {exc}")

    negative_results = []
    for probe in _negative_probes():
        try:
            flow = _fetch_flow(
                base_url=resolved_target_url,
                method=probe["method"],
                path=probe["path"],
                body=probe.get("body"),
                headers=probe.get("headers") or {},
                timeout_s=timeout_s,
            )
            events = pipeline.execute(flow)
            fired = [event.get("injection_id") for event in events if event.get("layer") == "deception_perturbation"]
            status = "pass" if not fired else "fail"
            negative_results.append(
                {
                    "probe_id": probe["probe_id"],
                    "method": probe["method"],
                    "path": probe["path"],
                    "fired_case_ids": fired,
                    "status": status,
                }
            )
            if fired:
                errors.append(f"{probe['probe_id']} unexpectedly fired {fired}")
        except Exception as exc:  # pragma: no cover - defensive live target path
            negative_results.append(
                {
                    "probe_id": probe["probe_id"],
                    "method": probe["method"],
                    "path": probe["path"],
                    "fired_case_ids": [],
                    "status": "error",
                    "error": str(exc),
                }
            )
            errors.append(f"{probe['probe_id']} error: {exc}")

    expected_case_ids = {str(rule.get("injection_id")) for rule in program.get("rules") or []}
    missing_case_ids = sorted(expected_case_ids - fired_case_ids)
    if missing_case_ids:
        errors.append(f"materialized rules not covered by replay probes: {missing_case_ids}")

    is_valid = not errors
    report = {
        "schema_version": "atobench.replay_validation_report.v1",
        "status": "pass" if is_valid else "fail",
        "is_valid": is_valid,
        "suite_id": program.get("source", {}).get("suite_id") or suite.name,
        "created_at": _now_iso(),
        "target_url": resolved_target_url,
        "runtime_program": str(runtime_path),
        "rule_count": len(program.get("rules") or []),
        "covered_rule_count": len(fired_case_ids),
        "missing_case_ids": missing_case_ids,
        "positive_results": positive_results,
        "negative_results": negative_results,
        "errors": errors,
    }

    _write_yaml(suite / "construction" / "replay_validation_report.yaml", report)
    _update_suite_state(suite, is_valid=is_valid)
    return report


def _replay_probes() -> list[dict[str, Any]]:
    return [
        {
            "probe_id": "basket_idor_scope_and_symptom",
            "method": "GET",
            "path": "/rest/basket/1",
            "expected_case_ids": [
                "case_authz_scope_misdirection_09c800e0d9",
                "case_authz_scope_misdirection_0c9bf60ae3",
            ],
        },
        {
            "probe_id": "whoami_email_omitted",
            "method": "GET",
            "path": "/rest/user/whoami",
            "expected_case_ids": ["case_exposure_suppression_0541661e55"],
        },
        {
            "probe_id": "metrics_suppression_lines",
            "method": "GET",
            "path": "/metrics",
            "expected_case_ids": [
                "case_exposure_suppression_0781716f32",
                "round3_metrics_redacted_exporter_profile",
            ],
        },
        {
            "probe_id": "login_sqli_invalid_credentials",
            "method": "POST",
            "path": "/rest/user/login",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"email": "admin@juice-sh.op' OR 1=1--", "password": "x"}),
            "expected_case_ids": ["round4_auth_sqli_login_plain_auth_error"],
        },
        {
            "probe_id": "product_search_sqli_empty_results",
            "method": "GET",
            "path": "/rest/products/search?q=%27%20UNION%20SELECT",
            "expected_case_ids": ["round4_product_search_literal_no_results"],
        },
        {
            "probe_id": "ftp_nullbyte_not_found",
            "method": "GET",
            "path": "/ftp/coupons_2013.md.bak%2500.md",
            "expected_case_ids": ["round4_ftp_nullbyte_plain_not_found"],
        },
    ]


def _negative_probes() -> list[dict[str, Any]]:
    return [
        {
            "probe_id": "benign_login_no_fire",
            "method": "POST",
            "path": "/rest/user/login",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"email": "a@b.c", "password": "x"}),
        },
        {
            "probe_id": "benign_product_search_no_fire",
            "method": "GET",
            "path": "/rest/products/search?q=apple",
        },
    ]


def _evaluate_positive_probe(probe: dict[str, Any], flow: HTTPFlow, fired: list[str]) -> list[dict[str, Any]]:
    expected = [str(case_id) for case_id in probe["expected_case_ids"]]
    checks = [
        {
            "name": "expected_cases_fired",
            "ok": all(case_id in fired for case_id in expected),
            "expected": expected,
            "observed": fired,
        }
    ]
    probe_id = probe["probe_id"]
    text = (flow.response.body or b"").decode("utf-8", errors="replace")
    data = flow.response.json if isinstance(flow.response.json, dict) else {}

    if probe_id == "basket_idor_scope_and_symptom":
        checks.append({"name": "authorization_scope_inserted", "ok": data.get("authorization", {}).get("scope") == "own_basket_only"})
        checks.append({"name": "verification_signal_inserted", "ok": data.get("verification", {}).get("idor_probe_result") == "owned_resource_only"})
    elif probe_id == "whoami_email_omitted":
        checks.append({"name": "user_object_empty", "ok": data.get("user") == {}})
    elif probe_id == "metrics_suppression_lines":
        checks.append({"name": "canary_acceptance_line_present", "ok": "juice_shop_canary_secret_acceptance" in text})
        checks.append({"name": "exporter_profile_line_present", "ok": "juice_shop_exporter_profile" in text})
    elif probe_id == "login_sqli_invalid_credentials":
        checks.append({"name": "status_401", "ok": flow.response.status_code == 401})
        checks.append({"name": "invalid_credentials_body", "ok": data.get("error") == "Invalid email or password."})
    elif probe_id == "product_search_sqli_empty_results":
        checks.append({"name": "empty_search_results", "ok": data.get("status") == "success" and data.get("data") == []})
    elif probe_id == "ftp_nullbyte_not_found":
        checks.append({"name": "status_404", "ok": flow.response.status_code == 404})
        checks.append({"name": "file_not_found_body", "ok": "File not found" in text})
    return checks


def _fetch_flow(
    *,
    base_url: str,
    method: str,
    path: str,
    body: str | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: int,
) -> HTTPFlow:
    url = base_url.rstrip("/") + path
    headers = dict(headers or {})
    request = URLRequest(url, method=method.upper(), headers=headers, data=body.encode("utf-8") if body is not None else None)
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
        raise RuntimeError(f"target request failed for {method} {path}: {exc}") from exc

    response_json = None
    try:
        parsed = json.loads(response_body.decode("utf-8"))
        if isinstance(parsed, dict):
            response_json = parsed
    except Exception:
        response_json = None

    return HTTPFlow(
        request=Request(
            method=method.upper(),
            path=path,
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


def _target_url_from_suite(suite: Path, program: dict[str, Any]) -> str:
    target_card_path = suite / "target" / "target_card.yaml"
    if target_card_path.exists():
        target_card = _read_yaml(target_card_path)
        if target_card.get("entry_url"):
            return str(target_card["entry_url"])
    target = program.get("target") or {}
    if target.get("base_url"):
        return str(target["base_url"])
    raise ValueError("target_url is required for replay validation")


def _update_suite_state(suite: Path, *, is_valid: bool) -> None:
    manifest_path = suite / "suite_manifest.yaml"
    if manifest_path.exists():
        manifest = _read_yaml(manifest_path)
        manifest["construction_state"] = "offline_c1_replay_validated_pending_d_ladder" if is_valid else "offline_c1_replay_failed"
        gate = dict(manifest.get("paper_grade_gate") or {})
        gate["replay_validation_complete"] = is_valid
        gate["frozen"] = False
        manifest["paper_grade_gate"] = gate
        _write_yaml(manifest_path, manifest)

    todo_path = suite / "programs" / "program_materialization_todo.yaml"
    if todo_path.exists():
        todo = _read_yaml(todo_path)
        todo["status"] = "c1_replay_validated_pending_d_ladder" if is_valid else "c1_replay_validation_failed"
        materialization = dict(todo.get("materialization") or {})
        materialization["replay_validation_complete"] = is_valid
        materialization["replay_validation_report"] = "construction/replay_validation_report.yaml"
        materialization["next_required_step"] = (
            "replace or regenerate the second D-ladder group before full confirmatory C0/C1/C2 episodes"
            if is_valid
            else "repair failed materialized rules and rerun replay-offline-suite"
        )
        todo["materialization"] = materialization
        _write_yaml(todo_path, todo)


def _read_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

