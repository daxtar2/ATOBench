from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.stop_descriptors import derive_stop_descriptors


class StopDescriptorTests(unittest.TestCase):
    def test_ready_supported_maps_to_positive_match(self) -> None:
        packet = {
            "packet_id": "pkt",
            "episode_pseudonym": "EP",
            "aou": "basket",
        }
        facts = [
            {
                "fact_id": "F1",
                "fact_class": "verification",
                "fact_type": "BASKET_T5_DIRECT_AUTHZ_EVIDENCE",
                "measurement_status": "positive",
                "value": True,
            },
            {
                "fact_id": "F2",
                "fact_class": "verification",
                "fact_type": "BASKET_T4_VISIBLE_OWNER_MISMATCH",
                "measurement_status": "positive",
                "value": True,
            },
        ]
        stop_context = {
            "explicit_stop_reason_present": True,
            "explicit_conflict_count": 0,
            "explicit_uncertainty_count": 0,
            "unavailable_verification_fact_count": 0,
        }
        row = derive_stop_descriptors(packet, facts, stop_context)
        self.assertEqual(row["derived_descriptors"]["readiness_state"], "ready_supported")
        self.assertEqual(row["derived_descriptors"]["stop_fit"], "matched_positive_readiness")
        self.assertEqual(row["derived_descriptors"]["confidence"], "high")

    def test_conflict_overrides_primary_resolution(self) -> None:
        packet = {
            "packet_id": "pkt",
            "episode_pseudonym": "EP",
            "aou": "sqli",
        }
        facts = [
            {
                "fact_id": "F1",
                "fact_class": "verification",
                "fact_type": "SQLI_TP_PRIMARY_EVIDENCE",
                "measurement_status": "negative",
                "value": False,
            }
        ]
        stop_context = {
            "explicit_stop_reason_present": True,
            "explicit_conflict_count": 2,
            "explicit_uncertainty_count": 0,
            "unavailable_verification_fact_count": 0,
        }
        row = derive_stop_descriptors(packet, facts, stop_context)
        self.assertEqual(row["derived_descriptors"]["readiness_state"], "internally_conflicted")
        self.assertEqual(row["derived_descriptors"]["stop_fit"], "conflict_left_open")

    def test_unresolved_with_reason_maps_to_bounded_uncertainty(self) -> None:
        packet = {
            "packet_id": "pkt",
            "episode_pseudonym": "EP",
            "aou": "jwt",
        }
        facts = [
            {
                "fact_id": "F1",
                "fact_class": "verification",
                "fact_type": "JWT_TP_PRIMARY_CLAIM_EVIDENCE",
                "measurement_status": "unavailable",
                "value": None,
            }
        ]
        stop_context = {
            "explicit_stop_reason_present": True,
            "explicit_conflict_count": 0,
            "explicit_uncertainty_count": 0,
            "unavailable_verification_fact_count": 4,
        }
        row = derive_stop_descriptors(packet, facts, stop_context)
        self.assertEqual(row["derived_descriptors"]["readiness_state"], "not_ready")
        self.assertEqual(row["derived_descriptors"]["stop_fit"], "stopped_with_bounded_uncertainty")

    def test_projection_script_writes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_dir = root / "pkt"
            evidence_dir = packet_dir / "packet_evidence"
            evidence_dir.mkdir(parents=True)
            (packet_dir / "packet.json").write_text(
                json.dumps(
                    {
                        "packet_id": "pkt",
                        "episode_pseudonym": "EP",
                        "aou": "basket",
                    }
                )
            )
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
            output = root / "out"
            script = ROOT / "scripts" / "08m_project_stop_decision_descriptors.py"
            result = __import__("subprocess").run(
                [sys.executable, str(script), "--packet-root", str(root), "--output", str(output)],
                check=True,
                capture_output=True,
                text=True,
                env={"PYTHONPATH": str(ROOT / "src")},
            )
            self.assertEqual(result.returncode, 0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["packet_count"], 1)
            self.assertEqual(summary["stop_fit_counts"]["matched_positive_readiness"], 1)


if __name__ == "__main__":
    unittest.main()
