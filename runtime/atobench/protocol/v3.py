"""Protocol-v3 preregistration validation and execution-lock generation."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from atobench.experiment.config import load_experiment_config
from atobench.schema.loader import validate_runtime_program

SCHEMA_VERSION = "atobench.protocol_v3.v1"
ATOBENCH_ROOT = Path(__file__).resolve().parents[1]


def validate_protocol_v3(
    manifest_path: str | Path,
    *,
    write_lock: str | Path | None = None,
    repository_root: str | Path | None = None,
) -> dict[str, Any]:
    """Validate a protocol-v3 preregistration and optionally write its lock report.

    Structural errors describe malformed or inconsistent protocol artifacts.
    Blockers describe deliberately unresolved collection gates. A draft can be
    structurally valid while remaining ineligible for confirmatory collection.
    """

    manifest_path = Path(manifest_path).expanduser().resolve()
    root = Path(repository_root).expanduser().resolve() if repository_root else ATOBENCH_ROOT
    raw = _load_mapping(manifest_path)

    errors: list[dict[str, str]] = []
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if raw.get("schema_version") != SCHEMA_VERSION:
        _issue(errors, "schema_version", f"expected {SCHEMA_VERSION}")
    if raw.get("status") != "frozen_for_collection":
        _issue(blockers, "protocol_not_frozen", "status must be frozen_for_collection before confirmatory episodes")

    victim = _mapping(raw.get("victim_agent"), "victim_agent", errors)
    expected_model = str(victim.get("model_backend") or "").strip()
    if not expected_model:
        _issue(errors, "model_backend_missing", "victim_agent.model_backend is required")
    for field in ("provider", "immutable_revision", "provider_route"):
        if not _resolved(victim.get(field)):
            _issue(blockers, f"model_{field}_unresolved", f"victim_agent.{field} must be resolved before collection")
    model_provenance = _validate_model_provenance(victim, root=root, errors=errors, blockers=blockers)

    carrier = _mapping(victim.get("carrier"), "victim_agent.carrier", errors)
    required_claude_version = str(carrier.get("required_version") or "").strip()
    observed_claude_version = _command_version("claude", ["--version"])
    if not observed_claude_version:
        _issue(blockers, "claude_code_unavailable", "claude executable/version could not be observed")
    elif required_claude_version and required_claude_version not in observed_claude_version:
        _issue(
            blockers,
            "claude_code_version_mismatch",
            f"required {required_claude_version!r}, observed {observed_claude_version!r}",
        )

    artifact_hashes: dict[str, dict[str, Any]] = {}
    for item in raw.get("frozen_artifacts") or []:
        if not isinstance(item, dict):
            _issue(errors, "artifact_entry_invalid", "each frozen_artifacts entry must be a mapping")
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        rel_path = str(item.get("path") or "").strip()
        if not artifact_id or not rel_path:
            _issue(errors, "artifact_identity_missing", "frozen artifact requires artifact_id and path")
            continue
        path = _resolve(root, rel_path)
        if not path.is_file():
            _issue(errors, "artifact_missing", f"{artifact_id}: {rel_path}")
            continue
        digest = _sha256_file(path)
        declared = str(item.get("sha256") or "").strip() or None
        if raw.get("status") == "frozen_for_collection" and not declared:
            _issue(errors, "artifact_hash_unfrozen", f"{artifact_id}: sha256 is required in a frozen protocol")
        artifact_hashes[artifact_id] = {
            "path": rel_path,
            "sha256": digest,
            "declared_sha256": declared,
            "matches_declared": declared in {None, digest},
        }
        if declared and declared != digest:
            _issue(errors, "artifact_hash_mismatch", f"{artifact_id}: declared {declared}, observed {digest}")

    units = raw.get("aous") or []
    if not isinstance(units, list) or not units:
        _issue(errors, "aous_missing", "at least one AOU is required")
        units = []
    unit_ids: set[str] = set()
    validated_units: list[dict[str, Any]] = []
    for unit in units:
        if not isinstance(unit, dict):
            _issue(errors, "aou_entry_invalid", "each AOU entry must be a mapping")
            continue
        validated = _validate_aou(unit, root=root, expected_model=expected_model, errors=errors)
        unit_id = validated["unit_id"]
        if unit_id in unit_ids:
            _issue(errors, "duplicate_unit_id", unit_id)
        unit_ids.add(unit_id)
        validated_units.append(validated)

    assignment = _mapping(raw.get("assignment"), "assignment", errors)
    assignment_audit = _validate_assignment(assignment, unit_ids=unit_ids, errors=errors)

    target = _mapping(raw.get("target"), "target", errors)
    target_audit = _validate_target(target, root=root, errors=errors, blockers=blockers)

    reset = _mapping(raw.get("episode_isolation"), "episode_isolation", errors)
    if reset.get("implementation_status") != "implemented_and_replay_validated":
        _issue(
            blockers,
            "episode_reset_not_validated",
            "episode_isolation.implementation_status must be implemented_and_replay_validated",
        )
    if not _resolved(reset.get("state_fingerprint_artifact")):
        _issue(blockers, "state_fingerprint_unresolved", "post-reset state fingerprint artifact is not frozen")

    source = _repository_state(root)
    source_snapshot = _validate_source_snapshot(raw.get("source_snapshot"), root=root, errors=errors, blockers=blockers)
    if source["dirty"] and not source_snapshot["verified"]:
        _issue(blockers, "repository_not_clean", "freeze confirmatory collection from a clean immutable commit or verified source archive")
    elif source["dirty"]:
        _issue(warnings, "repository_dirty_snapshot_verified", "working tree is dirty; the verified source archive is the collection source")

    validation_policy = _mapping(raw.get("validation_policy"), "validation_policy", errors)
    if validation_policy.get("episodes_enter_effect_estimate") is not False:
        _issue(errors, "validation_episode_leakage", "validation-only episodes must be excluded from effect estimates")

    result = {
        "schema_version": "atobench.protocol_v3_lock.v1",
        "protocol_id": raw.get("protocol_id"),
        "protocol_manifest": str(manifest_path),
        "protocol_manifest_sha256": _sha256_file(manifest_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "structurally_valid": not errors,
        "collection_ready": not errors and not blockers,
        "effect_claim_status": "not_evaluated_by_preflight",
        "errors": errors,
        "collection_blockers": blockers,
        "warnings": warnings,
        "environment_observation": {
            "claude_code_version": observed_claude_version,
            "repository": source,
        },
        "model_provenance": model_provenance,
        "source_snapshot": source_snapshot,
        "target_audit": target_audit,
        "assignment_audit": assignment_audit,
        "aou_audit": validated_units,
        "artifact_hashes": artifact_hashes,
    }
    if write_lock:
        output = Path(write_lock).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(yaml.safe_dump(result, sort_keys=False, allow_unicode=False), encoding="utf-8")
        result["lock_path"] = str(output)
    return result


def freeze_protocol_v3(
    manifest_path: str | Path,
    *,
    repository_root: str | Path | None = None,
    write_lock: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically freeze a collection-ready draft and declare all artifact hashes.

    Freezing intentionally refuses to resolve any open gate. It only converts a
    draft whose *sole* blocker is ``protocol_not_frozen`` into an immutable
    collection manifest. If final validation fails, the original manifest is
    restored rather than leaving a half-frozen protocol on disk.
    """

    manifest = Path(manifest_path).expanduser().resolve()
    root = Path(repository_root).expanduser().resolve() if repository_root else ATOBENCH_ROOT
    original_text = manifest.read_text(encoding="utf-8")
    raw = _load_mapping(manifest)
    if raw.get("status") != "draft_precollection":
        raise ValueError("only a draft_precollection manifest may be frozen")
    preflight = validate_protocol_v3(manifest, repository_root=root)
    blocker_codes = {item["code"] for item in preflight["collection_blockers"]}
    if preflight["errors"] or blocker_codes != {"protocol_not_frozen"}:
        raise ValueError(f"freeze refused; unresolved gates: {sorted(blocker_codes)}")
    for item in raw.get("frozen_artifacts") or []:
        if not isinstance(item, dict):
            raise ValueError("freeze refused; frozen_artifacts contains a non-mapping entry")
        path = _resolve(root, str(item.get("path") or ""))
        if not path.is_file():
            raise ValueError(f"freeze refused; artifact missing: {item.get('path')}")
        item["sha256"] = _sha256_file(path)
    raw["status"] = "frozen_for_collection"
    raw["frozen_at"] = datetime.now(timezone.utc).isoformat()
    manifest.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=False), encoding="utf-8")
    result = validate_protocol_v3(manifest, write_lock=write_lock, repository_root=root)
    if not result["collection_ready"]:
        manifest.write_text(original_text, encoding="utf-8")
        raise RuntimeError(f"freeze validation failed: {result['errors']} {result['collection_blockers']}")
    return result


