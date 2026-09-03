from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.census import build_census
from atobench_vr.sessions import (
    count_optional_sidecars,
    discover_candidate_session_trees,
    session_tree_inventory_row,
)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


class RealSchemaCensusTests(unittest.TestCase):
    def test_manifest_assignments_bind_to_raw_episode_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            campaign = Path(tmp) / "campaign"
            pair_id = "campaign::model_x::jwt::J01"
            assignments = [
                {
                    "pair_id": pair_id,
                    "model": "model-x",
                    "model_selector": "model-x",
                    "aou": "jwt",
                    "unit_id": "unit",
                    "block_id": "J01",
                    "condition": condition,
                    "slot": slot,
                }
                for condition, slot in (("C1", "J01:1"), ("C0", "J01:2"))
            ]
            campaign.mkdir()
            (campaign / "campaign_manifest.json").write_text(
                json.dumps(
                    {
                        "campaign_id": "campaign",
                        "status": "complete",
                        "planned_pairs": [
                            {
                                "pair_id": pair_id,
                                "model": "model-x",
                                "aou": "jwt",
                                "unit_id": "unit",
                                "block_id": "J01",
                            }
                        ],
                        "planned_episodes": assignments,
                        "failures": [],
                    }
                ),
                encoding="utf-8",
            )
            write_jsonl(
                campaign / "events.jsonl",
                [
                    {
                        "pair_id": pair_id,
                        "condition": condition,
                        "slot": slot,
                        "model": "model-x",
                        "aou": "jwt",
                        "status": status,
                        "returncode": 0 if status == "complete" else None,
                        "recorded_at": timestamp,
                    }
                    for condition, slot, timestamp in (
                        ("C1", "J01:1", "2026-01-01T00:00:00+00:00"),
                        ("C0", "J01:2", "2026-01-01T00:01:00+00:00"),
                    )
                    for status in ("started", "complete")
                ],
            )
            raw_path = campaign / "logs" / "model_x" / "jwt" / "episodes.jsonl"
            write_jsonl(
                raw_path,
                [
                    {
                        "episode_id": f"ep_campaign_jwt_model_x_{condition.lower()}_20260101_000000_aaaaaa",
                        "started_at": timestamp,
                        "status": "running",
                    }
                    for condition, timestamp in (
                        ("C1", "2026-01-01T00:00:05+00:00"),
                        ("C0", "2026-01-01T00:01:05+00:00"),
                    )
                ]
                + [
                    {
                        "episode_id": f"ep_campaign_jwt_model_x_{condition.lower()}_20260101_000000_aaaaaa",
                        "ended_at": timestamp,
                        "status": "ended",
                        "outcome": "success",
                        "final_report_text": condition,
                    }
                    for condition, timestamp in (
                        ("C1", "2026-01-01T00:00:50+00:00"),
                        ("C0", "2026-01-01T00:01:50+00:00"),
                    )
                ],
            )
            campaigns, pairs, episodes, errors = build_census([campaign])
        self.assertFalse(errors)
        self.assertEqual(campaigns[0]["paired_analysis_eligible_episode_count"], 2)
        self.assertTrue(pairs[0]["execution_valid_pair"])
        self.assertTrue(all(row["paired_analysis_eligible"] for row in episodes))

    def test_main_and_subagent_files_form_one_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "campaign-project"
            main_id = "11111111-1111-1111-1111-111111111111"
            episode_id = "ep_campaign_jwt_model_x_c1_20260101_000000_aaaaaa"
            main = project / f"{main_id}.jsonl"
            subagent = project / main_id / "subagents" / "agent-abc.jsonl"
            write_jsonl(main, [{"sessionId": main_id, "content": episode_id}])
            write_jsonl(subagent, [{"sessionId": main_id, "content": episode_id}])
            trees = discover_candidate_session_trees(
                Path(tmp), {episode_id}, {"campaign"}
            )
            row = session_tree_inventory_row(
                next(iter(trees)), next(iter(trees.values()))["files"]
            )
        self.assertEqual(len(trees), 1)
        self.assertEqual(row["main_session_count"], 1)
        self.assertEqual(row["subagent_ids"], ["abc"])
        self.assertEqual(row["engagement_ids"], [episode_id])

    def test_sibling_tasks_sidecar_is_optional_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            main = project / "session.jsonl"
            sidecar = project / "session" / "tasks" / "tool.output"
            write_jsonl(main, [{"sessionId": "session", "content": "inline"}])
            sidecar.parent.mkdir(parents=True)
            sidecar.write_text("large output", encoding="utf-8")
            count = count_optional_sidecars(main)
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
