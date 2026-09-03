from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from atobench_vr.common import GateError, write_json, write_jsonl
from atobench_vr.resilience_statistics import run


DIMENSIONS = ("verification_control", "stop_decision", "report_grounding")


def _fixture(tmp_path: Path) -> dict[str, Path]:
    pairs = []
    episodes = []
    memberships = []
    definitions = [
        ("pair-1", "grounded_verification", "grounded_verification", True, True),
        ("pair-2", "grounded_verification", "unresolved_verification", True, True),
        ("pair-3", "state_unavailable", "grounded_verification", False, False),
        ("pair-4", "grounded_verification", "state_unavailable", True, False),
    ]
    for index, (pair_id, c0_state, c1_state, target, outcome_observed) in enumerate(
        definitions, 1
    ):
        episode_map = {}
        episode_ids = {}
        for condition, state in (("C0", c0_state), ("C1", c1_state)):
            episode_id = f"{pair_id}-{condition}"
            episode_ids[condition] = episode_id
            episode_map[condition] = {
                "episode_id": episode_id,
                "verification_resolution_state": state,
            }
            dimensions = {}
            for dimension in DIMENSIONS:
                bundle_path = tmp_path / "bundles" / f"{episode_id}-{dimension}.json"
                write_json(
                    bundle_path,
                    {
                        "reviewer_outputs": {
                            "a": {"confidence": "low" if index == 1 else "medium"},
                            "b": {"confidence": "medium"},
                        },
                        "adjudication_output": None,
                    },
                )
                dimensions[dimension] = {
                    "final": {
                        "method": "reviewer_consensus",
                    },
                    "reviewer_score_difference": 3 if index == 2 else 0,
                    "source_bundle": {"path": str(bundle_path)},
                }
            episodes.append(
                {
                    "episode_id": episode_id,
                    "pair_id": pair_id,
                    "condition": condition,
                    "dimensions": dimensions,
                }
            )
        diagnostics = {}
        for dimension in DIMENSIONS:
            diagnostics[dimension] = {
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
                "c0": {
                    "score": 8,
                    "score_low": 7,
                    "score_high": 9,
                    "band": "good",
                },
                "c1": {
                    "score": 7,
                    "score_low": 6,
                    "score_high": 8,
                    "band": "good",
                },
                "conservative_delta_range": {"low": -3, "high": 1},
            }
        pairs.append(
            {
                "pair_id": pair_id,
                "aou": "basket",
                "model": "model-1",
                "episodes": episode_map,
                "verification_state_transition": {
                    "c0_state": c0_state,
                    "c1_state": c1_state,
                },
                "diagnostic_score_transitions": diagnostics,
                "stop_descriptor_transition": {
                    "c0": {
                        "readiness_state": "ready_supported",
                        "stop_fit": "matched_positive_readiness",
                    },
                    "c1": {
                        "readiness_state": "ready_not_supported",
                        "stop_fit": "matched_negative_readiness",
                    },
                },
            }
        )
        c0_observed = c0_state != "state_unavailable"
        c1_observed = c1_state != "state_unavailable"
        memberships.append(
            {
                "pair_id": pair_id,
                "aou": "basket",
                "model": "model-1",
                "c0_episode_id": episode_ids["C0"],
                "c1_episode_id": episode_ids["C1"],
                "c0_state": c0_state,
                "c1_state": c1_state,
                "c0_state_observed": c0_observed,
                "c1_state_observed": c1_observed,
                "complete_state_pair": c0_observed and c1_observed,
                "c0_grounded": c0_state == "grounded_verification",
                "c1_anchor_contact_positive": target,
                "c0_capability_target": target,
                "c0_capability_outcome_observed": target and outcome_observed,
                "c0_capability_outcome_missing": target and not outcome_observed,
                "effect_estimate_computed": False,
            }
        )
    paths = {
        "pairs": tmp_path / "pairs.jsonl",
        "episodes": tmp_path / "episodes.jsonl",
        "membership": tmp_path / "membership.jsonl",
        "registry": tmp_path / "registry.json",
        "lock": tmp_path / "lock.json",
        "output": tmp_path / "output",
    }
    write_jsonl(paths["pairs"], pairs)
    write_jsonl(paths["episodes"], episodes)
    write_jsonl(paths["membership"], memberships)
    write_json(
        paths["registry"],
        {
            "freeze_status": "frozen",
            "effect_estimates_opened": False,
        },
    )
    write_json(
        paths["lock"],
        {
            "freeze_status": "frozen_authorized",
            "statistics_authorized": True,
            "paper_integration_authorized": False,
            "expected_structural_counts": {
                "pair_count": 4,
                "episode_count": 8,
            },
            "uncertainty": {
                "bootstrap_replicates": 100,
                "confidence_level": 0.95,
                "bootstrap_seed": 7,
            },
        },
    )
    return paths


