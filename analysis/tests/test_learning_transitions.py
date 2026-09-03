from __future__ import annotations

import json
from pathlib import Path

import pytest

from atobench_vr.common import GateError, load_jsonl, write_jsonl
from atobench_vr.learning_transitions import export_learning_transitions


ROOT = Path(__file__).resolve().parents[1]
COHORT = ROOT / "reference" / "latest" / "cohort"

pytestmark = pytest.mark.skipif(
    not COHORT.exists(),
    reason="reference dataset is not shipped with this release; regenerate via export scripts",
)


def _source_subset(tmp_path: Path) -> tuple[Path, Path, Path]:
    pair = load_jsonl(COHORT / "pair_resilience_profiles.jsonl")[0]
    episode_ids = {
        pair["episodes"]["C0"]["episode_id"],
        pair["episodes"]["C1"]["episode_id"],
    }
    episodes = [
        row
        for row in load_jsonl(COHORT / "unblinded_episode_states.jsonl")
        if row["episode_id"] in episode_ids
    ]
    pair_path = tmp_path / "pairs.jsonl"
    episode_path = tmp_path / "episodes.jsonl"
    write_jsonl(pair_path, [pair])
    write_jsonl(episode_path, episodes)
    nodes = []
    for episode in episodes:
        common = {
            "episode_id": episode["episode_id"],
            "pair_id": episode["pair_id"],
            "aou": episode["aou"],
            "condition": episode["condition"],
            "model": episode["model"],
            "graph_version": "test-graph",
            "turn_idx": 1,
            "source": {
                "parse_status": "ok",
                "parser_version": "test-parser",
                "raw_file_sha256": "f" * 64,
                "raw_source_pointer": "/absolute/path/must-not-leak.jsonl:1",
            },
        }
        nodes.extend(
            [
                {
                    **common,
                    "node_id": f"{episode['episode_id']}::1::action::0",
                    "node_type": "action",
                    "data": {
                        "tool": "http",
                        "method": "GET",
                        "endpoint_family": "catalog",
                        "route_template": "/api/items",
                        "request_body_type": "none",
                        "request_typed_values": [
                            {"value_hash": "do-not-export"}
                        ],
                        "raw_path_sha256": "do-not-export",
                    },
                },
                {
                    **common,
                    "node_id": f"{episode['episode_id']}::1::visible::0",
                    "node_type": "visible_observation",
                    "data": {
                        "status_code": 200,
                        "visible_body_type": "json",
                        "visible_body_schema": {"type": "object"},
                        "visible_typed_values": [
                            {"value_hash": "do-not-export"}
                        ],
                    },
                },
                {
                    **common,
                    "node_id": f"{episode['episode_id']}::1::evidence::0",
                    "node_type": "evidence",
                    "data": {"claim_id": "do-not-export", "polarity": "support", "role": "observation", "scope": "turn"},
                },
                {
                    **common,
                    "node_id": f"{episode['episode_id']}::1::intervention::0",
                    "node_type": "intervention",
                    "data": {
                        "event_id": "do-not-export",
                        "rule_id": "do-not-export",
                        "is_target_aou_contact": episode["condition"] == "C1",
                        "layer": "response",
                        "operation": "replace",
                        "primitive": "test",
                        "status": "applied",
                        "target_dims": ["response"],
                        "target_kind": "observation",
                    },
                },
            ]
        )
    graph_path = tmp_path / "graph_nodes.jsonl"
    write_jsonl(graph_path, nodes)
    return graph_path, pair_path, episode_path


def test_transition_export_is_structural_and_fail_closed(tmp_path: Path) -> None:
    graph_path, pair_path, episode_path = _source_subset(tmp_path)
    output = tmp_path / "transitions"
    result = export_learning_transitions(
        graph_nodes_path=graph_path,
        pair_profiles_path=pair_path,
        episode_states_path=episode_path,
        output_dir=output,
        target_id="juice-shop",
        target_snapshot_id="test-reference",
        allow_real_data=True,
        dry_run=False,
        new_version=False,
    )
    assert result["status"] == "CANDIDATE_EXPORT_NOT_DIRECT_RL_READY"
    assert result["counts"]["transition_records"] == 2
    rows = load_jsonl(output / "transition_records.jsonl")
    assert all(row["training"]["direct_offline_rl_ready"] is False for row in rows)
    rendered = json.dumps(rows, sort_keys=True)
    assert "do-not-export" not in rendered
    assert "/absolute/path" not in rendered
    audit = json.loads((output / "export_audit.json").read_text())
    assert audit["status"] == "PASS"
    assert audit["request_and_response_values_included"] is False
    assert audit["identity_and_resource_values_included"] is False
    with pytest.raises(GateError, match="refusing to overwrite"):
        export_learning_transitions(
            graph_nodes_path=graph_path,
            pair_profiles_path=pair_path,
            episode_states_path=episode_path,
            output_dir=output,
            target_id="juice-shop",
            target_snapshot_id="test-reference",
            allow_real_data=True,
            dry_run=False,
            new_version=False,
        )


def test_transition_export_requires_explicit_real_data_authorization(
    tmp_path: Path,
) -> None:
    graph_path, pair_path, episode_path = _source_subset(tmp_path)
    with pytest.raises(GateError, match="allow-real-data"):
        export_learning_transitions(
            graph_nodes_path=graph_path,
            pair_profiles_path=COHORT / "pair_resilience_profiles.jsonl",
            episode_states_path=episode_path,
            output_dir=tmp_path / "transitions",
            target_id="juice-shop",
            target_snapshot_id="test-reference",
            allow_real_data=False,
            dry_run=False,
            new_version=False,
        )
