"""Reception classifier wrapper.

Wraps `experiment/pilot/reception_classifier.py` behind the atobench.eval
contract. Classifies an episode's reception into one of 6 classes
(ignored/queried/accepted/acted/committed/flagged) and detects
false-positive-vuln, false-negative, and termination issues.

Phase 1.5: thin wrapper around the existing classifier. Full migration
(into atobench/eval/reception.py with no sys.path hack) is deferred to v0.2.
"""

from __future__ import annotations

from typing import Any

from atobench.eval.reception_classifier import (
    classify_reception,
    classify_reception_behavioral,
    detect_false_negative,
    detect_false_positive_vuln,
    detect_termination_issues,
    ReceptionState,
)

__all__ = [
    "ReceptionState",
    "classify_reception",
    "classify_reception_behavioral",
    "detect_false_positive_vuln",
    "detect_false_negative",
    "detect_termination_issues",
]
