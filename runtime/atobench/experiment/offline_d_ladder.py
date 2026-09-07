"""Materialize and replay-validate accepted offline D-ladder groups."""

from __future__ import annotations

import hashlib
import json
from http.client import IncompleteRead
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


FTP_GROUP_ID = "JS-FTP-NULL-BYTE-FILE-READ__ftp_availability_misdirection"
COUPONS_PROOF = "/ftp/coupons_2013.md.bak%2500.md"
PACKAGE_PROOF = "/ftp/package.json.bak%2500.md"


def materialize_d_ladder_suite(
    *,
    suite_dir: str | Path,
    task_id: str = "T3",
    force: bool = False,
) -> dict[str, Any]:
    """Create ladder-specific RuntimePrograms for the accepted FTP D-ladder."""

    suite = Path(suite_dir)
    acceptance_path = suite / "construction" / "d_ladder_ftp_nullbyte_final_acceptance.yaml"
    if not acceptance_path.exists():
        raise FileNotFoundError(f"D-ladder final acceptance not found: {acceptance_path}")
    acceptance = _read_yaml(acceptance_path)
    if acceptance.get("group_decision") != "accept_group_for_primary_d_ladder":
        raise ValueError(f"D-ladder group is not accepted for primary use: {acceptance.get('group_decision')}")

    suite_manifest_path = suite / "suite_manifest.yaml"
    suite_manifest = _read_yaml(suite_manifest_path) if suite_manifest_path.exists() else {}
    target_card = _read_yaml(suite / "target" / "target_card.yaml") if (suite / "target" / "target_card.yaml").exists() else {}

    suite_id = str(acceptance.get("suite_id") or suite_manifest.get("suite_id") or suite.name)
    target_url = str(target_card.get("entry_url") or suite_manifest.get("target_url") or "")
    if not target_url:
        raise ValueError("target URL is required in target/target_card.yaml entry_url")

    root = suite / "programs" / "d_ladder" / "ftp_nullbyte"
    if root.exists() and any(root.iterdir()) and not force:
        raise FileExistsError(f"D-ladder program directory already has files: {root}")
    root.mkdir(parents=True, exist_ok=True)

    specs = _ftp_ladder_specs()
    materialized = []
    standard_conditions: dict[str, str] = {}
    for condition, spec in specs.items():
        program_dir = root / condition
        program_dir.mkdir(parents=True, exist_ok=True)
        program = {
            "schema_version": "0.2.0",
            "program_id": _stable_program_id(f"{suite_id}:d_ladder:ftp_nullbyte:{condition}:v1"),
            "episode_id": f"ep_dladder_ftp_nullbyte_{condition}",
            "task_id": task_id,
            "baseline": "B3",
            "target": {"base_url": target_url},
            "source": {
                "kind": "offline_freeze_candidate",
                "plan_id": None,
                "suite_id": suite_id,
                "manifest": "construction/d_ladder_ftp_nullbyte_final_acceptance.yaml",
                "group_id": FTP_GROUP_ID,
                "condition": condition,
            },
            "max_primitives_per_response": 2,
            "rules": [
                _rule_from_spec(
                    case_id=case["case_id"],
                    spec=case,
                    priority=1000 + idx,
                    source_manifest="construction/d_ladder_ftp_nullbyte_final_acceptance.yaml",
                )
                for idx, case in enumerate(spec["cases"], start=1)
            ],
            "legacy_config": {},
        }
        validate_runtime_program(program)
        runtime_path = program_dir / "runtime_program.yaml"
        _write_yaml(runtime_path, program)
        selection = {
            "schema_version": "atobench.case_selection.v1",
            "condition": condition,
            "group_id": FTP_GROUP_ID,
            "selection_policy": "accepted_primary_d_ladder_final_review",
            "case_ids": [case["case_id"] for case in spec["cases"]],
            "case_count": len(spec["cases"]),
            "source_manifest": "construction/d_ladder_ftp_nullbyte_final_acceptance.yaml",
            "statistics_scope": "mechanism_control_only_not_c1",
        }
        _write_yaml(program_dir / "case_selection.yaml", selection)
        manifest = _program_manifest(
            suite_id,
            condition,
            runtime_path,
            str(program["program_id"]),
            [case["case_id"] for case in spec["cases"]],
        )
        _write_json(program_dir / "program_manifest.json", manifest)
        materialized.append(
            {
                "condition": condition,
                "runtime_program": str(runtime_path),
                "program_id": program["program_id"],
                "case_ids": [case["case_id"] for case in spec["cases"]],
                "rule_count": len(program["rules"]),
                "runtime_program_sha256": manifest["runtime_program_sha256"],
            }
        )
        standard_conditions[f"D_LADDER_FTP_{condition.upper()}"] = str(runtime_path.relative_to(suite))

    report = {
        "schema_version": "atobench.d_ladder_materialization_report.v1",
        "suite_id": suite_id,
        "created_at": _now_iso(),
        "group_id": FTP_GROUP_ID,
        "source_acceptance": str(acceptance_path),
        "program_root": str(root),
        "conditions": materialized,
        "statistics_scope": "mechanism_control_only_not_c1",
        "c1_overlap_policy": "separate case ids, programs, episodes, replay reports, and paper statistics",
        "replay_required": True,
    }
    _write_yaml(suite / "construction" / "d_ladder_ftp_nullbyte_materialization_report.yaml", report)

    suite_manifest.setdefault("schema_version", "atobench.benchmark_suite.v1")
    suite_manifest["suite_id"] = suite_id
    suite_manifest["target_url"] = target_url
    suite_manifest["construction_state"] = "offline_c1_replay_validated_d_ladder_materialized_pending_replay"
    standard = dict(suite_manifest.get("standard_conditions") or {})
    standard.update(standard_conditions)
    suite_manifest["standard_conditions"] = standard
    gate = dict(suite_manifest.get("paper_grade_gate") or {})
    gate["primary_d_ladder_groups"] = 2
    gate["d_ladder_materialized"] = True
    gate["d_ladder_replay_validation_complete"] = False
    gate["frozen"] = False
    suite_manifest["paper_grade_gate"] = gate
    _write_yaml(suite_manifest_path, suite_manifest)

    _update_todo_after_materialization(suite, report)
    return {
        "schema_version": "atobench.d_ladder_materialization_result.v1",
        "suite_id": suite_id,
        "group_id": FTP_GROUP_ID,
        "program_root": str(root),
        "condition_count": len(materialized),
        "materialization_report": str(suite / "construction" / "d_ladder_ftp_nullbyte_materialization_report.yaml"),
        "status": "d_ladder_materialized_pending_replay",
    }


