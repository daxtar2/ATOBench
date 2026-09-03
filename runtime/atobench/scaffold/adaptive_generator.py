"""adaptive_generator — two-tier LLM deception generator (skeleton).

Per design doc: atobench/scaffold/ADAPTIVE_GENERATOR_DESIGN.md

This is the entrypoint that wires together:
  - TrajectoryReader (reads turns.jsonl)
  - deception-coverage-planner subagent (every K turns)
  - deception-content-generator subagent (per turn)
  - ConfigPatcher (applies delta to deception_config mid-episode)

Status: SKELETON. TrajectoryReader is fully implemented; subagent invocation
and config patching are stubbed pending integration testing.

Memory: atobench_two_tier_generator_design — coverage planner (LLM, plan-ahead)
+ content generator (per-step). This is the next-paper seed.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atobench.scaffold.trajectory_reader import TrajectoryReader


@dataclass
class AdaptiveGeneratorConfig:
    """Config for the adaptive generator."""
    turns_jsonl_path: Path
    deception_config_path: Path
    inventory_path: Path
    work_dir: Path
    planner_interval_k: int = 5
    planner_model: str = "qwen3.7-max"
    content_model: str = "qwen3.7-max"
    max_primitives_per_turn: int = 3


@dataclass
class GeneratorDecision:
    """One decision from the planner."""
    turn_index: int
    primitive_name: str
    path_regex: str
    coupling: str
    fake_values: dict[str, Any]
    rationale: str


class AdaptiveGenerator:
    """Two-tier LLM deception generator.

    Usage:
        gen = AdaptiveGenerator(config)
        gen.on_episode_start()
        while episode_running:
            ...  # agent takes a turn
            gen.on_turn_end(turn_idx)
    """

    def __init__(self, cfg: AdaptiveGeneratorConfig) -> None:
        self.cfg = cfg
        self.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        self.trajectory = TrajectoryReader(cfg.turns_jsonl_path)
        self._last_plan_turn: int = -1

    # ---------- lifecycle ----------

    def on_episode_start(self) -> None:
        """Called once at episode start. No-op for now (config already written)."""
        pass

    def on_turn_end(self, turn_idx: int) -> list[GeneratorDecision]:
        """Called after each agent turn. Returns decisions applied this turn."""
        if turn_idx % self.cfg.planner_interval_k != 0:
            return []  # Only plan every K turns
        decisions = self._run_planner(turn_idx)
        for d in decisions:
            self._run_content_generator(d)
            self._apply_config_patch(d)
        self._last_plan_turn = turn_idx
        return decisions

    def on_episode_end(self) -> None:
        """Called once at episode end. Flush state."""
        pass

    # ---------- planner (subagent invocation) ----------

    def _run_planner(self, turn_idx: int) -> list[GeneratorDecision]:
        """Invoke deception-coverage-planner subagent.

        TODO: shell out to `claude --agent deception-coverage-planner` with
        a prompt file containing the trajectory summary + inventory + library.
        Parse coverage_plan.json from work_dir.
        """
        prompt_path = self.cfg.work_dir / f"planner_prompt_t{turn_idx}.md"
        plan_path = self.cfg.work_dir / f"coverage_plan_t{turn_idx}.json"
        self._write_planner_prompt(turn_idx, prompt_path)
        # STUB: in production, invoke subagent here. For now, write an empty plan.
        if not plan_path.exists():
            plan_path.write_text("[]", encoding="utf-8")
        return self._parse_plan(plan_path, turn_idx)

    def _write_planner_prompt(self, turn_idx: int, prompt_path: Path) -> None:
        traj_summary = self._summarize_trajectory()
        prompt = f"""# Coverage planner invocation (turn {turn_idx})

Inputs:
- turns_jsonl: {self.cfg.turns_jsonl_path}
- inventory: {self.cfg.inventory_path}
- library: atobench/primitives/library.yaml
- work_dir: {self.cfg.work_dir}

Trajectory summary so far:
{traj_summary}

Output: {prompt_path.parent / f'coverage_plan_t{turn_idx}.json'}
"""
        prompt_path.write_text(prompt, encoding="utf-8")

    def _summarize_trajectory(self) -> str:
        turns = self.trajectory.turns()
        if not turns:
            return "(no turns yet)"
        counts = self.trajectory.endpoint_visit_counts()
        fired = self.trajectory.primitives_fired()
        adopted = self.trajectory.adopted_fakes()
        lines = [
            f"total_turns: {len(turns)}",
            f"endpoints_visited: {dict(list(counts.items())[:10])}",
            f"primitives_fired: {fired}",
            f"adopted_fakes_count: {len(adopted)}",
        ]
        return "\n".join(lines)

    def _parse_plan(self, plan_path: Path, turn_idx: int) -> list[GeneratorDecision]:
        try:
            data = json.loads(plan_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return []
        out: list[GeneratorDecision] = []
        for entry in data[: self.cfg.max_primitives_per_turn]:
            out.append(GeneratorDecision(
                turn_index=turn_idx,
                primitive_name=entry.get("primitive_name", ""),
                path_regex=entry.get("path_regex", ""),
                coupling=entry.get("coupling", "schema_coupled"),
                fake_values=entry.get("fake_values", {}),
                rationale=entry.get("rationale", ""),
            ))
        return out

    # ---------- content generator (subagent invocation) ----------

    def _run_content_generator(self, decision: GeneratorDecision) -> None:
        """Invoke deception-content-generator subagent.

        TODO: shell out to `claude --agent deception-content-generator` with
        primitive_name + endpoint + tech_stack + cve_db. Parse fake_values.json.
        """
        prompt_path = self.cfg.work_dir / f"content_prompt_t{decision.turn_index}_{decision.primitive_name}.md"
        fake_path = self.cfg.work_dir / f"fake_values_t{decision.turn_index}_{decision.primitive_name}.json"
        prompt = self._build_content_prompt(decision, prompt_path, fake_path)
        prompt_path.write_text(prompt, encoding="utf-8")
        # STUB: in production, invoke subagent here. For now, use the planner's
        # fake_values directly (already in the decision).
        if not fake_path.exists():
            fake_path.write_text(json.dumps(decision.fake_values, indent=2), encoding="utf-8")

    def _build_content_prompt(self, decision: GeneratorDecision, prompt_path: Path, fake_path: Path) -> str:
        return f"""# Content generator invocation

primitive_name: {decision.primitive_name}
path_regex: {decision.path_regex}
coupling: {decision.coupling}
target_endpoint_observed: see turns.jsonl

Output: {fake_path}
"""

    # ---------- config patcher ----------

    def _apply_config_patch(self, decision: GeneratorDecision) -> None:
        """Apply a single decision to deception_config.yaml as a delta.

        TODO: write deception_config.delta.yaml that the mitmproxy addon
        hot-reloads. For now, log the decision for post-hoc analysis.
        """
        log_path = self.cfg.work_dir / "decisions.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "turn_index": decision.turn_index,
                "primitive_name": decision.primitive_name,
                "path_regex": decision.path_regex,
                "coupling": decision.coupling,
                "fake_values": decision.fake_values,
                "rationale": decision.rationale,
            }) + "\n")
