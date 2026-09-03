"""ATOBench agent adapters.

Subclasses of BaseAgent wrap a third-party pentest agent (Claude Code, CAI,
PentAGI, hackingBuddyGPT, custom GPT-4o, etc.) behind a uniform
reset/run/finalize contract so the episode runner can drive any agent
against the same EpisodeSpec.

Subagent-style adapters (ClaudeCodeAgent, CAIAdapter) override `run()` to
spawn their target process and block until completion. The harness
passively observes HTTP traffic via the proxy.

Streaming adapters (future: GPT4oAdapter) implement `step()` per turn and
use the default `run()` which loops step until observation.done.
"""

from atobench.agents.base import Action, AgentReport, BaseAgent, Observation  # noqa: F401
from atobench.agents.claude_code import ClaudeCodeAgent  # noqa: F401
from atobench.agents.cai_adapter import CAIAdapter  # noqa: F401

__all__ = ["BaseAgent", "Action", "Observation", "AgentReport", "ClaudeCodeAgent", "CAIAdapter"]
