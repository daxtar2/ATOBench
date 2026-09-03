from __future__ import annotations

import json
from pathlib import Path

import pytest

from atobench_vr.common import GateError, load_jsonl
from atobench_vr.learning_data import export_learning_data


ROOT = Path(__file__).resolve().parents[1]
COHORT = ROOT / "reference" / "latest" / "cohort"

pytestmark = pytest.mark.skipif(
    not COHORT.exists(),
    reason="reference dataset is not shipped with this release; regenerate via export scripts",
)


def _export(tmp_path: Path) -> tuple[dict, Path]:
    output = tmp_path / "learning_data"
    result = export_learning_data(
        pair_profiles_path=COHORT / "pair_resilience_profiles.jsonl",
        episode_states_path=COHORT / "unblinded_episode_states.jsonl",
        fact_registry_path=COHORT / "episode_fact_registry.jsonl",
        output_dir=output,
        target_id="juice-shop",
        target_snapshot_id="stage20-reference",
        allow_real_data=True,
        dry_run=False,
        new_version=False,
    )
    return result, output


def test_reference_learning_data_export_is_complete_and_fail_closed(
    tmp_path: Path,
) -> None:
    result, output = _export(tmp_path)
    assert result["status"] == "CANDIDATE_EXPORT_NOT_RL_READY"
    assert result["rl_ready"] is False
    assert result["counts"]["episode_records"] == 450
    assert result["counts"]["process_label_records"] == 8170
    assert result["counts"]["counterfactual_pair_records"] == 225
    assert result["counts"]["all_records"] == 8845
    audit = json.loads((output / "export_audit.json").read_text())
    assert audit["status"] == "PASS"
    assert audit["absolute_path_leakage_count"] == 0
    assert audit["residual_sensitive_record_count"] == 0
    assert audit["residual_sensitive_kind_counts"] == {}
    assert audit["split_group_leakage_count"] == 0
    assert audit["hidden_chain_of_thought_included"] is False
    assert audit["numeric_reward_assigned"] is False
    quality = json.loads((output / "quality_summary.json").read_text())
    assert quality["status"] == "PASS_CANDIDATE_QUALITY_AUDIT"
    assert quality["preference_candidate_distribution"]["pair_count"] == 102
    assert quality["process_label_distribution"]["verifiable_candidates_by_aou"]
    with pytest.raises(GateError, match="refusing to overwrite"):
        export_learning_data(
            pair_profiles_path=COHORT / "pair_resilience_profiles.jsonl",
            episode_states_path=COHORT / "unblinded_episode_states.jsonl",
            fact_registry_path=COHORT / "episode_fact_registry.jsonl",
            output_dir=output,
            target_id="juice-shop",
            target_snapshot_id="stage20-reference",
            allow_real_data=True,
            dry_run=False,
            new_version=False,
        )


def test_split_group_keeps_all_models_and_pair_members_together(
    tmp_path: Path,
) -> None:
    _, output = _export(tmp_path)
    episodes = load_jsonl(output / "episode_records.jsonl")
    pairs = load_jsonl(output / "counterfactual_pair_records.jsonl")
    split_by_episode = {
        row["identity"]["episode_id"]: row["split"] for row in episodes
    }
    group_splits: dict[str, set[str]] = {}
    for row in episodes:
        group_splits.setdefault(row["split"]["group_id"], set()).add(
            row["split"]["name"]
        )
    assert all(len(values) == 1 for values in group_splits.values())
    for row in pairs:
        split = row["split"]["name"]
        assert row["training"]["direct_dpo_ready"] is False
        assert row["training"]["condition_confounded"] is True
        assert split_by_episode[row["identity"]["c0_episode_id"]]["name"] == split
        assert split_by_episode[row["identity"]["c1_episode_id"]]["name"] == split


def test_process_candidates_are_environment_verified_only(tmp_path: Path) -> None:
    _, output = _export(tmp_path)
    rows = load_jsonl(output / "process_label_records.jsonl")
    candidates = [
        row for row in rows if row["training"]["process_supervision_candidate"]
    ]
    assert candidates
    assert all(
        row["label"]["provenance_tier"] == "environment_verified"
        and row["label"]["measurement_status"] in {"positive", "negative"}
        for row in candidates
    )
    assert all(row["training"]["numeric_reward_assigned"] is False for row in rows)
