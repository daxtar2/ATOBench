#!/usr/bin/env python3
"""Local preflight gate for the CloudGoat Action/Plan candidate.

This script is deliberately read-only. It does not call AWS APIs, deploy
CloudGoat, run Terraform, or start an agent. It checks whether the local
workspace is even eligible to begin the live deployment/reset qualification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "targets/cloudgoat/source/cloudgoat"
FROZEN_COMMIT = "abf1ba8f5e47d7ced750fdfa025d51c99f1a43ed"
CONTRACTS = [
    REPO_ROOT
    / "targets/cloudgoat/action_plan_aou_discovery/CLOUDGOAT_DEPLOYMENT_SAFETY_CONTRACT_20260718.yaml",
    REPO_ROOT
    / "targets/cloudgoat/action_plan_aou_discovery/CLOUDGOAT_RESET_FINGERPRINT_CONTRACT_20260718.yaml",
    REPO_ROOT
    / "targets/cloudgoat/action_plan_aou_discovery/CLOUDGOAT_CLEAN_CALIBRATION_CONTRACT_20260718.yaml",
    REPO_ROOT / "experiment/ACTION_PLAN_CLOUD_RANGE_ADMISSION_GATE_20260718.yaml",
    REPO_ROOT
    / "targets/cloudgoat/action_plan_aou_discovery/CLOUDGOAT_CODEBUILD_CREDENTIAL_STEERING_AUGMENTATION_PROPOSAL.yaml",
]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 10) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _tool_checks() -> list[Check]:
    checks: list[Check] = []
    for tool in ("git", "aws", "terraform", "cloudgoat"):
        path = shutil.which(tool)
        checks.append(Check(f"tool_present:{tool}", bool(path), path or "missing"))
    return checks


def _contract_checks() -> tuple[list[Check], list[dict[str, str]]]:
    checks: list[Check] = []
    manifests: list[dict[str, str]] = []
    for path in CONTRACTS:
        rel = path.relative_to(REPO_ROOT)
        if not path.exists():
            checks.append(Check(f"contract_exists:{rel}", False, "missing"))
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(f"contract_parse:{rel}", False, str(exc)))
            continue
        schema = data.get("schema_version") if isinstance(data, dict) else None
        checks.append(Check(f"contract_parse:{rel}", isinstance(schema, str), str(schema)))
        manifests.append({"path": str(rel), "sha256": _sha256(path)})
    return checks, manifests


def _source_checks(source: Path) -> list[Check]:
    checks: list[Check] = [Check("source_dir_exists", source.exists(), str(source))]
    if not source.exists():
        return checks
    git_dir = source / ".git"
    checks.append(Check("source_git_dir_exists", git_dir.exists(), str(git_dir)))
    rc, stdout, stderr = _run(["git", "rev-parse", "HEAD"], cwd=source)
    head = stdout.strip()
    if rc == 0:
        checks.append(Check("source_head_readable", True, head))
        checks.append(Check("source_head_matches_frozen_commit", head == FROZEN_COMMIT, head))
    else:
        checks.append(Check("source_head_readable", False, stderr or stdout or "git rev-parse failed"))
        checks.append(Check("source_head_matches_frozen_commit", False, "no HEAD"))
    scenario = source / "cloudgoat/scenarios/aws/codebuild_secrets"
    if not scenario.exists():
        scenario = source / "scenarios/aws/codebuild_secrets"
    checks.append(Check("codebuild_secrets_source_present", scenario.exists(), str(scenario)))
    return checks


def _aws_env_checks() -> list[Check]:
    profile = os.environ.get("AWS_PROFILE", "")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    checks = [
        Check("aws_profile_explicit", bool(profile), profile or "unset"),
        Check(
            "aws_profile_not_default",
            bool(profile) and profile != "default",
            profile or "unset",
        ),
        Check("aws_region_frozen", region == "us-east-1", region or "unset"),
    ]
    return checks


def _private_manifest_checks(private_manifest: Path | None) -> list[Check]:
    if private_manifest is None:
        return [
            Check("private_manifest_declared", False, "missing --private-manifest"),
            Check("dedicated_account_allowlist_present", False, "not checked"),
            Check("cost_guard_declared", False, "not checked"),
        ]
    if not private_manifest.exists():
        return [
            Check("private_manifest_declared", False, str(private_manifest)),
            Check("dedicated_account_allowlist_present", False, "manifest missing"),
            Check("cost_guard_declared", False, "manifest missing"),
        ]
    try:
        data = yaml.safe_load(private_manifest.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [
            Check("private_manifest_declared", False, f"parse failed: {exc}"),
            Check("dedicated_account_allowlist_present", False, "parse failed"),
            Check("cost_guard_declared", False, "parse failed"),
        ]
    account_ids = data.get("allowed_account_ids") if isinstance(data, dict) else None
    cost_guard = data.get("cost_guard") if isinstance(data, dict) else None
    return [
        Check("private_manifest_declared", True, str(private_manifest)),
        Check(
            "dedicated_account_allowlist_present",
            isinstance(account_ids, list) and bool(account_ids),
            "present" if isinstance(account_ids, list) and bool(account_ids) else "missing",
        ),
        Check(
            "cost_guard_declared",
            isinstance(cost_guard, dict) and bool(cost_guard.get("max_campaign_usd")),
            "present" if isinstance(cost_guard, dict) and bool(cost_guard.get("max_campaign_usd")) else "missing",
        ),
    ]


def build_report(source: Path, private_manifest: Path | None) -> dict[str, Any]:
    checks: list[Check] = []
    checks.extend(_tool_checks())
    contract_checks, contract_manifest = _contract_checks()
    checks.extend(contract_checks)
    checks.extend(_source_checks(source))
    checks.extend(_aws_env_checks())
    checks.extend(_private_manifest_checks(private_manifest))
    hard_blockers = [
        check.name
        for check in checks
        if not check.ok
        and (
            check.name.startswith("tool_present:")
            or check.name.startswith("contract_")
            or check.name.startswith("source_")
            or check.name in {
                "codebuild_secrets_source_present",
                "aws_profile_explicit",
                "aws_profile_not_default",
                "aws_region_frozen",
                "private_manifest_declared",
                "dedicated_account_allowlist_present",
                "cost_guard_declared",
            }
        )
    ]
    return {
        "schema_version": "atobench.cloudgoat_preflight_gate_report.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "target": {
            "range": "CloudGoat",
            "scenario": "codebuild_secrets",
            "frozen_commit": FROZEN_COMMIT,
            "source_dir": str(source),
        },
        "contract_manifest": contract_manifest,
        "checks": [check.to_json() for check in checks],
        "hard_blockers": hard_blockers,
        "go_decision": "no_run" if hard_blockers else "eligible_for_live_deployment_safety_review",
        "authorized_next_step": (
            "fix_preflight_blockers"
            if hard_blockers
            else "run dedicated-account live deployment/reset qualification"
        ),
        "treatment_authorized": False,
        "formal_aou_admission": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--private-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(args.source, args.private_manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"go_decision": report["go_decision"], "hard_blockers": report["hard_blockers"]}, indent=2))
    return 0 if report["go_decision"] != "no_run" else 2


if __name__ == "__main__":
    raise SystemExit(main())
