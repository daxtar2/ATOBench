"""multi_llm_generator — strict-benchmark multi-LLM deception config generator.

Wires together:
  1. ProposalSampler (Mode A: 3 LLMs) — atobench/scaffold/proposal_sampler.py
  2. ConvergenceFilter — atobench/scaffold/convergence_filter.py
  3. TrajectoryFitFilter — atobench/scaffold/trajectory_fit_filter.py
  4. CouplingEnforcer — atobench/scaffold/coupling_enforcer.py
  5. PrimitiveTranslator — atobench/scaffold/primitive_template.py
  6. ContentGenerator (optional, only for proposals with empty fake_values)
  7. Writes deception_config.yaml

Memory: atobench_multi_llm_generator_benchmark_design — Phases F+G.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from atobench.scaffold.convergence_filter import ConvergenceReport, filter_by_convergence
from atobench.scaffold.coupling_enforcer import CouplingEnforcerReport, enforce_coupling
from atobench.scaffold.primitive_template import (
    PrimitiveTemplate,
    PrimitiveTranslator,
    to_deception_config_entry,
)
from atobench.scaffold.proposal_sampler import (
    LLMConfig,
    ProposalList,
    ProposalSampler,
    build_proposal_prompt,
)
from atobench.scaffold.trajectory_fit_filter import (
    TrajectoryFitReport,
    compute_visited_distribution,
    filter_by_trajectory_fit,
    find_prior_sweeps,
)


@dataclass
class GenerationReport:
    """End-to-end generation report."""
    target: str = ""
    task_id: str = ""
    baseline: str = "B3"
    output_path: Path | None = None
    # Per-stage reports
    n_llm_proposals: int = 0
    n_after_convergence: int = 0
    n_after_trajectory_fit: int = 0
    n_after_coupling: int = 0
    n_mappable: int = 0
    n_novel: int = 0  # proposals with no existing transformer
    used_fallback: bool = False  # convergence filter fallback
    # Outputs
    deception_config: dict[str, Any] = field(default_factory=dict)
    novel_proposals: list[PrimitiveTemplate] = field(default_factory=list)
    elapsed_s: float = 0.0
    error: str | None = None


class MultiLLMGenerator:
    """End-to-end multi-LLM deception config generator.

    Usage:
        gen = MultiLLMGenerator(target="juice-shop", task_id="T3")
        report = gen.generate(output_path=Path("targets/juice-shop/deception_config_t3_generated.yaml"))
    """

    def __init__(
        self,
        target: str,
        task_id: str,
        baseline: str = "B3",
        writeup_path: Path | None = None,
        logs_dir: Path = Path("logs"),
        llm_configs: list[LLMConfig] | None = None,
        skip_content_generation: bool = True,  # require LLM to fill fake_values in proposal
    ) -> None:
        self.target = target
        self.task_id = task_id
        self.baseline = baseline
        self.logs_dir = logs_dir
        self.skip_content_generation = skip_content_generation

        # Locate writeup
        if writeup_path is None:
            writeup_path = Path(__file__).resolve().parents[1] / "targets" / target / "writeup.yaml"
        self.writeup_path = writeup_path
        if not self.writeup_path.exists():
            raise FileNotFoundError(f"writeup not found: {self.writeup_path}")

        # Load writeup
        self.writeup = yaml.safe_load(self.writeup_path.read_text(encoding="utf-8"))

        # LLM configs (Mode A default)
        self.llm_configs = llm_configs  # if None, built lazily in sampler

    def generate(self, output_path: Path) -> GenerationReport:
        """Run end-to-end generation. Writes deception_config.yaml to output_path."""
        report = GenerationReport(
            target=self.target,
            task_id=self.task_id,
            baseline=self.baseline,
            output_path=output_path,
        )
        t0 = time.time()

        try:
            # Stage 1: build prompt + sample proposals from 3 LLMs
            sampler = ProposalSampler(llm_configs=self.llm_configs)
            visited = self._compute_visited()
            prompt = build_proposal_prompt(
                target_writeup=self.writeup,
                task_id=self.task_id,
                visited_endpoints=visited,
                prior_attempts=None,
            )
            proposal_lists = sampler.sample(prompt)
            report.n_llm_proposals = sum(len(pl.proposals) for pl in proposal_lists)

            # Stage 2: convergence filter
            conv_report = filter_by_convergence(proposal_lists, min_supporters=2)
            report.n_after_convergence = len(conv_report.kept)
            report.used_fallback = conv_report.used_fallback
            kept = conv_report.kept

            # Stage 3: trajectory fit filter
            traj_report = filter_by_trajectory_fit(kept, visited, retarget=True)
            report.n_after_trajectory_fit = len(traj_report.kept)
            kept = traj_report.kept

            # Stage 4: coupling enforcer
            coup_report = enforce_coupling(kept)
            report.n_after_coupling = len(coup_report.kept)
            kept = coup_report.kept

            # Stage 5: translate to registered primitive names
            translator = PrimitiveTranslator()
            mappable, novel = translator.translate_all(kept)
            report.n_mappable = len(mappable)
            report.n_novel = len(novel)
            report.novel_proposals = novel

            # Stage 6: build deception_config.yaml
            config = self._build_deception_config(mappable)
            report.deception_config = config

            # Write output
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)

        except Exception as e:
            report.error = f"generation failed: {e}"
            import traceback
            traceback.print_exc()

        report.elapsed_s = time.time() - t0
        return report

    def _compute_visited(self) -> dict[str, int]:
        """Compute visited endpoint distribution from prior sweeps on this target."""
        sweeps = find_prior_sweeps(self.target, logs_dir=self.logs_dir)
        return compute_visited_distribution(sweeps)

    def _build_deception_config(self, mappable: list[PrimitiveTemplate]) -> dict[str, Any]:
        """Build the deception_config.yaml dict from mappable proposals."""
        target = self.writeup.get("target", {})
        base_url = target.get("base_url", "http://target:80")
        swagger_path = target.get("swagger")

        # Generate a hex episode_id matching schema pattern ^ep_[0-9a-f]{6,}$
        import os
        ep_suffix = os.urandom(6).hex()  # 12 hex chars
        ep_id = f"ep_{ep_suffix}"

        primitives = []
        for template in mappable:
            try:
                entry = to_deception_config_entry(template)
                primitives.append(entry)
            except ValueError as e:
                # Skip entries that fail to convert
                continue

        # Build config
        config: dict[str, Any] = {
            "schema_version": "0.1.0",
            "episode_id": ep_id,
            "task_id": self.task_id,
            "baseline": self.baseline,
            "target": {
                "base_url": base_url,
                "swagger_path": swagger_path,
            },
            "primitives": primitives,
            "logging": {
                "turns_jsonl": "/logs/turns.jsonl",
                "state_path": f"/logs/state_{ep_id}.json",
            },
        }

        return config