def _validate_model_provenance(
    victim: dict[str, Any], *, root: Path, errors: list[dict[str, str]], blockers: list[dict[str, str]]
) -> dict[str, Any]:
    rel_path = str(victim.get("provenance_attestation") or "").strip()
    if not _resolved(rel_path):
        _issue(blockers, "model_provenance_unresolved", "victim_agent.provenance_attestation is required before collection")
        return {"verified": False, "path": None}
    path = _resolve(root, rel_path)
    if not path.is_file():
        _issue(blockers, "model_provenance_missing", rel_path)
        return {"verified": False, "path": rel_path}
    try:
        raw = _load_mapping(path)
    except Exception as exc:
        _issue(errors, "model_provenance_invalid", str(exc))
        return {"verified": False, "path": rel_path}
    if raw.get("schema_version") != "atobench.model_provenance_attestation.v1":
        _issue(errors, "model_provenance_schema", rel_path)
    fields = {
        "provider": victim.get("provider"),
        "model_identifier": victim.get("model_backend"),
    }
    for field, expected in fields.items():
        if str(raw.get(field) or "").casefold() != str(expected or "").casefold():
            _issue(errors, "model_provenance_mismatch", f"{field}: protocol and attestation differ")
    route_hash = ((raw.get("configuration") or {}).get("route_sha256"))
    expected_route = str(victim.get("provider_route") or "")
    if not route_hash or route_hash not in expected_route:
        _issue(errors, "model_route_provenance_mismatch", "provider_route must include attested route SHA-256")
    revision = raw.get("revision") if isinstance(raw.get("revision"), dict) else {}
    status = str(revision.get("status") or "")
    if status not in {"provider_build_not_exposed", "immutable_revision_observed"}:
        _issue(errors, "model_revision_status_invalid", status or "missing")
    protocol_revision = str(victim.get("immutable_revision") or "")
    if status == "provider_build_not_exposed":
        if protocol_revision != "provider_build_not_exposed":
            _issue(errors, "model_revision_provenance_mismatch", "protocol must disclose provider_build_not_exposed")
    elif protocol_revision != str(revision.get("value") or ""):
        _issue(errors, "model_revision_provenance_mismatch", "protocol and attested immutable revision differ")
    return {
        "verified": not errors,
        "path": rel_path,
        "sha256": _sha256_file(path),
        "revision_status": status,
        "configuration_sha256": raw.get("configuration_sha256"),
    }


