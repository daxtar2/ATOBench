from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from atobench_vr.common import GateError, write_json
from atobench_vr.result_facts import run


def _csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _fixture(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "stats"
    root.mkdir()
    state = {
        "scope": "aou",
        "aou": "basket",
        "model": "",
        "condition": "C0",
        "pair_n": 2,
        "state_observed_n": 1,
        "grounded_verification_n": 1,
        "grounded_verification_proportion": 0.5,
        "unsupported_closure_n": 0,
        "unsupported_closure_proportion": 0,
        "unreported_verification_n": 0,
        "unreported_verification_proportion": 0,
        "unresolved_verification_n": 0,
        "unresolved_verification_proportion": 0,
        "state_unavailable_n": 1,
        "state_unavailable_proportion": 0.5,
    }
    _csv(root / "pooled_by_aou.csv", [state, {**state, "condition": "C1"}])
    model_rows = [
        {**state, "scope": "model_by_aou", "model": "model-1", "condition": condition}
        for condition in ("C0", "C1")
    ]
    _csv(root / "per_model_by_aou.csv", model_rows)
    transition_rows = []
    for scope, model in (("aou", ""), ("model_by_aou", "model-1")):
        for c0 in ("grounded_verification", "unresolved_verification"):
            for c1 in ("grounded_verification", "unresolved_verification"):
                transition_rows.append(
                    {
                        "scope": scope,
                        "aou": "basket",
                        "model": model,
                        "complete_pair_n": 1,
                        "excluded_incomplete_pair_n": 1,
                        "c0_state": c0,
                        "c1_state": c1,
                        "transition_n": int(
                            c0 == "grounded_verification"
                            and c1 == "grounded_verification"
                        ),
                        "transition_proportion_of_complete": int(
                            c0 == "grounded_verification"
                            and c1 == "grounded_verification"
                        ),
                    }
                )
    _csv(root / "verification_state_transitions.csv", transition_rows)
    capability = {
        "scope": "aou",
        "aou": "basket",
        "model": "",
        "capability_target_n": 2,
        "outcome_observed_n": 1,
        "outcome_missing_n": 1,
        "outcome_missing_proportion": 0.5,
        "retained_n": 1,
        "loss_n": 0,
        "observed_grounded_retention": 1,
        "observed_grounded_retention_bootstrap_ci_low": 1,
        "observed_grounded_retention_bootstrap_ci_high": 1,
        "observed_grounded_retention_exact_ci_low": 0.025,
        "observed_grounded_retention_exact_ci_high": 1,
        "observed_ato_induced_loss": 0,
        "retention_worst_case_bound": 0.5,
        "retention_best_case_bound": 1,
        "primary_inferential_scope": True,
    }
    _csv(
        root / "capability_conditioned.csv",
        [
            capability,
            {
                **capability,
                "scope": "model_by_aou",
                "model": "model-1",
                "observed_grounded_retention_bootstrap_ci_low": "",
                "observed_grounded_retention_bootstrap_ci_high": "",
                "primary_inferential_scope": False,
            },
        ],
    )
    missing = {
        "scope": "aou",
        "aou": "basket",
        "model": "",
        "pair_n": 2,
        "c0_observed_c1_observed_n": 1,
        "c0_observed_c1_unavailable_n": 0,
        "c0_unavailable_c1_observed_n": 0,
        "c0_unavailable_c1_unavailable_n": 1,
        "c0_observed_n": 1,
        "c1_observed_n": 1,
        "c0_observed_proportion": 0.5,
        "c1_observed_proportion": 0.5,
        "paired_availability_difference_c1_minus_c0": 0,
        "paired_availability_difference_bootstrap_ci_low": 0,
        "paired_availability_difference_bootstrap_ci_high": 0,
        "discordant_n": 0,
        "c1_observed_among_discordant_n": 0,
        "c1_observed_among_discordant_proportion": "",
        "discordant_proportion_exact_ci_low": "",
        "discordant_proportion_exact_ci_high": "",
        "primary_inferential_scope": True,
    }
    _csv(
        root / "missingness_sensitivity.csv",
        [
            missing,
            {
                **missing,
                "scope": "model_by_aou",
                "model": "model-1",
                "paired_availability_difference_bootstrap_ci_low": "",
                "paired_availability_difference_bootstrap_ci_high": "",
                "primary_inferential_scope": False,
            },
        ],
    )
    scores = []
    for scope, model in (("aou", ""), ("model_by_aou", "model-1")):
        for dimension in (
            "verification_control",
            "stop_decision",
            "report_grounding",
        ):
            scores.append(
                {
                    "scope": scope,
                    "aou": "basket",
                    "model": model,
                    "dimension": dimension,
                    "pair_n": 2,
                    "c0_numeric_n": 2,
                    "c1_numeric_n": 1,
                    "paired_numeric_n": 1,
                    "excluded_from_paired_numeric_n": 1,
                    "measurement_role": (
                        "descriptor_primary_numeric_provenance"
                        if dimension == "stop_decision"
                        else "retained_numeric_diagnostic"
                    ),
                    "numeric_interpretation": (
                        "engineering_provenance_only"
                        if dimension == "stop_decision"
                        else "retained_diagnostic"
                    ),
                    "c0_median": 7,
                    "c0_q1": 7,
                    "c0_q3": 7,
                    "c0_mean_descriptive": 7,
                    "c1_median": 6,
                    "c1_q1": 6,
                    "c1_q3": 6,
                    "c1_mean_descriptive": 6,
                    "paired_median_delta": -1,
                    "paired_mean_delta": -1,
                    "paired_mean_delta_bootstrap_ci_low": -1 if scope == "aou" else "",
                    "paired_mean_delta_bootstrap_ci_high": -1 if scope == "aou" else "",
                    "c1_lower_n": 1,
                    "unchanged_n": 0,
                    "c1_higher_n": 0,
                    "probability_c1_lower": 1,
                    "primary_inferential_scope": scope == "aou",
                }
            )
    _csv(root / "diagnostic_score_statistics.csv", scores)
    descriptors = []
    for scope, model in (("aou", ""), ("model_by_aou", "model-1")):
        for descriptor in ("readiness_state", "stop_fit"):
            descriptors.append(
                {
                    "scope": scope,
                    "aou": "basket",
                    "model": model,
                    "descriptor": descriptor,
                    "pair_n": 2,
                    "c0_value": "ready_supported",
                    "c1_value": "ready_supported",
                    "transition_n": 2,
                    "descriptor_primary": True,
                }
            )
    _csv(root / "stop_descriptor_transitions.csv", descriptors)
    write_json(
        root / "statistical_tests.json",
        {"p_values_computed": False},
    )
    write_json(
        root / "statistics_summary.json",
        {
            "status": "PASS",
            "pair_count": 2,
            "aou_count": 1,
            "model_count": 1,
            "complete_state_pair_count": 1,
            "capability_target_pair_count": 2,
            "capability_observed_outcome_pair_count": 1,
            "capability_missing_outcome_pair_count": 1,
            "paper_integration_authorized": False,
        },
    )
    write_json(root / "stage_manifest.json", {"status": "complete"})
    audit = tmp_path / "audit.json"
    write_json(
        audit,
        {
            "status": "PASS_INTERNAL_STATISTICS_AUDIT",
            "structural_checks": {"frozen_pair_count": 2},
        },
    )
    return {"root": root, "audit": audit, "output": tmp_path / "facts"}


class ResultFactTests(unittest.TestCase):
    def test_exports_auditable_primary_and_model_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _fixture(Path(tmp))
            summary = run(
                statistics_root=paths["root"],
                audit_path=paths["audit"],
                output_dir=paths["output"],
                allow_real_data=False,
                dry_run=False,
                new_version=False,
            )
            self.assertEqual(
                summary["status"],
                "INTERNAL_FACTS_EXPORTED_PENDING_STAGE_15_QA",
            )
            payload = json.loads(
                (paths["output"] / "results_facts.json").read_text()
            )
            self.assertFalse(
                payload["global_interpretation_boundaries"][
                    "model_ranking_authorized"
                ]
            )
            model_fact = next(
                fact
                for fact in payload["facts"]
                if fact["family"] == "capability_conditioned_retention"
                and fact["scope"] == "model_by_aou"
            )
            self.assertEqual(
                model_fact["inferential_role"], "secondary_descriptive"
            )
            self.assertEqual(model_fact["population"]["outcome_missing_n"], 1)
            self.assertEqual(
                model_fact["uncertainty"]["exact_binomial_ci"], [0.025, 1.0]
            )
            self.assertFalse(model_fact["paper_facing_authorized"])
            self.assertTrue((paths["output"] / "results_facts.md").is_file())
            self.assertTrue(
                (paths["output"] / "VERIFICATION_RESILIENCE_REPORT.md").is_file()
            )

    def test_blocks_unreconciled_capability_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _fixture(Path(tmp))
            with (paths["root"] / "capability_conditioned.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["retained_n"] = "2"
            _csv(paths["root"] / "capability_conditioned.csv", rows)
            with self.assertRaisesRegex(
                GateError, "capability counts do not reconcile"
            ):
                run(
                    statistics_root=paths["root"],
                    audit_path=paths["audit"],
                    output_dir=paths["output"],
                    allow_real_data=False,
                    dry_run=False,
                    new_version=False,
                )

    def test_dry_run_does_not_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _fixture(Path(tmp))
            plan = run(
                statistics_root=paths["root"],
                audit_path=paths["audit"],
                output_dir=paths["output"],
                allow_real_data=False,
                dry_run=True,
                new_version=False,
            )
            self.assertEqual(plan["status"], "READY")
            self.assertFalse(paths["output"].exists())


if __name__ == "__main__":
    unittest.main()
