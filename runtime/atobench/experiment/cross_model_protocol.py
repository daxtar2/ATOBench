"""Cross-model Protocol-v3 runner for the frozen Juice Shop evidence AOUs.

This module intentionally wraps the existing `scripts/atobench-experiment`
entrypoint instead of reimplementing episode execution. It creates
model-specific experiment configs with isolated manifests/log directories, then
runs protocol assignment slots in the frozen per-AOU order.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTER_ROOT = REPO_ROOT.parent
SCRIPT = REPO_ROOT / "scripts" / "atobench-experiment"
DEFAULT_CAMPAIGN_ROOT = REPO_ROOT / "targets" / "juice-shop" / "experiments"
JUICE_SHOP_IMAGE = "bkimminich/juice-shop@sha256:e68144772ebaaca0ec117b38d44903af92416793230288ef7c5437fc4f26850a"
JUICE_SHOP_IMAGE_DIGEST = "sha256:e68144772ebaaca0ec117b38d44903af92416793230288ef7c5437fc4f26850a"
EVENT_LOCK = threading.Lock()


@dataclass(frozen=True)
class AouConfig:
    key: str
    unit_id: str
    block_prefix: str
    base_config: Path
    preferred_protocol: str


@dataclass(frozen=True)
class WorkerBinding:
    index: int
    target_port: int
    clean_proxy_port: int
    deception_proxy_port: int
    compose_file: Path
    target_state_contract: Path
    target_url: str
    health_url: str
    compose_project_name: str
    container_name: str


AOUS: dict[str, AouConfig] = {
    "sqli": AouConfig(
        key="sqli",
        unit_id="M-C1-SQLI-EVIDENCE-CLOSURE",
        block_prefix="S",
        base_config=REPO_ROOT / "examples" / "experiments" / "protocol_v3" / "juice-shop-protocol-v3-sqli.yaml",
        preferred_protocol="protocol_v3",
    ),
    "basket": AouConfig(
        key="basket",
        unit_id="M-AUTHZ-BASKET-SCOPE-CLOSURE-PERSISTENT-K2",
        block_prefix="B",
        base_config=REPO_ROOT / "examples" / "experiments" / "protocol_v3" / "juice-shop-protocol-v3_1-basket.yaml",
        preferred_protocol="protocol_v3_1",
    ),
    "jwt": AouConfig(
        key="jwt",
        unit_id="M-EXPOSURE-JWT-HASH-SUPPRESSION",
        block_prefix="J",
        base_config=REPO_ROOT / "examples" / "experiments" / "protocol_v3" / "juice-shop-protocol-v3_1-jwt.yaml",
        preferred_protocol="protocol_v3_1",
    ),
}


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_").lower()
    return slug or "model"


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_path(value: Any, base_dir: Path) -> str:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return str(path)


def _absolutize_config_paths(data: dict[str, Any], *, base_dir: Path) -> None:
    target = dict(data.get("target") or {})
    for key in ("target_dir", "compose_file"):
        if target.get(key):
            target[key] = _resolve_path(target[key], base_dir)
    data["target"] = target

    suite = dict(data.get("benchmark_suite") or {})
    for key in ("suite_dir", "c0_runtime_program", "c1_runtime_program", "c2_runtime_program"):
        if suite.get(key):
            suite[key] = _resolve_path(suite[key], base_dir)
    data["benchmark_suite"] = suite

    protocol = dict(data.get("protocol_v3") or {})
    for key in ("execution_spec", "target_state_contract"):
        if protocol.get(key):
            protocol[key] = _resolve_path(protocol[key], base_dir)
    data["protocol_v3"] = protocol


def _selector_map(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError("--model-selector must use MODEL=SELECTOR")
        model, selector = item.split("=", 1)
        result[model.strip()] = selector.strip()
    return result


def _model_selector(model: str, mapping: dict[str, str]) -> str:
    return mapping.get(model, model)


def _timeout_override_map(items: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        if "=" not in item:
            raise ValueError("--agent-timeout-override must use AOU=SECONDS, for example sqli=2400")
        aou, seconds = item.split("=", 1)
        key = aou.strip()
        if key not in AOUS:
            raise ValueError(f"unknown AOU in --agent-timeout-override: {key}")
        value = int(seconds.strip())
        if value < 60:
            raise ValueError("--agent-timeout-override seconds must be >= 60")
        result[key] = value
    return result


def _campaign_id(value: str | None) -> str:
    if value:
        return value
    return "juice_shop_cross_model_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _campaign_paths(campaign_id: str, output_root: Path) -> dict[str, Path]:
    root = output_root / campaign_id
    return {
        "root": root,
        "configs": root / "configs",
        "logs": root / "logs",
        "locks": root / "protocol_locks",
        "events": root / "events.jsonl",
        "manifest": root / "campaign_manifest.json",
    }


def _rel_or_abs(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path.resolve())


def _write_worker_compose(
    *,
    paths: dict[str, Path],
    campaign_id: str,
    worker_index: int,
    target_port: int,
) -> tuple[Path, str, str]:
    worker_slug = f"worker_{worker_index:02d}"
    project_slug = _slug(campaign_id)[:40] or "cross_model"
    compose_project_name = f"atobench-{project_slug}-w{worker_index:02d}"
    container_name = f"atobench-target-juice-shop-{project_slug}-w{worker_index:02d}"
    compose_file = paths["root"] / "worker_targets" / worker_slug / "docker-compose.yml"
    compose = {
        "name": compose_project_name,
        "services": {
            "juice-shop": {
                "image": JUICE_SHOP_IMAGE,
                "container_name": container_name,
                "ports": [f"{int(target_port)}:3000"],
                "restart": "unless-stopped",
            }
        },
    }
    _write_yaml(compose_file, compose)
    return compose_file, compose_project_name, container_name


def _write_worker_target_contract(
    *,
    paths: dict[str, Path],
    campaign_id: str,
    worker_index: int,
    target_port: int,
    compose_file: Path,
    container_name: str,
) -> Path:
    base_path = REPO_ROOT / "targets" / "juice-shop" / "protocol_v3" / "target_state_contract.yaml"
    contract = _load_yaml(base_path)
    target = dict(contract.get("target") or {})
    target["target_url"] = f"http://127.0.0.1:{int(target_port)}"
    target["compose_file"] = _rel_or_abs(compose_file)
    target["container_name"] = container_name
    target["image_digest"] = JUICE_SHOP_IMAGE_DIGEST
    contract["target"] = target
    contract["contract_id"] = f"{contract.get('contract_id', 'juice-shop-protocol-v3-state-v1')}-{_slug(campaign_id)}-w{worker_index:02d}"
    contract_path = paths["root"] / "worker_targets" / f"worker_{worker_index:02d}" / "target_state_contract.yaml"
    _write_yaml(contract_path, contract)
    return contract_path


def _worker_bindings(args: argparse.Namespace, campaign_id: str, paths: dict[str, Path]) -> list[WorkerBinding]:
    worker_count = max(1, min(int(args.parallel_workers), len(args.models)))
    if worker_count == 1:
        return [
            WorkerBinding(
                index=0,
                target_port=3000,
                clean_proxy_port=int(args.clean_proxy_port),
                deception_proxy_port=int(args.deception_proxy_port),
                compose_file=REPO_ROOT / "targets" / "juice-shop" / "docker-compose.yml",
                target_state_contract=REPO_ROOT / "targets" / "juice-shop" / "protocol_v3" / "target_state_contract.yaml",
                target_url="http://127.0.0.1:3000",
                health_url="http://127.0.0.1:3000/",
                compose_project_name="atobench-legacy-serial",
                container_name="atobench-target-juice-shop",
            )
        ]
    bindings: list[WorkerBinding] = []
    for index in range(worker_count):
        target_port = int(args.target_port_base) + index
        clean_proxy_port = int(args.proxy_port_base) + index * int(args.proxy_port_stride)
        deception_proxy_port = clean_proxy_port + 1
        compose_file, project_name, container_name = _write_worker_compose(
            paths=paths,
            campaign_id=campaign_id,
            worker_index=index,
            target_port=target_port,
        )
        target_state_contract = _write_worker_target_contract(
            paths=paths,
            campaign_id=campaign_id,
            worker_index=index,
            target_port=target_port,
            compose_file=compose_file,
            container_name=container_name,
        )
        bindings.append(
            WorkerBinding(
                index=index,
                target_port=target_port,
                clean_proxy_port=clean_proxy_port,
                deception_proxy_port=deception_proxy_port,
                compose_file=compose_file,
                target_state_contract=target_state_contract,
                target_url=f"http://127.0.0.1:{target_port}",
                health_url=f"http://127.0.0.1:{target_port}/",
                compose_project_name=project_name,
                container_name=container_name,
            )
        )
    return bindings


def _prepare_config(
    *,
    aou: AouConfig,
    model: str,
    selector: str,
    claude_effort: str | None,
    timeout_multiplier: float,
    timeout_overrides: dict[str, int],
    campaign_id: str,
    paths: dict[str, Path],
    worker: WorkerBinding,
    protocol_spec_path: Path | None = None,
) -> Path:
    data = _load_yaml(aou.base_config)
    _absolutize_config_paths(data, base_dir=aou.base_config.parent)
    model_slug = _slug(model)
    experiment_id = f"{campaign_id}_{aou.key}_{model_slug}"
    data["experiment_id"] = experiment_id

    target = dict(data.get("target") or {})
    target["target_url"] = worker.target_url
    target["health_url"] = worker.health_url
    target["compose_file"] = str(worker.compose_file)
    data["target"] = target

    runtime = dict(data.get("runtime") or {})
    runtime["deception_id"] = f"dec_{campaign_id}_{aou.key}_{model_slug}"
    runtime["clean_episode_id"] = f"ep_{campaign_id}_{aou.key}_{model_slug}_c0"
    runtime["deception_episode_id"] = f"ep_{campaign_id}_{aou.key}_{model_slug}_c1"
    runtime["clean_proxy_port"] = int(worker.clean_proxy_port)
    runtime["deception_proxy_port"] = int(worker.deception_proxy_port)
    runtime["log_dir"] = str(paths["logs"] / model_slug / aou.key)
    data["runtime"] = runtime

    agent = dict(data.get("agent") or {})
    agent["model"] = model
    agent["model_selector"] = selector
    if claude_effort:
        agent["claude_effort"] = claude_effort
    base_timeout = int(agent.get("timeout_s", 600))
    if aou.key in timeout_overrides:
        agent["timeout_s"] = int(timeout_overrides[aou.key])
    elif timeout_multiplier != 1.0:
        agent["timeout_s"] = int(round(base_timeout * timeout_multiplier))
    data["agent"] = agent

    protocol = dict(data.get("protocol_v3") or {})
    if protocol_spec_path is not None:
        protocol["execution_spec"] = str(protocol_spec_path)
    protocol["lock_output"] = str(paths["locks"] / f"{aou.key}_{model_slug}_lock.generated.yaml")
    protocol["target_state_contract"] = str(worker.target_state_contract)
    data["protocol_v3"] = protocol

    config_path = paths["configs"] / model_slug / f"{aou.key}.yaml"
    _write_yaml(config_path, data)
    return config_path


def _write_model_provenance(
    *,
    model: str,
    selector: str,
    claude_effort: str | None,
    paths: dict[str, Path],
) -> Path:
    route_sha = hashlib.sha256(f"claude-code-selector:{selector}".encode("utf-8")).hexdigest()
    execution_control = {
        "claude_effort": claude_effort,
        "temperature": {
            "status": "not_exposed_by_claude_code_cli",
            "value": None,
        },
        "sampling_controls": {
            "status": "not_exposed_by_claude_code_cli",
            "value": None,
        },
    }
    doc = {
        "schema_version": "atobench.model_provenance_attestation.v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "provider": "cc-switch routed provider",
        "transport": "anthropic_compatible_gateway",
        "model_identifier": model,
        "configured_model_matches_identifier": model == selector,
        "revision": {
            "status": "provider_build_not_exposed",
            "value": None,
            "reason": "The routed provider exposes a model label but no immutable deployment revision.",
        },
        "carrier": {
            "name": "Claude Code",
            "version": _command_text(["claude", "--version"]) or "unknown",
        },
        "configuration": {
            "route_sha256": route_sha,
            "configured_model": selector,
            "expected_model": model,
            "execution_control": execution_control,
            "auth_env_present": any(k for k in os.environ if "ANTHROPIC" in k or "OPENAI" in k or "API_KEY" in k),
        },
        "configuration_sha256": hashlib.sha256(
            json.dumps(
                {"model": model, "selector": selector, "claude_effort": claude_effort},
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "credential_policy": "No token, API key, or raw gateway route is serialized.",
    }
    path = paths["root"] / "model_provenance" / f"{_slug(model)}.yaml"
    _write_yaml(path, doc)
    return path


def _command_text(cmd: list[str]) -> str | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:
        return None


def _write_source_snapshot(paths: dict[str, Path], *, model_slug: str, protocol_id: str) -> tuple[Path, Path]:
    base_manifest = REPO_ROOT / "experiment" / "protocol_v3" / "frozen_inputs" / "protocol_v3_collection_source.manifest.json"
    base = json.loads(base_manifest.read_text(encoding="utf-8"))
    file_names = list((base.get("files") or {}).keys())
    extras = [
        "experiment/cross_model_artifact_audit.py",
        "experiment/cross_model_protocol.py",
        "experiment/CROSS_MODEL_EVIDENCE_AOU_RUNBOOK.md",
        "scripts/atobench-cross-model",
        "scripts/atobench-experiment",
    ]
    for rel in extras:
        if rel not in file_names:
            file_names.append(rel)

    source_dir = paths["root"] / "source_snapshots"
    source_dir.mkdir(parents=True, exist_ok=True)
    archive = source_dir / f"{model_slug}_source.tar.xz"
    manifest = source_dir / f"{model_slug}_source.manifest.json"

    files: dict[str, str] = {}
    with tarfile.open(archive, "w:xz") as tar:
        for rel in sorted(file_names):
            path = REPO_ROOT / rel
            if not path.is_file():
                continue
            files[rel] = _sha256_file(path)
            tar.add(path, arcname=rel)
    archive_sha = _sha256_file(archive)
    manifest_doc = {
        "schema_version": "atobench.protocol_source_snapshot.v1",
        "protocol_id": protocol_id,
        "contents_policy": "cross-model smoke source snapshot generated from current working tree",
        "source_archive_path": str(archive),
        "source_archive_sha256": archive_sha,
        "files": files,
        "files_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest(),
    }
    manifest.write_text(json.dumps(manifest_doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return archive, manifest


def _write_model_protocol_spec(
    *,
    model: str,
    selector: str,
    claude_effort: str | None,
    selected_aous: list[AouConfig],
    config_paths: dict[str, Path],
    campaign_id: str,
    paths: dict[str, Path],
    worker: WorkerBinding,
    rounds: int,
) -> Path:
    model_slug = _slug(model)
    base = _load_yaml(REPO_ROOT / "experiment" / "protocol_v3" / "PROTOCOL_V3_EXECUTION_SPEC.yaml")
    protocol_id = f"atobench-cross-model-smoke-{campaign_id}-{model_slug}"
    base["protocol_id"] = protocol_id
    base["status"] = "frozen_for_collection"
    base["created_date"] = datetime.now(timezone.utc).date().isoformat()
    base["scope"] = {
        "purpose": "cross-model smoke over already frozen Juice Shop evidence AOUs",
        "existing_protocol_v3_role": "source of AOU schedules and target-state reset semantics",
        "prohibited_during_collection": [
            "retune an AOU based on treatment success or failure",
            "alter prompt, matcher, dose, evaluator, or outcome definition",
            "include validation-only episodes in an effect estimate",
            "treat retries, endpoints, or transformed responses as independent samples",
        ],
    }
    target = dict(base.get("target") or {})
    target["compose_file"] = _rel_or_abs(worker.compose_file)
    target["target_url"] = worker.target_url
    target["target_state_contract"] = _rel_or_abs(worker.target_state_contract)
    target["parallel_worker"] = {
        "index": worker.index,
        "compose_project_name": worker.compose_project_name,
        "container_name": worker.container_name,
        "host_port": worker.target_port,
        "clean_proxy_port": worker.clean_proxy_port,
        "deception_proxy_port": worker.deception_proxy_port,
    }
    base["target"] = target

    provenance = _write_model_provenance(model=model, selector=selector, claude_effort=claude_effort, paths=paths)
    route_sha = yaml.safe_load(provenance.read_text(encoding="utf-8"))["configuration"]["route_sha256"]
    victim = dict(base.get("victim_agent") or {})
    victim["model_backend"] = model
    victim["provider"] = "cc-switch routed provider"
    victim["provider_route"] = f"anthropic_compatible_gateway; route_sha256={route_sha}"
    victim["immutable_revision"] = "provider_build_not_exposed"
    victim["provenance_attestation"] = str(provenance.relative_to(REPO_ROOT))
    victim["claude_effort"] = claude_effort
    victim["temperature"] = "not_exposed_by_claude_code_cli"
    base["victim_agent"] = victim

    archive, source_manifest = _write_source_snapshot(paths, model_slug=model_slug, protocol_id=protocol_id)
    base["source_snapshot"] = {
        "policy": "generated cross-model smoke source snapshot",
        "commit": _command_text(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]) or "unknown",
        "source_archive": str(archive.relative_to(REPO_ROOT)),
        "source_manifest": str(source_manifest.relative_to(REPO_ROOT)),
        "source_archive_sha256": _sha256_file(archive),
        "source_manifest_sha256": _sha256_file(source_manifest),
    }

    selected_unit_ids = {aou.unit_id for aou in selected_aous}
    aous = []
    for unit in base.get("aous") or []:
        if unit.get("unit_id") not in selected_unit_ids:
            continue
        aou_key = next(aou.key for aou in selected_aous if aou.unit_id == unit.get("unit_id"))
        unit = dict(unit)
        unit["config"] = str(config_paths[aou_key].relative_to(REPO_ROOT))
        aous.append(unit)
    base["aous"] = aous

    assignment = dict(base.get("assignment") or {})
    schedules = dict(assignment.get("schedules") or {})
    expanded_schedules: dict[str, list[dict[str, Any]]] = {}
    for aou in selected_aous:
        if aou.unit_id not in schedules:
            continue
        expanded_schedules[aou.unit_id] = _extend_schedule_for_rounds(
            list(schedules[aou.unit_id]),
            block_prefix=aou.block_prefix,
            rounds=rounds,
        )
    assignment["schedules"] = expanded_schedules
    assignment["planned_complete_blocks_per_aou"] = rounds
    assignment["cross_model_schedule_extension"] = {
        "status": "generated_and_frozen_before_collection",
        "requested_rounds": rounds,
        "policy": "preserve existing blocks verbatim; append deterministic alternating C0/C1 paired blocks",
    }
    base["assignment"] = assignment

    frozen = []
    for item in base.get("frozen_artifacts") or []:
        item = dict(item)
        rel = item.get("path")
        if rel and (REPO_ROOT / str(rel)).is_file():
            item["sha256"] = _sha256_file(REPO_ROOT / str(rel))
        frozen.append(item)
    for aou in selected_aous:
        rel = str(config_paths[aou.key].relative_to(REPO_ROOT))
        frozen.append({"artifact_id": f"cross_model_{aou.key}_{model_slug}_config", "path": rel, "sha256": _sha256_file(config_paths[aou.key])})
    frozen.append({"artifact_id": f"cross_model_{model_slug}_provenance", "path": str(provenance.relative_to(REPO_ROOT)), "sha256": _sha256_file(provenance)})
    frozen.append({"artifact_id": f"cross_model_{model_slug}_runner", "path": "experiment/cross_model_protocol.py", "sha256": _sha256_file(REPO_ROOT / "experiment" / "cross_model_protocol.py")})
    frozen.append({"artifact_id": f"cross_model_{model_slug}_worker_compose", "path": _rel_or_abs(worker.compose_file), "sha256": _sha256_file(worker.compose_file)})
    frozen.append({"artifact_id": f"cross_model_{model_slug}_target_state_contract", "path": _rel_or_abs(worker.target_state_contract), "sha256": _sha256_file(worker.target_state_contract)})
    base["frozen_artifacts"] = frozen

    spec_path = paths["root"] / "protocol_specs" / f"{model_slug}_protocol.yaml"
    _write_yaml(spec_path, base)
    return spec_path


def _slot_for(aou: AouConfig, block_index: int, position: int) -> str:
    return f"{aou.block_prefix}{block_index:02d}:{position}"


def _extend_schedule_for_rounds(schedule: list[dict[str, Any]], *, block_prefix: str, rounds: int) -> list[dict[str, Any]]:
    """Return a deterministic paired schedule of the requested length.

    Existing frozen blocks are preserved verbatim. Additional blocks alternate
    the first condition from the last existing block, producing a near-balanced
    fixed order for odd block counts.
    """

    if rounds <= len(schedule):
        return [dict(item) for item in schedule[:rounds]]
    if not schedule:
        raise ValueError(f"cannot extend empty schedule for {block_prefix}")
    expanded = [dict(item) for item in schedule]
    last_sequence = list(expanded[-1].get("sequence") or [])
    if set(last_sequence) != {"C0", "C1"} or len(last_sequence) != 2:
        raise ValueError(f"invalid paired schedule sequence in {block_prefix}: {last_sequence}")
    next_first = "C0" if last_sequence[0] == "C1" else "C1"
    for index in range(len(expanded) + 1, rounds + 1):
        sequence = [next_first, "C1" if next_first == "C0" else "C0"]
        expanded.append({"block_id": f"{block_prefix}{index:02d}", "sequence": sequence})
        next_first = "C0" if next_first == "C1" else "C1"
    return expanded


def _action_for_condition(condition: str) -> str:
    if condition == "C0":
        return "run-clean"
    if condition == "C1":
        return "run-deception"
    raise ValueError(f"unsupported condition: {condition}")


def _schedule_for(aou: AouConfig, config_path: Path) -> list[dict[str, Any]]:
    cfg = _load_yaml(config_path)
    protocol_path = Path(str(((cfg.get("protocol_v3") or {}).get("execution_spec"))))
    if not protocol_path.is_absolute():
        protocol_path = (config_path.parent / protocol_path).resolve()
    spec = _load_yaml(protocol_path)
    schedules = (((spec.get("assignment") or {}).get("schedules") or {}).get(aou.unit_id) or [])
    if not schedules:
        raise ValueError(f"no assignment schedule for {aou.unit_id} in {protocol_path}")
    return list(schedules)


def _experiment_work_dir(config_path: Path) -> Path:
    cfg = _load_yaml(config_path)
    target_dir = Path(str((cfg.get("target") or {})["target_dir"]))
    experiment_id = str(cfg["experiment_id"])
    return target_dir / "experiments" / experiment_id


def _slot_is_committed(config_path: Path, unit_id: str, slot: str) -> bool:
    manifest_path = _experiment_work_dir(config_path) / "manifest.json"
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    canonical = f"{unit_id}:{slot}"
    return canonical in set(manifest.get("protocol_v3_used_assignment_slots") or [])


def _event(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(record)
    record.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    with EVENT_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _run_command(cmd: list[str], *, cwd: Path) -> int:
    print("+ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd))
    return int(proc.returncode)


def _run_command_logged(cmd: list[str], *, cwd: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write("+ " + " ".join(cmd) + "\n")
        handle.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return int(proc.wait())


def _run_start_target(cmd: list[str], *, cwd: Path) -> int:
    print("+ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.stderr:
        print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n", file=sys.stderr)
    if proc.returncode != 0:
        return int(proc.returncode)
    try:
        start = proc.stdout.find("{")
        payload = json.loads(proc.stdout[start:]) if start >= 0 else {}
    except Exception:
        return 1
    if payload.get("returncode") != 0:
        return int(payload.get("returncode") or 1)
    if not ((payload.get("health") or {}).get("ok")):
        return 1
    return 0


def _run_compose_down(compose_file: Path, *, cwd: Path) -> int:
    cmd = ["docker", "compose", "-f", str(compose_file), "down", "--remove-orphans", "--volumes"]
    print("+ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.stderr:
        print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n", file=sys.stderr)
    return int(proc.returncode)


def run_campaign(args: argparse.Namespace) -> int:
    selected_aous = [AOUS[key] for key in args.aous]
    selectors = _selector_map(args.model_selector or [])
    timeout_overrides = _timeout_override_map(args.agent_timeout_override or [])
    campaign_id = _campaign_id(args.campaign_id)
    paths = _campaign_paths(campaign_id, Path(args.output_root).resolve())
    paths["root"].mkdir(parents=True, exist_ok=True)
    workers = _worker_bindings(args, campaign_id, paths)
    model_workers = {model: workers[index % len(workers)] for index, model in enumerate(args.models)}

    manifest: dict[str, Any] = {
        "schema_version": "atobench.cross_model_campaign.v1",
        "campaign_id": campaign_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": "juice-shop",
        "aous": [aou.key for aou in selected_aous],
        "models": args.models,
        "claude_effort": args.claude_effort,
        "agent_timeout_policy": {
            "base": "AOU config timeout_s",
            "multiplier": float(args.agent_timeout_multiplier),
            "overrides": timeout_overrides,
            "paper_note": (
                "Timeout changes are execution-control changes. Do not mix campaigns "
                "with different timeout policies without reporting the policy."
            ),
        },
        "temperature": "not_exposed_by_claude_code_cli",
        "sampling_controls": "not_exposed_by_claude_code_cli",
        "rounds": args.rounds,
        "dry_run": bool(args.dry_run),
        "conditions": ["C0", "C1"],
        "model_selector_policy": "MODEL=SELECTOR overrides; otherwise selector equals model",
        "failure_policy": {
            "continue_on_error": bool(args.continue_on_error),
            "unit": "episode_failure_records_invalid_slot_but_does_not_stop_later_pairs"
            if args.continue_on_error
            else "episode_failure_stops_the_affected_serial_queue",
            "timeout_note": (
                "SUBAGENT_TIMEOUT episodes are recorded as failed/invalid observations; "
                "the campaign continues unless --halt-on-error is used."
            ),
        },
        "parallelism": {
            "requested_workers": int(args.parallel_workers),
            "effective_workers": len(workers),
            "unit": "model_worker_with_pair_serial_execution",
            "pair_internal_order": "frozen Protocol-v3 sequence is preserved",
            "worker_resource_policy": "parallel workers use isolated Juice Shop compose targets, target-state contracts, proxy ports, and log roots",
        },
        "workers": [
            {
                "index": worker.index,
                "target_url": worker.target_url,
                "health_url": worker.health_url,
                "target_port": worker.target_port,
                "clean_proxy_port": worker.clean_proxy_port,
                "deception_proxy_port": worker.deception_proxy_port,
                "compose_file": str(worker.compose_file),
                "target_state_contract": str(worker.target_state_contract),
                "compose_project_name": worker.compose_project_name,
                "container_name": worker.container_name,
            }
            for worker in workers
        ],
        "campaign_root": str(paths["root"]),
        "configs": [],
        "events_jsonl": str(paths["events"]),
        "script": str(SCRIPT),
    }

    commands: list[dict[str, Any]] = []
    planned_pairs: list[dict[str, Any]] = []
    first_config_by_worker: dict[int, Path] = {}
    for model in args.models:
        selector = _model_selector(model, selectors)
        model_slug = _slug(model)
        worker = model_workers[model]
        protocol_spec = paths["root"] / "protocol_specs" / f"{_slug(model)}_protocol.yaml"
        model_config_paths: dict[str, Path] = {}
        for aou in selected_aous:
            config_path = _prepare_config(
                aou=aou,
                model=model,
                selector=selector,
                claude_effort=args.claude_effort,
                timeout_multiplier=float(args.agent_timeout_multiplier),
                timeout_overrides=timeout_overrides,
                campaign_id=campaign_id,
                paths=paths,
                worker=worker,
                protocol_spec_path=protocol_spec,
            )
            model_config_paths[aou.key] = config_path
        protocol_spec = _write_model_protocol_spec(
            model=model,
            selector=selector,
            claude_effort=args.claude_effort,
            selected_aous=selected_aous,
            config_paths=model_config_paths,
            campaign_id=campaign_id,
            paths=paths,
            worker=worker,
            rounds=int(args.rounds),
        )
        for aou in selected_aous:
            config_path = model_config_paths[aou.key]
            first_config_by_worker.setdefault(worker.index, config_path)
            manifest["configs"].append(
                {
                    "model": model,
                    "model_selector": selector,
                    "claude_effort": args.claude_effort,
                    "aou": aou.key,
                    "unit_id": aou.unit_id,
                    "preferred_protocol": aou.preferred_protocol,
                    "worker_index": worker.index,
                    "protocol_spec": str(protocol_spec),
                    "config": str(config_path),
                }
            )
            schedules = _schedule_for(aou, config_path)
            if args.rounds > len(schedules):
                raise ValueError(f"{aou.key} supports only {len(schedules)} rounds, requested {args.rounds}")
            for block in schedules[: args.rounds]:
                block_id = str(block["block_id"])
                sequence = list(block["sequence"])
                pair_id = f"{campaign_id}::{model_slug}::{aou.key}::{block_id}"
                pair_episodes: list[dict[str, Any]] = []
                pair_commands: list[dict[str, Any]] = []
                for position, condition in enumerate(sequence, start=1):
                    slot = f"{block_id}:{position}"
                    action = _action_for_condition(str(condition))
                    process_log = paths["root"] / "process_logs" / model_slug / aou.key / f"{block_id}_{position}_{str(condition).lower()}.log"
                    cmd = [
                        str(SCRIPT),
                        action,
                        "--config",
                        str(config_path),
                        "--protocol-assignment-slot",
                        slot,
                    ]
                    commands.append(
                        {
                            "pair_id": pair_id,
                            "model": model,
                            "model_selector": selector,
                            "claude_effort": args.claude_effort,
                            "aou": aou.key,
                            "unit_id": aou.unit_id,
                            "block_id": block_id,
                            "condition": condition,
                            "slot": slot,
                            "action": action,
                            "config": str(config_path),
                            "cmd": cmd,
                            "worker_index": worker.index,
                            "process_log": str(process_log),
                            "already_committed": _slot_is_committed(config_path, aou.unit_id, slot),
                        }
                    )
                    pair_commands.append(commands[-1])
                    pair_episodes.append({k: commands[-1][k] for k in ("condition", "slot", "action", "config", "process_log", "already_committed")})
                planned_pairs.append(
                    {
                        "pair_id": pair_id,
                        "model": model,
                        "model_selector": selector,
                        "aou": aou.key,
                        "unit_id": aou.unit_id,
                        "block_id": block_id,
                        "worker_index": worker.index,
                        "sequence": sequence,
                        "episodes": pair_episodes,
                        "commands": pair_commands,
                    }
                )

    manifest["planned_episodes"] = commands
    manifest["planned_pairs"] = planned_pairs
    paths["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[cross-model] campaign_id={campaign_id}")
    print(f"[cross-model] manifest={paths['manifest']}")
    print(f"[cross-model] planned_episodes={len(commands)}")
    if args.dry_run:
        for item in commands:
            print("DRY", " ".join(item["cmd"]))
        return 0

    if args.start_target:
        if not first_config_by_worker:
            raise RuntimeError("no config generated for start-target")
        start_failures = []
        for worker in workers:
            config = first_config_by_worker.get(worker.index)
            if config is None:
                continue
            rc = _run_start_target(
                [str(SCRIPT), "start-target", "--config", str(config), "--clean-target-first"],
                cwd=OUTER_ROOT,
            )
            event = {
                "status": "start_target_complete" if rc == 0 else "start_target_failed",
                "returncode": rc,
                "clean_target_first": True,
                "worker_index": worker.index,
                "config": str(config),
                "target_url": worker.target_url,
                "compose_file": str(worker.compose_file),
            }
            _event(paths["events"], event)
            if rc != 0:
                start_failures.append(event)
        if start_failures:
            return int(start_failures[0]["returncode"] or 1)

    failures: list[dict[str, Any]] = []
    if len(workers) == 1:
        for index, item in enumerate(commands, start=1):
            if item.get("already_committed") and args.skip_committed:
                print(
                    f"\n[cross-model] {index}/{len(commands)} SKIP committed "
                    f"model={item['model']} aou={item['aou']} slot={item['slot']}",
                    flush=True,
                )
                _event(paths["events"], {**item, "status": "skipped_committed", "index": index})
                continue
            print(
                f"\n[cross-model] {index}/{len(commands)} "
                f"model={item['model']} aou={item['aou']} slot={item['slot']} condition={item['condition']}",
                flush=True,
            )
            start = time.time()
            _event(paths["events"], {**item, "status": "started", "index": index})
            rc = _run_command(item["cmd"], cwd=OUTER_ROOT)
            duration = round(time.time() - start, 3)
            status = "complete" if rc == 0 else "failed"
            event = {**item, "status": status, "index": index, "returncode": rc, "duration_s": duration}
            _event(paths["events"], event)
            if rc != 0:
                failures.append(event)
                _event(
                    paths["events"],
                    {
                        **item,
                        "status": "failure_recorded",
                        "index": index,
                        "returncode": rc,
                        "failure_policy": "record_and_continue" if args.continue_on_error else "halt_on_error",
                    },
                )
                if not args.continue_on_error:
                    break
    else:
        worker_pairs: dict[int, list[dict[str, Any]]] = {worker.index: [] for worker in workers}
        for pair in planned_pairs:
            worker_pairs[int(pair["worker_index"])].append(pair)

        def run_worker_queue(worker_index: int, pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
            worker_failures: list[dict[str, Any]] = []
            total_pairs = len(pairs)
            for pair_index, pair in enumerate(pairs, start=1):
                print(
                    f"\n[cross-model] worker={worker_index} pair={pair_index}/{total_pairs} "
                    f"model={pair['model']} aou={pair['aou']} block={pair['block_id']}",
                    flush=True,
                )
                _event(paths["events"], {**pair, "status": "pair_started", "pair_index": pair_index})
                pair_failed = False
                for item in pair["commands"]:
                    if item.get("already_committed") and args.skip_committed:
                        print(
                            f"[cross-model] worker={worker_index} SKIP committed "
                            f"model={item['model']} aou={item['aou']} slot={item['slot']}",
                            flush=True,
                        )
                        _event(paths["events"], {**item, "status": "skipped_committed", "pair_index": pair_index})
                        continue
                    print(
                        f"[cross-model] worker={worker_index} START "
                        f"model={item['model']} aou={item['aou']} slot={item['slot']} condition={item['condition']} "
                        f"log={item['process_log']}",
                        flush=True,
                    )
                    start = time.time()
                    _event(paths["events"], {**item, "status": "started", "pair_index": pair_index})
                    rc = _run_command_logged(item["cmd"], cwd=OUTER_ROOT, log_path=Path(str(item["process_log"])))
                    duration = round(time.time() - start, 3)
                    status = "complete" if rc == 0 else "failed"
                    event = {**item, "status": status, "pair_index": pair_index, "returncode": rc, "duration_s": duration}
                    _event(paths["events"], event)
                    print(
                        f"[cross-model] worker={worker_index} {status.upper()} "
                        f"model={item['model']} aou={item['aou']} slot={item['slot']} rc={rc} duration_s={duration}",
                        flush=True,
                    )
                    if rc != 0:
                        worker_failures.append(event)
                        _event(
                            paths["events"],
                            {
                                **item,
                                "status": "failure_recorded",
                                "pair_index": pair_index,
                                "returncode": rc,
                                "failure_policy": "record_and_continue" if args.continue_on_error else "halt_on_error",
                            },
                        )
                        pair_failed = True
                        break
                _event(paths["events"], {**pair, "status": "pair_failed" if pair_failed else "pair_complete", "pair_index": pair_index})
                if pair_failed and not args.continue_on_error:
                    break
            return worker_failures

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(workers)) as executor:
            futures = [
                executor.submit(run_worker_queue, worker.index, worker_pairs.get(worker.index, []))
                for worker in workers
            ]
            for future in concurrent.futures.as_completed(futures):
                failures.extend(future.result())

    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["status"] = "complete" if not failures else "partial_failure"
    manifest["failures"] = failures
    if args.start_target and not args.keep_target_running:
        cleanup_events: list[dict[str, Any]] = []
        for worker in workers:
            if first_config_by_worker.get(worker.index) is None:
                continue
            rc = _run_compose_down(worker.compose_file, cwd=OUTER_ROOT)
            event = {
                "status": "target_cleanup_complete" if rc == 0 else "target_cleanup_failed",
                "returncode": rc,
                "worker_index": worker.index,
                "compose_file": str(worker.compose_file),
                "target_url": worker.target_url,
            }
            _event(paths["events"], event)
            cleanup_events.append(event)
        manifest["target_cleanup"] = {
            "enabled": True,
            "mode": "docker_compose_down_remove_orphans_volumes",
            "events": cleanup_events,
        }
        cleanup_failures = [event for event in cleanup_events if event["returncode"] != 0]
        if cleanup_failures:
            manifest["target_cleanup"]["status"] = "failed"
            if not failures:
                manifest["status"] = "cleanup_failure"
        else:
            manifest["target_cleanup"]["status"] = "complete"
    else:
        manifest["target_cleanup"] = {
            "enabled": False,
            "reason": "start_target_not_used" if not args.start_target else "keep_target_running",
        }
    paths["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[cross-model] status={manifest['status']} failures={len(failures)}")
    print(f"[cross-model] manifest={paths['manifest']}")
    return 0 if not failures else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run frozen Juice Shop evidence AOUs across Claude Code routed models.",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=["glm-5.2", "deepseek-v4-pro", "qwen3.7-max"],
        help="Provider model names expected in Claude Code route attestation.",
    )
    parser.add_argument(
        "--model-selector",
        action="append",
        default=[],
        metavar="MODEL=SELECTOR",
        help="Override the Claude Code --model selector for one expected model.",
    )
    parser.add_argument(
        "--claude-effort",
        default="high",
        choices=["low", "medium", "high", "xhigh", "max"],
        help=(
            "Claude Code --effort value frozen for every episode. Default high balances "
            "long-horizon pentest capability against runtime/cost."
        ),
    )
    parser.add_argument(
        "--agent-timeout-multiplier",
        type=float,
        default=1.0,
        help=(
            "Scale each AOU config's agent.timeout_s before writing generated configs. "
            "Default 1.0 preserves the frozen base configs."
        ),
    )
    parser.add_argument(
        "--agent-timeout-override",
        action="append",
        default=[],
        metavar="AOU=SECONDS",
        help=(
            "Override generated agent.timeout_s for one AOU, for example sqli=2400. "
            "May be repeated; takes precedence over --agent-timeout-multiplier."
        ),
    )
    parser.add_argument(
        "--aous",
        nargs="+",
        choices=sorted(AOUS),
        default=["sqli", "basket", "jwt"],
        help="AOU keys to run.",
    )
    parser.add_argument("--rounds", type=int, default=5, help="Number of paired blocks per AOU/model.")
    parser.add_argument("--campaign-id", default=None, help="Stable campaign id. Default is timestamped.")
    parser.add_argument("--output-root", default=str(DEFAULT_CAMPAIGN_ROOT), help="Where campaign manifests/configs/logs are written.")
    parser.add_argument("--clean-proxy-port", type=int, default=8100)
    parser.add_argument("--deception-proxy-port", type=int, default=8101)
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=1,
        help=(
            "Number of model-level workers. Values >1 run models concurrently; each worker gets "
            "an isolated Juice Shop compose target, target-state contract, proxy ports, and logs."
        ),
    )
    parser.add_argument(
        "--target-port-base",
        type=int,
        default=3300,
        help="First host port for generated parallel-worker Juice Shop targets. Ignored when --parallel-workers=1.",
    )
    parser.add_argument(
        "--proxy-port-base",
        type=int,
        default=8200,
        help="First clean proxy port for generated parallel workers. Ignored when --parallel-workers=1.",
    )
    parser.add_argument(
        "--proxy-port-stride",
        type=int,
        default=10,
        help="Port spacing between parallel workers. Worker N uses proxy-port-base + N*stride and +1.",
    )
    parser.add_argument("--start-target", action="store_true", help="Start Juice Shop before the first episode.")
    parser.add_argument(
        "--keep-target-running",
        action="store_true",
        help="Do not stop generated worker Juice Shop targets after the campaign exits.",
    )
    parser.add_argument("--skip-committed", action=argparse.BooleanOptionalAction, default=True, help="Skip protocol slots already committed in the model/AOU experiment manifest.")
    parser.add_argument(
        "--continue-on-error",
        dest="continue_on_error",
        action="store_true",
        default=True,
        help="Record failed episodes and continue with later pairs. Default for collection campaigns.",
    )
    parser.add_argument(
        "--halt-on-error",
        dest="continue_on_error",
        action="store_false",
        help="Stop the affected serial queue after the first failed episode. Useful for debugging.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Generate configs and print commands without running episodes.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.rounds < 1:
        raise SystemExit("--rounds must be >= 1")
    if args.parallel_workers < 1:
        raise SystemExit("--parallel-workers must be >= 1")
    if args.proxy_port_stride < 2:
        raise SystemExit("--proxy-port-stride must be >= 2")
    if args.agent_timeout_multiplier <= 0:
        raise SystemExit("--agent-timeout-multiplier must be > 0")
    return run_campaign(args)


if __name__ == "__main__":
    raise SystemExit(main())
