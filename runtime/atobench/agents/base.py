"""BaseAgent ABC: uniform contract for pentest agent adapters.

A third-party pentest agent (Claude Code, CAI, PentAGI, hackingBuddyGPT,
custom GPT-4o, etc.) is wrapped behind this interface so the episode
runner can drive any agent against the same EpisodeSpec.

ATOBench's threat model (instructions.md Notice 2) treats the agent as a
black box: only HTTP traffic (request line, status, body) is observable.
This constrains the adapter contract:

- **Subagent-style victims** (Claude Code, CAI, PentAGI): the adapter
  spawns the agent process and blocks until completion. The harness
  passively observes HTTP traffic via the proxy. Implements `run()` only.

- **Streaming victims** (custom GPT-4o agent we own the loop for): the
  adapter emits one action at a time, harness applies it. Implements
  `step()` and uses the default `run()` which loops step until done.

Both styles share `reset()` and `finalize()`.

Phase 1.3: ABC + ClaudeCodeAgent adapter (subagent style). The CAI adapter
is a stub demonstrating the contract for third-party contributors.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Observation:
    """One step of agent-visible state.

    For subagent-style agents, only the initial observation (task prompt)
    is meaningful; subsequent observations are empty (the subagent drives
    itself). For streaming agents, `tool_results` is the list of HTTP
    transcripts the agent saw this step (request line, status, body).
    `done` is True iff the agent signaled task completion or hit a budget.
    """
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    info: dict[str, Any] = field(default_factory=dict)


@dataclass
class Action:
    """Agent-emitted action.

    `tool_calls` is a normalized list of {tool_name, arguments} dicts.
    `final_answer` is set when the agent signals task completion.
    For subagent-style agents, `tool_calls` is empty and `final_answer`
    carries the subagent's full output.
    """
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_answer: Optional[str] = None


@dataclass
class AgentReport:
    """Post-episode agent self-report."""
    n_turns: int = 0
    total_cost_usd: Optional[float] = None
    total_tokens: Optional[int] = None
    duration_seconds: float = 0.0
    session_id: Optional[str] = None
    final_report_text: str = ""
    flag: Optional[str] = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)


class BaseAgent(abc.ABC):
    """Abstract base class for pentest agent adapters.

    Subclasses must implement reset, run, finalize. The episode runner
    drives the agent through this contract; the underlying LLM/subagent
    is an implementation detail.

    `run()` is the primary entry point. The default implementation loops
    `step()` until observation.done — subagent-style agents override `run()`
    directly and never call `step()`.
    """

    def __init__(self, episode_spec: dict[str, Any], proxy_urls: dict[str, str], **kwargs: Any) -> None:
        self.episode_spec = episode_spec
        self.proxy_urls = proxy_urls
        self.kwargs = kwargs
        self._report: Optional[AgentReport] = None

    @abc.abstractmethod
    def reset(self, task_spec: dict[str, Any]) -> Observation:
        """Initialize the agent for a new episode. Returns the first observation."""
        raise NotImplementedError

    def run(self) -> AgentReport:
        """Drive the agent to completion. Default: loop step() until done.

        Subagent-style agents (Claude Code, CAI) override this to spawn
        their subagent process and block. Streaming agents use this
        default implementation.
        """
        obs = self.reset({})
        while not obs.done:
            action = self.step(obs)
            if action.final_answer is not None:
                break
            # Streaming agents: harness applies action and returns next obs.
            # Default implementation can't actually drive the env, so this
            # is a no-op for subagent-style agents.
            obs = Observation(done=True)
        return self.finalize()

    def step(self, obs: Observation) -> Action:
        """Given an observation, emit the next action.

        Only required for streaming agents. Subagent-style agents that
        override run() can leave this unimplemented.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement step(); it uses run() directly.")

    @abc.abstractmethod
    def finalize(self) -> AgentReport:
        """Called once after the episode ends (success, give-up, or budget)."""
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{type(self).__name__}(episode_id={self.episode_spec.get('episode_id', '?')})"
