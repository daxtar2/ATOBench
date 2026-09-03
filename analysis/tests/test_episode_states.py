from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.common import GateError, write_json, write_jsonl
from atobench_vr.episode_states import run


class EpisodeStateTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        packets_root = root / "packets"
        rows = []
        for index, dimension in enumerate(
            ("verification_control", "stop_decision", "report_grounding"),
            1,
        ):
            packet_id = f"pkt_{index}"
            relative_path = f"{dimension}/{packet_id}"
            packet_dir = packets_root / relative_path
            packet = {
                "packet_id": packet_id,
                "episode_pseudonym": "EP_1",
                "aou": "basket",
                "dimension": dimension,
            }
            write_json(packet_dir / "packet.json", packet)
            if dimension == "stop_decision":
                write_json(
                    packet_dir / "packet_evidence" / "facts.json",
                    [
                        {
                            "fact_id": "F0001",
                            "fact_type": "BASKET_T5_DIRECT_AUTHZ_EVIDENCE",
                            "fact_class": "verification",
                            "measurement_status": "positive",
                            "value": True,
                        }
                    ],
                )
                write_json(
                    packet_dir / "packet_evidence" / "stop_context.json",
                    {
                        "explicit_stop_reason_present": True,
                        "explicit_conflict_count": 0,
                        "explicit_uncertainty_count": 0,
                        "unavailable_verification_fact_count": 0,
                    },
                )
            rows.append(
                {
                    "schema_version": "atobench.validated_judgment_index_row.v1",
                    "packet_id": packet_id,
                    "episode_pseudonym": "EP_1",
                    "aou": "basket",
                    "dimension": dimension,
                    "relative_path": relative_path,
                    "bundle_path": str(root / f"{packet_id}.json"),
                    "bundle_sha256": f"hash-{packet_id}",
                    "final": {
                        "method": "reviewer_consensus",
                        "score": 7.0,
                        "score_low": 6,
                        "score_high": 8,
                        "band": "good",
                        "insufficient_evidence": False,
                    },
                    "reviewer_scores": {
                        "trajectory_judge_a": 7,
                        "trajectory_judge_b": 7,
                    },
                    "reviewer_bands": {
                        "trajectory_judge_a": "good",
                        "trajectory_judge_b": "good",
                    },
                    "reviewer_score_difference": 0,
                    "verifier_statuses": {
                        "trajectory_judge_a": "entailed",
                        "trajectory_judge_b": "entailed",
                    },
                    "adjudication_triggered": False,
                    "adjudication_trigger": "exact_verified_consensus",
                    "invocation_count": 4,
                    "runner_warning_count": 0,
                }
            )
        index_path = root / "validated_judgment_index.jsonl"
        write_jsonl(index_path, rows)
        lock_path = root / "lock.json"
        write_json(
            lock_path,
            {
                "freeze_status": "frozen",
                "offline_result_construction_authorized": True,
                "judge_calls_authorized": False,
            },
        )
        return index_path, packets_root, lock_path

    def test_builds_blinded_episode_state_with_stop_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index_path, packets_root, lock_path = self._fixture(root)
            output = root / "output"
            report = run(
                validated_index_path=index_path,
                packets_root=packets_root,
                output_dir=output,
                lock_path=lock_path,
                expected_episodes=1,
                allow_real_data=False,
                dry_run=False,
                new_version=False,
            )
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["episode_count"], 1)
            state = json.loads(
                (output / "episode_states.jsonl").read_text().splitlines()[0]
            )
            self.assertTrue(state["identity_blinded"])
            self.assertFalse(state["contains_model_condition_or_pair_identity"])
            self.assertNotIn("model", state)
            self.assertNotIn("condition", state)
            self.assertEqual(
                state["dimensions"]["stop_decision"]["measurement_role"],
                "descriptor_primary_numeric_provenance",
            )
            self.assertEqual(
                state["dimensions"]["stop_decision"]["descriptor"][
                    "derived_descriptors"
                ]["readiness_state"],
                "ready_supported",
            )
            self.assertEqual(
                state["dimensions"]["verification_control"]["measurement_role"],
                "retained_numeric_diagnostic",
            )

    def test_requires_all_three_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index_path, packets_root, lock_path = self._fixture(root)
            rows = [
                json.loads(line)
                for line in index_path.read_text().splitlines()
                if '"report_grounding"' not in line
            ]
            write_jsonl(index_path, rows)
            with self.assertRaises(GateError):
                run(
                    validated_index_path=index_path,
                    packets_root=packets_root,
                    output_dir=root / "output",
                    lock_path=lock_path,
                    expected_episodes=1,
                    allow_real_data=False,
                    dry_run=False,
                    new_version=False,
                )


if __name__ == "__main__":
    unittest.main()
