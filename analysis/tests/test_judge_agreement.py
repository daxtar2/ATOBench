from __future__ import annotations

import unittest

from atobench_vr.judge_agreement import (
    agreement_block,
    cohen_kappa,
    imbalance_warning,
    observed_agreement,
    quadratic_weighted_kappa,
)
from atobench_vr.judge_pilot import _agreement_summary


class JudgeAgreementTests(unittest.TestCase):
    def test_perfect_agreement_kappa_is_one(self) -> None:
        labels = ["a", "b", "a", "b", "c"]
        self.assertEqual(observed_agreement(labels, labels), 1.0)
        self.assertEqual(cohen_kappa(labels, labels), 1.0)

    def test_imbalanced_high_agreement_low_kappa(self) -> None:
        # Classic κ-paradox: 90% class "0", raters almost always say 0.
        a = [0] * 90 + [1] * 10
        b = [0] * 90 + [0] * 5 + [1] * 5  # agree on all 0s; half of the rares
        rate = observed_agreement(a, b)
        kappa = cohen_kappa(a, b)
        assert rate is not None and kappa is not None
        self.assertGreater(rate, 0.9)
        self.assertLess(kappa, rate)
        warning = imbalance_warning(a)
        self.assertIsNotNone(warning)
        assert warning is not None
        self.assertEqual(warning["code"], "class_imbalance_kappa_paradox_risk")

    def test_quadratic_weighted_kappa_tolerates_near_misses(self) -> None:
        a = [5, 6, 7, 8]
        b = [5, 7, 7, 9]  # off-by-one on two items
        exact = cohen_kappa(a, b)
        weighted = quadratic_weighted_kappa(a, b)
        assert exact is not None and weighted is not None
        self.assertGreater(weighted, exact)

    def test_agreement_block_marks_same_model_replicate(self) -> None:
        block = agreement_block([3, 5, None], [3, 6, 7], ["poor", "limited"], ["poor", "good"])
        self.assertEqual(block["reviewer_panel_kind"], "same_model_replicate")
        self.assertEqual(block["n_paired_scores"], 2)
        self.assertIn("cohen_kappa_exact_score", block)
        self.assertIn("quadratic_weighted_kappa", block)

    def test_pilot_summary_includes_metrology(self) -> None:
        rows = [
            {
                "score_a": 8,
                "score_b": 8,
                "band_a": "good",
                "band_b": "good",
                "exact_score_agreement": True,
                "within_one_point": True,
                "band_agreement": True,
                "verifier_statuses": ["entailed", "entailed"],
                "adjudication_trigger": "exact_verified_consensus",
            },
            {
                "score_a": 3,
                "score_b": 5,
                "band_a": "poor",
                "band_b": "limited",
                "exact_score_agreement": False,
                "within_one_point": False,
                "band_agreement": False,
                "verifier_statuses": ["entailed", "partially_entailed"],
                "adjudication_trigger": "two_point_disagreement",
            },
        ]
        summary = _agreement_summary(rows)
        self.assertEqual(summary["exact_score_agreement_rate"], 0.5)
        self.assertIn("metrology", summary)
        self.assertEqual(
            summary["metrology"]["reviewer_panel_kind"], "same_model_replicate"
        )
        self.assertIsNotNone(summary["metrology"]["cohen_kappa_exact_score"])


if __name__ == "__main__":
    unittest.main()