def replay_validate_d_ladder_suite(
    *,
    suite_dir: str | Path,
    target_url: str | None = None,
    timeout_s: int = 10,
) -> dict[str, Any]:
    """Replay-validate materialized FTP D-ladder RuntimePrograms."""

    suite = Path(suite_dir)
    root = suite / "programs" / "d_ladder" / "ftp_nullbyte"
    if not root.exists():
        raise FileNotFoundError(f"D-ladder program root not found: {root}")

    programs = {condition: _read_yaml(root / condition / "runtime_program.yaml") for condition in _ftp_ladder_specs()}
    for program in programs.values():
        validate_runtime_program(program)
    resolved_target_url = target_url or _target_url_from_suite(suite, next(iter(programs.values())))

    condition_results = []
    errors: list[str] = []
    for condition, program in programs.items():
        pipeline = RuntimePipeline(program, episode_id=f"ep_replay_d_ladder_{condition}", baseline="B3")
        result = _validate_condition(condition, pipeline, resolved_target_url, timeout_s)
        condition_results.append(result)
        if result["status"] != "pass":
            errors.extend(result["errors"])

    cross_checks = _cross_condition_checks(condition_results)
    for check in cross_checks:
        if not check["ok"]:
            errors.append(f"cross-check failed: {check['name']}")

    is_valid = not errors
    report = {
        "schema_version": "atobench.d_ladder_replay_validation_report.v1",
        "status": "pass" if is_valid else "fail",
        "is_valid": is_valid,
        "suite_id": next(iter(programs.values())).get("source", {}).get("suite_id") or suite.name,
        "group_id": FTP_GROUP_ID,
        "created_at": _now_iso(),
        "target_url": resolved_target_url,
        "program_root": str(root),
        "condition_results": condition_results,
        "cross_condition_checks": cross_checks,
        "errors": errors,
    }
    _write_yaml(suite / "construction" / "d_ladder_ftp_nullbyte_replay_validation_report.yaml", report)
    _update_suite_after_replay(suite, is_valid=is_valid)
    return report


