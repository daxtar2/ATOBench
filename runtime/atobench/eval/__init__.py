"""ATOBench evaluators.

The active benchmark evaluator is the clean-relative pentest-effect pipeline.
It consumes paired clean/deception run artifacts and writes `pentest_effect.json`.
"""

from atobench.eval.pentest_effect import evaluate_pentest_effect, evaluate_pentest_effect_from_paths  # noqa: F401

__all__ = [
    "evaluate_pentest_effect",
    "evaluate_pentest_effect_from_paths",
]
