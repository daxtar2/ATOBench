from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from atobench_vr.common import GateError
from atobench_vr.judge_episode import DIMENSIONS
from atobench_vr.judge_pilot import (
    EXPECTED_EPISODES,
    EXPECTED_PACKETS,
    _agreement_summary,
    _stop_decision_descriptor_projection,
    _load_frozen_plan,
)


class JudgePilotTests(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        rows = []
        for episode_index in range(EXPECTED_EPISODES):
            episode = f"EP-{episode_index:02d}"
            for dimension in DIMENSIONS:
                packet_id = f"pkt-{episode_index:02d}-{dimension}"
                relative = f"{dimension}/{packet_id}"
                packet_dir = root / relative
                packet_dir.mkdir(parents=True)
                (packet_dir / "packet.json").write_text(
                    json.dumps(
                        {
                            "packet_id": packet_id,
                            "episode_pseudonym": episode,
                            "dimension": dimension,
                        }
                    )
                )
                rows.append(
                    {
                        "episode_pseudonym": episode,
                        "dimension": dimension,
                        "packet_id": packet_id,
                        "relative_path": relative,
                    }
                )
        path = root / "plan.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def test_frozen_plan_requires_twelve_complete_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self._fixture(root)
            rows = _load_frozen_plan(plan, root)
            self.assertEqual(len(rows), EXPECTED_PACKETS)
            self.assertEqual(rows[0]["dimension"], DIMENSIONS[0])

    def test_frozen_plan_rejects_missing_packet(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self._fixture(root)
            lines = plan.read_text().splitlines()
            plan.write_text("\n".join(lines[:-1]) + "\n")
            with self.assertRaises(GateError):
                _load_frozen_plan(plan, root)

    def test_agreement_summary(self) -> None:
        rows = [
            {
                "exact_score_agreement": False,
                "within_one_point": True,
                "band_agreement": True,
                "verifier_statuses": ["entailed", "partially_entailed"],
                "adjudication_trigger": "review_assertion_not_fully_entailed",
            },
            {
                "exact_score_agreement": True,
                "within_one_point": True,
                "band_agreement": True,
                "verifier_statuses": ["entailed", "entailed"],
                "adjudication_trigger": "exact_verified_consensus",
            },
        ]
        summary = _agreement_summary(rows)
        self.assertEqual(summary["within_one_point_rate"], 1.0)
        self.assertEqual(summary["exact_score_agreement_rate"], 0.5)
        self.assertEqual(summary["verifier_status_counts"]["entailed"], 3)

    def test_stop_decision_descriptor_projection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plan = self._fixture(root)
            rows = _load_frozen_plan(plan, root)
            for row in rows:
                packet_dir = root / row["relative_path"]
                packet = json.loads((packet_dir / "packet.json").read_text())
                packet["aou"] = (
                    "basket"
                    if row["dimension"] == "stop_decision"
                    else "basket"
                )
                (packet_dir / "packet.json").write_text(json.dumps(packet))
                evidence_dir = packet_dir / "packet_evidence"
                evidence_dir.mkdir(exist_ok=True)
                if row["dimension"] == "stop_decision":
                    (evidence_dir / "facts.json").write_text(
                        json.dumps(
                            [
                                {
                                    "fact_id": "F1",
                                    "fact_class": "verification",
                                    "fact_type": "BASKET_T5_DIRECT_AUTHZ_EVIDENCE",
                                    "measurement_status": "positive",
                                    "value": True,
                                }
                            ]
                        )
                    )
                    (evidence_dir / "stop_context.json").write_text(
                        json.dumps(
                            {
                                "explicit_stop_reason_present": True,
                                "explicit_conflict_count": 0,
                                "explicit_uncertainty_count": 0,
                                "unavailable_verification_fact_count": 0,
                            }
                        )
                    )
            projection = _stop_decision_descriptor_projection(rows, root)
            self.assertEqual(projection["packet_count"], EXPECTED_EPISODES)
            self.assertEqual(
                projection["readiness_state_counts"]["ready_supported"],
                EXPECTED_EPISODES,
            )
            self.assertEqual(
                projection["stop_fit_counts"]["matched_positive_readiness"],
                EXPECTED_EPISODES,
            )


if __name__ == "__main__":
    unittest.main()
