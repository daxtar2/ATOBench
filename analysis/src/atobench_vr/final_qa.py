from __future__ import annotations

import argparse
import csv
import json
import math
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    read_json,
    require_real_data_authorization,
    sha256_file,
    stage_manifest,
    utc_now,
    write_json,
)
from .redaction import residual_sensitive_kinds
from .result_facts import run as run_result_fact_export


ALLOWED_FAMILIES = {
    "verification_state_distribution",
    "capability_conditioned_retention",
    "paired_state_availability",
    "verification_state_transition_matrix",
    "diagnostic_score",
    "stop_descriptor_transition",
}
ALLOWED_ROLES = {"primary", "secondary_descriptive", "diagnostic"}
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\bgh[ps]_[A-Za-z0-9]{20,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
}
PAPER_LEAKAGE_PATTERNS = {
    "paper_workspace": re.compile(r"AuthorKit27"),
    "paper_expected_result_reference": re.compile(
        r"(?i)(?:expected|target|desired)[-_ ](?:paper|manuscript)[-_ ]result"
    ),
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise GateError(f"QA source table is missing: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _same(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
    return left == right


def _csv_value(value: str | None) -> Any:
    if value is None or value == "":
        return None
    try:
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        return float(value)
    except ValueError:
        return value


def _matching_rows(
    statistics_root: Path, source: dict[str, Any]
) -> list[dict[str, str]]:
    filename = source.get("file")
    selector = source.get("row_selector")
    if not isinstance(filename, str) or not isinstance(selector, dict):
        raise GateError("fact source is missing file or row_selector")
    path = statistics_root / filename
    rows = _read_csv(path)
    matches = [
        row
        for row in rows
        if all(str(row.get(key) or "") == str(value or "") for key, value in selector.items())
    ]
    if not matches:
        raise GateError(f"fact selector resolves no rows: {filename} {selector}")
    return matches


def _assert_value(
    violations: list[dict[str, Any]],
    fact: dict[str, Any],
    path: str,
    actual: Any,
    expected: Any,
) -> None:
    if not _same(actual, expected):
        violations.append(
            {
                "fact_id": fact.get("fact_id"),
                "path": path,
                "actual": actual,
                "expected": expected,
                "rule": "source_value_mismatch",
            }
        )


def _validate_fact_against_source(
    fact: dict[str, Any],
    rows: list[dict[str, str]],
    violations: list[dict[str, Any]],
) -> None:
    family = fact["family"]
    if family == "verification_state_distribution":
        row = rows[0]
        _assert_value(
            violations,
            fact,
            "population.denominator_n",
            fact["population"]["denominator_n"],
            _csv_value(row["pair_n"]),
        )
        for state, value in fact["result"]["counts"].items():
            _assert_value(
                violations,
                fact,
                f"result.counts.{state}",
                value,
                _csv_value(row[f"{state}_n"]),
            )
    elif family == "capability_conditioned_retention":
        row = rows[0]
        mappings = {
            "population.target_n": (fact["population"]["target_n"], "capability_target_n"),
            "population.outcome_observed_n": (
                fact["population"]["outcome_observed_n"],
                "outcome_observed_n",
            ),
            "population.outcome_missing_n": (
                fact["population"]["outcome_missing_n"],
                "outcome_missing_n",
            ),
            "estimate.numerator_n": (
                fact["estimate"]["numerator_n"],
                "retained_n",
            ),
            "estimate.denominator_n": (
                fact["estimate"]["denominator_n"],
                "outcome_observed_n",
            ),
            "estimate.value": (
                fact["estimate"]["value"],
                "observed_grounded_retention",
            ),
        }
        for path, (actual, column) in mappings.items():
            _assert_value(
                violations, fact, path, actual, _csv_value(row[column])
            )
        interval_mappings = {
            "uncertainty.pair_bootstrap_ci": (
                "observed_grounded_retention_bootstrap_ci_low",
                "observed_grounded_retention_bootstrap_ci_high",
            ),
            "uncertainty.exact_binomial_ci": (
                "observed_grounded_retention_exact_ci_low",
                "observed_grounded_retention_exact_ci_high",
            ),
            "uncertainty.missing_outcome_bounds": (
                "retention_worst_case_bound",
                "retention_best_case_bound",
            ),
        }
        for path, columns in interval_mappings.items():
            key = path.split(".")[-1]
            expected = [_csv_value(row[column]) for column in columns]
            actual = fact["uncertainty"][key]
            for index in range(2):
                _assert_value(
                    violations,
                    fact,
                    f"{path}[{index}]",
                    actual[index],
                    expected[index],
                )
    elif family == "paired_state_availability":
        row = rows[0]
        _assert_value(
            violations,
            fact,
            "population.denominator_n",
            fact["population"]["denominator_n"],
            _csv_value(row["pair_n"]),
        )
        _assert_value(
            violations,
            fact,
            "estimate.value",
            fact["estimate"]["value"],
            _csv_value(row["paired_availability_difference_c1_minus_c0"]),
        )
        pattern_columns = {
            "c0_observed_c1_observed": "c0_observed_c1_observed_n",
            "c0_observed_c1_unavailable": "c0_observed_c1_unavailable_n",
            "c0_unavailable_c1_observed": "c0_unavailable_c1_observed_n",
            "c0_unavailable_c1_unavailable": "c0_unavailable_c1_unavailable_n",
        }
        for key, column in pattern_columns.items():
            _assert_value(
                violations,
                fact,
                f"estimate.pattern_counts.{key}",
                fact["estimate"]["pattern_counts"][key],
                _csv_value(row[column]),
            )
    elif family == "diagnostic_score":
        row = rows[0]
        for key, column in {
            "frozen_pair_n": "pair_n",
            "c0_numeric_n": "c0_numeric_n",
            "c1_numeric_n": "c1_numeric_n",
            "paired_numeric_n": "paired_numeric_n",
            "excluded_from_paired_numeric_n": "excluded_from_paired_numeric_n",
        }.items():
            _assert_value(
                violations,
                fact,
                f"population.{key}",
                fact["population"][key],
                _csv_value(row[column]),
            )
        _assert_value(
            violations,
            fact,
            "estimate.value",
            fact["estimate"]["value"],
            _csv_value(row["paired_mean_delta"]),
        )
        _assert_value(
            violations,
            fact,
            "numeric_interpretation",
            fact["numeric_interpretation"],
            row["numeric_interpretation"],
        )
    elif family in {
        "verification_state_transition_matrix",
        "stop_descriptor_transition",
    }:
        if family == "verification_state_transition_matrix":
            expected = sorted(
                (
                    row["c0_state"],
                    row["c1_state"],
                    int(row["transition_n"]),
                )
                for row in rows
                if int(row["transition_n"]) > 0
            )
            actual = sorted(
                (cell["c0_state"], cell["c1_state"], cell["n"])
                for cell in fact["result"]["nonzero_transition_cells"]
            )
            denominator = int(rows[0]["complete_pair_n"])
        else:
            expected = sorted(
                (
                    row["c0_value"],
                    row["c1_value"],
                    int(row["transition_n"]),
                )
                for row in rows
                if int(row["transition_n"]) > 0
            )
            actual = sorted(
                (cell["c0_value"], cell["c1_value"], cell["n"])
                for cell in fact["result"]["nonzero_transition_cells"]
            )
            denominator = int(rows[0]["pair_n"])
        _assert_value(
            violations, fact, "result.nonzero_transition_cells", actual, expected
        )
        _assert_value(
            violations,
            fact,
            "population.denominator_n",
            fact["population"]["denominator_n"],
            denominator,
        )


def _schema_and_source_validation(
    payload: dict[str, Any],
    statistics_root: Path,
) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    required_top = {
        "schema_version",
        "status",
        "cohort",
        "fact_count",
        "fact_counts_by_role",
        "global_interpretation_boundaries",
        "facts",
    }
    missing_top = sorted(required_top - set(payload))
    if missing_top:
        violations.append({"rule": "missing_top_fields", "fields": missing_top})
    facts = payload.get("facts")
    if not isinstance(facts, list):
        raise GateError("results facts payload does not contain a facts list")
    ids = [fact.get("fact_id") for fact in facts if isinstance(fact, dict)]
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        violations.append({"rule": "fact_ids_not_unique_or_missing"})
    for fact in facts:
        if not isinstance(fact, dict):
            violations.append({"rule": "fact_not_object"})
            continue
        for key in (
            "fact_id",
            "family",
            "scope",
            "aou",
            "inferential_role",
            "source",
            "interpretation_boundary",
            "paper_facing_authorized",
        ):
            if key not in fact:
                violations.append(
                    {
                        "fact_id": fact.get("fact_id"),
                        "rule": "missing_required_fact_field",
                        "field": key,
                    }
                )
        if fact.get("family") not in ALLOWED_FAMILIES:
            violations.append(
                {
                    "fact_id": fact.get("fact_id"),
                    "rule": "invalid_family",
                    "value": fact.get("family"),
                }
            )
            continue
        if fact.get("inferential_role") not in ALLOWED_ROLES:
            violations.append(
                {
                    "fact_id": fact.get("fact_id"),
                    "rule": "invalid_inferential_role",
                }
            )
        if fact.get("paper_facing_authorized") is not False:
            violations.append(
                {
                    "fact_id": fact.get("fact_id"),
                    "rule": "paper_facing_authorization_not_false",
                }
            )
        source = fact.get("source")
        if not isinstance(source, dict):
            continue
        path = statistics_root / str(source.get("file") or "")
        if not path.is_file():
            violations.append(
                {
                    "fact_id": fact.get("fact_id"),
                    "rule": "source_file_missing",
                    "file": source.get("file"),
                }
            )
            continue
        if source.get("sha256") != sha256_file(path):
            violations.append(
                {
                    "fact_id": fact.get("fact_id"),
                    "rule": "source_provenance_hash_mismatch",
                    "file": source.get("file"),
                }
            )
        try:
            matches = _matching_rows(statistics_root, source)
            _validate_fact_against_source(fact, matches, violations)
        except (GateError, KeyError, TypeError, ValueError) as exc:
            violations.append(
                {
                    "fact_id": fact.get("fact_id"),
                    "rule": "source_validation_error",
                    "error": str(exc),
                }
            )
    if payload.get("fact_count") != len(facts):
        violations.append(
            {
                "rule": "fact_count_mismatch",
                "declared": payload.get("fact_count"),
                "observed": len(facts),
            }
        )
    role_counts = Counter(fact.get("inferential_role") for fact in facts)
    if payload.get("fact_counts_by_role") != {
        role: role_counts[role] for role in ("primary", "secondary_descriptive", "diagnostic")
    }:
        violations.append({"rule": "fact_role_counts_mismatch"})
    return {
        "schema_version": "atobench.final_schema_validation.v1",
        "status": "PASS" if not violations else "FAIL",
        "fact_count": len(facts),
        "unique_fact_id_count": len(set(ids)),
        "source_selector_validation_count": len(facts),
        "source_provenance_hash_check": "PASS" if not any(
            row.get("rule") == "source_provenance_hash_mismatch"
            for row in violations
        ) else "FAIL",
        "violation_count": len(violations),
        "violations": violations,
    }


def _sum_rows(rows: list[dict[str, str]], fields: list[str]) -> dict[str, int]:
    return {
        field: sum(int(row[field]) for row in rows)
        for field in fields
    }


def _denominator_reconciliation(
    statistics_root: Path,
    membership_path: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    memberships = load_jsonl(membership_path)
    violations: list[dict[str, Any]] = []
    if len(memberships) != payload["cohort"]["pair_count"]:
        violations.append(
            {
                "rule": "membership_pair_count_mismatch",
                "membership_n": len(memberships),
                "fact_cohort_n": payload["cohort"]["pair_count"],
            }
        )
    if len({row["pair_id"] for row in memberships}) != len(memberships):
        violations.append({"rule": "membership_pair_ids_not_unique"})

    checks = [
        (
            "pooled_by_aou.csv",
            [
                "pair_n",
                "state_observed_n",
                "grounded_verification_n",
                "unsupported_closure_n",
                "unreported_verification_n",
                "unresolved_verification_n",
                "state_unavailable_n",
            ],
            ("aou", "condition"),
        ),
        (
            "capability_conditioned.csv",
            [
                "capability_target_n",
                "outcome_observed_n",
                "outcome_missing_n",
                "retained_n",
                "loss_n",
            ],
            ("aou",),
        ),
        (
            "missingness_sensitivity.csv",
            [
                "pair_n",
                "c0_observed_c1_observed_n",
                "c0_observed_c1_unavailable_n",
                "c0_unavailable_c1_observed_n",
                "c0_unavailable_c1_unavailable_n",
                "c0_observed_n",
                "c1_observed_n",
            ],
            ("aou",),
        ),
    ]
    reconciliation_count = 0
    for filename, fields, group_fields in checks:
        rows = _read_csv(statistics_root / filename)
        pooled = [row for row in rows if row["scope"] == "aou"]
        model = [row for row in rows if row["scope"] == "model_by_aou"]
        if filename == "pooled_by_aou.csv":
            model = _read_csv(statistics_root / "per_model_by_aou.csv")
        for pooled_row in pooled:
            subset = [
                row
                for row in model
                if all(row[field] == pooled_row[field] for field in group_fields)
            ]
            sums = _sum_rows(subset, fields)
            reconciliation_count += len(fields)
            for field in fields:
                if int(pooled_row[field]) != sums[field]:
                    violations.append(
                        {
                            "rule": "pooled_model_denominator_mismatch",
                            "file": filename,
                            "group": {
                                field_name: pooled_row[field_name]
                                for field_name in group_fields
                            },
                            "field": field,
                            "pooled": int(pooled_row[field]),
                            "model_sum": sums[field],
                        }
                    )

    capability = [
        row
        for row in _read_csv(statistics_root / "capability_conditioned.csv")
        if row["scope"] == "aou"
    ]
    target_n = sum(int(row["capability_target_n"]) for row in capability)
    observed_n = sum(int(row["outcome_observed_n"]) for row in capability)
    missing_n = sum(int(row["outcome_missing_n"]) for row in capability)
    expected = payload["cohort"]
    for name, actual, expected_value in (
        ("capability_target_pair_count", target_n, expected["capability_target_pair_count"]),
        (
            "capability_observed_outcome_pair_count",
            observed_n,
            expected["capability_observed_outcome_pair_count"],
        ),
        (
            "capability_missing_outcome_pair_count",
            missing_n,
            expected["capability_missing_outcome_pair_count"],
        ),
    ):
        if actual != expected_value:
            violations.append(
                {
                    "rule": "cohort_capability_count_mismatch",
                    "field": name,
                    "source_sum": actual,
                    "fact_value": expected_value,
                }
            )
    if observed_n + missing_n != target_n:
        violations.append({"rule": "global_capability_partition_mismatch"})
    return {
        "schema_version": "atobench.denominator_reconciliation.v1",
        "status": "PASS" if not violations else "FAIL",
        "frozen_membership_pair_count": len(memberships),
        "unique_membership_pair_count": len({row["pair_id"] for row in memberships}),
        "pooled_model_field_reconciliation_count": reconciliation_count,
        "capability_target_n": target_n,
        "capability_observed_n": observed_n,
        "capability_missing_n": missing_n,
        "violation_count": len(violations),
        "violations": violations,
    }


def _claim_boundary_audit(payload: dict[str, Any]) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    boundaries = payload.get("global_interpretation_boundaries", {})
    required_false = (
        "cross_aou_pooled_primary_estimate",
        "cross_dimension_composite",
        "primary_p_values",
        "model_ranking_authorized",
        "state_unavailable_coerced_to_failure",
        "stop_decision_numeric_is_primary",
        "paper_integration_authorized",
    )
    for key in required_false:
        if boundaries.get(key) is not False:
            violations.append(
                {"rule": "global_boundary_not_false", "field": key}
            )
    for fact in payload["facts"]:
        if fact["scope"] == "model_by_aou" and fact["inferential_role"] != "secondary_descriptive":
            violations.append(
                {
                    "fact_id": fact["fact_id"],
                    "rule": "model_fact_not_secondary_descriptive",
                }
            )
        if fact["inferential_role"] == "primary" and fact["scope"] != "aou":
            violations.append(
                {
                    "fact_id": fact["fact_id"],
                    "rule": "primary_fact_not_aou_scoped",
                }
            )
        if fact["family"] == "diagnostic_score":
            if fact["dimension"] == "stop_decision" and fact["numeric_interpretation"] != "engineering_provenance_only":
                violations.append(
                    {
                        "fact_id": fact["fact_id"],
                        "rule": "stop_numeric_promoted_beyond_provenance",
                    }
                )
            if "converted to zero" not in " ".join(
                fact["interpretation_boundary"]
            ):
                violations.append(
                    {
                        "fact_id": fact["fact_id"],
                        "rule": "null_to_zero_boundary_missing",
                    }
                )
        if fact["family"] == "capability_conditioned_retention":
            if fact["population"]["target_n"] != (
                fact["population"]["outcome_observed_n"]
                + fact["population"]["outcome_missing_n"]
            ):
                violations.append(
                    {
                        "fact_id": fact["fact_id"],
                        "rule": "missing_outcome_partition_not_explicit",
                    }
                )
            if len(fact["uncertainty"]["missing_outcome_bounds"]) != 2:
                violations.append(
                    {
                        "fact_id": fact["fact_id"],
                        "rule": "missing_outcome_bounds_missing",
                    }
                )
    return {
        "schema_version": "atobench.claim_boundary_audit.v1",
        "status": "PASS" if not violations else "FAIL",
        "checked_fact_count": len(payload["facts"]),
        "model_ranking_authorized": False,
        "paper_integration_authorized": False,
        "violation_count": len(violations),
        "violations": violations,
    }


def _scan_final_artifacts(
    facts_root: Path,
    packet_leakage_path: Path,
    packet_secret_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    leakage_hits: list[dict[str, Any]] = []
    secret_hits: list[dict[str, Any]] = []
    files = (
        facts_root / "results_facts.json",
        facts_root / "results_facts.md",
        facts_root / "VERIFICATION_RESILIENCE_REPORT.md",
    )
    for path in files:
        text = path.read_text(encoding="utf-8")
        for kind in residual_sensitive_kinds(text):
            secret_hits.append({"file": path.name, "rule": f"redaction_{kind}"})
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                secret_hits.append({"file": path.name, "rule": name})
        for name, pattern in PAPER_LEAKAGE_PATTERNS.items():
            if pattern.search(text):
                leakage_hits.append({"file": path.name, "rule": name})
    packet_leakage = read_json(packet_leakage_path)
    packet_secret = read_json(packet_secret_path)
    upstream_leakage_hits = packet_leakage.get("hits")
    upstream_secret_hits = packet_secret.get("hits")
    if not isinstance(upstream_leakage_hits, list) or not isinstance(
        upstream_secret_hits, list
    ):
        raise GateError("packet safety scans are malformed")
    leakage = {
        "schema_version": "atobench.final_leakage_scan.v1",
        "status": (
            "PASS"
            if not leakage_hits and not upstream_leakage_hits
            else "FAIL"
        ),
        "upstream_packet_leakage_hit_count": len(upstream_leakage_hits),
        "final_artifact_leakage_hit_count": len(leakage_hits),
        "hits": [*upstream_leakage_hits, *leakage_hits],
    }
    secrets = {
        "schema_version": "atobench.final_secret_scan.v1",
        "status": (
            "PASS" if not secret_hits and not upstream_secret_hits else "FAIL"
        ),
        "upstream_packet_secret_hit_count": len(upstream_secret_hits),
        "final_artifact_secret_hit_count": len(secret_hits),
        "hits": [*upstream_secret_hits, *secret_hits],
    }
    return leakage, secrets


def _normalized_facts(path: Path) -> dict[str, Any]:
    value = read_json(path)
    value.pop("created_at", None)
    return value


def _reproducibility_check(
    *,
    statistics_root: Path,
    statistics_audit_path: Path,
    facts_root: Path,
    allow_real_data: bool,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "facts"
        run_result_fact_export(
            statistics_root=statistics_root,
            audit_path=statistics_audit_path,
            output_dir=output,
            allow_real_data=allow_real_data,
            dry_run=False,
            new_version=False,
        )
        checks = {
            "results_facts_semantic_equal_excluding_created_at": (
                _normalized_facts(facts_root / "results_facts.json")
                == _normalized_facts(output / "results_facts.json")
            ),
            "results_facts_markdown_byte_identical": (
                (facts_root / "results_facts.md").read_bytes()
                == (output / "results_facts.md").read_bytes()
            ),
            "internal_report_byte_identical": (
                (facts_root / "VERIFICATION_RESILIENCE_REPORT.md").read_bytes()
                == (output / "VERIFICATION_RESILIENCE_REPORT.md").read_bytes()
            ),
        }
    return {
        "schema_version": "atobench.final_reproducibility_check.v1",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "model_calls_made": 0,
        "network_accessed": False,
    }


def _upstream_gate_status(
    *,
    census_manifest_path: Path,
    alignment_summary_path: Path,
    fact_registry_validation_path: Path,
    judge_validation_path: Path,
    semantic_validation_path: Path,
    statistics_audit_path: Path,
    validation_root: Path,
) -> dict[str, Any]:
    census = read_json(census_manifest_path)
    alignment = read_json(alignment_summary_path)
    facts = read_json(fact_registry_validation_path)
    judge = read_json(judge_validation_path)
    semantic = read_json(semantic_validation_path)
    statistics = read_json(statistics_audit_path)
    required_validation_files = (
        "development_sample.csv",
        "validation_holdout.csv",
        "agent_only_validation_summary.json",
        "judge_cross_validation.json",
        "prompt_sensitivity.json",
        "rationale_ablation.json",
        "perturbation_tests.json",
        "synthetic_fixtures/fixture_manifest.json",
        "AGENT_ONLY_VALIDATION_REPORT.md",
    )
    present = {
        name: (validation_root / name).is_file()
        for name in required_validation_files
    }
    agent_validation_checks: dict[str, Any] = {
        "required_artifacts_present": all(present.values())
    }
    if all(present.values()):
        development = _read_csv(validation_root / "development_sample.csv")
        holdout = _read_csv(validation_root / "validation_holdout.csv")
        identity_key = (
            "episode_id"
            if development and "episode_id" in development[0]
            else next(iter(development[0]), "")
            if development
            else ""
        )
        development_ids = (
            {row.get(identity_key) for row in development} if identity_key else set()
        )
        holdout_ids = (
            {row.get(identity_key) for row in holdout} if identity_key else set()
        )
        summary = read_json(
            validation_root / "agent_only_validation_summary.json"
        )
        fixture = read_json(
            validation_root / "synthetic_fixtures/fixture_manifest.json"
        )
        components = {
            name: read_json(validation_root / name)
            for name in (
                "judge_cross_validation.json",
                "prompt_sensitivity.json",
                "rationale_ablation.json",
                "perturbation_tests.json",
            )
        }
        report_text = (
            validation_root / "AGENT_ONLY_VALIDATION_REPORT.md"
        ).read_text(encoding="utf-8")
        agent_validation_checks.update(
            {
                "development_episode_count_36": len(development) == 36,
                "holdout_episode_count_24": len(holdout) == 24,
                "development_holdout_disjoint": bool(identity_key)
                and development_ids.isdisjoint(holdout_ids),
                "summary_pass": summary.get("status")
                == "PASS_AGENT_ONLY_VALIDATION",
                "summary_declares_band_diagnostic_not_primary": summary.get(
                    "band_agreement_role"
                )
                == "diagnostic_not_primary_reliability_gate",
                "fixture_count_at_least_24": int(
                    fixture.get("fixture_count", 0)
                )
                >= 24,
                "fixture_status_pass": fixture.get("status") == "PASS",
                "component_statuses_pass": all(
                    value.get("status") == "PASS"
                    for value in components.values()
                ),
                "report_pass_marker": "PASS_AGENT_ONLY_VALIDATION"
                in report_text,
            }
        )
    agent_validation_pass = all(agent_validation_checks.values())
    return {
        "census": {
            "status": (
                "PASS"
                if census.get("status") == "complete"
                and census.get("details", {}).get("blocking_conflict_count") == 0
                else "FAIL"
            ),
            "evidence": str(census_manifest_path),
        },
        "alignment": {
            "status": (
                "PASS"
                if alignment.get("pending_alignment_count") == 0
                and int(alignment.get("accepted_alignment_row_count", 0)) > 0
                else "FAIL"
            ),
            "evidence": str(alignment_summary_path),
        },
        "fact_registry": {
            "status": (
                "PASS"
                if facts.get("status") == "passed"
                and (
                    facts.get("violation_count")
                    if facts.get("violation_count") is not None
                    else facts.get("details", {}).get("violation_count")
                )
                == 0
                else "FAIL"
            ),
            "evidence": str(fact_registry_validation_path),
        },
        "judge_bundle_validation": {
            "status": (
                "PASS"
                if judge.get("status") == "PASS"
                and judge.get("violation_count") == 0
                and judge.get("validated_bundle_count") == 1290
                else "FAIL"
            ),
            "evidence": str(judge_validation_path),
        },
        "semantic_validation": {
            "status": (
                "PASS"
                if semantic.get("status") == "PASS"
                and semantic.get("violation_count") == 0
                and semantic.get("validated_episode_count") == 430
                else "FAIL"
            ),
            "evidence": str(semantic_validation_path),
        },
        "statistics_internal_audit": {
            "status": (
                "PASS"
                if statistics.get("status") == "PASS_INTERNAL_STATISTICS_AUDIT"
                else "FAIL"
            ),
            "evidence": str(statistics_audit_path),
        },
        "agent_only_development_holdout_validation": {
            "status": (
                "PASS"
                if agent_validation_pass
                else "BLOCKED_NOT_RUN_UNTOUCHED_HOLDOUT_NOT_RECOVERABLE_FROM_OPENED_COHORT"
            ),
            "required_artifacts": present,
            "validation_checks": agent_validation_checks,
            "evidence_root": str(validation_root),
            "recovery_boundary": (
                None
                if agent_validation_pass
                else (
                    "The 430-episode full-cohort judgments and effects are already "
                    "opened. A holdout selected now from that cohort cannot be labeled "
                    "untouched. Satisfying this gate requires prospectively frozen new "
                    "validation evidence, or an explicit protocol amendment that keeps "
                    "the present corpus retrospective diagnostic."
                )
            ),
        },
    }


def _qa_markdown(
    *,
    status: str,
    checks: dict[str, dict[str, Any]],
    final_gates: dict[str, dict[str, Any]],
    blocking_gates: list[str],
    fact_count: int,
) -> str:
    lines = [
        "# Verification Resilience Final QA Report",
        "",
        f"Status: `{status}`",
        "",
        f"Validated result facts: {fact_count}",
        "",
        "## Internal Stage 15 checks",
        "",
        "| Check | Status |",
        "|---|---|",
    ]
    for name, report in checks.items():
        lines.append(f"| {name} | `{report['status']}` |")
    lines.extend(
        [
            "",
            "## Final release gates",
            "",
            "| Gate | Status |",
            "|---|---|",
        ]
    )
    for name, report in final_gates.items():
        lines.append(f"| {name} | `{report['status']}` |")
    lines.extend(
        [
            "",
            "## Decision",
            "",
        ]
    )
    if blocking_gates:
        lines.append(
            "Internal result QA passed, but `PASS_VERIFICATION_RESILIENCE_V1` "
            "is not issued. Blocking gates: "
            + ", ".join(f"`{name}`" for name in blocking_gates)
            + "."
        )
    else:
        lines.append(
            "Every required final gate passed. The facts are eligible for "
            "explicit author review; this report does not itself modify the manuscript."
        )
    lines.extend(
        [
            "",
            "Paper integration remains disabled unless the final author-approval gate is recorded explicitly.",
            "",
        ]
    )
    return "\n".join(lines)


def run(
    *,
    facts_root: Path,
    statistics_root: Path,
    statistics_audit_path: Path,
    population_membership_path: Path,
    packet_leakage_path: Path,
    packet_secret_path: Path,
    census_manifest_path: Path,
    alignment_summary_path: Path,
    fact_registry_validation_path: Path,
    judge_validation_path: Path,
    semantic_validation_path: Path,
    validation_root: Path,
    output_dir: Path,
    author_approval_path: Path | None,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    inputs = [
        facts_root / "results_facts.json",
        facts_root / "results_facts.md",
        facts_root / "VERIFICATION_RESILIENCE_REPORT.md",
        facts_root / "stage_manifest.json",
        statistics_root / "statistics_summary.json",
        statistics_root / "stage_manifest.json",
        statistics_audit_path,
        population_membership_path,
        packet_leakage_path,
        packet_secret_path,
        census_manifest_path,
        alignment_summary_path,
        fact_registry_validation_path,
        judge_validation_path,
        semantic_validation_path,
    ]
    if author_approval_path is not None:
        inputs.append(author_approval_path)
    require_real_data_authorization(inputs, allow_real_data)
    for path in inputs:
        if not path.is_file():
            raise GateError(f"QA input is missing: {path}")
    stage14_manifest = read_json(facts_root / "stage_manifest.json")
    if stage14_manifest.get("status") != "complete":
        raise GateError("Stage 14 manifest is not complete")
    plan = {
        "schema_version": "atobench.final_qa_plan.v1",
        "status": "READY",
        "paper_integration_authorized": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    if dry_run:
        return plan

    payload = read_json(facts_root / "results_facts.json")
    schema = _schema_and_source_validation(payload, statistics_root)
    denominators = _denominator_reconciliation(
        statistics_root, population_membership_path, payload
    )
    claim_boundary = _claim_boundary_audit(payload)
    leakage, secrets = _scan_final_artifacts(
        facts_root, packet_leakage_path, packet_secret_path
    )
    reproducibility = _reproducibility_check(
        statistics_root=statistics_root,
        statistics_audit_path=statistics_audit_path,
        facts_root=facts_root,
        allow_real_data=allow_real_data,
    )
    upstream = _upstream_gate_status(
        census_manifest_path=census_manifest_path,
        alignment_summary_path=alignment_summary_path,
        fact_registry_validation_path=fact_registry_validation_path,
        judge_validation_path=judge_validation_path,
        semantic_validation_path=semantic_validation_path,
        statistics_audit_path=statistics_audit_path,
        validation_root=validation_root,
    )
    author_approved = False
    if author_approval_path is not None:
        approval = read_json(author_approval_path)
        author_approved = (
            approval.get("schema_version")
            == "atobench.author_fact_approval.v1"
            and approval.get("approved") is True
            and approval.get("paper_integration_authorized") is True
        )
    checks = {
        "schema_validation": schema,
        "leakage_scan": leakage,
        "secret_scan": secrets,
        "reproducibility_check": reproducibility,
        "statistical_denominator_reconciliation": denominators,
        "claim_boundary_audit": claim_boundary,
    }
    internal_pass = all(report["status"] == "PASS" for report in checks.values())
    final_gates = {
        "census": upstream["census"],
        "alignment": upstream["alignment"],
        "agent_only_validation": upstream[
            "agent_only_development_holdout_validation"
        ],
        "schema": {"status": schema["status"]},
        "leakage": {"status": leakage["status"]},
        "secret_scan": {"status": secrets["status"]},
        "reproducibility": {"status": reproducibility["status"]},
        "statistical_denominator_reconciliation": {
            "status": denominators["status"]
        },
        "claim_boundary_audit": {"status": claim_boundary["status"]},
        "author_approval": {
            "status": "PASS" if author_approved else "BLOCKED_NOT_PROVIDED"
        },
    }
    blocking_gates = [
        name for name, report in final_gates.items() if report["status"] != "PASS"
    ]
    if internal_pass and blocking_gates:
        status = "PASS_INTERNAL_QA_FINAL_GATE_BLOCKED"
    elif internal_pass:
        status = "PASS_VERIFICATION_RESILIENCE_V1"
    else:
        status = "FAIL_INTERNAL_QA"

    ensure_output_available(output_dir, new_version)
    outputs = {
        "schema": output_dir / "schema_validation.json",
        "leakage": output_dir / "leakage_scan.json",
        "secrets": output_dir / "secret_scan.json",
        "reproducibility": output_dir / "reproducibility_check.json",
        "denominators": output_dir / "denominator_reconciliation.json",
        "claim_boundary": output_dir / "claim_boundary_audit.json",
        "upstream": output_dir / "upstream_gate_status.json",
        "final": output_dir / "final_gate_status.json",
        "report": output_dir / "QA_REPORT.md",
    }
    write_json(outputs["schema"], schema)
    write_json(outputs["leakage"], leakage)
    write_json(outputs["secrets"], secrets)
    write_json(outputs["reproducibility"], reproducibility)
    write_json(outputs["denominators"], denominators)
    write_json(outputs["claim_boundary"], claim_boundary)
    write_json(outputs["upstream"], upstream)
    final = {
        "schema_version": "atobench.final_gate_status.v1",
        "created_at": utc_now(),
        "status": status,
        "internal_qa_pass": internal_pass,
        "final_gates": final_gates,
        "blocking_gates": blocking_gates,
        "paper_integration_authorized": author_approved and not blocking_gates,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    write_json(outputs["final"], final)
    outputs["report"].write_text(
        _qa_markdown(
            status=status,
            checks=checks,
            final_gates=final_gates,
            blocking_gates=blocking_gates,
            fact_count=len(payload["facts"]),
        ),
        encoding="utf-8",
    )
    summary = {
        "schema_version": "atobench.final_qa_summary.v1",
        "status": status,
        "internal_qa_pass": internal_pass,
        "fact_count": len(payload["facts"]),
        "blocking_gate_count": len(blocking_gates),
        "blocking_gates": blocking_gates,
        "paper_integration_authorized": final[
            "paper_integration_authorized"
        ],
        "model_calls_made": 0,
        "network_accessed": False,
    }
    manifest = stage_manifest(
        "15_run_qa",
        inputs,
        list(outputs.values()),
        "complete" if internal_pass else "failed",
        dry_run=False,
        details=summary,
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    return summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--facts-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--author-approval", type=Path)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    root = args.experiment_root
    try:
        report = run(
            facts_root=args.facts_root,
            statistics_root=root / "10_statistics/13_full_statistics_v1",
            statistics_audit_path=root
            / "10_statistics/13b_statistics_audit_v1/statistics_audit.json",
            population_membership_path=root
            / "10_statistics/13a_statistics_freeze_v1/pair_analysis_population_membership.jsonl",
            packet_leakage_path=root
            / "05_judge_packets/07c_full_cohort_packets_v2/leakage_scan.json",
            packet_secret_path=root
            / "05_judge_packets/07c_full_cohort_packets_v2/secret_scan.json",
            census_manifest_path=root / "01_census/stage_manifest.json",
            alignment_summary_path=root
            / "03_alignment/04i_alignment_deterministic_final_v1/deterministic_resolution_summary.json",
            fact_registry_validation_path=root
            / "04_facts/05c_fact_registry_validation_v1/fact_registry_validation.json",
            judge_validation_path=root
            / "07_validation/10_full_cohort_validation_v1/judge_validation_report.json",
            semantic_validation_path=root
            / "08_semantic_matches/12k_semantic_match_full_cohort_validation_v2/semantic_validation_summary.json",
            validation_root=root / "07_validation",
            output_dir=args.output,
            author_approval_path=args.author_approval,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
