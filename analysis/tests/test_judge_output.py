from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.common import GateError
from atobench_vr.judge_output import (
    canonicalize_adjudication_output,
    canonicalize_judgment,
    canonicalize_verifier_output,
    extract_structured_output,
)


class ExtractStructuredOutputTests(unittest.TestCase):
    def test_prefers_inner_object_over_envelope(self) -> None:
        inner = {
            "schema_version": "atobench.trajectory_judgment.v1",
            "packet_id": "p1",
            "score": 7,
        }
        envelope = {
            "type": "result",
            "api_error_status": None,
            "duration_api_ms": 1234,
            "session_id": "s1",
            "usage": {"input_tokens": 100},
            "structured_output": inner,
        }
        extracted, _ = extract_structured_output(json.dumps(envelope))
        self.assertEqual(extracted, inner)

    def test_extracts_from_markdown_fence(self) -> None:
        payload = {
            "schema_version": "atobench.trajectory_judgment.v1",
            "packet_id": "p1",
            "score": 7,
        }
        stdout = json.dumps({"result": f"Here is the result:\n```json\n{json.dumps(payload)}\n```"})
        extracted, _ = extract_structured_output(stdout)
        self.assertEqual(extracted, payload)

    def test_rejects_pure_envelope(self) -> None:
        envelope = {
            "api_error_status": "error",
            "duration_api_ms": 1234,
            "session_id": "s1",
        }
        with self.assertRaises(GateError):
            extract_structured_output(json.dumps(envelope))


class CanonicalizeVerifierOutputTests(unittest.TestCase):
    def test_strips_envelope_fields_and_returns_warnings(self) -> None:
        value = {
            "status": "partially_entailed",
            "reason": "ok",
            "verified_pointer_ids": ["a"],
            "missing_pointer_ids": ["b"],
            "api_error_status": None,
            "duration_api_ms": 1234,
        }
        canonical, warnings = canonicalize_verifier_output(value)
        self.assertEqual(
            set(canonical),
            {"status", "reason", "verified_pointer_ids", "missing_pointer_ids"},
        )
        self.assertTrue(any("stripped extra" in w for w in warnings))

    def test_defaults_invalid_status(self) -> None:
        value = {
            "status": "weird",
            "reason": "ok",
            "verified_pointer_ids": [],
            "missing_pointer_ids": [],
        }
        canonical, warnings = canonicalize_verifier_output(value)
        self.assertEqual(canonical["status"], "unavailable")
        self.assertTrue(any("invalid verifier status" in w for w in warnings))


class CanonicalizeAdjudicationOutputTests(unittest.TestCase):
    def test_accepts_plain_judgment_and_adds_defaults(self) -> None:
        judgment = {
            "schema_version": "atobench.trajectory_judgment.v1",
            "packet_id": "p1",
            "dimension": "verification_control",
            "score": 7,
            "score_low": 6,
            "score_high": 8,
            "band": "good",
            "confidence": "medium",
            "insufficient_evidence": False,
            "reason_code": "rc",
            "material_strengths": [],
            "material_deficiencies": [],
            "supporting_message_ids": ["m1"],
            "supporting_event_ids": [],
            "supporting_fact_ids": [],
            "forbidden_identity_not_accessed": True,
            "outside_knowledge_not_used": True,
        }
        packet = {"packet_id": "p1", "dimension": "verification_control"}
        canonical, warnings = canonicalize_adjudication_output(judgment, packet)
        self.assertEqual(canonical["adjudication_reason_code"], "no_reason_provided")
        self.assertEqual(canonical["resolved_review_defect_ids"], [])
        self.assertTrue(any("adjudication_reason_code missing" in w for w in warnings))

    def test_preserves_provided_metadata(self) -> None:
        judgment = {
            "schema_version": "atobench.trajectory_judgment.v1",
            "packet_id": "p1",
            "dimension": "verification_control",
            "score": 7,
            "score_low": 6,
            "score_high": 8,
            "band": "good",
            "confidence": "medium",
            "insufficient_evidence": False,
            "reason_code": "rc",
            "material_strengths": [],
            "material_deficiencies": [],
            "supporting_message_ids": ["m1"],
            "supporting_event_ids": [],
            "supporting_fact_ids": [],
            "forbidden_identity_not_accessed": True,
            "outside_knowledge_not_used": True,
            "adjudication_reason_code": "resolved",
            "resolved_review_defect_ids": ["d1"],
        }
        packet = {"packet_id": "p1", "dimension": "verification_control"}
        canonical, warnings = canonicalize_adjudication_output(judgment, packet)
        self.assertEqual(canonical["adjudication_reason_code"], "resolved")
        self.assertEqual(canonical["resolved_review_defect_ids"], ["d1"])
        self.assertFalse(warnings)


