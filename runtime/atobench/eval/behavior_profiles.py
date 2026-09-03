"""Validate behavioral-profile sidecars and audit case-level trace coverage."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from atobench.eval.action_trace import read_jsonl
from atobench.eval.behavior_predicates import validate_confirmatory_profile

PROOF_INTERFERENCE_MODES = {"informational", "evidential", "interdictive", "navigational", "stateful"}
BEHAVIORAL_INTENTS = {
    "verification_suppression",
    "lure_redirect",
    "budget_drain",
    "credential_use",
    "scope_abandonment",
    "evidence_fabrication",
    "report_suppression",
    "recovery_test",
}


def _load_yaml(path: str | Path) -> dict[str, Any]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected YAML object: {path}")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_behavior_profiles(
    profile_path: str | Path,
    runtime_program_path: str | Path,
    mode: str = "exploratory",
) -> dict[str, Any]:
    profile_path = Path(profile_path)
    runtime_program_path = Path(runtime_program_path)
    sidecar = _load_yaml(profile_path)
    runtime = _load_yaml(runtime_program_path)
    errors: list[str] = []
    warnings: list[str] = []
    validation_mode = mode
    if validation_mode not in {"exploratory", "confirmatory"}:
        raise ValueError(f"unsupported behavioral-profile validation mode: {mode}")

    profiles = sidecar.get("profiles") or []
    rules = runtime.get("rules") or []
    if sidecar.get("schema_version") != "atobench.behavioral_profiles.v1":
        errors.append("unsupported or missing behavioral-profile schema_version")
    if not isinstance(profiles, list):
        errors.append("profiles must be a list")
        profiles = []

    runtime_by_case: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        if isinstance(rule, dict) and rule.get("injection_id"):
            runtime_by_case.setdefault(str(rule.get("injection_id")), []).append(rule)
    profile_ids = [str(profile.get("case_id")) for profile in profiles if isinstance(profile, dict)]
    if len(profile_ids) != len(set(profile_ids)):
        errors.append("behavioral profiles contain duplicate case_id values")
    missing = sorted(set(runtime_by_case) - set(profile_ids))
    extra = sorted(set(profile_ids) - set(runtime_by_case))
    if missing:
        errors.append(f"missing runtime cases: {', '.join(missing)}")
    if extra:
        errors.append(f"profiles not present in runtime program: {', '.join(extra)}")

    declared_hash = str(sidecar.get("runtime_program_sha256") or "")
    actual_hash = _sha256(runtime_program_path)
    if declared_hash != actual_hash:
        errors.append(f"runtime program hash mismatch: declared={declared_hash} actual={actual_hash}")
    if sidecar.get("program_id") != runtime.get("program_id"):
        errors.append("profile program_id does not match runtime program")
    if sidecar.get("profile_provenance") == "post_hoc_from_existing_c0_c1_results":
        if sidecar.get("eligible_for_current_confirmatory_claims") is not False:
            errors.append("post-hoc profiles must be ineligible for current confirmatory claims")
        warnings.append("profiles are post-hoc and exploratory for the existing campaign")
    if validation_mode == "confirmatory":
        if sidecar.get("profile_provenance") != "preregistered_before_collection":
            errors.append("confirmatory profiles must use preregistered_before_collection provenance")
        if sidecar.get("profile_status") != "frozen_for_confirmatory":
            errors.append("confirmatory profiles must have profile_status frozen_for_confirmatory")
        if sidecar.get("eligible_for_future_confirmatory_runs") is not True:
            errors.append("confirmatory profiles must explicitly enable future confirmatory runs")

    for profile in profiles:
        if not isinstance(profile, dict):
            errors.append("profile entry must be an object")
            continue
        case_id = str(profile.get("case_id") or "<missing>")
        candidate_rules = runtime_by_case.get(case_id) or []
        interference_mode = profile.get("proof_interference_mode")
        intent = profile.get("behavioral_intent")
        if interference_mode not in PROOF_INTERFERENCE_MODES:
            errors.append(f"{case_id}: invalid proof_interference_mode {interference_mode!r}")
        if intent not in BEHAVIORAL_INTENTS:
            errors.append(f"{case_id}: invalid behavioral_intent {intent!r}")
        if not profile.get("false_hypothesis"):
            errors.append(f"{case_id}: false_hypothesis is required")
        if not profile.get("contact_predicates"):
            errors.append(f"{case_id}: at least one contact predicate is required")
        if not profile.get("causal_identifiability"):
            errors.append(f"{case_id}: causal_identifiability is required")
        selector = profile.get("selector") or {}
        matching_rule = next(
            (
                rule
                for rule in candidate_rules
                if (rule.get("match") or {}).get("path_regex") == selector.get("path_regex")
                and sorted((rule.get("match") or {}).get("methods") or []) == sorted(selector.get("methods") or [])
            ),
            candidate_rules[0] if candidate_rules else {},
        )
        if profile.get("family_id") != matching_rule.get("family"):
            errors.append(
                f"{case_id}: family mismatch profile={profile.get('family_id')} runtime={matching_rule.get('family')}"
            )
        rule_match = matching_rule.get("match") or {}
        if selector.get("path_regex") != rule_match.get("path_regex"):
            errors.append(f"{case_id}: selector path_regex does not match frozen runtime rule")
        if sorted(selector.get("methods") or []) != sorted(rule_match.get("methods") or []):
            errors.append(f"{case_id}: selector methods do not match frozen runtime rule")
        if not profile.get("action_adoption_predicates"):
            warnings.append(f"{case_id}: no action-adoption predicate; fails paper-grade observability")
        if not profile.get("recovery_predicates"):
            warnings.append(f"{case_id}: no recovery predicate")
        if validation_mode == "confirmatory":
            errors.extend(validate_confirmatory_profile(profile))

    return {
        "schema_version": "atobench.behavioral_profile_validation.v1",
        "profile_path": str(profile_path),
        "runtime_program_path": str(runtime_program_path),
        "profile_count": len(profiles),
        "runtime_case_count": len(runtime_by_case),
        "runtime_program_sha256": actual_hash,
        "is_valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "validation_mode": validation_mode,
    }


def audit_behavior_profile_coverage(
    profile_path: str | Path,
    runtime_program_path: str | Path,
    pair_manifest_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    validation = validate_behavior_profiles(profile_path, runtime_program_path)
    if not validation["is_valid"]:
        raise ValueError("invalid behavioral profiles: " + "; ".join(validation["errors"]))
    sidecar = _load_yaml(profile_path)
    manifest = json.loads(Path(pair_manifest_path).read_text(encoding="utf-8"))
    pair_specs = manifest.get("pairs") if isinstance(manifest, dict) else manifest
    if not isinstance(pair_specs, list):
        raise ValueError("pair manifest must contain a pairs list")

    episodes: list[tuple[str, list[dict[str, Any]]]] = []
    for spec in pair_specs:
        run_dir = Path(spec["deception_run_dir"])
        actions = list(read_jsonl(run_dir / "agent_action_trace.jsonl"))
        episode_id = str(actions[0].get("episode_id") if actions else run_dir.name)
        episodes.append((episode_id, actions))

    coverage: list[dict[str, Any]] = []
    for profile in sidecar["profiles"]:
        case_id = str(profile["case_id"])
        contacted_episode_ids: list[str] = []
        transform_count = 0
        solo_transform_count = 0
        co_applied: set[str] = set()
        selector = profile.get("selector") or {}
        path_pattern = re.compile(str(selector.get("path_regex") or r"$^"))
        methods = set(selector.get("methods") or [])
        endpoint_reached_episode_ids: list[str] = []

        for episode_id, actions in episodes:
            selected = [
                action for action in actions
                if action.get("method") in methods and path_pattern.search(str(action.get("path") or ""))
            ]
            if selected:
                endpoint_reached_episode_ids.append(episode_id)
            contacts = [action for action in actions if case_id in (action.get("case_ids_applied") or [])]
            if contacts:
                contacted_episode_ids.append(episode_id)
            for action in contacts:
                transform_count += 1
                applied = set(action.get("case_ids_applied") or [])
                others = applied - {case_id}
                co_applied.update(str(item) for item in others)
                if not others:
                    solo_transform_count += 1

        empirical_identifiability = "not_contacted"
        if transform_count:
            empirical_identifiability = "has_solo_exposure" if solo_transform_count else "joint_exposure_only"
        coverage.append(
            {
                "case_id": case_id,
                "measurement_status": profile.get("measurement_status"),
                "endpoint_reached_episodes": len(endpoint_reached_episode_ids),
                "contacted_episodes": len(contacted_episode_ids),
                "contacted_episode_ids": contacted_episode_ids,
                "transformed_response_count": transform_count,
                "solo_transformed_response_count": solo_transform_count,
                "co_applied_case_ids": sorted(co_applied),
                "empirical_identifiability": empirical_identifiability,
            }
        )

    contacted_solo_units = [
        item["case_id"] for item in coverage if item["empirical_identifiability"] == "has_solo_exposure"
    ]
    contacted_joint_groups: dict[str, list[str]] = {}
    profiles_by_id = {str(profile["case_id"]): profile for profile in sidecar["profiles"]}
    for item in coverage:
        if item["empirical_identifiability"] != "joint_exposure_only":
            continue
        declared = profiles_by_id[item["case_id"]].get("causal_identifiability") or {}
        group = str(declared.get("joint_intervention_group") or "undeclared_joint_group")
        contacted_joint_groups.setdefault(group, []).append(item["case_id"])
    effective_units = [
        {"unit_id": case_id, "kind": "individual_case", "case_ids": [case_id]}
        for case_id in contacted_solo_units
    ] + [
        {"unit_id": group, "kind": "joint_intervention_group", "case_ids": sorted(case_ids)}
        for group, case_ids in sorted(contacted_joint_groups.items())
    ]

    result = {
        "schema_version": "atobench.behavioral_profile_coverage.v1",
        "analysis_status": "retrospective_exploratory",
        "episode_count": len(episodes),
        "validation": validation,
        "coverage": coverage,
        "aggregate": {
            "configured_case_count": len(coverage),
            "contacted_case_count": sum(item["contacted_episodes"] > 0 for item in coverage),
            "individually_attributable_contacted_case_count": len(contacted_solo_units),
            "joint_only_contacted_case_count": sum(
                item["empirical_identifiability"] == "joint_exposure_only" for item in coverage
            ),
            "joint_intervention_group_count": len(contacted_joint_groups),
            "uncontacted_case_count": sum(
                item["empirical_identifiability"] == "not_contacted" for item in coverage
            ),
            "effective_contacted_intervention_unit_count": len(effective_units),
            "effective_contacted_intervention_units": effective_units,
        },
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path = output_path.with_suffix(".md")
    result["artifacts"] = {"json": str(output_path), "markdown": str(markdown_path)}
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(_render_coverage_markdown(result), encoding="utf-8")
    return result


def _render_coverage_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# C1 Behavioral Profile Coverage Audit",
        "",
        "Status: retrospective exploratory; profiles were authored after the existing runs",
        "",
        "| Case | Endpoint reached | Contacted | Transforms | Solo | Empirical identifiability |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for item in result["coverage"]:
        lines.append(
            f"| `{item['case_id']}` | {item['endpoint_reached_episodes']}/{result['episode_count']} | "
            f"{item['contacted_episodes']}/{result['episode_count']} | {item['transformed_response_count']} | "
            f"{item['solo_transformed_response_count']} | {item['empirical_identifiability']} |"
        )
    lines.extend(
        [
            "",
            "## Effective Intervention Units",
            "",
            f"The {result['aggregate']['contacted_case_count']} contacted case IDs collapse to "
            f"{result['aggregate']['effective_contacted_intervention_unit_count']} effective contacted intervention units: "
            f"{result['aggregate']['individually_attributable_contacted_case_count']} individually attributable cases and "
            f"{result['aggregate']['joint_intervention_group_count']} joint groups.",
            "",
        ]
    )
    for unit in result["aggregate"]["effective_contacted_intervention_units"]:
        lines.append(
            f"- `{unit['unit_id']}` ({unit['kind']}): " + ", ".join(f"`{case_id}`" for case_id in unit["case_ids"])
        )
    lines.extend(
        [
            "",
            "## Gate Interpretation",
            "",
            "- `has_solo_exposure` means the trace contains responses attributable to that case without another C1 case on the same response.",
            "- `joint_exposure_only` means individual causal effects cannot be estimated from this campaign.",
            "- `not_contacted` excludes the case from action-effect and robustness denominators.",
            "- These post-hoc profiles can organize exploratory analysis only. They must be reviewed and frozen before a new confirmatory campaign.",
            "",
        ]
    )
    return "\n".join(lines)