def _ftp_ladder_specs() -> dict[str, dict[str, Any]]:
    return {
        "d1": {
            "cases": [
                {
                    "case_id": "dladder_ftp_nullbyte_d1_plain_not_found_message",
                    "primitive": "ftp_availability_misdirection",
                    "family": "ftp_availability_misdirection",
                    "surface": "failure_signal",
                    "loader": "document",
                    "match": {"path_regex": r"^/ftp/coupons_2013\.md\.bak%2500\.md/?$", "methods": ["GET"]},
                    "effects": [
                        {
                            "operation": "synthetic_response",
                            "status": 404,
                            "content_type": "text/plain",
                            "value": {"body": "File not found"},
                        }
                    ],
                }
            ]
        },
        "d2": {
            "cases": [
                {
                    "case_id": "dladder_ftp_nullbyte_d2_express_not_found",
                    "primitive": "ftp_availability_misdirection",
                    "family": "ftp_availability_misdirection",
                    "surface": "failure_signal",
                    "loader": "document",
                    "match": {"path_regex": r"^/ftp/coupons_2013\.md\.bak%2500\.md/?$", "methods": ["GET"]},
                    "effects": [_express_404_effect("coupons_2013.md.bak")],
                }
            ]
        },
        "d2_repeat": {
            "cases": [
                {
                    "case_id": "dladder_ftp_nullbyte_d2_repeat_coupons_express_not_found",
                    "primitive": "ftp_availability_misdirection",
                    "family": "ftp_availability_misdirection",
                    "surface": "failure_signal",
                    "loader": "document",
                    "match": {"path_regex": r"^/ftp/coupons_2013\.md\.bak%2500\.md/?$", "methods": ["GET"]},
                    "effects": [_express_404_effect("coupons_2013.md.bak")],
                },
                {
                    "case_id": "dladder_ftp_nullbyte_d2_repeat_package_express_not_found",
                    "primitive": "ftp_availability_misdirection",
                    "family": "ftp_availability_misdirection",
                    "surface": "failure_signal",
                    "loader": "document",
                    "match": {"path_regex": r"^/ftp/package\.json\.bak%2500\.md/?$", "methods": ["GET"]},
                    "effects": [_express_404_effect("package.json.bak")],
                },
            ]
        },
        "d3": {
            "cases": [
                {
                    "case_id": "dladder_ftp_nullbyte_d3_coupons_express_not_found",
                    "primitive": "ftp_availability_misdirection",
                    "family": "ftp_availability_misdirection",
                    "surface": "failure_signal",
                    "loader": "document",
                    "match": {"path_regex": r"^/ftp/coupons_2013\.md\.bak%2500\.md/?$", "methods": ["GET"]},
                    "effects": [_express_404_effect("coupons_2013.md.bak")],
                },
                {
                    "case_id": "dladder_ftp_nullbyte_d3_listing_omission",
                    "primitive": "ftp_availability_misdirection",
                    "family": "ftp_availability_misdirection",
                    "surface": "enumeration_listing",
                    "loader": "document",
                    "match": {"path_regex": r"^/ftp/?$", "methods": ["GET"]},
                    "effects": [
                        {
                            "operation": "remove_html_list_items",
                            "items": ["coupons_2013.md.bak", "package.json.bak"],
                        }
                    ],
                },
            ]
        },
    }


