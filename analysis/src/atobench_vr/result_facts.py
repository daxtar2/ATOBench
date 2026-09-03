from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    read_json,
    require_real_data_authorization,
    sha256_file,
    stage_manifest,
    utc_now,
    write_json,
)


SOURCE_FILES = (
    "pooled_by_aou.csv",
    "per_model_by_aou.csv",
    "verification_state_transitions.csv",
    "capability_conditioned.csv",
    "missingness_sensitivity.csv",
    "diagnostic_score_statistics.csv",
    "stop_descriptor_transitions.csv",
    "statistical_tests.json",
    "statistics_summary.json",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise GateError(f"result-fact source is missing: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _integer(row: dict[str, str], key: str) -> int:
    value = row.get(key)
    if value is None or value == "":
        raise GateError(f"result-fact source is missing integer field {key}")
    try:
        return int(value)
    except ValueError as exc:
        raise GateError(f"result-fact source has invalid integer {key}: {value}") from exc


def _number(row: dict[str, str], key: str) -> float | None:
    value = row.get(key)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise GateError(f"result-fact source has invalid number {key}: {value}") from exc


def _identity(row: dict[str, str]) -> dict[str, str | None]:
    return {
        "scope": row["scope"],
        "aou": row["aou"],
        "model": row.get("model") or None,
    }


def _slug(value: str | None) -> str:
    return (value or "pooled").upper().replace(".", "_").replace("-", "_")


def _role(scope: str, family: str) -> str:
    if scope == "model_by_aou":
        return "secondary_descriptive"
    if family in {"diagnostic_score", "stop_descriptor_transition"}:
        return "diagnostic"
    return "primary"


def _source(
    statistics_root: Path,
    filename: str,
    selector: dict[str, Any],
) -> dict[str, Any]:
    path = statistics_root / filename
    return {
        "file": filename,
        "sha256": sha256_file(path),
        "row_selector": selector,
    }


def _base(
    *,
    fact_id: str,
    family: str,
    row: dict[str, str],
    statistics_root: Path,
    filename: str,
    selector: dict[str, Any],
) -> dict[str, Any]:
    identity = _identity(row)
    return {
        "fact_id": fact_id,
        "family": family,
        **identity,
        "inferential_role": _role(identity["scope"], family),
        "status": "internal_fact_pending_final_qa",
        "source": _source(statistics_root, filename, selector),
        "paper_facing_authorized": False,
    }


def _distribution_facts(
    statistics_root: Path,
    filename: str,
) -> list[dict[str, Any]]:
    output = []
    for row in _read_csv(statistics_root / filename):
        selector = {
            "scope": row["scope"],
            "aou": row["aou"],
            "model": row.get("model") or "",
            "condition": row["condition"],
        }
        fact = _base(
            fact_id=(
                f"VR-STATE-{_slug(row['aou'])}-{_slug(row.get('model'))}-"
                f"{row['condition']}"
            ),
            family="verification_state_distribution",
            row=row,
            statistics_root=statistics_root,
            filename=filename,
            selector=selector,
        )
        pair_n = _integer(row, "pair_n")
        counts = {
            state: _integer(row, f"{state}_n")
            for state in (
                "grounded_verification",
                "unsupported_closure",
                "unreported_verification",
                "unresolved_verification",
                "state_unavailable",
            )
        }
        if sum(counts.values()) != pair_n:
            raise GateError(f"state counts do not reconcile for {selector}")
        fact.update(
            {
                "population": {
                    "name": "frozen paired cohort",
                    "denominator_n": pair_n,
                    "condition": row["condition"],
                    "state_observed_n": _integer(row, "state_observed_n"),
                },
                "result": {
                    "counts": counts,
                    "proportions_of_all_frozen_pairs": {
                        state: _number(row, f"{state}_proportion")
                        for state in counts
                    },
                },
                "interpretation_boundary": [
                    "state_unavailable is retained as an explicit endpoint state",
                    "proportions use all frozen pairs in this stratum as denominator",
                    (
                        "within-model descriptive result; no direct model-ranking inference"
                        if row["scope"] == "model_by_aou"
                        else "AOU-specific result; no cross-AOU pooling"
                    ),
                ],
            }
        )
        output.append(fact)
    return output


def _capability_facts(statistics_root: Path) -> list[dict[str, Any]]:
    filename = "capability_conditioned.csv"
    output = []
    for row in _read_csv(statistics_root / filename):
        selector = {
            "scope": row["scope"],
            "aou": row["aou"],
            "model": row.get("model") or "",
        }
        fact = _base(
            fact_id=f"VR-RETENTION-{_slug(row['aou'])}-{_slug(row.get('model'))}",
            family="capability_conditioned_retention",
            row=row,
            statistics_root=statistics_root,
            filename=filename,
            selector=selector,
        )
        target_n = _integer(row, "capability_target_n")
        observed_n = _integer(row, "outcome_observed_n")
        missing_n = _integer(row, "outcome_missing_n")
        retained_n = _integer(row, "retained_n")
        loss_n = _integer(row, "loss_n")
        if observed_n + missing_n != target_n or retained_n + loss_n != observed_n:
            raise GateError(f"capability counts do not reconcile for {selector}")
        fact.update(
            {
                "population": {
                    "name": "C0 grounded and C1 target-contact-positive pairs",
                    "target_n": target_n,
                    "outcome_observed_n": observed_n,
                    "outcome_missing_n": missing_n,
                    "outcome_missing_proportion": _number(
                        row, "outcome_missing_proportion"
                    ),
                },
                "estimate": {
                    "name": "observed_grounded_retention",
                    "numerator_n": retained_n,
                    "denominator_n": observed_n,
                    "value": _number(row, "observed_grounded_retention"),
                    "complement_name": "observed_ato_induced_loss",
                    "complement_numerator_n": loss_n,
                    "complement_value": _number(row, "observed_ato_induced_loss"),
                },
                "uncertainty": {
                    "pair_bootstrap_ci": [
                        _number(row, "observed_grounded_retention_bootstrap_ci_low"),
                        _number(
                            row, "observed_grounded_retention_bootstrap_ci_high"
                        ),
                    ],
                    "exact_binomial_ci": [
                        _number(row, "observed_grounded_retention_exact_ci_low"),
                        _number(row, "observed_grounded_retention_exact_ci_high"),
                    ],
                    "missing_outcome_bounds": [
                        _number(row, "retention_worst_case_bound"),
                        _number(row, "retention_best_case_bound"),
                    ],
                },
                "interpretation_boundary": [
                    "retention is conditioned on demonstrated C0 capability and positive C1 target contact",
                    "observed retention excludes missing C1 outcomes from its denominator",
                    "missing-outcome bounds retain all target pairs",
                    "exact interval is reported alongside the empirical pair-bootstrap interval",
                    (
                        "within-model descriptive result with no pairwise model test or ranking"
                        if row["scope"] == "model_by_aou"
                        else "AOU-primary result with no cross-AOU pooling"
                    ),
                ],
            }
        )
        output.append(fact)
    return output


def _availability_facts(statistics_root: Path) -> list[dict[str, Any]]:
    filename = "missingness_sensitivity.csv"
    output = []
    for row in _read_csv(statistics_root / filename):
        selector = {
            "scope": row["scope"],
            "aou": row["aou"],
            "model": row.get("model") or "",
        }
        fact = _base(
            fact_id=f"VR-AVAILABILITY-{_slug(row['aou'])}-{_slug(row.get('model'))}",
            family="paired_state_availability",
            row=row,
            statistics_root=statistics_root,
            filename=filename,
            selector=selector,
        )
        pair_n = _integer(row, "pair_n")
        pattern_counts = {
            "c0_observed_c1_observed": _integer(
                row, "c0_observed_c1_observed_n"
            ),
            "c0_observed_c1_unavailable": _integer(
                row, "c0_observed_c1_unavailable_n"
            ),
            "c0_unavailable_c1_observed": _integer(
                row, "c0_unavailable_c1_observed_n"
            ),
            "c0_unavailable_c1_unavailable": _integer(
                row, "c0_unavailable_c1_unavailable_n"
            ),
        }
        if sum(pattern_counts.values()) != pair_n:
            raise GateError(f"availability counts do not reconcile for {selector}")
        fact.update(
            {
                "population": {
                    "name": "frozen paired cohort",
                    "denominator_n": pair_n,
                },
                "estimate": {
                    "name": "paired_availability_difference_c1_minus_c0",
                    "value": _number(
                        row, "paired_availability_difference_c1_minus_c0"
                    ),
                    "c0_observed_n": _integer(row, "c0_observed_n"),
                    "c1_observed_n": _integer(row, "c1_observed_n"),
                    "c0_observed_proportion": _number(
                        row, "c0_observed_proportion"
                    ),
                    "c1_observed_proportion": _number(
                        row, "c1_observed_proportion"
                    ),
                    "pattern_counts": pattern_counts,
                },
                "uncertainty": {
                    "pair_bootstrap_ci": [
                        _number(
                            row,
                            "paired_availability_difference_bootstrap_ci_low",
                        ),
                        _number(
                            row,
                            "paired_availability_difference_bootstrap_ci_high",
                        ),
                    ],
                    "discordant_pair_exact_interval": [
                        _number(row, "discordant_proportion_exact_ci_low"),
                        _number(row, "discordant_proportion_exact_ci_high"),
                    ],
                },
                "interpretation_boundary": [
                    "availability is an estimand and is not silently excluded",
                    "difference is C1 minus C0 on the same frozen pairs",
                    (
                        "within-model descriptive result; bootstrap interval is not computed at this scope"
                        if row["scope"] == "model_by_aou"
                        else "AOU-primary paired estimate"
                    ),
                ],
            }
        )
        output.append(fact)
    return output


def _transition_facts(statistics_root: Path) -> list[dict[str, Any]]:
    filename = "verification_state_transitions.csv"
    rows = _read_csv(statistics_root / filename)
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[(row["scope"], row["aou"], row.get("model") or "")].append(row)
    output = []
    for (scope, aou, model), group in sorted(groups.items()):
        first = group[0]
        selector = {"scope": scope, "aou": aou, "model": model}
        fact = _base(
            fact_id=f"VR-TRANSITION-{_slug(aou)}-{_slug(model)}",
            family="verification_state_transition_matrix",
            row=first,
            statistics_root=statistics_root,
            filename=filename,
            selector=selector,
        )
        complete_n = _integer(first, "complete_pair_n")
        excluded_n = _integer(first, "excluded_incomplete_pair_n")
        cells = [
            {
                "c0_state": row["c0_state"],
                "c1_state": row["c1_state"],
                "n": _integer(row, "transition_n"),
                "proportion_of_complete": _number(
                    row, "transition_proportion_of_complete"
                ),
            }
            for row in group
            if _integer(row, "transition_n") > 0
        ]
        if sum(cell["n"] for cell in cells) != complete_n:
            raise GateError(f"transition counts do not reconcile for {selector}")
        fact.update(
            {
                "population": {
                    "name": "pairs with observed C0 and C1 verification states",
                    "denominator_n": complete_n,
                    "excluded_incomplete_pair_n": excluded_n,
                },
                "result": {"nonzero_transition_cells": cells},
                "interpretation_boundary": [
                    "matrix denominator contains only pairs with both states observed",
                    "excluded incomplete pairs are reported explicitly",
                    (
                        "within-model descriptive transition matrix"
                        if scope == "model_by_aou"
                        else "AOU-specific transition matrix"
                    ),
                ],
            }
        )
        output.append(fact)
    return output


def _score_facts(statistics_root: Path) -> list[dict[str, Any]]:
    filename = "diagnostic_score_statistics.csv"
    output = []
    for row in _read_csv(statistics_root / filename):
        selector = {
            "scope": row["scope"],
            "aou": row["aou"],
            "model": row.get("model") or "",
            "dimension": row["dimension"],
        }
        fact = _base(
            fact_id=(
                f"VR-SCORE-{_slug(row['aou'])}-{_slug(row.get('model'))}-"
                f"{_slug(row['dimension'])}"
            ),
            family="diagnostic_score",
            row=row,
            statistics_root=statistics_root,
            filename=filename,
            selector=selector,
        )
        fact.update(
            {
                "dimension": row["dimension"],
                "population": {
                    "name": "dimension-specific paired numeric-score population",
                    "frozen_pair_n": _integer(row, "pair_n"),
                    "c0_numeric_n": _integer(row, "c0_numeric_n"),
                    "c1_numeric_n": _integer(row, "c1_numeric_n"),
                    "paired_numeric_n": _integer(row, "paired_numeric_n"),
                    "excluded_from_paired_numeric_n": _integer(
                        row, "excluded_from_paired_numeric_n"
                    ),
                },
                "estimate": {
                    "name": "paired_mean_score_delta_c1_minus_c0",
                    "value": _number(row, "paired_mean_delta"),
                    "paired_median_delta": _number(row, "paired_median_delta"),
                    "c1_lower_n": _integer(row, "c1_lower_n"),
                    "unchanged_n": _integer(row, "unchanged_n"),
                    "c1_higher_n": _integer(row, "c1_higher_n"),
                },
                "uncertainty": {
                    "pair_bootstrap_ci": [
                        _number(row, "paired_mean_delta_bootstrap_ci_low"),
                        _number(row, "paired_mean_delta_bootstrap_ci_high"),
                    ]
                },
                "measurement_role": row["measurement_role"],
                "numeric_interpretation": row["numeric_interpretation"],
                "interpretation_boundary": [
                    "null scores remain missing and are never converted to zero",
                    "numeric denominators are dimension-specific",
                    (
                        "Stop Decision descriptor is primary; this numeric result is engineering provenance only"
                        if row["dimension"] == "stop_decision"
                        else "score result is diagnostic and not a cross-dimension resilience composite"
                    ),
                    (
                        "within-model descriptive diagnostic with no model-ranking inference"
                        if row["scope"] == "model_by_aou"
                        else "AOU-specific diagnostic"
                    ),
                ],
            }
        )
        output.append(fact)
    return output


def _descriptor_facts(statistics_root: Path) -> list[dict[str, Any]]:
    filename = "stop_descriptor_transitions.csv"
    rows = _read_csv(statistics_root / filename)
    groups: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[
            (
                row["scope"],
                row["aou"],
                row.get("model") or "",
                row["descriptor"],
            )
        ].append(row)
    output = []
    for (scope, aou, model, descriptor), group in sorted(groups.items()):
        first = group[0]
        selector = {
            "scope": scope,
            "aou": aou,
            "model": model,
            "descriptor": descriptor,
        }
        fact = _base(
            fact_id=(
                f"VR-STOP-{_slug(aou)}-{_slug(model)}-{_slug(descriptor)}"
            ),
            family="stop_descriptor_transition",
            row=first,
            statistics_root=statistics_root,
            filename=filename,
            selector=selector,
        )
        pair_n = _integer(first, "pair_n")
        cells = [
            {
                "c0_value": row["c0_value"],
                "c1_value": row["c1_value"],
                "n": _integer(row, "transition_n"),
            }
            for row in group
            if _integer(row, "transition_n") > 0
        ]
        if sum(cell["n"] for cell in cells) != pair_n:
            raise GateError(f"stop descriptor counts do not reconcile for {selector}")
        fact.update(
            {
                "descriptor": descriptor,
                "population": {
                    "name": "frozen paired cohort",
                    "denominator_n": pair_n,
                },
                "result": {"nonzero_transition_cells": cells},
                "interpretation_boundary": [
                    "descriptor transition is the primary Stop Decision representation",
                    "numeric Stop Decision score is not substituted for this descriptor",
                    (
                        "within-model descriptive transition matrix"
                        if scope == "model_by_aou"
                        else "AOU-specific descriptor transition matrix"
                    ),
                ],
            }
        )
        output.append(fact)
    return output


def _format(value: float | None) -> str:
    return "NA" if value is None else f"{value:.3f}"


def _markdown(
    payload: dict[str, Any],
    *,
    report: bool,
) -> str:
    facts = payload["facts"]
    capability = [
        fact
        for fact in facts
        if fact["family"] == "capability_conditioned_retention"
    ]
    availability = [
        fact for fact in facts if fact["family"] == "paired_state_availability"
    ]
    pooled_capability = [
        fact for fact in capability if fact["scope"] == "aou"
    ]
    model_capability = [
        fact for fact in capability if fact["scope"] == "model_by_aou"
    ]
    pooled_availability = {
        fact["aou"]: fact for fact in availability if fact["scope"] == "aou"
    }
    lines = [
        "# Verification Resilience Internal Results Report"
        if report
        else "# Verification Resilience Result Facts",
        "",
        f"Status: `{payload['status']}`",
        "",
        "**These facts are not authorized for manuscript integration. Stage 15 QA and author approval remain required.**",
        "",
        "## Frozen scope",
        "",
        f"- Frozen pairs: {payload['cohort']['pair_count']}",
        f"- AOUs: {payload['cohort']['aou_count']}",
        f"- Models: {payload['cohort']['model_count']}",
        f"- Complete verification-state pairs: {payload['cohort']['complete_state_pair_count']}",
        f"- Capability targets: {payload['cohort']['capability_target_pair_count']}",
        f"- Capability outcomes observed/missing: {payload['cohort']['capability_observed_outcome_pair_count']} / {payload['cohort']['capability_missing_outcome_pair_count']}",
        "",
        "## AOU-primary results",
        "",
        "| AOU | Target n | Observed n | Retained n | Retention | Exact 95% CI | Missing-outcome bounds | Availability Δ C1−C0 | Bootstrap 95% CI |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for fact in sorted(pooled_capability, key=lambda item: item["aou"]):
        estimate = fact["estimate"]
        uncertainty = fact["uncertainty"]
        available = pooled_availability[fact["aou"]]
        available_estimate = available["estimate"]
        available_ci = available["uncertainty"]["pair_bootstrap_ci"]
        lines.append(
            "| {aou} | {target} | {observed} | {retained} | {retention} | "
            "[{exact_low}, {exact_high}] | [{bound_low}, {bound_high}] | "
            "{availability} | [{availability_low}, {availability_high}] |".format(
                aou=fact["aou"],
                target=fact["population"]["target_n"],
                observed=fact["population"]["outcome_observed_n"],
                retained=estimate["numerator_n"],
                retention=_format(estimate["value"]),
                exact_low=_format(uncertainty["exact_binomial_ci"][0]),
                exact_high=_format(uncertainty["exact_binomial_ci"][1]),
                bound_low=_format(uncertainty["missing_outcome_bounds"][0]),
                bound_high=_format(uncertainty["missing_outcome_bounds"][1]),
                availability=_format(available_estimate["value"]),
                availability_low=_format(available_ci[0]),
                availability_high=_format(available_ci[1]),
            )
        )
    lines.extend(
        [
            "",
            "## Model-within-AOU descriptive results",
            "",
            "These rows support stratified inspection, not direct model ranking. No pairwise model test or multiplicity-adjusted comparison was performed.",
            "",
            "| AOU | Model | Target n | Observed n | Retained n | Retention | Exact 95% CI | Missing bounds |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for fact in sorted(
        model_capability, key=lambda item: (item["aou"], item["model"])
    ):
        estimate = fact["estimate"]
        uncertainty = fact["uncertainty"]
        lines.append(
            "| {aou} | {model} | {target} | {observed} | {retained} | "
            "{retention} | [{exact_low}, {exact_high}] | [{bound_low}, {bound_high}] |".format(
                aou=fact["aou"],
                model=fact["model"],
                target=fact["population"]["target_n"],
                observed=fact["population"]["outcome_observed_n"],
                retained=estimate["numerator_n"],
                retention=_format(estimate["value"]),
                exact_low=_format(uncertainty["exact_binomial_ci"][0]),
                exact_high=_format(uncertainty["exact_binomial_ci"][1]),
                bound_low=_format(uncertainty["missing_outcome_bounds"][0]),
                bound_high=_format(uncertainty["missing_outcome_bounds"][1]),
            )
        )
    if report:
        lines.extend(
            [
                "",
                "## Interpretation boundaries",
                "",
                "- Primary inference is AOU-specific; there is no cross-AOU pooled primary estimate.",
                "- Model-within-AOU facts are descriptive because strata are small and uneven.",
                "- Verification-state unavailability is retained, not treated as failure or zero.",
                "- Stop Decision descriptors are primary; numeric Stop Decision scores are provenance only.",
                "- No cross-dimension composite and no primary p-values were produced.",
                "- Exact retention intervals supplement, rather than replace, frozen pair-bootstrap intervals and missing-outcome bounds.",
                "",
                "## Provenance",
                "",
                f"- Stage 13 statistics status: `{payload['upstream']['statistics_status']}`",
                f"- Internal statistics audit: `{payload['upstream']['audit_status']}`",
                f"- Machine-readable facts: `results_facts.json` ({len(facts)} facts)",
                "- Every machine-readable fact includes a source file hash and exact row selector.",
                "",
                "## Next gate",
                "",
                "Run Stage 15 final QA. Paper integration remains disabled until QA passes and the author explicitly approves the facts.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Machine-readable coverage",
                "",
                f"- Total facts: {len(facts)}",
                f"- Primary facts: {sum(f['inferential_role'] == 'primary' for f in facts)}",
                f"- Secondary descriptive facts: {sum(f['inferential_role'] == 'secondary_descriptive' for f in facts)}",
                f"- Diagnostic facts: {sum(f['inferential_role'] == 'diagnostic' for f in facts)}",
                "",
                "The JSON artifact is authoritative; this Markdown file is a compact review view.",
            ]
        )
    return "\n".join(lines) + "\n"


def run(
    *,
    statistics_root: Path,
    audit_path: Path,
    output_dir: Path,
    allow_real_data: bool,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    inputs = [statistics_root / name for name in SOURCE_FILES]
    inputs.extend([statistics_root / "stage_manifest.json", audit_path])
    require_real_data_authorization([statistics_root, audit_path], allow_real_data)
    for path in inputs:
        if not path.is_file():
            raise GateError(f"result-fact input is missing: {path}")
    summary = read_json(statistics_root / "statistics_summary.json")
    manifest = read_json(statistics_root / "stage_manifest.json")
    audit = read_json(audit_path)
    if summary.get("status") != "PASS":
        raise GateError("Stage 13 statistics summary is not PASS")
    if manifest.get("status") != "complete":
        raise GateError("Stage 13 manifest is not complete")
    if audit.get("status") != "PASS_INTERNAL_STATISTICS_AUDIT":
        raise GateError("internal statistics audit is not PASS")
    if summary.get("paper_integration_authorized") is not False:
        raise GateError("paper integration must remain disabled")
    plan = {
        "schema_version": "atobench.results_fact_export_plan.v1",
        "status": "READY",
        "statistics_status": summary["status"],
        "audit_status": audit["status"],
        "paper_integration_authorized": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    if dry_run:
        return plan

    facts = [
        *_distribution_facts(statistics_root, "pooled_by_aou.csv"),
        *_distribution_facts(statistics_root, "per_model_by_aou.csv"),
        *_capability_facts(statistics_root),
        *_availability_facts(statistics_root),
        *_transition_facts(statistics_root),
        *_score_facts(statistics_root),
        *_descriptor_facts(statistics_root),
    ]
    ids = [fact["fact_id"] for fact in facts]
    if len(ids) != len(set(ids)):
        raise GateError("result-fact IDs are not unique")
    cohort = {
        key: summary[key]
        for key in (
            "pair_count",
            "aou_count",
            "model_count",
            "complete_state_pair_count",
            "capability_target_pair_count",
            "capability_observed_outcome_pair_count",
            "capability_missing_outcome_pair_count",
        )
    }
    if cohort["pair_count"] != audit["structural_checks"]["frozen_pair_count"]:
        raise GateError("Stage 13 summary and audit pair counts differ")
    payload = {
        "schema_version": "atobench.verification_resilience.results_facts.v1",
        "created_at": utc_now(),
        "status": "INTERNAL_FACTS_EXPORTED_PENDING_STAGE_15_QA",
        "cohort": cohort,
        "upstream": {
            "statistics_status": summary["status"],
            "audit_status": audit["status"],
            "statistics_root": str(statistics_root.resolve()),
            "audit_path": str(audit_path.resolve()),
        },
        "fact_count": len(facts),
        "fact_counts_by_role": {
            role: sum(fact["inferential_role"] == role for fact in facts)
            for role in ("primary", "secondary_descriptive", "diagnostic")
        },
        "global_interpretation_boundaries": {
            "cross_aou_pooled_primary_estimate": False,
            "cross_dimension_composite": False,
            "primary_p_values": False,
            "model_ranking_authorized": False,
            "state_unavailable_coerced_to_failure": False,
            "stop_decision_numeric_is_primary": False,
            "paper_integration_authorized": False,
        },
        "facts": facts,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    ensure_output_available(output_dir, new_version)
    json_path = output_dir / "results_facts.json"
    markdown_path = output_dir / "results_facts.md"
    report_path = output_dir / "VERIFICATION_RESILIENCE_REPORT.md"
    write_json(json_path, payload)
    markdown_path.write_text(_markdown(payload, report=False), encoding="utf-8")
    report_path.write_text(_markdown(payload, report=True), encoding="utf-8")
    result = {
        "schema_version": "atobench.results_fact_export_summary.v1",
        "status": payload["status"],
        "fact_count": len(facts),
        "fact_counts_by_role": payload["fact_counts_by_role"],
        "paper_integration_authorized": False,
        "model_calls_made": 0,
        "network_accessed": False,
    }
    output_manifest = stage_manifest(
        "14_export_results_facts",
        inputs,
        [json_path, markdown_path, report_path],
        "complete",
        dry_run=False,
        details=result,
    )
    write_json(output_dir / "stage_manifest.json", output_manifest)
    return result


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--statistics-root", type=Path, required=True)
    parser.add_argument("--statistics-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-real-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(
            statistics_root=args.statistics_root,
            audit_path=args.statistics_audit,
            output_dir=args.output,
            allow_real_data=args.allow_real_data,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0
