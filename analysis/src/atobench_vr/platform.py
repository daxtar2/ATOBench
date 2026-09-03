"""Generic, fail-closed registry for frozen targets, AOUs and profiles.

This is intentionally an orchestration *spine*, not a second implementation of
the legacy runners.  A manifest binds one frozen target, a set of frozen AOUs,
one analysis profile and a declared execution adapter.  The first adapter only
plans the already-existing ``atobench-cross-model`` command; it never starts a
target or calls a model.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import GateError, read_json, sha256_file, sha256_json, utc_now, write_json

REGISTRY_SCHEMA = "atobench.platform_registry.v1"
FREEZE_SCHEMA = "atobench.platform_freeze.v1"
HANDOFF_SCHEMA = "atobench.execution_handoff.v1"
RECEIPT_SCHEMA = "atobench.execution_receipt.v1"
DEFAULT_REGISTRY = Path(__file__).resolve().parents[2] / "config" / "platform" / "registry.json"
CAMPAIGN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,100}$")


@dataclass(frozen=True)
class PlatformContext:
    project_root: Path
    registry_path: Path
    registry: dict[str, Any]


def _mapping(path: Path) -> dict[str, Any]:
    try:
        value = read_json(path)
    except FileNotFoundError as exc:
        raise GateError(f"missing manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GateError(f"invalid JSON manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateError(f"manifest root must be an object: {path}")
    return value


def _project_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "config").is_dir():
            return candidate
    raise GateError(f"cannot locate ATOBench project root from {start}")


def _resolve(project_root: Path, declared: str) -> Path:
    value = Path(declared).expanduser()
    return value.resolve() if value.is_absolute() else (project_root / value).resolve()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def load_platform(registry_path: Path) -> PlatformContext:
    project_root = _project_root(registry_path)
    registry = _mapping(registry_path)
    if registry.get("schema_version") != REGISTRY_SCHEMA:
        raise GateError(f"unsupported platform registry schema: {registry.get('schema_version')}")
    for key in ("targets", "aous", "profiles", "campaigns"):
        if not isinstance(registry.get(key), dict):
            raise GateError(f"platform registry missing object section: {key}")
    return PlatformContext(project_root, registry_path.resolve(), registry)


def _component(
    context: PlatformContext, section: str, component_id: str
) -> tuple[Path, dict[str, Any]]:
    declared = context.registry[section].get(component_id)
    if not isinstance(declared, str):
        raise GateError(f"unknown {section[:-1]} id: {component_id}")
    path = _resolve(context.project_root, declared)
    value = _mapping(path)
    if value.get("id") != component_id:
        raise GateError(f"{section[:-1]} id mismatch in {path}: {value.get('id')}")
    return path, value


def _validate_artifacts(
    *,
    component_name: str,
    component: dict[str, Any],
    project_root: Path,
    allow_external_artifacts: bool,
) -> list[dict[str, Any]]:
    artifacts = component.get("frozen_artifacts") or []
    if not isinstance(artifacts, list):
        raise GateError(f"{component_name}.frozen_artifacts must be a list")
    checked: list[dict[str, Any]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            raise GateError(f"invalid artifact declaration in {component_name}")
        artifact_id = item.get("artifact_id")
        declared_path = item.get("path")
        expected = item.get("sha256")
        if not isinstance(artifact_id, str) or not isinstance(declared_path, str):
            raise GateError(f"artifact needs artifact_id and path in {component_name}")
        if not isinstance(expected, str) or len(expected) != 64:
            raise GateError(f"artifact needs 64-char sha256 in {component_name}:{artifact_id}")
        path = _resolve(project_root, declared_path)
        if not _inside(path, project_root) and not allow_external_artifacts:
            raise GateError(
                f"external frozen artifact requires --allow-external-artifacts: {artifact_id}"
            )
        if not path.is_file():
            raise GateError(f"missing frozen artifact {artifact_id}: {path}")
        actual = sha256_file(path)
        if actual != expected:
            raise GateError(
                f"frozen artifact hash mismatch for {artifact_id}: expected {expected}, got {actual}"
            )
        checked.append(
            {
                "artifact_id": artifact_id,
                "declared_path": declared_path,
                "sha256": actual,
                "external_by_design": not _inside(path, project_root),
            }
        )
    return checked


def _campaign_bundle(
    context: PlatformContext, campaign_id: str
) -> dict[str, Any]:
    campaign_path, campaign = _component(context, "campaigns", campaign_id)
    if campaign.get("schema_version") != "atobench.campaign.v1":
        raise GateError(f"unsupported campaign schema in {campaign_path}")
    target_id = campaign.get("target_id")
    profile_id = campaign.get("profile_id")
    aou_ids = campaign.get("aou_ids")
    if not isinstance(target_id, str) or not isinstance(profile_id, str):
        raise GateError("campaign requires target_id and profile_id")
    if not isinstance(aou_ids, list) or not aou_ids or not all(isinstance(value, str) for value in aou_ids):
        raise GateError("campaign requires a non-empty aou_ids list")
    target_path, target = _component(context, "targets", target_id)
    profile_path, profile = _component(context, "profiles", profile_id)
    aous = []
    for aou_id in aou_ids:
        aou_path, aou = _component(context, "aous", aou_id)
        if aou.get("target_id") != target_id:
            raise GateError(f"AOU {aou_id} is not compatible with target {target_id}")
        if aou.get("freeze_status") != "frozen":
            raise GateError(f"AOU {aou_id} is not frozen")
        aous.append((aou_path, aou))
    return {
        "campaign_path": campaign_path,
        "campaign": campaign,
        "target_path": target_path,
        "target": target,
        "profile_path": profile_path,
        "profile": profile,
        "aous": aous,
    }


def validate_campaign(
    *,
    registry_path: Path,
    campaign_id: str,
    allow_external_artifacts: bool,
) -> dict[str, Any]:
    context = load_platform(registry_path)
    bundle = _campaign_bundle(context, campaign_id)
    campaign = bundle["campaign"]
    target = bundle["target"]
    profile = bundle["profile"]
    if campaign.get("freeze_status") != "frozen":
        raise GateError(f"campaign {campaign_id} is not frozen")
    if target.get("freeze_status") != "frozen":
        raise GateError(f"target {target.get('id')} is not frozen")
    if profile.get("freeze_status") != "frozen":
        raise GateError(f"profile {profile.get('id')} is not frozen")
    artifact_groups = {
        "campaign": _validate_artifacts(
            component_name="campaign",
            component=campaign,
            project_root=context.project_root,
            allow_external_artifacts=allow_external_artifacts,
        ),
        "target": _validate_artifacts(
            component_name="target",
            component=target,
            project_root=context.project_root,
            allow_external_artifacts=allow_external_artifacts,
        ),
        "profile": _validate_artifacts(
            component_name="profile",
            component=profile,
            project_root=context.project_root,
            allow_external_artifacts=allow_external_artifacts,
        ),
        "aous": {},
    }
    for _, aou in bundle["aous"]:
        artifact_groups["aous"][aou["id"]] = _validate_artifacts(
            component_name=f"aou:{aou['id']}",
            component=aou,
            project_root=context.project_root,
            allow_external_artifacts=allow_external_artifacts,
        )
    return {
        "schema_version": "atobench.platform_validation.v1",
        "status": "PASS_FROZEN_BINDING",
        "registry_sha256": sha256_file(context.registry_path),
        "campaign_id": campaign_id,
        "target_id": target["id"],
        "aou_ids": [aou["id"] for _, aou in bundle["aous"]],
        "profile_id": profile["id"],
        "manifest_hashes": {
            "campaign": sha256_file(bundle["campaign_path"]),
            "target": sha256_file(bundle["target_path"]),
            "profile": sha256_file(bundle["profile_path"]),
            "aous": {
                aou["id"]: sha256_file(path) for path, aou in bundle["aous"]
            },
        },
        "artifacts": artifact_groups,
        "model_calls_made": 0,
        "network_accessed": False,
    }


def init_campaign(
    *,
    registry_path: Path,
    campaign_id: str,
    target_id: str,
    aou_ids: list[str],
    profile_id: str,
    models: list[str],
    rounds: int,
    parallel_workers: int,
    start_target: bool,
    continue_on_error: bool,
    agent_timeout_multiplier: float | None,
    max_tool_calls: int | None,
    runner_adapter: str,
    runner_entrypoint: str,
    runner_module: str | None,
    output_path: Path | None,
    register: bool,
    allow_external_artifacts: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    """Build a new immutable campaign manifest from registered frozen components.

    Registration is deliberately separate from construction: callers can inspect
    an unregistered manifest first, and a registry changes only with ``register``.
    """
    if not CAMPAIGN_ID_PATTERN.fullmatch(campaign_id):
        raise GateError(
            "campaign id must use lowercase letters, digits and hyphens (3-101 characters)"
        )
    if not aou_ids or not all(isinstance(value, str) and value for value in aou_ids):
        raise GateError("--aous requires one or more registered AOU ids")
    if len(set(aou_ids)) != len(aou_ids):
        raise GateError("--aous must not contain duplicate AOU ids")
    if not models or not all(isinstance(value, str) and value for value in models):
        raise GateError("--models requires one or more model identifiers")
    if rounds < 1 or parallel_workers < 1:
        raise GateError("--rounds and --parallel-workers must be positive")
    if agent_timeout_multiplier is not None and agent_timeout_multiplier <= 0:
        raise GateError("--agent-timeout-multiplier must be positive when supplied")
    if max_tool_calls is not None and max_tool_calls < 1:
        raise GateError("--max-tool-calls must be positive when supplied")
    if runner_adapter != "atobench-cross-model-v1":
        raise GateError(f"unsupported runner adapter: {runner_adapter}")

    context = load_platform(registry_path)
    if campaign_id in context.registry["campaigns"]:
        raise GateError(f"campaign id is already registered: {campaign_id}")
    target_path, target = _component(context, "targets", target_id)
    profile_path, profile = _component(context, "profiles", profile_id)
    if target.get("freeze_status") != "frozen":
        raise GateError(f"target {target_id} is not frozen")
    if profile.get("freeze_status") != "frozen":
        raise GateError(f"profile {profile_id} is not frozen")
    aous: list[tuple[Path, dict[str, Any]]] = []
    for aou_id in aou_ids:
        aou_path, aou = _component(context, "aous", aou_id)
        if aou.get("freeze_status") != "frozen":
            raise GateError(f"AOU {aou_id} is not frozen")
        if aou.get("target_id") != target_id:
            raise GateError(f"AOU {aou_id} is not compatible with target {target_id}")
        runtime_adapter = aou.get("runtime_adapter") or {}
        if not isinstance(runtime_adapter.get("aou_key"), str) or not runtime_adapter["aou_key"]:
            raise GateError(f"AOU {aou_id} lacks legacy runtime_adapter.aou_key")
        aous.append((aou_path, aou))

    # A manifest is only called frozen when all of its source artifacts still
    # validate at construction time, including explicitly authorized external ones.
    _validate_artifacts(
        component_name="target",
        component=target,
        project_root=context.project_root,
        allow_external_artifacts=allow_external_artifacts,
    )
    _validate_artifacts(
        component_name="profile",
        component=profile,
        project_root=context.project_root,
        allow_external_artifacts=allow_external_artifacts,
    )
    for _, aou in aous:
        _validate_artifacts(
            component_name=f"aou:{aou['id']}",
            component=aou,
            project_root=context.project_root,
            allow_external_artifacts=allow_external_artifacts,
        )
    runner_path = _resolve(context.project_root, runner_entrypoint)
    if not _inside(runner_path, context.project_root) and not allow_external_artifacts:
        raise GateError("external runner requires --allow-external-artifacts")
    if not runner_path.is_file():
        raise GateError(f"runner entrypoint is missing: {runner_path}")
    runner_artifact_specs = [("legacy_runner", runner_entrypoint, runner_path)]
    # The entrypoint delegates to these Python modules. Freeze them alongside
    # the shell wrapper so a compatibility fix cannot silently change the
    # source actually executed by a previously prepared campaign.
    if runner_adapter == "atobench-cross-model-v1" and runner_module == "atobench.experiment.cross_model_protocol":
        for artifact_id, declared_path in (
            ("legacy_runner_protocol", "../runtime/atobench/experiment/cross_model_protocol.py"),
        ):
            artifact_path = _resolve(context.project_root, declared_path)
            if not _inside(artifact_path, context.project_root) and not allow_external_artifacts:
                raise GateError("external runner requires --allow-external-artifacts")
            if not artifact_path.is_file():
                raise GateError(f"runner module is missing: {artifact_path}")
            runner_artifact_specs.append((artifact_id, declared_path, artifact_path))

    if output_path is None:
        output_path = (
            context.project_root
            / "config"
            / "platform"
            / "campaigns"
            / f"{campaign_id.replace('-', '_')}.json"
        )
    output_path = output_path.resolve()
    if register and not _inside(output_path, context.project_root):
        raise GateError("a registered campaign manifest must be inside the project root")
    if not dry_run and output_path.exists() and not new_version:
        raise GateError(f"refusing to overwrite campaign manifest: {output_path}")

    manifest = {
        "schema_version": "atobench.campaign.v1",
        "id": campaign_id,
        "label": f"Generated frozen campaign {campaign_id}",
        "target_id": target_id,
        "aou_ids": aou_ids,
        "profile_id": profile_id,
        "freeze_status": "frozen",
        "runner": {
            "adapter": runner_adapter,
            "entrypoint": runner_entrypoint,
            "module": runner_module,
        },
        "execution": {
            "rounds": rounds,
            "models": models,
            "parallel_workers": parallel_workers,
            "continue_on_error": continue_on_error,
            "start_target": start_target,
            "agent_timeout_multiplier": agent_timeout_multiplier,
            "max_tool_calls": max_tool_calls,
        },
        "frozen_artifacts": [
            {
                "artifact_id": artifact_id,
                "path": declared_path,
                "sha256": sha256_file(artifact_path),
            }
            for artifact_id, declared_path, artifact_path in runner_artifact_specs
        ],
    }
    registry_path_value = None
    if register:
        registry_path_value = output_path.relative_to(context.project_root).as_posix()
    result = {
        "schema_version": "atobench.platform_campaign_init.v1",
        "status": (
            "DRY_RUN_READY"
            if dry_run
            else "CREATED_AND_REGISTERED"
            if register
            else "CREATED_UNREGISTERED"
        ),
        "campaign_id": campaign_id,
        "manifest_path": str(output_path),
        "manifest": manifest,
        "registration": {
            "requested": register,
            "performed": register and not dry_run,
            "registry_path": registry_path_value,
        },
        "model_calls_made": 0,
        "network_accessed": False,
    }
    if dry_run:
        return result
    write_json(output_path, manifest)
    if register:
        registry = dict(context.registry)
        campaigns = dict(registry["campaigns"])
        campaigns[campaign_id] = registry_path_value
        registry["campaigns"] = campaigns
        write_json(context.registry_path, registry)
    return result


def plan_campaign(
    *,
    registry_path: Path,
    campaign_id: str,
    allow_external_artifacts: bool,
) -> dict[str, Any]:
    validation = validate_campaign(
        registry_path=registry_path,
        campaign_id=campaign_id,
        allow_external_artifacts=allow_external_artifacts,
    )
    context = load_platform(registry_path)
    bundle = _campaign_bundle(context, campaign_id)
    campaign = bundle["campaign"]
    runner = campaign.get("runner") or {}
    if runner.get("adapter") != "atobench-cross-model-v1":
        raise GateError(f"unsupported runner adapter: {runner.get('adapter')}")
    entrypoint = runner.get("entrypoint")
    if not isinstance(entrypoint, str):
        raise GateError("runner requires entrypoint")
    runner_path = _resolve(context.project_root, entrypoint)
    if not _inside(runner_path, context.project_root) and not allow_external_artifacts:
        raise GateError("external runner requires --allow-external-artifacts")
    if not runner_path.is_file():
        raise GateError(f"runner entrypoint is missing: {runner_path}")
    execution = campaign.get("execution") or {}
    models = execution.get("models") or []
    aou_ids = campaign.get("aou_ids") or []
    legacy_aou_keys: list[str] = []
    for _, aou in bundle["aous"]:
        runtime_adapter = aou.get("runtime_adapter") or {}
        aou_key = runtime_adapter.get("aou_key")
        if not isinstance(aou_key, str) or not aou_key:
            raise GateError(f"AOU {aou['id']} lacks legacy runtime_adapter.aou_key")
        legacy_aou_keys.append(aou_key)
    if not all(isinstance(value, str) and value for value in models):
        raise GateError("execution.models must be a non-empty string list")
    rounds = execution.get("rounds")
    workers = execution.get("parallel_workers", 1)
    if not isinstance(rounds, int) or rounds < 1 or not isinstance(workers, int) or workers < 1:
        raise GateError("execution rounds and parallel_workers must be positive integers")
    command = [
        entrypoint,
        "--campaign-id",
        campaign_id,
        "--rounds",
        str(rounds),
        "--models",
        *models,
        "--aous",
        *legacy_aou_keys,
        "--parallel-workers",
        str(workers),
    ]
    if execution.get("continue_on_error", True):
        command.append("--continue-on-error")
    if execution.get("start_target", False):
        command.append("--start-target")
    if isinstance(execution.get("agent_timeout_multiplier"), (int, float)):
        command.extend(["--agent-timeout-multiplier", str(execution["agent_timeout_multiplier"])])
    if execution.get("max_tool_calls") is not None:
        command.extend(["--agent-max-tool-calls", str(execution["max_tool_calls"])])
    return {
        "schema_version": "atobench.platform_execution_plan.v1",
        "status": "DRY_RUN_PLAN_ONLY",
        "validation": validation,
        "runner": {
            "adapter": runner["adapter"],
            "entrypoint": entrypoint,
            "entrypoint_sha256": sha256_file(runner_path),
            "module": runner.get("module"),
        },
        "aou_bindings": [
            {
                "platform_aou_id": aou_id,
                "legacy_aou_key": legacy_aou_keys[index],
            }
            for index, aou_id in enumerate(aou_ids)
        ],
        "command": command,
        "execution_block": (
            "This scaffold emits a validated plan only. It does not start a target, "
            "invoke a model, or execute the legacy runner."
        ),
        "model_calls_made": 0,
        "network_accessed": False,
    }


def freeze_campaign(
    *,
    registry_path: Path,
    campaign_id: str,
    output_path: Path,
    allow_external_artifacts: bool,
    new_version: bool,
) -> dict[str, Any]:
    if output_path.exists() and not new_version:
        raise GateError(f"refusing to overwrite freeze lock: {output_path}")
    plan = plan_campaign(
        registry_path=registry_path,
        campaign_id=campaign_id,
        allow_external_artifacts=allow_external_artifacts,
    )
    lock = {
        "schema_version": FREEZE_SCHEMA,
        "status": "FROZEN_PLAN_ONLY",
        "campaign_id": campaign_id,
        "binding": plan["validation"],
        "runner": plan["runner"],
        "command": plan["command"],
        "lock_id": "plock_" + sha256_json([campaign_id, plan["validation"], plan["runner"]])[:20],
        "execution_performed": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    write_json(output_path, lock)
    return lock


def _validate_freeze_lock(lock_path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    lock = _mapping(lock_path)
    if lock.get("schema_version") != FREEZE_SCHEMA:
        raise GateError(f"unsupported freeze lock schema: {lock.get('schema_version')}")
    if lock.get("status") != "FROZEN_PLAN_ONLY":
        raise GateError(f"freeze lock is not plan-only frozen: {lock.get('status')}")
    for key in ("campaign_id", "binding", "runner", "command"):
        expected = {
            "campaign_id": plan["validation"]["campaign_id"],
            "binding": plan["validation"],
            "runner": plan["runner"],
            "command": plan["command"],
        }[key]
        actual = lock.get(key)
        # The registry is an index/locator, not part of a campaign's immutable
        # source binding. Adding an unrelated campaign must not revoke every
        # existing frozen lock. Component IDs, manifest hashes and artifacts are
        # still compared exactly.
        if key == "binding":
            if not isinstance(actual, dict) or not isinstance(expected, dict):
                raise GateError("freeze lock binding must be an object")
            actual = {name: value for name, value in actual.items() if name != "registry_sha256"}
            expected = {name: value for name, value in expected.items() if name != "registry_sha256"}
        if actual != expected:
            raise GateError(f"freeze lock mismatch at {key}; revalidate and freeze a new lock")
    expected_lock_id = "plock_" + sha256_json(
        [lock["campaign_id"], lock["binding"], lock["runner"]]
    )[:20]
    if lock.get("lock_id") != expected_lock_id:
        raise GateError("freeze lock id does not match the bound plan")
    return lock


def prepare_execution(
    *,
    registry_path: Path,
    campaign_id: str,
    lock_path: Path,
    output_dir: Path,
    allow_external_artifacts: bool,
    new_version: bool,
) -> dict[str, Any]:
    handoff_path = output_dir / "execution_handoff.json"
    if output_dir.exists() and any(output_dir.iterdir()) and not new_version:
        raise GateError(f"refusing to reuse non-empty execution output: {output_dir}")
    plan = plan_campaign(
        registry_path=registry_path,
        campaign_id=campaign_id,
        allow_external_artifacts=allow_external_artifacts,
    )
    lock = _validate_freeze_lock(lock_path, plan)
    context = load_platform(registry_path)
    bundle = _campaign_bundle(context, campaign_id)
    target_adapter = bundle["target"].get("adapter") or {}
    supports = target_adapter.get("supports") or []
    required_capabilities = ["health"]
    if (bundle["campaign"].get("execution") or {}).get("start_target"):
        required_capabilities.extend(["start", "reset"])
    missing_capabilities = [
        capability for capability in required_capabilities if capability not in supports
    ]
    if missing_capabilities:
        raise GateError(
            f"target adapter lacks execution capabilities: {', '.join(missing_capabilities)}"
        )
    handoff = {
        "schema_version": HANDOFF_SCHEMA,
        "status": "READY_FOR_EXPLICIT_EXECUTION",
        "campaign_id": campaign_id,
        "freeze_lock": {
            "declared_path": str(lock_path),
            "sha256": sha256_file(lock_path),
            "lock_id": lock["lock_id"],
        },
        "plan_sha256": sha256_json(plan),
        "command": plan["command"],
        "working_directory": ".",
        "preflight": {
            "frozen_binding_status": plan["validation"]["status"],
            "external_artifacts_revalidated": True,
            "target_adapter": bundle["target"]["id"],
            "required_capabilities": required_capabilities,
            "missing_capabilities": [],
            "output_directory": str(output_dir),
            "output_directory_is_new": True,
        },
        "authorization": {
            "real_execution_requires": [
                "--allow-external-artifacts",
                "--allow-real-execution",
            ],
            "execution_performed": False,
        },
        "safety": {
            "stdout_or_agent_content_captured": False,
            "raw_credentials_captured": False,
            "receipt_contains_only_metadata": True,
        },
        "model_calls_made": 0,
        "network_accessed": False,
    }
    write_json(handoff_path, handoff)
    return handoff


def execute_campaign(
    *,
    registry_path: Path,
    campaign_id: str,
    lock_path: Path,
    handoff_path: Path,
    allow_external_artifacts: bool,
    allow_real_execution: bool,
    new_version: bool,
) -> dict[str, Any]:
    if not allow_real_execution:
        raise GateError("real execution requires --allow-real-execution")
    handoff = _mapping(handoff_path)
    if handoff.get("schema_version") != HANDOFF_SCHEMA:
        raise GateError(f"unsupported handoff schema: {handoff.get('schema_version')}")
    if handoff.get("status") != "READY_FOR_EXPLICIT_EXECUTION":
        raise GateError(f"handoff is not executable: {handoff.get('status')}")
    if handoff.get("campaign_id") != campaign_id:
        raise GateError("handoff campaign differs from requested campaign")
    plan = plan_campaign(
        registry_path=registry_path,
        campaign_id=campaign_id,
        allow_external_artifacts=allow_external_artifacts,
    )
    lock = _validate_freeze_lock(lock_path, plan)
    if handoff.get("plan_sha256") != sha256_json(plan):
        raise GateError("handoff plan differs from current validated plan; prepare a new handoff")
    freeze = handoff.get("freeze_lock") or {}
    if freeze.get("sha256") != sha256_file(lock_path) or freeze.get("lock_id") != lock["lock_id"]:
        raise GateError("handoff freeze-lock binding differs from supplied lock")
    output_dir = handoff_path.parent
    receipt_path = output_dir / "execution_receipt.json"
    if receipt_path.exists() and not new_version:
        raise GateError(f"refusing to overwrite execution receipt: {receipt_path}")
    context = load_platform(registry_path)
    command = [str(_resolve(context.project_root, plan["command"][0])), *plan["command"][1:]]
    start = utc_now()
    result = subprocess.run(command, cwd=context.project_root, check=False)
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "status": "COMPLETED" if result.returncode == 0 else "FAILED",
        "campaign_id": campaign_id,
        "freeze_lock_id": lock["lock_id"],
        "plan_sha256": sha256_json(plan),
        "command_sha256": sha256_json(plan["command"]),
        "started_at": start,
        "finished_at": utc_now(),
        "exit_code": result.returncode,
        "stdout_or_agent_content_captured": False,
        "raw_credentials_captured": False,
        "model_calls_made_by_platform": 0,
        "network_accessed_by_platform": False,
        "next_step": "inspect the legacy runner campaign manifest before profile ingestion",
    }
    write_json(receipt_path, receipt)
    return receipt


def list_platform(registry_path: Path) -> dict[str, Any]:
    context = load_platform(registry_path)
    return {
        "schema_version": "atobench.platform_list.v1",
        "status": "PASS",
        "registry_sha256": sha256_file(context.registry_path),
        "targets": sorted(context.registry["targets"]),
        "aous": sorted(context.registry["aous"]),
        "profiles": sorted(context.registry["profiles"]),
        "campaigns": sorted(context.registry["campaigns"]),
        "model_calls_made": 0,
        "network_accessed": False,
    }


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ATOBench generic frozen-platform scaffold")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    init = subparsers.add_parser(
        "init-campaign",
        help="build a new frozen campaign from registered components",
    )
    init.add_argument("--id", required=True, dest="campaign_id")
    init.add_argument("--target", required=True, dest="target_id")
    init.add_argument("--aous", required=True, nargs="+", dest="aou_ids")
    init.add_argument("--profile", required=True, dest="profile_id")
    init.add_argument("--models", required=True, nargs="+")
    init.add_argument("--rounds", type=int, default=1)
    init.add_argument("--parallel-workers", type=int, default=1)
    init.add_argument("--start-target", action="store_true")
    init.add_argument("--no-continue-on-error", action="store_true")
    init.add_argument("--agent-timeout-multiplier", type=float)
    init.add_argument("--max-tool-calls", type=int)
    init.add_argument("--runner-adapter", default="atobench-cross-model-v1")
    init.add_argument("--runner-entrypoint", default="../runtime/atobench/scripts/atobench-cross-model")
    init.add_argument("--runner-module", default="atobench.experiment.cross_model_protocol")
    init.add_argument("--output", type=Path)
    init.add_argument("--register", action="store_true")
    init.add_argument("--allow-external-artifacts", action="store_true")
    init.add_argument("--dry-run", action="store_true")
    init.add_argument("--new-version", action="store_true")
    for name in ("validate", "plan", "run", "freeze", "prepare", "execute"):
        child = subparsers.add_parser(name)
        child.add_argument("--campaign", required=True)
        child.add_argument("--allow-external-artifacts", action="store_true")
        if name == "run":
            child.add_argument("--dry-run", action="store_true")
        if name == "prepare":
            child.add_argument("--lock", required=True, type=Path)
            child.add_argument("--output", required=True, type=Path)
            child.add_argument("--new-version", action="store_true")
        if name == "execute":
            child.add_argument("--lock", required=True, type=Path)
            child.add_argument("--handoff", required=True, type=Path)
            child.add_argument("--allow-real-execution", action="store_true")
            child.add_argument("--new-version", action="store_true")
        if name == "freeze":
            child.add_argument("--output", required=True, type=Path)
            child.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            result = list_platform(args.registry)
        elif args.command == "init-campaign":
            result = init_campaign(
                registry_path=args.registry,
                campaign_id=args.campaign_id,
                target_id=args.target_id,
                aou_ids=args.aou_ids,
                profile_id=args.profile_id,
                models=args.models,
                rounds=args.rounds,
                parallel_workers=args.parallel_workers,
                start_target=args.start_target,
                continue_on_error=not args.no_continue_on_error,
                agent_timeout_multiplier=args.agent_timeout_multiplier,
                max_tool_calls=args.max_tool_calls,
                runner_adapter=args.runner_adapter,
                runner_entrypoint=args.runner_entrypoint,
                runner_module=args.runner_module,
                output_path=args.output,
                register=args.register,
                allow_external_artifacts=args.allow_external_artifacts,
                dry_run=args.dry_run,
                new_version=args.new_version,
            )
        elif args.command == "validate":
            result = validate_campaign(
                registry_path=args.registry,
                campaign_id=args.campaign,
                allow_external_artifacts=args.allow_external_artifacts,
            )
        elif args.command in {"plan", "run"}:
            if args.command == "run" and not args.dry_run:
                raise GateError("use prepare then execute with explicit authorization; or pass --dry-run")
            result = plan_campaign(
                registry_path=args.registry,
                campaign_id=args.campaign,
                allow_external_artifacts=args.allow_external_artifacts,
            )
        elif args.command == "freeze":
            result = freeze_campaign(
                registry_path=args.registry,
                campaign_id=args.campaign,
                output_path=args.output,
                allow_external_artifacts=args.allow_external_artifacts,
                new_version=args.new_version,
            )
        elif args.command == "prepare":
            result = prepare_execution(
                registry_path=args.registry,
                campaign_id=args.campaign,
                lock_path=args.lock,
                output_dir=args.output,
                allow_external_artifacts=args.allow_external_artifacts,
                new_version=args.new_version,
            )
        else:
            result = execute_campaign(
                registry_path=args.registry,
                campaign_id=args.campaign,
                lock_path=args.lock,
                handoff_path=args.handoff,
                allow_external_artifacts=args.allow_external_artifacts,
                allow_real_execution=args.allow_real_execution,
                new_version=args.new_version,
            )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