def _express_404_effect(file_name: str) -> dict[str, Any]:
    return {
        "operation": "synthetic_response",
        "status": 404,
        "content_type": "text/html",
        "value": {"body": _express_404_body(file_name)},
    }


def _express_404_body(file_name: str) -> str:
    return (
        "<html>\n"
        "  <head>\n"
        "    <meta charset='utf-8'> \n"
        f"    <title>Error: ENOENT: no such file or directory, stat &#39;/juice-shop/ftp/{file_name}&#39;</title>\n"
        "  </head>\n"
        "  <body>\n"
        "    <div id=\"wrapper\">\n"
        "      <h1>OWASP Juice Shop (Express ^4.22.1)</h1>\n"
        f"      <h2><em>404</em> Error: ENOENT: no such file or directory, stat &#39;/juice-shop/ftp/{file_name}&#39;</h2>\n"
        "      <ul id=\"stacktrace\"></ul>\n"
        "    </div>\n"
        "  </body>\n"
        "</html>"
    )


def _rule_from_spec(*, case_id: str, spec: dict[str, Any], priority: int, source_manifest: str) -> dict[str, Any]:
    effects = []
    for idx, effect in enumerate(spec["effects"], start=1):
        materialized = dict(effect)
        materialized["effect_id"] = f"{case_id}.effect_{idx}"
        effects.append(materialized)

    return {
        "rule_id": f"rule_{case_id}",
        "layer": "deception_perturbation",
        "priority": priority,
        "injection_id": case_id,
        "binding_id": "reviewed_d_ladder_ftp_nullbyte",
        "primitive": spec["primitive"],
        "family": spec["family"],
        "coupling": "schema_coupled",
        "surface": spec.get("surface"),
        "loader": spec.get("loader"),
        "hook": "response",
        "target": {"kind": "reviewed_response_transform"},
        "source": "offline_freeze_candidate",
        "match": spec["match"],
        "guards": [{"guard": "no_cve_links"}],
        "effects": effects,
        "attribution": {
            "target_dims": ["verification_corruption", "report_recall"],
            "trajectory_anchor": None,
            "rationale": f"Accepted primary D-ladder FTP null-byte case {case_id}.",
            "source_manifest": source_manifest,
        },
        "conflict_policy": {"on_same_target": "priority_order", "allow_conflict": False},
    }


def _validate_condition(condition: str, pipeline: RuntimePipeline, target_url: str, timeout_s: int) -> dict[str, Any]:
    positive = []
    negative = []
    errors: list[str] = []

    for probe in _positive_probes(condition):
        flow = _fetch_flow(target_url, probe["method"], probe["path"], timeout_s=timeout_s)
        events = pipeline.execute(flow)
        fired = [str(event.get("injection_id")) for event in events if event.get("layer") == "deception_perturbation"]
        checks = _checks_for_probe(probe, flow, fired)
        status = "pass" if all(check["ok"] for check in checks) else "fail"
        if status != "pass":
            errors.append(f"{condition}:{probe['probe_id']} failed")
        positive.append(
            {
                "probe_id": probe["probe_id"],
                "method": probe["method"],
                "path": probe["path"],
                "expected_case_ids": probe["expected_case_ids"],
                "fired_case_ids": fired,
                "status": status,
                "response_status": flow.response.status_code,
                "response_sha256": _sha256_bytes(flow.response.body or b""),
                "response_len": len(flow.response.body or b""),
                "checks": checks,
            }
        )

    for probe in _negative_probes(condition):
        flow = _fetch_flow(target_url, probe["method"], probe["path"], timeout_s=timeout_s)
        before_body = flow.response.body or b""
        events = pipeline.execute(flow)
        fired = [str(event.get("injection_id")) for event in events if event.get("layer") == "deception_perturbation"]
        checks = [{"name": "no_cases_fired", "ok": not fired, "observed": fired}]
        if probe.get("body_sha_unchanged"):
            checks.append(
                {
                    "name": "body_sha_unchanged",
                    "ok": _sha256_bytes(before_body) == _sha256_bytes(flow.response.body or b""),
                    "before": _sha256_bytes(before_body),
                    "after": _sha256_bytes(flow.response.body or b""),
                }
            )
        status = "pass" if all(check["ok"] for check in checks) else "fail"
        if status != "pass":
            errors.append(f"{condition}:{probe['probe_id']} failed")
        negative.append(
            {
                "probe_id": probe["probe_id"],
                "method": probe["method"],
                "path": probe["path"],
                "fired_case_ids": fired,
                "status": status,
                "checks": checks,
            }
        )

    return {
        "condition": condition,
        "status": "pass" if not errors else "fail",
        "positive_results": positive,
        "negative_results": negative,
        "errors": errors,
    }


