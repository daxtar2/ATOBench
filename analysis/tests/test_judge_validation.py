from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.common import write_json
from atobench_vr.judge_validation import run


class JudgeValidationTests(unittest.TestCase):
    def _judgment(self, packet_id: str, dimension: str) -> dict:
        return {
            "schema_version": "atobench.trajectory_judgment.v1",
            "packet_id": packet_id,
            "dimension": dimension,
            "score": 7,
            "score_low": 6,
            "score_high": 8,
            "band": "good",
            "confidence": "medium",
            "insufficient_evidence": False,
            "reason_code": "fixture",
            "material_strengths": ["supported"],
            "material_deficiencies": [],
            "supporting_message_ids": [],
            "supporting_event_ids": [],
            "supporting_fact_ids": ["F0001"],
            "forbidden_identity_not_accessed": True,
            "outside_knowledge_not_used": True,
        }

    def _fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        packets_root = root / "packets"
        judgments_root = root / "judgments"
        rows = []
        for index, dimension in enumerate(
            ("verification_control", "stop_decision", "report_grounding"),
            1,
        ):
            packet_id = f"pkt_{index}"
            relative_path = f"{dimension}/{packet_id}"
            packet_dir = packets_root / relative_path
            judgment_dir = judgments_root / relative_path
            packet = {
                "packet_id": packet_id,
                "episode_pseudonym": "EP_1",
                "aou": "basket",
                "dimension": dimension,
                "input_message_ids": [],
                "input_event_ids": [],
                "input_fact_ids": ["F0001"],
            }
            write_json(packet_dir / "packet.json", packet)
            a = self._judgment(packet_id, dimension)
            b = self._judgment(packet_id, dimension)
            verifier = {
                "status": "entailed",
                "reason": "supported",
                "verified_pointer_ids": ["F0001"],
                "missing_pointer_ids": [],
            }
            bundle = {
                "schema_version": "atobench.trajectory_judgment_bundle.v1",
                "packet_id": packet_id,
                "dimension": dimension,
                "reviewer_outputs": {
                    "trajectory_judge_a": a,
                    "trajectory_judge_b": b,
                },
                "evidence_verifier_outputs": {
                    "trajectory_judge_a": verifier,
                    "trajectory_judge_b": verifier,
                },
                "adjudication_triggered": False,
                "adjudication_trigger": "exact_verified_consensus",
                "adjudication_output": None,
                "final": {
                    "method": "reviewer_consensus",
                    "score": 7.0,
                    "score_low": 6,
                    "score_high": 8,
                    "band": "good",
                    "insufficient_evidence": False,
                },
                "invocation_count": 4,
            }
            write_json(judgment_dir / "judgment_bundle.json", bundle)
            write_json(
                judgment_dir / "stage_manifest.json",
                {"status": "complete"},
            )
            for receipt_index in range(1, 5):
                write_json(
                    judgment_dir / "receipts" / f"{receipt_index:02d}.json",
                    {"receipt": receipt_index},
                )
            rows.append(
                {
                    "packet_id": packet_id,
                    "dimension": dimension,
                    "relative_path": relative_path,
                }
            )
        manifest_path = root / "packet_manifest.json"
        write_json(
            manifest_path,
            {
                "packet_count": 3,
                "dimension_counts": {
                    "verification_control": 1,
                    "stop_decision": 1,
                    "report_grounding": 1,
                },
                "packets": rows,
            },
        )
        lock_path = root / "lock.json"
        write_json(
            lock_path,
            {
                "freeze_status": "frozen",
                "offline_result_construction_authorized": True,
                "judge_calls_authorized": False,
            },
        )
        return judgments_root, manifest_path, packets_root, lock_path

    def test_validates_complete_three_dimension_episode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            judgments, manifest, packets, lock = self._fixture(root)
            output = root / "output"
            report = run(
                judgments_root=judgments,
                packet_manifest_path=manifest,
                packets_root=packets,
                output_dir=output,
                lock_path=lock,
                expected_packets=3,
                expected_episodes=1,
                allow_real_data=False,
                dry_run=False,
                new_version=False,
            )
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["validated_bundle_count"], 3)
            self.assertEqual(report["validated_episode_count"], 1)
            self.assertEqual(report["violation_count"], 0)
            rows = [
                json.loads(line)
                for line in (output / "validated_judgment_index.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertEqual(len(rows), 3)

    def test_blocks_pointer_outside_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            judgments, manifest, packets, lock = self._fixture(root)
            bundle_path = (
                judgments
                / "verification_control"
                / "pkt_1"
                / "judgment_bundle.json"
            )
            bundle = json.loads(bundle_path.read_text())
            bundle["reviewer_outputs"]["trajectory_judge_a"][
                "supporting_fact_ids"
            ] = ["F_NOT_IN_PACKET"]
            write_json(bundle_path, bundle)
            output = root / "output"
            report = run(
                judgments_root=judgments,
                packet_manifest_path=manifest,
                packets_root=packets,
                output_dir=output,
                lock_path=lock,
                expected_packets=3,
                expected_episodes=1,
                allow_real_data=False,
                dry_run=False,
                new_version=False,
            )
            self.assertEqual(report["status"], "FAIL")
            violations = [
                json.loads(line)
                for line in (output / "validation_violations.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertIn(
                "review_pointer_outside_packet",
                {row["code"] for row in violations},
            )


if __name__ == "__main__":
    unittest.main()
