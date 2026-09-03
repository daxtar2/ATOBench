"""ATOBench defenses.

Subclasses of BaseDefense wrap a defense mechanism (LLM monitor,
AgentSpec-DSL constraint layer, GuardAgent executable checks) behind a
uniform `score()` contract so the attack×defense matrix can sweep
defenses the same way it sweeps primitives.

Phase 0.3: LLMMonitorDefense (reactive, post-hoc, zero-training).
Proactive defenses (AgentSpec-DSL, GuardAgent) land in Phase 2.6.
"""

from atobench.defenses.base import BaseDefense, DefenseReport, DriftEvidence  # noqa: F401
from atobench.defenses.llm_monitor import LLMMonitorDefense  # noqa: F401

__all__ = ["BaseDefense", "DefenseReport", "DriftEvidence", "LLMMonitorDefense"]
