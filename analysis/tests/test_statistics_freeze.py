from __future__ import annotations

from pathlib import Path

import pytest

from atobench_vr.common import GateError, load_jsonl, write_json, write_jsonl
from atobench_vr.statistics_freeze import run


def _fixture(tmp_path: Path) -> dict[str, Path]:
    pairs = []
    episodes = []
    facts = []
    definitions = [
        ("pair-1", "grounded_verification", "grounded_verification", True),
        ("pair-2", "grounded_verification", "state_unavailable", True),
    ]
    for pair_id, c0_state, c1_state, contact in definitions:
        pair_episodes = {}
        for condition, state in (("C0", c0_state), ("C1", c1_state)):
            episode_id = f"{pair_id}-{condition}"
            pair_episodes[condition] = {
                "episode_id": episode_id,
                "verification_resolution_state": state,
            }
            observed = state != "state_unavailable"
            episodes.append(
                {
                    "episode_id": episode_id,
                    "pair_id": pair_id,
                    "condition": condition,
                    "task_evidence_endpoint": {
                        "measurement_status": "positive" if observed else "unavailable",
                        "value": True if observed else None,
                    },
                    "verification_resolution_state": state,
                }
            )
            facts.append(
                {
                    "episode_id": episode_id,
                    "fact_type": "COMMON_TARGET_CONTACT",
                    "measurement_status": (
                        "positive" if condition == "C1" and contact else "unavailable"
                    ),
                    "value": True if condition == "C1" and contact else None,
                }
            )
        pairs.append(
            {
                "pair_id": pair_id,
                "aou": "basket",
                "model": "model-1",
                "episodes": pair_episodes,
            }
        )
    paths = {
        "pairs": tmp_path / "pairs.jsonl",
        "episodes": tmp_path / "episodes.jsonl",
        "facts": tmp_path / "facts.jsonl",
        "lock": tmp_path / "lock.json",
        "output": tmp_path / "output",
    }
    write_jsonl(paths["pairs"], pairs)
    write_jsonl(paths["episodes"], episodes)
    write_jsonl(paths["facts"], facts)
    write_json(
        paths["lock"],
        {
            "freeze_status": "frozen_authorized",
            "statistics_authorized": True,
            "paper_integration_authorized": False,
            "hash_gate_enabled": False,
            "expected_structural_counts": {
                "pair_count": 2,
                "episode_count": 4,
                "complete_state_pair_count": 1,
                "incomplete_state_pair_count": 1,
                "c0_grounded_pair_count": 2,
                "c1_anchor_contact_positive_pair_count": 2,
                "c1_anchor_contact_unavailable_pair_count": 0,
                "capability_target_pair_count": 2,
                "capability_outcome_observed_pair_count": 1,
                "capability_outcome_missing_pair_count": 1,
            },
            "analysis_populations": {},
            "primary_estimands": {},
            "diagnostic_score_estimands": {},
            "stratification": {},
            "uncertainty": {},
            "prohibited_actions": [],
        },
    )
    return paths


def _run(paths: dict[str, Path]) -> dict:
    return run(
        pair_profiles_path=paths["pairs"],
        episode_states_path=paths["episodes"],
        fact_registry_path=paths["facts"],
        output_dir=paths["output"],
        lock_path=paths["lock"],
        allow_real_data=False,
        dry_run=False,
        new_version=False,
    )


def test_freezes_exact_population_membership_without_statistics(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    result = _run(paths)
    assert result["status"] == "PASS_FROZEN"
    assert result["complete_state_pair_count"] == 1
    assert result["capability_target_pair_count"] == 2
    assert result["capability_outcome_observed_pair_count"] == 1
    assert result["capability_outcome_missing_pair_count"] == 1
    assert result["statistics_performed"] is False
    rows = load_jsonl(paths["output"] / "pair_analysis_population_membership.jsonl")
    assert rows[0]["effect_estimate_computed"] is False


def test_blocks_population_drift(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    lock = __import__("json").loads(paths["lock"].read_text())
    lock["expected_structural_counts"]["complete_state_pair_count"] = 2
    write_json(paths["lock"], lock)
    with pytest.raises(GateError, match="complete_state_pair_count"):
        _run(paths)


def test_blocks_task_state_availability_disagreement(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    rows = load_jsonl(paths["episodes"])
    rows[0]["task_evidence_endpoint"] = {
        "measurement_status": "unavailable",
        "value": None,
    }
    write_jsonl(paths["episodes"], rows)
    with pytest.raises(GateError, match="task/state availability mismatch"):
        _run(paths)