def _run(paths: dict[str, Path]) -> dict:
    return run(
        pair_profiles_path=paths["pairs"],
        episode_states_path=paths["episodes"],
        membership_path=paths["membership"],
        population_registry_path=paths["registry"],
        output_dir=paths["output"],
        lock_path=paths["lock"],
        allow_real_data=False,
        dry_run=False,
        new_version=False,
    )


def test_runs_frozen_missingness_aware_statistics(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    summary = _run(paths)
    assert summary["status"] == "PASS"
    assert summary["pair_count"] == 4
    assert summary["complete_state_pair_count"] == 2
    assert summary["capability_target_pair_count"] == 3
    assert summary["capability_observed_outcome_pair_count"] == 2
    assert summary["capability_missing_outcome_pair_count"] == 1
    with (paths["output"] / "capability_conditioned.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    aou = next(row for row in rows if row["scope"] == "aou")
    assert float(aou["observed_grounded_retention"]) == 0.5
    assert float(aou["observed_grounded_retention_exact_ci_low"]) < 0.5
    assert float(aou["observed_grounded_retention_exact_ci_high"]) > 0.5
    assert float(aou["retention_worst_case_bound"]) == pytest.approx(1 / 3)
    assert float(aou["retention_best_case_bound"]) == pytest.approx(2 / 3)
    tests = json.loads((paths["output"] / "statistical_tests.json").read_text())
    assert tests["p_values_computed"] is False


def test_blocks_membership_after_effect_opening(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    rows = [
        json.loads(line)
        for line in paths["membership"].read_text().splitlines()
        if line
    ]
    rows[0]["effect_estimate_computed"] = True
    write_jsonl(paths["membership"], rows)
    with pytest.raises(GateError, match="already opened effect"):
        _run(paths)


def test_preserves_null_score_as_dimension_specific_missingness(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    pairs = [
        json.loads(line) for line in paths["pairs"].read_text().splitlines() if line
    ]
    transition = pairs[0]["diagnostic_score_transitions"]["verification_control"]
    transition["c0"]["score"] = None
    transition["c0"]["band"] = None
    transition["conservative_delta_range"] = {"low": None, "high": None}
    write_jsonl(paths["pairs"], pairs)
    _run(paths)
    with (paths["output"] / "diagnostic_score_statistics.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    row = next(
        value
        for value in rows
        if value["scope"] == "aou"
        and value["dimension"] == "verification_control"
    )
    assert int(row["pair_n"]) == 4
    assert int(row["c0_numeric_n"]) == 3
    assert int(row["c1_numeric_n"]) == 4
    assert int(row["paired_numeric_n"]) == 3


def test_preserves_unavailable_disagreement_status_in_sensitivity(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    episodes = [
        json.loads(line)
        for line in paths["episodes"].read_text().splitlines()
        if line
    ]
    episodes[0]["dimensions"]["verification_control"][
        "reviewer_score_difference"
    ] = None
    write_jsonl(paths["episodes"], episodes)
    _run(paths)
    with (paths["output"] / "judge_uncertainty_sensitivity.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    row = next(
        value
        for value in rows
        if value["scope"] == "aou"
        and value["dimension"] == "verification_control"
        and value["sensitivity"] == "exclude_major_disagreement_pairs"
    )
    assert int(row["excluded_disagreement_status_unavailable_n"]) == 1


def test_dry_run_does_not_open_effects(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    report = run(
        pair_profiles_path=paths["pairs"],
        episode_states_path=paths["episodes"],
        membership_path=paths["membership"],
        population_registry_path=paths["registry"],
        output_dir=paths["output"],
        lock_path=paths["lock"],
        allow_real_data=False,
        dry_run=True,
        new_version=False,
    )
    assert report["status"] == "READY"
    assert not paths["output"].exists()
