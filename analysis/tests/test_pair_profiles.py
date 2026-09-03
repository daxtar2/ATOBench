from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from atobench_vr.common import GateError, load_jsonl, write_json, write_jsonl
from atobench_vr.pair_profiles import run


DIMENSIONS = ("report_grounding", "stop_decision", "verification_control")


def _dimension(dimension: str, score: float, band: str) -> dict:
    result = {
        "packet_id": "",
        "measurement_role": {
            "verification_control": "retained_numeric_diagnostic",
            "stop_decision": "descriptor_primary_numeric_provenance",
            "report_grounding": "retained_numeric_score_proximity",
        }[dimension],
        "numeric_interpretation": {
            "verification_control": "retained_diagnostic",
            "stop_decision": "engineering_provenance_only",
            "report_grounding": "retained_with_score_proximity_reliability",
        }[dimension],
        "final": {
            "score": score,
            "score_low": score - 1,
            "score_high": score + 1,
            "band": band,
            "insufficient_evidence": False,
            "method": "reviewer_consensus",
        },
    }
    if dimension == "stop_decision":
        result["descriptor"] = {
            "primary_evidence": {
                "fact_id": "F0001",
                "fact_type": "BASKET_T5_DIRECT_AUTHZ_EVIDENCE",
                "measurement_status": "positive",
                "value": True,
            },
            "derived_descriptors": {
                "readiness_state": "ready_supported",
                "stop_fit": "matched_positive_readiness",
                "reason_trace": "explicit_stop_reason_present",
                "confidence": "high",
            },
        }
    return result


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _fixture(tmp_path: Path) -> dict[str, Path]:
    states = []
    lineages = []
    episode_census = []
    for condition, score in (("C0", 7.0), ("C1", 5.0)):
        episode_id = f"episode-{condition}"
        pseudonym = f"EP_{condition}"
        source_packet_ids = {}
        dimensions = {}
        for dimension in DIMENSIONS:
            packet_id = f"pkt-{condition}-{dimension}"
            source_packet_ids[dimension] = packet_id
            dimensions[dimension] = _dimension(dimension, score, "good")
            dimensions[dimension]["packet_id"] = packet_id
            lineages.append(
                {
                    "packet_id": packet_id,
                    "episode_id": episode_id,
                    "pair_id": "pair-1",
                    "campaign_id": "campaign-1",
                    "condition": condition,
                    "model": "model-1",
                    "dimension": dimension,
                    "must_never_be_sent_to_judge": True,
                }
            )
        states.append(
            {
                "schema_version": "atobench.episode_state.v1",
                "episode_pseudonym": pseudonym,
                "aou": "basket",
                "dimensions": dimensions,
                "source_packet_ids": source_packet_ids,
                "complete": True,
                "identity_blinded": True,
                "contains_model_condition_or_pair_identity": False,
            }
        )
        episode_census.append(
            {
                "episode_id": episode_id,
                "pair_id": "pair-1",
                "global_episode_id": f"global-{condition}",
                "campaign_id": "campaign-1",
                "condition": condition,
                "model": "model-1",
                "aou": "basket",
                "unit_id": "unit-1",
                "block_id": "block-1",
                "paired_analysis_eligible": "True",
            }
        )
    pair_census = [
        {
            "pair_id": "pair-1",
            "campaign_id": "campaign-1",
            "model": "model-1",
            "aou": "basket",
            "unit_id": "unit-1",
            "block_id": "block-1",
            "pair_complete": "True",
            "execution_valid_pair": "True",
        }
    ]
    paths = {
        "states": tmp_path / "episode_states.jsonl",
        "lineage": tmp_path / "packet_lineage.jsonl",
        "pairs": tmp_path / "pair_census.csv",
        "episodes": tmp_path / "episode_census.csv",
        "lock": tmp_path / "lock.yaml",
        "output": tmp_path / "output",
    }
    write_jsonl(paths["states"], states)
    write_jsonl(paths["lineage"], lineages)
    _write_csv(paths["pairs"], pair_census)
    _write_csv(paths["episodes"], episode_census)
    write_json(
        paths["lock"],
        {
            "freeze_status": "frozen",
            "offline_result_construction_authorized": True,
            "judge_calls_authorized": False,
        },
    )
    return paths


def _run(paths: dict[str, Path]) -> dict:
    return run(
        episode_states_path=paths["states"],
        packet_lineage_path=paths["lineage"],
        pair_census_path=paths["pairs"],
        episode_census_path=paths["episodes"],
        output_dir=paths["output"],
        lock_path=paths["lock"],
        expected_pairs=1,
        expected_episodes=2,
        allow_real_data=False,
        dry_run=False,
        new_version=False,
    )