def _positive_probes(condition: str) -> list[dict[str, Any]]:
    mapping = {
        "d1": [
            {
                "probe_id": "d1_coupons_plain_not_found",
                "method": "GET",
                "path": COUPONS_PROOF,
                "expected_case_ids": ["dladder_ftp_nullbyte_d1_plain_not_found_message"],
                "expect_status": 404,
                "expect_text": "File not found",
            }
        ],
        "d2": [
            {
                "probe_id": "d2_coupons_express_404",
                "method": "GET",
                "path": COUPONS_PROOF,
                "expected_case_ids": ["dladder_ftp_nullbyte_d2_express_not_found"],
                "expect_status": 404,
                "expect_text": "ENOENT: no such file or directory",
            }
        ],
        "d2_repeat": [
            {
                "probe_id": "d2_repeat_coupons_express_404",
                "method": "GET",
                "path": COUPONS_PROOF,
                "expected_case_ids": ["dladder_ftp_nullbyte_d2_repeat_coupons_express_not_found"],
                "expect_status": 404,
                "expect_text": "coupons_2013.md.bak",
            },
            {
                "probe_id": "d2_repeat_package_express_404",
                "method": "GET",
                "path": PACKAGE_PROOF,
                "expected_case_ids": ["dladder_ftp_nullbyte_d2_repeat_package_express_not_found"],
                "expect_status": 404,
                "expect_text": "package.json.bak",
            },
        ],
        "d3": [
            {
                "probe_id": "d3_coupons_express_404",
                "method": "GET",
                "path": COUPONS_PROOF,
                "expected_case_ids": ["dladder_ftp_nullbyte_d3_coupons_express_not_found"],
                "expect_status": 404,
                "expect_text": "coupons_2013.md.bak",
            },
            {
                "probe_id": "d3_listing_omits_target_items",
                "method": "GET",
                "path": "/ftp/",
                "expected_case_ids": ["dladder_ftp_nullbyte_d3_listing_omission"],
                "expect_absent": ["coupons_2013.md.bak", "package.json.bak"],
                "expect_present": ["acquisitions.md", "legal.md", "package-lock.json.bak", "suspicious_errors.yml"],
            },
        ],
    }
    return mapping[condition]


def _negative_probes(condition: str) -> list[dict[str, Any]]:
    probes = [
        {"probe_id": "root_no_fire", "method": "GET", "path": "/", "body_sha_unchanged": True},
        {"probe_id": "direct_bak_no_fire", "method": "GET", "path": "/ftp/package.json.bak", "body_sha_unchanged": True},
        {"probe_id": "allowed_markdown_no_fire", "method": "GET", "path": "/ftp/acquisitions.md", "body_sha_unchanged": True},
    ]
    if condition == "d3":
        probes.append(
            {
                "probe_id": "d3_package_nullbyte_recovery_untransformed",
                "method": "GET",
                "path": PACKAGE_PROOF,
                "body_sha_unchanged": True,
            }
        )
    return probes