def _validate_source_snapshot(
    value: Any, *, root: Path, errors: list[dict[str, str]], blockers: list[dict[str, str]]
) -> dict[str, Any]:
    source = _mapping(value, "source_snapshot", errors)
    archive_rel = str(source.get("source_archive") or "").strip()
    manifest_rel = str(source.get("source_manifest") or "").strip()
    declared_archive = str(source.get("source_archive_sha256") or "").strip()
    declared_manifest = str(source.get("source_manifest_sha256") or "").strip()
    audit: dict[str, Any] = {"verified": False, "source_archive": archive_rel or None, "source_manifest": manifest_rel or None}
    if not all(_resolved(item) for item in (archive_rel, manifest_rel, declared_archive, declared_manifest)):
        _issue(blockers, "source_archive_unresolved", "source archive, manifest, and hashes are required before collection")
        return audit
    archive = _resolve(root, archive_rel)
    manifest = _resolve(root, manifest_rel)
    if not archive.is_file() or not manifest.is_file():
        _issue(blockers, "source_archive_missing", f"archive={archive_rel}, manifest={manifest_rel}")
        return audit
    archive_hash = _sha256_file(archive)
    manifest_hash = _sha256_file(manifest)
    audit.update({"archive_sha256": archive_hash, "manifest_sha256": manifest_hash})
    if archive_hash != declared_archive or manifest_hash != declared_manifest:
        _issue(errors, "source_archive_hash_mismatch", "declared source archive or manifest hash differs")
        return audit
    try:
        raw_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _issue(errors, "source_snapshot_manifest_invalid", str(exc))
        return audit
    if raw_manifest.get("schema_version") != "atobench.protocol_source_snapshot.v1":
        _issue(errors, "source_snapshot_manifest_schema", manifest_rel)
        return audit
    if raw_manifest.get("source_archive_sha256") != archive_hash:
        _issue(errors, "source_snapshot_archive_binding", "snapshot manifest does not bind the archive hash")
        return audit
    files = raw_manifest.get("files")
    if not isinstance(files, dict) or not files:
        _issue(errors, "source_snapshot_files_missing", "snapshot manifest must list source file hashes")
        return audit
    drifted = []
    for rel_path, expected_hash in files.items():
        path = _resolve(root, str(rel_path))
        if not path.is_file() or _sha256_file(path) != expected_hash:
            drifted.append(str(rel_path))
    if drifted:
        _issue(errors, "source_snapshot_worktree_drift", ", ".join(drifted[:8]))
        audit["drifted_files"] = drifted
        return audit
    audit["verified"] = True
    audit["file_count"] = len(files)
    return audit


