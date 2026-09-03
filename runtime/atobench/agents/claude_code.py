"""Claude Code agent adapter (subagent style).

Drives `claude -p` as a subprocess behind the BaseAgent contract, captures
JSON output, and returns an AgentReport.

This is the canonical reference adapter. Third-party adapters (CAI, PentAGI,
custom GPT-4o) should follow the same contract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from atobench.agents.base import Action, AgentReport, BaseAgent, Observation

PROMPT_DIR = Path(__file__).parent / "prompts"

# Agent invocation cap (seconds). 15 min covers B0 oracle + retry headroom.
DEFAULT_TIMEOUT_S = 900

# Cap final_report_text at 16KB to prevent JSONL bloat (rare truncation).
MAX_FINAL_TEXT_BYTES = 16384

ALLOWED_TOOLS_BASE = ["Bash(curl:*)", "Bash(wget:*)", "Bash(httpie:*)"]
ALLOWED_TOOLS_T3 = ALLOWED_TOOLS_BASE + ["Agent:*"]


class ClaudeCodeAgent(BaseAgent):
    """Subagent-style adapter: spawns `claude -p` to completion.

    The harness passively observes HTTP traffic via the proxy; the
    subagent drives itself. `run()` is the primary entry point.
    """

    def __init__(self, episode_spec: dict[str, Any], proxy_urls: dict[str, str], **kwargs: Any) -> None:
        super().__init__(episode_spec, proxy_urls, **kwargs)
        self.timeout_s = kwargs.get("timeout_s", DEFAULT_TIMEOUT_S)
        self.task_id = episode_spec.get("task_id", "T1")
        self.defense_posture = episode_spec.get("defense_posture", "strong")
        self.engagement_id = kwargs.get("engagement_id") or f"atobench-{self.task_id.lower()}-default"
        self._prompt: str = ""
        self._raw_output: dict[str, Any] = {}
        self._duration: float = 0.0
        self._final_text: str = ""
        self._flag: str | None = None

    def reset(self, task_spec: dict[str, Any]) -> Observation:
        """Load and format the prompt for this episode's task."""
        self._prompt = self._build_prompt()
        return Observation(tool_results=[], done=False, info={"prompt": self._prompt})

    def _build_prompt(self) -> str:
        """Load the prompt template from prompts/<task>.md and format it."""
        if self.task_id not in {"T1", "T2", "T3", "T4"}:
            raise ValueError(f"unknown task: {self.task_id}")
        template_path = PROMPT_DIR / f"{self.task_id}.md"
        if not template_path.exists():
            raise FileNotFoundError(f"Prompt template not found: {template_path}")
        template = template_path.read_text(encoding="utf-8")

        if self.task_id == "T1":
            return template.format(proxy_url=self.proxy_urls["acme"])
        elif self.task_id == "T2":
            return template.format(
                idp=self.proxy_urls["acme_idp"],
                auth=self.proxy_urls["auth_service"],
                admin=self.proxy_urls["admin_api"],
            )
        elif self.task_id == "T3":
            return template.format(
                acme_idp=self.proxy_urls["acme_idp"],
                auth=self.proxy_urls["auth_service"],
                admin=self.proxy_urls["admin_api"],
                defense_posture=self.defense_posture,
                engagement_id=self.engagement_id,
            )
        else:
            raise ValueError(f"task {self.task_id} not yet supported by ClaudeCodeAgent (T4 lands in Phase 2.4)")

    def run(self) -> AgentReport:
        """Spawn `claude -p` and block until completion."""
        self._prompt = self._build_prompt()
        allowed = ALLOWED_TOOLS_T3 if self.task_id == "T3" else ALLOWED_TOOLS_BASE
        cmd = [
            "claude", "-p", self._prompt,
            "--output-format", "json",
            "--allowedTools", *allowed,
        ]

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=self.timeout_s, cwd="/tmp",
            )
        except subprocess.TimeoutExpired:
            self._duration = float(self.timeout_s)
            self._final_text = f"SUBAGENT_TIMEOUT after {self.timeout_s}s"
            self._flag = None
            return self.finalize()

        self._duration = time.time() - t0

        try:
            self._raw_output = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self._raw_output = {}
            self._final_text = (
                f"PARSE_FAIL: stdout[:500]={proc.stdout[:500]!r}"
                f"\nstderr[:500]={proc.stderr[:500]!r}"
            )
            self._flag = None
            return self.finalize()

        self._final_text = self._raw_output.get("result", "") or ""
        if len(self._final_text) > MAX_FINAL_TEXT_BYTES:
            self._final_text = self._final_text[:MAX_FINAL_TEXT_BYTES] + "\n...[truncated]..."

        self._flag = _extract_flag(self._final_text)

        # Append a clean FINAL_FLAG line if agent didn't already emit one
        if "FINAL_FLAG:" not in self._final_text:
            self._final_text = self._final_text.rstrip() + f"\n\nFINAL_FLAG: {self._flag or 'none'}"

        return self.finalize()

    def finalize(self) -> AgentReport:
        usage = self._raw_output.get("usage", {}) or {}
        token_cost = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        return AgentReport(
            n_turns=int(self._raw_output.get("num_turns", 0)),
            total_cost_usd=self._raw_output.get("total_cost_usd"),
            total_tokens=token_cost,
            duration_seconds=self._duration,
            session_id=self._raw_output.get("session_id"),
            final_report_text=self._final_text,
            flag=self._flag,
            raw_metadata=dict(self._raw_output),
        )


def _extract_flag(text: str) -> str | None:
    """Look for FLAG{...} in text. Returns the first match or None."""
    m = re.search(r"FLAG\{[^}]+\}", text or "")
    return m.group(0) if m else None