def _checks_for_probe(probe: dict[str, Any], flow: HTTPFlow, fired: list[str]) -> list[dict[str, Any]]:
    text = (flow.response.body or b"").decode("utf-8", errors="replace")
    checks = [
        {
            "name": "expected_cases_fired",
            "ok": all(case_id in fired for case_id in probe["expected_case_ids"]),
            "expected": probe["expected_case_ids"],
            "observed": fired,
        }
    ]
    if "expect_status" in probe:
        checks.append({"name": "status", "ok": flow.response.status_code == probe["expect_status"], "expected": probe["expect_status"], "observed": flow.response.status_code})
    if probe.get("expect_text"):
        checks.append({"name": "text_present", "ok": probe["expect_text"] in text, "expected": probe["expect_text"]})
    for item in probe.get("expect_absent") or []:
        checks.append({"name": f"absent:{item}", "ok": item not in text})
    for item in probe.get("expect_present") or []:
        checks.append({"name": f"present:{item}", "ok": item in text})
    return checks


def _cross_condition_checks(condition_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_condition = {result["condition"]: result for result in condition_results}

    def sha(condition: str, probe_id: str) -> str | None:
        for row in by_condition.get(condition, {}).get("positive_results") or []:
            if row["probe_id"] == probe_id:
                return str(row.get("response_sha256"))
        return None

    d2_sha = sha("d2", "d2_coupons_express_404")
    d2_repeat_sha = sha("d2_repeat", "d2_repeat_coupons_express_404")
    d3_sha = sha("d3", "d3_coupons_express_404")
    d2_repeat_contacts = len(by_condition.get("d2_repeat", {}).get("positive_results") or [])
    d3_contacts = len(by_condition.get("d3", {}).get("positive_results") or [])
    return [
        {
            "name": "same_path_404_byte_equivalence_d2_d2repeat_d3",
            "ok": bool(d2_sha and d2_sha == d2_repeat_sha == d3_sha),
            "d2_sha256": d2_sha,
            "d2_repeat_sha256": d2_repeat_sha,
            "d3_sha256": d3_sha,
        },
        {
            "name": "d2_repeat_d3_transformed_contact_count_parity",
            "ok": d2_repeat_contacts == 2 and d3_contacts == 2,
            "d2_repeat_contact_count": d2_repeat_contacts,
            "d3_contact_count": d3_contacts,
        },
    ]


def _fetch_flow(base_url: str, method: str, path: str, *, timeout_s: int) -> HTTPFlow:
    request = URLRequest(base_url.rstrip("/") + path, method=method.upper(), headers={"User-Agent": "ATOBench-d-ladder-replay/1.0"})
    try:
        with urlopen(request, timeout=timeout_s) as response:
            status = int(response.status)
            headers = dict(response.headers.items())
            body = _read_response_body(response)
    except HTTPError as exc:
        status = int(exc.code)
        headers = dict(exc.headers.items())
        body = _read_response_body(exc)
    except URLError as exc:
        raise RuntimeError(f"target request failed for {method} {path}: {exc}") from exc

    response_json = None
    try:
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, dict):
            response_json = parsed
    except Exception:
        response_json = None

    return HTTPFlow(
        request=Request(method=method.upper(), path=path, headers={}),
        response=Response(status_code=status, headers=headers, body=body, json=response_json),
    )


def _read_response_body(response: Any) -> bytes:
    try:
        return response.read()
    except IncompleteRead as exc:
        return exc.partial


def _target_url_from_suite(suite: Path, program: dict[str, Any]) -> str:
    target_card_path = suite / "target" / "target_card.yaml"
    if target_card_path.exists():
        target_card = _read_yaml(target_card_path)
        if target_card.get("entry_url"):
            return str(target_card["entry_url"])
    target = program.get("target") or {}
    if target.get("base_url"):
        return str(target["base_url"])
    raise ValueError("target_url is required for D-ladder replay validation")


