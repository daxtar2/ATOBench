"""Frozen benchmark-suite construction helpers.

The suite layer separates benchmark construction artifacts from confirmatory
episode execution. It intentionally treats an existing deception workspace as a
source payload and records hashes around it, instead of regenerating deception
content during evaluation.
"""

from __future__ import annotations

import json
import shutil
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from atobench.schema.loader import validate_deception_plan, validate_runtime_program

REPO_ROOT = Path(__file__).resolve().parents[2]
IGNORED_FREEZE_NAMES = {".DS_Store"}


def freeze_suite_from_workspace(
    *,
    target_dir: str | Path,
    target_name: str,
    suite_id: str,
    source_deception_dir: str | Path,
    task_id: str,
    target_url: str,
    target_profile_path: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Create a frozen suite from an existing compiled deception workspace."""

    target = Path(target_dir)
    source = Path(source_deception_dir)
    suite_root = target / "benchmark_suites" / suite_id
    if suite_root.exists() and not force:
        raise FileExistsError(f"benchmark suite already exists: {suite_root}")
    if suite_root.exists() and force:
        shutil.rmtree(suite_root)

    required = {
        "deception_plan": source / "deception_plan.yaml",
        "runtime_program": source / "runtime_program.yaml",
        "target_profile": source / "target_profile.yaml",
    }
    missing = [str(path) for path in required.values() if not path.exists()]
    if missing:
        raise FileNotFoundError(f"source deception workspace is missing required files: {missing}")

    profile_src = _select_target_profile(
        override=Path(target_profile_path) if target_profile_path else None,
        target=target,
        source_profile=required["target_profile"],
    )

    plan = yaml.safe_load(required["deception_plan"].read_text(encoding="utf-8")) or {}
    runtime_program = yaml.safe_load(required["runtime_program"].read_text(encoding="utf-8")) or {}
    validate_deception_plan(plan)
    validate_runtime_program(runtime_program)

    _mkdirs(
        suite_root / "target",
        suite_root / "construction",
        suite_root / "programs" / "c0_identity",
        suite_root / "programs" / "c1_core",
        suite_root / "schemas",
    )

    # Target-level construction inputs.
    shutil.copyfile(profile_src, suite_root / "target" / "target_profile.yaml")
    inventory_src = target / "scaffold_work" / "endpoint_inventory.jsonl"
    if inventory_src.exists():
        shutil.copyfile(inventory_src, suite_root / "target" / "surface_inventory.jsonl")
    else:
        (suite_root / "target" / "surface_inventory.jsonl").write_text("", encoding="utf-8")

    gt_src = _default_ground_truth_path(target_name)
    if gt_src.exists():
        shutil.copyfile(gt_src, suite_root / "target" / "ground_truth_cards.yaml")

    target_card = suite_root / "target" / "target_card.md"
    target_card.write_text(
        "\n".join(
            [
                f"# {target_name} Target Card",
                "",
                f"- suite_id: {suite_id}",
                f"- target_url: {target_url}",
                f"- task_id: {task_id}",
                "- construction_note: migrated from an existing compiled deception workspace.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # Program-compatible workspace layout. evaluate-pair infers plan/runtime
    # from <workspace>/runs/<episode_id>, so keep these files beside runs/.
    program_dir = suite_root / "programs" / "c1_core"
    shutil.copyfile(required["deception_plan"], program_dir / "deception_plan.yaml")
    shutil.copyfile(required["runtime_program"], program_dir / "runtime_program.yaml")
    shutil.copyfile(profile_src, program_dir / "target_profile.yaml")
    for name in ["deception_config.yaml", "compile_report.json", "validation_report.md"]:
        src = source / name
        if src.exists():
            shutil.copyfile(src, program_dir / name)
    (program_dir / "runs").mkdir(exist_ok=True)

    c0_program = _identity_runtime_program(
        suite_id=suite_id,
        task_id=task_id,
        target_url=target_url,
    )
    _write_yaml(suite_root / "programs" / "c0_identity" / "runtime_program.yaml", c0_program)
    (suite_root / "programs" / "c0_identity" / "runs").mkdir(exist_ok=True)

    injections = list((plan.get("plan") or {}).get("injections") or [])
    runtime_rules = list(runtime_program.get("rules") or [])
    case_ids = []
    candidate_index = []
    review_rows = []
    for idx, injection in enumerate(injections, start=1):
        case_id = str(injection.get("id") or f"case_{idx:03d}")
        case_ids.append(case_id)
        rule_ids = [
            str(rule.get("rule_id"))
            for rule in runtime_rules
            if rule.get("injection_id") == case_id
        ]
        case_dir = suite_root / "cases" / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        case_doc = {
            "schema_version": "atobench.deception_case.v0",
            "case_id": case_id,
            "source": {
                "kind": "migrated_deception_plan_injection",
                "source_deception_dir": str(source),
                "plan_id": (plan.get("plan") or {}).get("plan_id"),
            },
            "status": "accepted_for_frozen_program",
            "paper_grade_review": "pending",
            "primitive": injection.get("primitive"),
            "attack_face": injection.get("attack_face"),
            "coupling": injection.get("coupling"),
            "target_dims": injection.get("target_dims") or [],
            "side_dims": injection.get("side_dims") or [],
            "trajectory_anchor": injection.get("trajectory_anchor"),
            "bindings": injection.get("bindings") or [],
            "runtime_rule_ids": rule_ids,
            "rationale": injection.get("rationale"),
        }
        _write_yaml(case_dir / "case.yaml", case_doc)
        (case_dir / "validation_report.json").write_text(
            json.dumps(
                {
                    "schema_version": "atobench.case_validation.v0",
                    "case_id": case_id,
                    "machine_validation": "source_plan_and_runtime_program_validated",
                    "scientific_review": "pending",
                    "notes": [
                        "Migrated from an existing deception workspace.",
                        "Manual false-hypothesis and evidence-graph review is still required before paper-grade use.",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        candidate_index.append(
            {
                "case_id": case_id,
                "candidate_id": f"{suite_id}:{case_id}",
                "status": "accepted_for_c1_core",
                "primitive": injection.get("primitive"),
                "attack_face": injection.get("attack_face"),
                "coupling": injection.get("coupling"),
                "bindings": len(injection.get("bindings") or []),
            }
        )
        review_rows.append(
            {
                "case_id": case_id,
                "decision": "accept_for_migration_freeze",
                "reviewer": "operator",
                "scientific_review": "pending",
                "reason": "Existing compiled workspace was frozen to support confirmatory execution without runtime regeneration.",
            }
        )

    _write_jsonl(suite_root / "construction" / "candidate_index.jsonl", candidate_index)
    _write_jsonl(suite_root / "construction" / "review_decisions.jsonl", review_rows)
    (suite_root / "construction" / "rejection_summary.json").write_text(
        json.dumps(
            {
                "schema_version": "atobench.rejection_summary.v0",
                "generated_candidates": len(candidate_index),
                "accepted_candidates": len(candidate_index),
                "rejected_candidates": 0,
                "note": "Migration freeze only; future offline construction must count rejected candidates.",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    _write_yaml(
        program_dir / "case_selection.yaml",
        {
            "schema_version": "atobench.case_selection.v0",
            "condition": "C1",
            "program_id": "c1_core",
            "selection_policy": "migrated_static_program",
            "case_ids": case_ids,
            "notes": [
                "Frozen before confirmatory episodes.",
                "No victim-agent clean trajectory may be used during evaluation.",
            ],
        },
    )
    _write_json(
        program_dir / "program_manifest.json",
        _program_manifest(
            suite_id=suite_id,
            program_id="c1_core",
            condition="C1",
            runtime_path=program_dir / "runtime_program.yaml",
            case_ids=case_ids,
            source=str(source),
        ),
    )
    _write_json(
        suite_root / "programs" / "c0_identity" / "program_manifest.json",
        _program_manifest(
            suite_id=suite_id,
            program_id="c0_identity",
            condition="C0",
            runtime_path=suite_root / "programs" / "c0_identity" / "runtime_program.yaml",
            case_ids=[],
            source="identity_program",
        ),
    )

    schema_manifest = _schema_manifest()
    _write_json(suite_root / "schemas" / "schema_version_manifest.json", schema_manifest)

    suite_manifest = {
        "schema_version": "atobench.benchmark_suite.v0",
        "suite_id": suite_id,
        "target_name": target_name,
        "target_url": target_url,
        "task_id": task_id,
        "created_at": _now_iso(),
        "construction_state": "migration_freeze_from_existing_workspace",
        "standard_conditions": {
            "C0": "programs/c0_identity/runtime_program.yaml",
            "C1": "programs/c1_core/runtime_program.yaml",
        },
        "source_deception_workspace": str(source),
        "source_target_profile": str(required["target_profile"]),
        "frozen_target_profile": str(profile_src),
        "case_count": len(case_ids),
        "runtime_rule_count": len(runtime_rules),
        "paper_grade_gate": {
            "machine_frozen": True,
            "manual_case_review_complete": False,
            "matched_d1_d2_d3_families_complete": False,
        },
    }
    _write_yaml(suite_root / "suite_manifest.yaml", suite_manifest)

    freeze_manifest = {
        "schema_version": "atobench.freeze_manifest.v0",
        "suite_id": suite_id,
        "frozen_at": _now_iso(),
        "suite_root": str(suite_root),
        "source_deception_workspace": str(source),
        "files": _file_hashes(suite_root),
    }
    _write_json(suite_root / "freeze_manifest.json", freeze_manifest)

    return {
        "suite_id": suite_id,
        "suite_dir": str(suite_root),
        "suite_manifest": str(suite_root / "suite_manifest.yaml"),
        "freeze_manifest": str(suite_root / "freeze_manifest.json"),
        "c0_runtime_program": str(suite_root / "programs" / "c0_identity" / "runtime_program.yaml"),
        "c1_runtime_program": str(program_dir / "runtime_program.yaml"),
        "case_count": len(case_ids),
        "runtime_rule_count": len(runtime_rules),
    }


def validate_frozen_suite(suite_dir: str | Path) -> dict[str, Any]:
    """Validate suite hashes and RuntimeProgram payloads."""

    suite = Path(suite_dir)
    freeze_path = suite / "freeze_manifest.json"
    manifest_path = suite / "suite_manifest.yaml"
    if not freeze_path.exists():
        raise FileNotFoundError(f"freeze manifest not found: {freeze_path}")
    if not manifest_path.exists():
        raise FileNotFoundError(f"suite manifest not found: {manifest_path}")

    freeze_manifest = json.loads(freeze_path.read_text(encoding="utf-8"))
    expected = {
        rel: digest
        for rel, digest in (freeze_manifest.get("files") or {}).items()
        if Path(rel).name not in IGNORED_FREEZE_NAMES
    }
    actual = _file_hashes(suite)
    mismatches = []
    for rel, expected_hash in expected.items():
        if rel == "freeze_manifest.json":
            continue
        if actual.get(rel) != expected_hash:
            mismatches.append(rel)
    missing = sorted(set(expected) - set(actual) - {"freeze_manifest.json"})
    extra = sorted(set(actual) - set(expected) - {"freeze_manifest.json"})

    suite_manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    standard_conditions = suite_manifest.get("standard_conditions") or {}
    runtime_paths = []
    if standard_conditions:
        runtime_paths = [suite / str(rel) for rel in standard_conditions.values()]
    else:
        runtime_paths = [
            suite / "programs" / "c0_identity" / "runtime_program.yaml",
            suite / "programs" / "c1_core" / "runtime_program.yaml",
        ]
    runtime_errors = []
    for runtime_path in runtime_paths:
        if runtime_path.exists():
            try:
                validate_runtime_program(yaml.safe_load(runtime_path.read_text(encoding="utf-8")) or {})
            except Exception as exc:
                runtime_errors.append({"path": str(runtime_path), "error": str(exc)})
        else:
            runtime_errors.append({"path": str(runtime_path), "error": "missing runtime program"})

    status = "valid" if not mismatches and not missing and not runtime_errors else "invalid"
    return {
        "schema_version": "atobench.suite_validation.v0",
        "suite_dir": str(suite),
        "status": status,
        "is_valid": status == "valid",
        "mismatches": mismatches,
        "missing": missing,
        "extra_unfrozen_files": extra,
        "runtime_programs_checked": [str(path) for path in runtime_paths if path.exists()],
        "runtime_errors": runtime_errors,
    }


def freeze_offline_materialized_suite(*, suite_dir: str | Path, force: bool = False) -> dict[str, Any]:
    """Freeze an offline-constructed suite after replay validation."""

    suite = Path(suite_dir)
    manifest_path = suite / "suite_manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"suite manifest not found: {manifest_path}")
    freeze_path = suite / "freeze_manifest.json"
    if freeze_path.exists() and not force:
        raise FileExistsError(f"freeze manifest already exists: {freeze_path}")

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    gate = dict(manifest.get("paper_grade_gate") or {})
    required_gate_flags = {
        "manual_case_review_complete": True,
        "compatible_programs_materialized": True,
        "replay_validation_complete": True,
        "d_ladder_materialized": True,
        "d_ladder_replay_validation_complete": True,
        "d_ladder_primary_groups_complete": True,
    }
    gate_errors = [
        f"{name} is not {expected!r}"
        for name, expected in required_gate_flags.items()
        if gate.get(name) is not expected
    ]
    if gate.get("primary_d_ladder_groups") != 2:
        gate_errors.append("primary_d_ladder_groups is not 2")
    if gate_errors:
        raise ValueError(f"offline suite is not ready to freeze: {gate_errors}")

    required_reports = {
        "c1_replay": suite / "construction" / "replay_validation_report.yaml",
        "d_ladder_replay": suite / "construction" / "d_ladder_ftp_nullbyte_replay_validation_report.yaml",
    }
    report_errors = []
    for label, path in required_reports.items():
        if not path.exists():
            report_errors.append(f"{label} report missing: {path}")
            continue
        report = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if report.get("is_valid") is not True and report.get("status") != "pass":
            report_errors.append(f"{label} report is not valid/pass")
    if report_errors:
        raise ValueError(f"offline suite replay reports are not freeze-ready: {report_errors}")

    standard_conditions = manifest.get("standard_conditions") or {}
    if not {"C0", "C1"}.issubset(set(standard_conditions)):
        raise ValueError("standard_conditions must include C0 and C1")
    if not any(str(name).startswith("D_LADDER_") for name in standard_conditions):
        raise ValueError("standard_conditions must include D-ladder programs")

    program_hashes = {}
    for condition, rel in sorted(standard_conditions.items()):
        runtime_path = suite / str(rel)
        if not runtime_path.exists():
            raise FileNotFoundError(f"standard condition runtime missing for {condition}: {runtime_path}")
        runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8")) or {}
        validate_runtime_program(runtime)
        program_hashes[str(condition)] = {
            "runtime_program": str(rel),
            "program_id": runtime.get("program_id"),
            "sha256": _sha256_file(runtime_path),
            "rule_count": len(runtime.get("rules") or []),
        }

    frozen_at = _now_iso()
    _update_offline_freeze_todo(
        suite=suite,
        frozen_at=frozen_at,
        freeze_report="construction/offline_suite_freeze_report.yaml",
        freeze_manifest="freeze_manifest.json",
    )
    manifest["construction_state"] = "frozen_offline_suite_v2"
    manifest["frozen_at"] = frozen_at
    manifest["freeze_manifest"] = "freeze_manifest.json"
    gate["frozen"] = True
    gate["machine_frozen"] = True
    manifest["paper_grade_gate"] = gate
    _write_yaml(manifest_path, manifest)

    freeze_report = {
        "schema_version": "atobench.offline_suite_freeze_report.v1",
        "suite_id": manifest.get("suite_id") or suite.name,
        "frozen_at": frozen_at,
        "status": "frozen",
        "program_hashes": program_hashes,
        "required_reports": {label: str(path.relative_to(suite)) for label, path in required_reports.items()},
        "notes": [
            "Offline C0/C1 and primary D-ladder programs were frozen after replay validation.",
            "Campaign outputs must be written outside benchmark_suites/ to preserve this hash lock.",
        ],
    }
    _write_yaml(suite / "construction" / "offline_suite_freeze_report.yaml", freeze_report)

    freeze_manifest = {
        "schema_version": "atobench.freeze_manifest.v1",
        "suite_id": manifest.get("suite_id") or suite.name,
        "frozen_at": frozen_at,
        "suite_root": str(suite),
        "construction_state": manifest["construction_state"],
        "standard_conditions": standard_conditions,
        "program_hashes": program_hashes,
        "ignored_names": sorted(IGNORED_FREEZE_NAMES),
        "files": _file_hashes(suite),
    }
    _write_json(freeze_path, freeze_manifest)

    validation = validate_frozen_suite(suite)
    return {
        "schema_version": "atobench.offline_suite_freeze_result.v1",
        "suite_id": manifest.get("suite_id") or suite.name,
        "suite_dir": str(suite),
        "suite_manifest": str(manifest_path),
        "freeze_manifest": str(freeze_path),
        "freeze_report": str(suite / "construction" / "offline_suite_freeze_report.yaml"),
        "status": "frozen" if validation["is_valid"] else "frozen_but_validation_failed",
        "validation": validation,
    }


def _update_offline_freeze_todo(
    *,
    suite: Path,
    frozen_at: str,
    freeze_report: str,
    freeze_manifest: str,
) -> None:
    todo_path = suite / "programs" / "program_materialization_todo.yaml"
    if not todo_path.exists():
        return
    todo = yaml.safe_load(todo_path.read_text(encoding="utf-8")) or {}
    todo["status"] = "frozen_offline_suite_v2"
    todo["frozen_at"] = frozen_at
    materialization = dict(todo.get("materialization") or {})
    materialization["status"] = "frozen_offline_suite_v2"
    materialization["next_required_step"] = "run confirmatory campaign outside benchmark_suites"
    materialization["freeze_report"] = freeze_report
    materialization["freeze_manifest"] = freeze_manifest
    todo["materialization"] = materialization
    d_ladder = dict(todo.get("d_ladder_next_step") or {})
    d_ladder["status"] = "frozen_offline_suite_v2"
    d_ladder["reason"] = (
        "Both primary D-ladder groups are replay-validated and included in the "
        "offline suite hash lock. Campaign outputs must stay outside benchmark_suites."
    )
    todo["d_ladder_next_step"] = d_ladder
    _write_yaml(todo_path, todo)


def _select_target_profile(
    *,
    override: Path | None,
    target: Path,
    source_profile: Path,
) -> Path:
    candidates = []
    if override:
        candidates.append(override)
    candidates.append(target / "scaffold_work" / "target_profile.yaml")
    candidates.append(source_profile)

    existing = [path for path in candidates if path.exists()]
    if not existing:
        raise FileNotFoundError("no target_profile.yaml candidate exists")

    for path in existing:
        try:
            profile = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if profile.get("known_real_vulns"):
            return path
    return existing[0]


def _identity_runtime_program(*, suite_id: str, task_id: str, target_url: str) -> dict[str, Any]:
    program = {
        "schema_version": "0.2.0",
        "program_id": "rp_" + hashlib.sha256(f"{suite_id}:c0_identity".encode("utf-8")).hexdigest()[:12],
        "episode_id": "ep_frozen_c0_identity",
        "task_id": task_id,
        "baseline": "B0",
        "target": {"base_url": target_url},
        "source": {"kind": "clean_runtime", "plan_id": None, "suite_id": suite_id},
        "max_primitives_per_response": 1,
        "rules": [],
    }
    validate_runtime_program(program)
    return program


def _program_manifest(
    *,
    suite_id: str,
    program_id: str,
    condition: str,
    runtime_path: Path,
    case_ids: list[str],
    source: str,
) -> dict[str, Any]:
    return {
        "schema_version": "atobench.program_manifest.v0",
        "suite_id": suite_id,
        "program_id": program_id,
        "condition": condition,
        "source": source,
        "runtime_program": str(runtime_path),
        "runtime_program_sha256": _sha256_file(runtime_path),
        "case_ids": case_ids,
        "case_count": len(case_ids),
        "frozen_at": _now_iso(),
    }


def _schema_manifest() -> dict[str, Any]:
    names = ["runtime_program.json", "deception_plan.json", "normalized_findings.json"]
    rows = {}
    for name in names:
        path = REPO_ROOT / "atobench" / "schema" / name
        if path.exists():
            rows[name] = {"path": str(path), "sha256": _sha256_file(path)}
    return {
        "schema_version": "atobench.schema_version_manifest.v0",
        "schemas": rows,
    }


def _default_ground_truth_path(target_name: str) -> Path:
    stem = target_name.replace("-", "_")
    return REPO_ROOT / "atobench" / "experiment" / "ground_truth" / f"{stem}_ground_truth_cards.yaml"


def _file_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in IGNORED_FREEZE_NAMES:
            continue
        rel = path.relative_to(root).as_posix()
        if rel == "freeze_manifest.json":
            continue
        hashes[rel] = _sha256_file(path)
    return hashes


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_yaml(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _mkdirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
