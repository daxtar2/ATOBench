#!/usr/bin/env python3
"""Stage-0 readiness checks for the CloudGoat Action/Plan candidate.

The default mode is static and local-only. It does not call AWS. Use
``--live-sts`` only after selecting a dedicated experiment profile/account.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only on broken envs
    yaml = None


REPO_ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_ROOT = REPO_ROOT / "targets/cloudgoat/action_plan_aou_discovery"
DEPLOYMENT_CONTRACT = DISCOVERY_ROOT / "CLOUDGOAT_DEPLOYMENT_SAFETY_CONTRACT.yaml"
RESET_CONTRACT = DISCOVERY_ROOT / "CLOUDGOAT_RESET_FINGERPRINT_CONTRACT.yaml"
CLEAN_CONTRACT = DISCOVERY_ROOT / "CLOUDGOAT_CLEAN_CALIBRATION_CONTRACT.yaml"
SOURCE_DIR = REPO_ROOT / "targets/cloudgoat/source/cloudgoat"
DEFAULT_OUTPUT = DISCOVERY_ROOT / "readiness/cloudgoat_stage0_readiness.json"


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return sha256_bytes(path.read_bytes())


def canonical_hash(value: Any) -> str:
    return sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        return {}
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def run_command(args: list[str], *, cwd: Path | None = None, timeout: int = 20) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": str(error), "args": args}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "args": args,
    }


def command_check(command: str, version_args: list[str]) -> dict[str, Any]:
    path = shutil.which(command)
    result: dict[str, Any] = {"command": command, "present": bool(path), "path": path}
    if path:
        version = run_command(version_args)
        result["version_ok"] = version["ok"]
        result["version_output"] = (version.get("stdout") or version.get("stderr") or "")[:600]
    return result


def env_presence() -> dict[str, Any]:
    profile = os.environ.get("ATOBENCH_CLOUDGOAT_AWS_PROFILE") or os.environ.get("AWS_PROFILE")
    region = os.environ.get("ATOBENCH_CLOUDGOAT_AWS_REGION") or os.environ.get("AWS_REGION")
    account_ids = [
        item.strip()
        for item in (
            os.environ.get("ATOBENCH_CLOUDGOAT_ALLOWED_ACCOUNT_IDS")
            or os.environ.get("ATOBENCH_CLOUDGOAT_EXPECTED_ACCOUNT_ID", "")
        ).split(",")
        if item.strip()
    ]
    resource_prefix = os.environ.get("ATOBENCH_CLOUDGOAT_RESOURCE_PREFIX")
    max_usd = os.environ.get("ATOBENCH_CLOUDGOAT_MAX_ESTIMATED_USD") or os.environ.get(
        "ATOBENCH_CLOUDGOAT_COST_CEILING_USD"
    )
    ack = os.environ.get("ATOBENCH_CLOUDGOAT_DEPLOY_ACK")
    return {
        "profile_set": bool(profile),
        "profile_name": profile if profile and profile != "default" else profile,
        "profile_is_default": profile == "default",
        "region_set": bool(region),
        "region": region,
        "account_allowlist_count": len(account_ids),
        "account_allowlist_hashes": [sha256_text(item) for item in account_ids],
        "resource_prefix_set": bool(resource_prefix),
        "resource_prefix": resource_prefix,
        "max_estimated_usd_set": bool(max_usd),
        "max_estimated_usd": max_usd,
        "deploy_ack_set": bool(ack),
        "deploy_ack_valid": ack == "cloudgoat-codebuild-atobench-dedicated-account",
    }


def source_lock(expected_commit: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source_dir": str(SOURCE_DIR),
        "source_dir_exists": SOURCE_DIR.exists(),
        "expected_commit": expected_commit,
    }
    if not SOURCE_DIR.exists():
        result["status"] = "fail_missing_source_dir"
        return result
    git_dir = SOURCE_DIR / ".git"
    result["git_dir_exists"] = git_dir.exists()
    if git_dir.exists():
        head = run_command(["git", "rev-parse", "HEAD"], cwd=SOURCE_DIR)
        result["head_ok"] = head["ok"]
        result["head_commit"] = head.get("stdout") if head["ok"] else None
        result["matches_expected_commit"] = bool(
            expected_commit and result["head_commit"] == expected_commit
        )
    scenario_candidates = [
        SOURCE_DIR / "cloudgoat/scenarios/aws/codebuild_secrets",
        SOURCE_DIR / "cloudgoat/scenarios/codebuild_secrets",
        SOURCE_DIR / "scenarios/aws/codebuild_secrets",
        SOURCE_DIR / "scenarios/codebuild_secrets",
    ]
    scenario = next((candidate for candidate in scenario_candidates if candidate.exists()), scenario_candidates[0])
    result["scenario_dir_candidates"] = [str(candidate) for candidate in scenario_candidates]
    result["scenario_dir"] = str(scenario)
    result["scenario_dir_exists"] = scenario.exists()
    if scenario.exists():
        entries: list[dict[str, str]] = []
        for path in sorted(item for item in scenario.rglob("*") if item.is_file()):
            rel = str(path.relative_to(SOURCE_DIR))
            entries.append({"path": rel, "sha256": sha256_bytes(path.read_bytes())})
        result["scenario_file_count"] = len(entries)
        result["scenario_tree_sha256"] = canonical_hash(entries)
    checks = [
        result.get("git_dir_exists"),
        result.get("matches_expected_commit"),
        result.get("scenario_dir_exists"),
    ]
    result["status"] = "pass" if all(checks) else "fail_source_lock_incomplete"
    return result


def static_decision(tools: dict[str, Any], env: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    if not tools["aws"]["present"]:
        failures.append("aws_cli_missing")
    if not tools["terraform"]["present"]:
        failures.append("terraform_missing")
    if not tools["cloudgoat"]["present"]:
        failures.append("cloudgoat_cli_missing")
    if not env["profile_set"]:
        failures.append("no_dedicated_aws_profile_selected")
    if env["profile_is_default"]:
        failures.append("selected_profile_is_default")
    if not env["region_set"]:
        failures.append("region_not_set")
    if env["account_allowlist_count"] < 1:
        failures.append("account_allowlist_missing")
    if not env["resource_prefix_set"]:
        failures.append("resource_prefix_missing")
    if not env["max_estimated_usd_set"]:
        failures.append("cost_budget_missing")
    if not env["deploy_ack_valid"]:
        failures.append("deploy_ack_missing_or_invalid")
    if source.get("status") != "pass":
        failures.append("source_lock_not_passed")
    return {
        "stage0_static_pass": not failures,
        "failures": failures,
        "authorized_next_stage": "stage_1_live_identity_readiness" if not failures else "hold",
        "treatment_authorized": False,
    }


def live_sts(profile: str, region: str, allowed_account_hashes: set[str]) -> dict[str, Any]:
    args = [
        "aws",
        "sts",
        "get-caller-identity",
        "--profile",
        profile,
        "--region",
        region,
        "--output",
        "json",
    ]
    raw = run_command(args)
    result: dict[str, Any] = {"called": True, "ok": raw["ok"]}
    if not raw["ok"]:
        result["error"] = raw.get("stderr") or raw.get("stdout") or raw.get("error")
        return result
    try:
        payload = json.loads(raw["stdout"])
    except json.JSONDecodeError as error:
        result["ok"] = False
        result["error"] = f"invalid sts json: {error}"
        return result
    account = str(payload.get("Account", ""))
    arn = str(payload.get("Arn", ""))
    result.update(
        {
            "account_id_hash": sha256_text(account),
            "account_allowlist_match": sha256_text(account) in allowed_account_hashes,
            "arn_shape": arn.split("/", 1)[0] if arn else "",
            "user_id_hash": sha256_text(str(payload.get("UserId", ""))),
        }
    )
    result["ok"] = bool(result["account_allowlist_match"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live-sts", action="store_true")
    args = parser.parse_args()

    deployment = load_yaml(DEPLOYMENT_CONTRACT)
    target_block = deployment.get("target", {}) if isinstance(deployment.get("target"), dict) else {}
    source_lock_block = (
        deployment.get("source_lock", {}) if isinstance(deployment.get("source_lock"), dict) else {}
    )
    expected_commit = source_lock_block.get("required_commit") or target_block.get(
        "frozen_source_commit"
    )
    tools = {
        "aws": command_check("aws", ["aws", "--version"]),
        "terraform": command_check("terraform", ["terraform", "version"]),
        "cloudgoat": command_check("cloudgoat", ["cloudgoat", "--version"]),
        "python3": command_check("python3", ["python3", "--version"]),
    }
    env = env_presence()
    source = source_lock(expected_commit)
    contract_hashes = {
        "deployment_contract_sha256": file_sha256(DEPLOYMENT_CONTRACT),
        "reset_contract_sha256": file_sha256(RESET_CONTRACT),
        "clean_calibration_contract_sha256": file_sha256(CLEAN_CONTRACT),
    }
    decision = static_decision(tools, env, source)

    live: dict[str, Any] = {"called": False}
    if args.live_sts:
        if not decision["stage0_static_pass"]:
            live = {
                "called": False,
                "blocked_reason": "stage0_static_readiness_failed",
            }
        else:
            allowed_hashes = set(env["account_allowlist_hashes"])
            live = live_sts(env["profile_name"], env["region"], allowed_hashes)

    receipt = {
        "schema_version": "atobench.cloudgoat_stage0_readiness_receipt.v1",
        "created_at": iso_now(),
        "contracts": contract_hashes,
        "toolchain": tools,
        "environment": env,
        "source_lock": source,
        "static_decision": decision,
        "live_sts": live,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"stage0_static_pass": decision["stage0_static_pass"], "failures": decision["failures"]}))
    return 0 if decision["stage0_static_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
