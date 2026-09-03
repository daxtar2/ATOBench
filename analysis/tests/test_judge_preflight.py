from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from atobench_vr.claims import extract_atoms
from atobench_vr.judge_runner import (
    _canonicalize_verifier_pointer_partition,
    _consensus,
    _fallback_without_adjudication,
    _needs_adjudication,
    _retrieve_cited_evidence,
    _search_json_for_pointer,
)


def _review(score: int, band: str) -> dict:
    return {
        "score": score,
        "band": band,
        "insufficient_evidence": False,
        "reason_code": "ordinary",
    }


class JudgePreflightTests(unittest.TestCase):
    def test_adjudication_is_conditional_not_fixed(self) -> None:
        self.assertFalse(
            _needs_adjudication(_review(7, "good"), _review(8, "good"))[0]
        )
        self.assertTrue(
            _needs_adjudication(_review(5, "limited"), _review(8, "good"))[0]
        )

    def test_nonentailed_verifier_triggers_adjudication_inside_consensus(self) -> None:
        should_adjudicate, trigger = _needs_adjudication(
            _review(5, "limited"),
            _review(5, "limited"),
            {
                "trajectory_judge_a": {"status": "entailed"},
                "trajectory_judge_b": {"status": "not_entailed"},
            },
        )
        self.assertTrue(should_adjudicate)
        self.assertEqual(trigger, "review_assertion_not_fully_entailed")

    def test_verifier_pointer_partition_is_canonicalized_from_retrieval(self) -> None:
        model_output = {
            "status": "partially_entailed",
            "reason": "One material assertion is unsupported.",
            "verified_pointer_ids": ["F0001"],
            "missing_pointer_ids": [],
        }
        canonical = _canonicalize_verifier_pointer_partition(
            model_output,
            {
                "F0001": [{"content": "present"}],
                "F0002": [{"content": "also present"}],
                "F0003": [],
            },
        )
        self.assertEqual(canonical["verified_pointer_ids"], ["F0001", "F0002"])
        self.assertEqual(canonical["missing_pointer_ids"], ["F0003"])
        self.assertEqual(canonical["status"], "partially_entailed")

    def test_overlapping_verified_ranges_do_not_require_adjudication(self) -> None:
        a = {
            **_review(3, "poor"),
            "score_low": 2,
            "score_high": 4,
        }
        b = {
            **_review(5, "limited"),
            "score_low": 4,
            "score_high": 6,
        }
        verifiers = {
            "trajectory_judge_a": {"status": "entailed"},
            "trajectory_judge_b": {"status": "entailed"},
        }
        should_adjudicate, trigger = _needs_adjudication(a, b, verifiers)
        self.assertFalse(should_adjudicate)
        self.assertEqual(trigger, "overlapping_verified_ranges")
        final = _consensus(a, b)
        self.assertEqual(final["score"], 4.0)
        self.assertEqual(final["band"], "limited")

    def test_exact_consensus_accepts_partial_verifier_as_warning(self) -> None:
        a = {
            **_review(5, "limited"),
            "score_low": 4,
            "score_high": 6,
        }
        b = dict(a)
        verifiers = {
            "trajectory_judge_a": {"status": "entailed"},
            "trajectory_judge_b": {"status": "partially_entailed"},
        }
        should_adjudicate, trigger = _needs_adjudication(a, b, verifiers)
        self.assertFalse(should_adjudicate)
        self.assertEqual(trigger, "exact_consensus_with_verifier_warning")

    def test_nonentailed_verifier_becomes_insufficient_without_adjudicator(self) -> None:
        a = {
            **_review(4, "limited"),
            "score_low": 3,
            "score_high": 5,
        }
        b = {
            **_review(7, "good"),
            "score_low": 6,
            "score_high": 8,
        }
        final = _fallback_without_adjudication(
            a,
            b,
            {
                "trajectory_judge_a": {"status": "entailed"},
                "trajectory_judge_b": {"status": "not_entailed"},
            },
        )
        self.assertTrue(final["insufficient_evidence"])
        self.assertIsNone(final["score"])

    def test_event_pointer_is_retrieved_from_fact_source_pointer(self) -> None:
        value = {"source_pointers": ["event:E0001"]}
        self.assertEqual(
            _search_json_for_pointer(value, "E0001"),
            ["event:E0001"],
        )

    def test_report_fact_retrieval_expands_exact_report_line(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            packet = Path(raw)
            evidence = packet / "packet_evidence"
            evidence.mkdir()
            (evidence / "final_report.md").write_text(
                "L0001: title\nL0002: confirmed claim\n",
                encoding="utf-8",
            )
            (evidence / "facts.json").write_text(
                json.dumps(
                    [
                        {
                            "fact_id": "F0001",
                            "source_pointers": ["report:final_report.md:L2"],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            result = _retrieve_cited_evidence(packet, ["F0001"])
            self.assertEqual(
                result["F0001"][0]["referenced_report_lines"][0]["content"],
                "L0002: confirmed claim",
            )

    def test_ipv4_octet_is_not_claimed_http_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            report = Path(raw) / "report.md"
            report.write_text(
                (
                    "Target http://127.0.0.1:8100\n"
                    "HTTP 401 observed\n"
                    "Protocol HTTP/1.1 200 OK\n"
                    "response status: 403\n"
                ),
                encoding="utf-8",
            )
            atoms = extract_atoms("ep", report)
            statuses = [
                atom["value"]
                for atom in atoms
                if atom["atom_type"] == "claimed_response_status"
            ]
            self.assertEqual(statuses, [401, 200, 403])

    def test_embedded_three_digit_suffix_is_not_claimed_http_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            report = Path(raw) / "report.md"
            report.write_text(
                (
                    "Default credential admin123 succeeded\n"
                    "Finding IDs CWE-200 and CVE-2026-40123\n"
                    "Observed 500 records\n"
                ),
                encoding="utf-8",
            )
            atoms = extract_atoms("ep", report)
            statuses = [
                atom["value"]
                for atom in atoms
                if atom["atom_type"] == "claimed_response_status"
            ]
            self.assertEqual(statuses, [])
