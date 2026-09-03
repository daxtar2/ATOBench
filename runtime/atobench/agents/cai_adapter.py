"""CAI (Cybersecurity AI framework) adapter — example third-party adapter.

This is a worked example showing how a third-party pentest agent can be
plugged into ATOBench. CAI is an open-source pentest framework
(https://github.com/orirosenberg/CAI) that exposes its own agent loop.

Phase 1.3: contract skeleton only. The actual CAI integration is deferred
to Phase 2.5 (cross-model sweep). The point of this file is to:
1. Demonstrate the BaseAgent contract for a non-Claude-Code agent.
2. Document the integration steps a third-party contributor would follow.
3. Provide a testable adapter even before CAI is installed (run() raises
   NotImplementedError with a clear "install CAI first" message).

To make this adapter functional:
1. `pip install cai` (when CAI is published to PyPI; until then, clone
   https://github.com/orirosenberg/CAI and add to PYTHONPATH).
2. Implement run() to invoke `cai.run_attack_chain(...)` with the proxy_urls
   as targets.
3. Map CAI's HTTP request log into AgentReport.final_report_text + flag.
"""

from __future__ import annotations

from typing import Any

from atobench.agents.base import AgentReport, BaseAgent, Observation


class CAIAdapter(BaseAgent):
    """Example third-party adapter for the CAI pentest framework.

    Demonstrates the BaseAgent contract for a non-Claude-Code agent.
    Subagent-style: run() invokes the CAI framework to completion.
    """

    def __init__(self, episode_spec: dict[str, Any], proxy_urls: dict[str, str], **kwargs: Any) -> None:
        super().__init__(episode_spec, proxy_urls, **kwargs)
        self.task_id = episode_spec.get("task_id", "T1")
        self._report: AgentReport = AgentReport()

    def reset(self, task_spec: dict[str, Any]) -> Observation:
        """Validate CAI is importable; build the CAI run config."""
        try:
            import cai  # noqa: F401  pylint: disable=unused-import
        except ImportError as e:
            raise NotImplementedError(
                "CAI adapter requires the `cai` package. Install with: "
                "pip install cai  (or clone https://github.com/orirosenberg/CAI)."
            ) from e
        # Build CAI run config from episode_spec + proxy_urls
        # (real implementation in Phase 2.5)
        return Observation(tool_results=[], done=False, info={"task": self.task_id})

    def run(self) -> AgentReport:
        """Invoke CAI to completion."""
        try:
            import cai  # noqa: F401
        except ImportError:
            return AgentReport(
                final_report_text=(
                    "CAI adapter not installed. `pip install cai` or clone "
                    "https://github.com/orirosenberg/CAI."
                ),
            )

        # Phase 2.5: real implementation
        # targets = list(self.proxy_urls.values())
        # cai_result = cai.run_attack_chain(targets=targets, budget=self.episode_spec["budget"]["wall_clock_s"])
        # self._report = AgentReport(
        #     n_turns=cai_result.num_requests,
        #     final_report_text=cai_result.findings_report,
        #     flag=_extract_flag(cai_result.findings_report),
        #     duration_seconds=cai_result.duration_s,
        # )
        raise NotImplementedError("CAI run() lands in Phase 2.5 (cross-model sweep).")

    def finalize(self) -> AgentReport:
        return self._report
