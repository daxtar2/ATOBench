from __future__ import annotations

import json
from pathlib import Path

import pytest

from atobench_vr.common import GateError, load_jsonl, write_jsonl
from atobench_vr.counterfactual_trajectories import (
    OBJECT_FILES,
    VIEW_FILES,
    export_counterfactual_trajectories,
)


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "reference" / "latest"

pytestmark = pytest.mark.skipif(
    not REFERENCE.exists(),
    reason="reference dataset is not shipped with this release; regenerate via export scripts",
)


def _subset(tmp_path: Path) -> tuple[Path, Path, Path, Path, int]:
    pair = load_jsonl(
        REFERENCE / "learning_data_v1" / "counterfactual_pair_records.jsonl"
    )[0]
    identity = pair["identity"]
    episode_ids = {identity["c0_episode_id"], identity["c1_episode_id"]}
    episodes = [
        row
        for row in load_jsonl(
            REFERENCE / "learning_data_v1" / "episode_records.jsonl"
        )
        if row["identity"]["episode_id"] in episode_ids
    ]
    transitions = [
        row
        for row in load_jsonl(
            REFERENCE / "learning_transitions_v1" / "transition_records.jsonl"
        )
        if row["identity"]["episode_id"] in episode_ids
    ]
    process_labels = [
        row
        for row in load_jsonl(
            REFERENCE / "learning_data_v1" / "process_label_records.jsonl"
        )
        if row["identity"]["episode_id"] in episode_ids
    ]
    paths = (
        tmp_path / "transitions.jsonl",
        tmp_path / "episodes.jsonl",
        tmp_path / "process.jsonl",
        tmp_path / "pairs.jsonl",
    )
    for path, rows in zip(paths, (transitions, episodes, process_labels, [pair])):
        write_jsonl(path, rows)
    return *paths, len(transitions)


def _export(tmp_path: Path) -> tuple[dict, Path, int]:
    transitions, episodes, process, pairs, transition_count = _subset(tmp_path)
    output = tmp_path / "trajectory-data"
    result = export_counterfactual_trajectories(
        transitions_path=transitions,
        episodes_path=episodes,
        process_labels_path=process,
        pairs_path=pairs,
        output_dir=output,
        allow_real_data=True,
        dry_run=False,
        new_version=False,
    )
    return result, output, transition_count


def test_exports_six_objects_and_three_consumer_views(tmp_path: Path) -> None:
    result, output, transition_count = _export(tmp_path)
    assert result["status"] == "DIAGNOSTIC_DATA_PRODUCT_READY"
    assert result["training_status"] == "NO_DOWNSTREAM_EFFECT_CLAIM"
    assert result["counts"]["canonical_trajectory"] == 2
    assert result["counts"]["evidence_claim_record"] == 2
    assert result["counts"]["counterfactual_pair_record"] == 1
    assert result["counts"]["step_behavior_record"] == transition_count
    for filename in (*OBJECT_FILES.values(), *VIEW_FILES.values()):
        assert (output / filename).is_file()

    canonical = load_jsonl(output / "canonical_trajectories.jsonl")
    steps = load_jsonl(output / "step_behavior_records.jsonl")
    decisions = load_jsonl(output / "decision_failure_points.jsonl")
    evidence = load_jsonl(output / "evidence_claim_records.jsonl")
    pairs = load_jsonl(output / "counterfactual_pair_records.jsonl")
    step_ids = {row["object_id"] for row in steps}
    assert all(
        item["step_id"] in step_ids
        for trajectory in canonical
        for item in trajectory["ordered_steps"]
    )
    assert all(row["point"]["internal_reasoning_observed"] is False for row in decisions)
    assert all(
        row["evidence"]["turn_level_fact_linkage"]
        == "unavailable_in_safe_transition_v1"
        for row in evidence
    )
    assert pairs[0]["training"]["direct_preference_ready"] is False
    audit = json.loads((output / "export_audit.json").read_text())
    assert audit["status"] == "PASS"
    assert audit["absolute_path_leakage_count"] == 0
    assert audit["hidden_chain_of_thought_included"] is False
    assert audit["internal_intent_recovered"] is False


def test_export_is_fail_closed_and_requires_real_data_authorization(
    tmp_path: Path,
) -> None:
    result, output, _ = _export(tmp_path)
    assert result["status"] == "DIAGNOSTIC_DATA_PRODUCT_READY"
    transitions, episodes, process, pairs, _ = _subset(tmp_path / "second")
    with pytest.raises(GateError, match="refusing to overwrite"):
        export_counterfactual_trajectories(
            transitions_path=transitions,
            episodes_path=episodes,
            process_labels_path=process,
            pairs_path=pairs,
            output_dir=output,
            allow_real_data=True,
            dry_run=False,
            new_version=False,
        )
    with pytest.raises(GateError, match="allow-real-data"):
        export_counterfactual_trajectories(
            transitions_path=REFERENCE
            / "learning_transitions_v1"
            / "transition_records.jsonl",
            episodes_path=episodes,
            process_labels_path=process,
            pairs_path=pairs,
            output_dir=tmp_path / "blocked",
            allow_real_data=False,
            dry_run=False,
            new_version=False,
        )