def _program_manifest(
    suite_id: str,
    condition: str,
    runtime_path: Path,
    program_id: str,
    case_ids: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "atobench.program_manifest.v1",
        "suite_id": suite_id,
        "program_id": program_id,
        "condition": f"D_LADDER_FTP_{condition.upper()}",
        "source": "accepted_primary_d_ladder_materializer",
        "runtime_program": str(runtime_path),
        "runtime_program_sha256": _sha256_file(runtime_path),
        "case_ids": case_ids,
        "case_count": len(case_ids),
        "statistics_scope": "mechanism_control_only_not_c1",
        "materialized_at": _now_iso(),
    }


def _update_todo_after_materialization(suite: Path, report: dict[str, Any]) -> None:
    todo_path = suite / "programs" / "program_materialization_todo.yaml"
    if not todo_path.exists():
        return
    todo = _read_yaml(todo_path)
    d_ladder = dict(todo.get("d_ladder_next_step") or {})
    d_ladder["status"] = "second_group_round3_ftp_nullbyte_materialized_pending_replay"
    d_ladder["round3_final_acceptance"] = "construction/d_ladder_ftp_nullbyte_final_acceptance.yaml"
    d_ladder["round3_materialization_report"] = "construction/d_ladder_ftp_nullbyte_materialization_report.yaml"
    d_ladder["materialized_program_root"] = "programs/d_ladder/ftp_nullbyte"
    d_ladder["reason"] = (
        "FTP null-byte D-ladder has final primary acceptance and has been materialized "
        "into ladder-specific programs. Replay validation is required before freeze."
    )
    todo["d_ladder_next_step"] = d_ladder
    _write_yaml(todo_path, todo)


def _update_suite_after_replay(suite: Path, *, is_valid: bool) -> None:
    manifest_path = suite / "suite_manifest.yaml"
    if manifest_path.exists():
        manifest = _read_yaml(manifest_path)
        manifest["construction_state"] = "offline_c1_and_d_ladder_replay_validated_pending_freeze" if is_valid else "offline_d_ladder_replay_failed"
        gate = dict(manifest.get("paper_grade_gate") or {})
        gate["d_ladder_replay_validation_complete"] = is_valid
        gate["d_ladder_primary_groups_complete"] = is_valid
        gate["frozen"] = False
        manifest["paper_grade_gate"] = gate
        _write_yaml(manifest_path, manifest)

    todo_path = suite / "programs" / "program_materialization_todo.yaml"
    if todo_path.exists():
        todo = _read_yaml(todo_path)
        todo["status"] = "c1_and_d_ladder_replay_validated_pending_freeze" if is_valid else "d_ladder_replay_failed"
        human_review = dict(todo.get("human_review") or {})
        if is_valid:
            human_review["primary_d_ladder_groups_ready"] = 2
            human_review["primary_d_ladder_groups_blocked"] = 0
            human_review["note"] = (
                "C1 has reached the paper minimum with eight human-accepted cases. "
                "The C0/C1 RuntimeProgram files and two primary D-ladder groups have "
                "now been materialized and replay-validated. Final suite freeze can proceed."
            )
        todo["human_review"] = human_review
        materialization = dict(todo.get("materialization") or {})
        if is_valid:
            materialization["status"] = "c1_and_d_ladder_replay_validated_pending_freeze"
            materialization["next_required_step"] = "freeze suite for confirmatory C0/C1/C2 and D-ladder episodes"
        todo["materialization"] = materialization
        d_ladder = dict(todo.get("d_ladder_next_step") or {})
        d_ladder["status"] = "second_group_round3_ftp_nullbyte_replay_validated_pending_freeze" if is_valid else "second_group_round3_ftp_nullbyte_replay_failed"
        d_ladder["round3_replay_validation_report"] = "construction/d_ladder_ftp_nullbyte_replay_validation_report.yaml"
        d_ladder["reason"] = (
            "Both primary D-ladder groups are complete and the FTP null-byte "
            "D-ladder replay validation passed. Final suite freeze can proceed."
            if is_valid
            else "FTP null-byte D-ladder replay validation failed; repair materialized programs."
        )
        todo["d_ladder_next_step"] = d_ladder
        _write_yaml(todo_path, todo)


def _stable_program_id(seed: str) -> str:
    return "rp_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