def _validate_aou(
    unit: dict[str, Any],
    *,
    root: Path,
    expected_model: str,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    unit_id = str(unit.get("unit_id") or "").strip()
    for field in ("unit_id", "intervention_axis", "aom", "estimand", "config"):
        if not _resolved(unit.get(field)):
            _issue(errors, "aou_field_missing", f"{unit_id or '<unknown>'}.{field}")

    config_path = _resolve(root, str(unit.get("config") or ""))
    config_valid = False
    runtime_hashes: dict[str, str] = {}
    config_model = None
    if config_path.is_file():
        try:
            cfg = load_experiment_config(config_path)
            config_model = cfg.agent.model
            if expected_model and str(config_model or "").casefold() != expected_model.casefold():
                _issue(errors, "aou_model_mismatch", f"{unit_id}: config model {config_model!r} != {expected_model!r}")
            expected_runtime = {
                "c0": cfg.benchmark_suite.c0_runtime_program,
                "c1": cfg.benchmark_suite.c1_runtime_program,
            }
            for condition in ("c0", "c1"):
                declared_path = _resolve(root, str(unit.get(f"{condition}_runtime_program") or ""))
                if expected_runtime[condition] and declared_path != expected_runtime[condition]:
                    _issue(errors, "runtime_config_mismatch", f"{unit_id}.{condition}: config and protocol paths differ")
                if not declared_path.is_file():
                    _issue(errors, "runtime_missing", f"{unit_id}.{condition}: {declared_path}")
                    continue
                program = yaml.safe_load(declared_path.read_text(encoding="utf-8")) or {}
                validate_runtime_program(program)
                runtime_hashes[condition] = _sha256_file(declared_path)
            config_valid = True
        except Exception as exc:
            _issue(errors, "aou_config_invalid", f"{unit_id}: {exc}")
    else:
        _issue(errors, "aou_config_missing", f"{unit_id}: {config_path}")

    evaluators = unit.get("evaluators") or []
    if not evaluators:
        _issue(errors, "aou_evaluator_missing", unit_id)
    evaluator_hashes: dict[str, str] = {}
    for rel in evaluators:
        path = _resolve(root, str(rel))
        if not path.is_file():
            _issue(errors, "aou_evaluator_missing", f"{unit_id}: {rel}")
        else:
            evaluator_hashes[str(rel)] = _sha256_file(path)

    dose = unit.get("treatment_dose")
    if dose is None:
        _issue(errors, "aou_dose_missing", unit_id)
    for field in ("eligibility_predicate", "contact_predicate", "capability_preservation", "recovery_or_resistance"):
        if not _resolved(unit.get(field)):
            _issue(errors, "aou_predicate_missing", f"{unit_id}.{field}")

    return {
        "unit_id": unit_id,
        "config": str(unit.get("config") or ""),
        "config_sha256": _sha256_file(config_path) if config_path.is_file() else None,
        "config_model": config_model,
        "config_valid": config_valid,
        "runtime_program_sha256": runtime_hashes,
        "evaluator_sha256": evaluator_hashes,
        "treatment_dose": dose,
    }


def _validate_assignment(
    assignment: dict[str, Any],
    *,
    unit_ids: set[str],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    if assignment.get("mode") != "paired_block_randomization":
        _issue(errors, "assignment_mode_invalid", "protocol-v3 requires paired_block_randomization")
    schedules = assignment.get("schedules") or {}
    if not isinstance(schedules, dict):
        _issue(errors, "assignment_schedules_invalid", "assignment.schedules must be a mapping")
        schedules = {}
    audit: dict[str, Any] = {}
    for unit_id in unit_ids:
        blocks = schedules.get(unit_id)
        if not isinstance(blocks, list) or not blocks:
            _issue(errors, "assignment_schedule_missing", unit_id)
            continue
        c0 = 0
        c1 = 0
        block_errors = 0
        for block in blocks:
            sequence = block.get("sequence") if isinstance(block, dict) else None
            if not isinstance(sequence, list) or sorted(sequence) != ["C0", "C1"]:
                _issue(errors, "assignment_block_invalid", f"{unit_id}: each block must contain one C0 and one C1")
                block_errors += 1
                continue
            c0 += sequence.count("C0")
            c1 += sequence.count("C1")
        audit[unit_id] = {"blocks": len(blocks), "c0": c0, "c1": c1, "block_errors": block_errors}
    extra = set(schedules) - unit_ids
    if extra:
        _issue(errors, "assignment_unknown_unit", ", ".join(sorted(extra)))
    return audit


def _validate_target(
    target: dict[str, Any],
    *,
    root: Path,
    errors: list[dict[str, str]],
    blockers: list[dict[str, str]],
) -> dict[str, Any]:
    compose_path = _resolve(root, str(target.get("compose_file") or ""))
    expected_digest = str(target.get("image_digest") or "").strip()
    observed_digest = None
    if not compose_path.is_file():
        _issue(errors, "target_compose_missing", str(compose_path))
    else:
        match = re.search(r"image:\s*[^\s@]+@(sha256:[0-9a-f]{64})", compose_path.read_text(encoding="utf-8"))
        observed_digest = match.group(1) if match else None
        if not observed_digest:
            _issue(errors, "target_digest_unpinned", str(compose_path))
        elif observed_digest != expected_digest:
            _issue(errors, "target_digest_mismatch", f"expected {expected_digest}, observed {observed_digest}")
    if target.get("mutable_state_reset_required") is not True:
        _issue(blockers, "target_reset_not_required", "protocol-v3 must require mutable-state reset per episode")
    return {
        "compose_file": str(target.get("compose_file") or ""),
        "compose_sha256": _sha256_file(compose_path) if compose_path.is_file() else None,
        "expected_image_digest": expected_digest,
        "observed_image_digest": observed_digest,
    }


def _repository_state(root: Path) -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
        ).stdout
        return {"root": str(root), "commit": commit, "dirty": bool(status.strip()), "dirty_entry_count": len(status.splitlines())}
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"root": str(root), "commit": None, "dirty": True, "error": str(exc)}


def _command_version(command: str, args: list[str]) -> str | None:
    executable = shutil.which(command)
    if not executable:
        return None
    try:
        return subprocess.run(
            [executable, *args], capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _load_mapping(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"protocol manifest root must be a mapping: {path}")
    return raw


def _mapping(value: Any, name: str, errors: list[dict[str, str]]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    _issue(errors, "mapping_required", name)
    return {}


def _resolved(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip().lower()
    return bool(text) and text not in {"pending", "unknown", "null", "todo", "tbd"}


def _resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _issue(items: list[dict[str, str]], code: str, detail: str) -> None:
    items.append({"code": code, "detail": detail})


def dump_protocol_result(result: dict[str, Any]) -> str:
    """Stable JSON rendering for CLI callers."""

    return json.dumps(result, ensure_ascii=False, indent=2)
