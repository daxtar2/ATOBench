"""BaseDefense ABC: uniform contract for defense adapters.

A defense wraps an agent (or operates on the agent's trajectory post-hoc)
to detect, prevent, or mitigate deception effects. Subclasses implement
either:
- **Proactive defense** (pre-turn): intercepts the agent's tool-call stream
  and enforces a policy (e.g., AgentSpec-DSL constraints).
- **Reactive monitor** (post-turn): scores a completed trajectory for
  drift / deception-affected behavior.

Phase 0.3: ABC + LLMMonitorDefense (reactive, post-hoc). Proactive
defenses (AgentSpec-DSL, GuardAgent) land in Phase 2.6.

The reactive monitor contract assumes the defender has:
1. A B0 baseline trajectory for the same task (no deception).
2. The EpisodeSpec's primitive_config (which deception was applied).
3. The test trajectory (what the agent actually did under deception).

This matches the realistic deployment: a defender running ATOBench on their
own agent controls all three.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Literal


Verdict = Literal["drifted_deceived", "drifted_resisted", "no_drift", "inconclusive"]


@dataclass
class DriftEvidence:
    """One piece of evidence for a drift at a specific turn.

    `turn_idx` is the index in the test trajectory.
    `field` is the field/path/header that drifted.
    `b0_value` is what the field was in the B0 baseline (or "absent").
    `test_value` is what the field became in the test trajectory.
    `explanation` is the LLM's one-sentence reasoning.
    """
    turn_idx: int
    field: str
    b0_value: str
    test_value: str
    explanation: str


@dataclass
class DefenseReport:
    """Post-hoc trajectory monitor report."""
    verdict: Verdict = "inconclusive"
    drift_turns: list[DriftEvidence] = field(default_factory=list)
    per_turn_drift: list[dict[str, Any]] = field(default_factory=list)
    overall_reasoning: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "drift_turns": [d.__dict__ for d in self.drift_turns],
            "per_turn_drift": self.per_turn_drift,
            "overall_reasoning": self.overall_reasoning,
            "raw_metadata": self.raw_metadata,
        }


class BaseDefense(abc.ABC):
    """Abstract base class for defense adapters.

    Reactive monitors implement `score()`. Proactive defenses (future)
    would implement `step()` to gate per-turn actions; not in this phase.
    """

    def __init__(self, defense_config: dict[str, Any] | None = None, **kwargs: Any) -> None:
        self.defense_config = defense_config or {}
        self.kwargs = kwargs

    @abc.abstractmethod
    def score(
        self,
        test_trajectory: list[dict[str, Any]],
        b0_baseline: list[dict[str, Any]],
        deception_config: dict[str, Any],
    ) -> DefenseReport:
        """Score a test trajectory against a B0 baseline + known deception config.

        Args:
            test_trajectory: list of turn dicts (turns.jsonl format) from the
                episode under evaluation (B1/B2/B3 or held-out third-party).
            b0_baseline: list of turn dicts from the same task under B0 (no
                deception). May be empty if no B0 is available (degraded mode).
            deception_config: dict with at least {primitive, coupling_variant,
                fake_values, target_fields} describing what deception was applied.

        Returns:
            DefenseReport with verdict, drift_turns, per_turn_drift, reasoning.
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"