def test_builds_exact_frozen_pair_profile(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    report = _run(paths)
    assert report["status"] == "PASS"
    assert report["pair_count"] == 1
    assert report["episode_count"] == 2
    assert report["score_transition_count"] == 3
    profiles = load_jsonl(paths["output"] / "pair_resilience_profiles.jsonl")
    assert profiles[0]["pairing_basis"] == "frozen_pair_census_exact_join"
    assert set(profiles[0]["episodes"]) == {"C0", "C1"}
    assert profiles[0]["rematching_performed"] is False


def test_numeric_delta_and_stop_descriptor_are_separated(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    _run(paths)
    scores = load_jsonl(paths["output"] / "pair_score_transitions.jsonl")
    stop = next(row for row in scores if row["dimension"] == "stop_decision")
    assert stop["score_delta_c1_minus_c0"] == -2
    assert stop["score_direction"] == "lower"
    assert stop["numeric_interpretation"] == "engineering_provenance_only"
    descriptors = load_jsonl(
        paths["output"] / "pair_stop_descriptor_transitions.jsonl"
    )
    assert descriptors[0]["descriptor_primary"] is True


def test_primary_state_remains_unavailable_without_semantic_match(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    report = _run(paths)
    assert report["primary_verification_state_transitions_ready"] is False
    assert report["primary_verification_state_blocker"] == (
        "semantic_report_match_unavailable"
    )
    assert report["state_unavailable_reason_counts"] == {
        "semantic_report_match_unavailable": 2
    }
    episodes = load_jsonl(paths["output"] / "unblinded_episode_states.jsonl")
    assert {row["verification_resolution_state"] for row in episodes} == {
        "state_unavailable"
    }
    transitions = load_jsonl(paths["output"] / "pair_state_transitions.jsonl")
    assert transitions[0]["must_not_be_interpreted_as_unresolved_verification"]


def test_blocks_cross_packet_identity_disagreement(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    rows = load_jsonl(paths["lineage"])
    rows[0]["pair_id"] = "hallucinated-pair"
    write_jsonl(paths["lineage"], rows)
    with pytest.raises(GateError, match="inconsistent pair_id"):
        _run(paths)


def test_blocks_pair_not_in_frozen_census(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    pair_rows = [
        {
            "pair_id": "different-pair",
            "campaign_id": "campaign-1",
            "model": "model-1",
            "aou": "basket",
            "unit_id": "unit-1",
            "block_id": "block-1",
            "pair_complete": "True",
            "execution_valid_pair": "True",
        }
    ]
    _write_csv(paths["pairs"], pair_rows)
    with pytest.raises(GateError, match="pair census mismatch"):
        _run(paths)


def test_semantic_matches_enable_primary_state_transition(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    states = load_jsonl(paths["states"])
    semantic_path = tmp_path / "validated_semantic_matches.jsonl"
    write_jsonl(
        semantic_path,
        [
            {
                "episode_pseudonym": state["episode_pseudonym"],
                "final": {
                    "schema_version": "atobench.report_semantic_match.v1",
                    "semantic_packet_id": f"smp-{state['episode_pseudonym']}",
                    "matcher_id": "matcher_consensus",
                    "registered_finding_mentioned": True,
                    "report_closure": True,
                    "matched_claim_atom_ids": ["A1"],
                    "claim_trace_support": "supported",
                    "supporting_fact_ids": ["F0001"],
                    "contradicting_fact_ids": [],
                    "confidence": "high",
                    "insufficient_evidence": False,
                    "reason_code": "fixture",
                    "reason": "fixture",
                    "forbidden_identity_not_accessed": True,
                    "outside_knowledge_not_used": True,
                },
            }
            for state in states
        ],
    )
    report = run(
        episode_states_path=paths["states"],
        packet_lineage_path=paths["lineage"],
        pair_census_path=paths["pairs"],
        episode_census_path=paths["episodes"],
        output_dir=tmp_path / "semantic_output",
        lock_path=paths["lock"],
        expected_pairs=1,
        expected_episodes=2,
        allow_real_data=False,
        dry_run=False,
        new_version=False,
        semantic_matches_path=semantic_path,
    )
    assert report["primary_verification_state_transitions_ready"] is True
    assert report["primary_verification_state_blocker"] is None
    assert report["state_unavailable_reason_counts"] == {}
    episodes = load_jsonl(tmp_path / "semantic_output" / "unblinded_episode_states.jsonl")
    assert {row["verification_resolution_state"] for row in episodes} == {
        "grounded_verification"
    }