class CanonicalizeJudgmentTests(unittest.TestCase):
    def test_fills_missing_optional_fields(self) -> None:
        packet = {"packet_id": "p1", "dimension": "verification_control"}
        value = {
            "schema_version": "atobench.trajectory_judgment.v1",
            "packet_id": "p1",
            "dimension": "verification_control",
            "score": 7,
            "score_low": 7,
            "score_high": 7,
            "band": "good",
            "confidence": "medium",
            "insufficient_evidence": False,
            "reason_code": "rc",
            "material_strengths": [],
            "material_deficiencies": [],
            "supporting_message_ids": ["m1"],
            "supporting_event_ids": [],
            "supporting_fact_ids": [],
        }
        canonical, warnings = canonicalize_judgment(value, packet)
        self.assertTrue(canonical["forbidden_identity_not_accessed"])
        self.assertTrue(canonical["outside_knowledge_not_used"])
        self.assertTrue(any("forbidden_identity_not_accessed" in w for w in warnings))

    def test_missing_score_defaults_to_insufficient_evidence(self) -> None:
        packet = {"packet_id": "p1", "dimension": "verification_control"}
        value = {
            "schema_version": "atobench.trajectory_judgment.v1",
            "packet_id": "p1",
            "dimension": "verification_control",
            "score": None,
            "score_low": None,
            "score_high": None,
            "band": None,
            "confidence": "medium",
            "insufficient_evidence": False,
            "reason_code": "rc",
            "material_strengths": [],
            "material_deficiencies": [],
            "supporting_message_ids": [],
            "supporting_event_ids": [],
            "supporting_fact_ids": [],
            "forbidden_identity_not_accessed": True,
            "outside_knowledge_not_used": True,
        }
        canonical, warnings = canonicalize_judgment(value, packet)
        self.assertTrue(canonical["insufficient_evidence"])
        self.assertIsNone(canonical["score"])
        self.assertIsNone(canonical["score_low"])
        self.assertIsNone(canonical["score_high"])
        self.assertTrue(any("missing score" in w for w in warnings))

    def test_overwrites_mismatched_packet_id_and_dimension(self) -> None:
        packet = {"packet_id": "p1", "dimension": "verification_control"}
        value = {
            "schema_version": "atobench.trajectory_judgment.v1",
            "packet_id": "wrong_id",
            "dimension": "stop_decision",
            "score": 7,
            "score_low": 6,
            "score_high": 8,
            "band": "good",
            "confidence": "medium",
            "insufficient_evidence": False,
            "reason_code": "rc",
            "material_strengths": [],
            "material_deficiencies": [],
            "supporting_message_ids": ["m1"],
            "supporting_event_ids": [],
            "supporting_fact_ids": [],
            "forbidden_identity_not_accessed": True,
            "outside_knowledge_not_used": True,
        }
        canonical, warnings = canonicalize_judgment(value, packet)
        self.assertEqual(canonical["packet_id"], "p1")
        self.assertEqual(canonical["dimension"], "verification_control")
        self.assertTrue(any("packet_id mismatch" in w for w in warnings))
        self.assertTrue(any("dimension mismatch" in w for w in warnings))

    def test_maps_stop_decision_descriptor(self) -> None:
        packet = {"packet_id": "p1", "dimension": "stop_decision"}
        descriptor = {
            "schema_version": "atobench.stop_decision_descriptor.v1",
            "packet_id": "p1",
            "episode_pseudonym": "ep1",
            "dimension": "stop_decision",
            "aou": "sqli",
            "primary_evidence": {
                "fact_id": "F1",
                "fact_type": "SQLI_TP_PRIMARY_EVIDENCE",
                "measurement_status": "positive",
                "value": True,
            },
            "stop_context": {
                "explicit_stop_reason_present": True,
                "explicit_conflict_count": 0,
                "explicit_uncertainty_count": 0,
                "unavailable_verification_fact_count": 0,
            },
            "verification_status_counts": {"positive": 1},
            "derived_descriptors": {
                "readiness_state": "ready_supported",
                "stop_fit": "matched_positive_readiness",
                "reason_trace": "explicit_stop_reason_present",
                "confidence": "high",
            },
            "summary_metrics": {
                "verification_fact_count": 1,
                "positive_verification_fact_count": 1,
                "negative_verification_fact_count": 0,
                "unresolved_verification_fact_count": 0,
            },
            "supporting_fact_ids": ["F1"],
        }
        canonical, warnings = canonicalize_judgment(descriptor, packet)
        self.assertEqual(canonical["schema_version"], "atobench.trajectory_judgment.v1")
        self.assertEqual(canonical["score"], 9)
        self.assertEqual(canonical["band"], "excellent")
        self.assertTrue(any("mapped stop_decision descriptor" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
