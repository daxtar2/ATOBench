"""ATOBench schema loader and validator.

Loads EpisodeSpec / TurnRecord / EpisodeSummary from JSON files or dicts,
validates against the versioned JSON Schemas in this directory, and
provides an env-var compatibility shim for the legacy ATOBENCH_* configuration
mechanism (used by services/_deception_tag.py and the episode runner).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import jsonschema

SCHEMA_DIR = Path(__file__).parent

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def _load_schema(name: str) -> dict[str, Any]:
    """Load and cache a JSON Schema by name (without .json extension)."""
    if name not in _SCHEMA_CACHE:
        path = SCHEMA_DIR / f"{name}.json"
        with open(path, "r", encoding="utf-8") as f:
            _SCHEMA_CACHE[name] = json.load(f)
    return _SCHEMA_CACHE[name]


def validate_episode_spec(spec: dict[str, Any]) -> None:
    """Validate an EpisodeSpec dict against the schema. Raises jsonschema.ValidationError on failure."""
    schema = _load_schema("episode_spec")
    jsonschema.validate(instance=spec, schema=schema)


def validate_turn_record(rec: dict[str, Any]) -> None:
    schema = _load_schema("turn_record")
    jsonschema.validate(instance=rec, schema=schema)


def validate_episode_summary(summary: dict[str, Any]) -> None:
    schema = _load_schema("episode_summary")
    jsonschema.validate(instance=summary, schema=schema)


def validate_deception_config(cfg: dict[str, Any]) -> None:
    """Validate a DeceptionConfig dict against the Phase 2.0 schema.

    Used by the mitmproxy addon at startup and by `atobench scaffold` to
    check its own output.
    """
    schema = _load_schema("deception_config")
    jsonschema.validate(instance=cfg, schema=schema)


def validate_writeup(writeup: dict[str, Any]) -> None:
    """Validate a Writeup dict (user-provided per-target description)."""
    schema = _load_schema("writeup")
    jsonschema.validate(instance=writeup, schema=schema)


def validate_primitive_library(library: dict[str, Any]) -> None:
    """Validate a PrimitiveLibrary dict (the strategy knowledge base)."""
    schema = _load_schema("primitive_library")
    jsonschema.validate(instance=library, schema=schema)


def validate_deception_plan(plan: dict[str, Any]) -> None:
    """Validate a DeceptionPlan dict (v3 §6.1 schema, human-readable).

    This is the planner subagent's output. The deception-validator subagent
    calls this, then translates the plan to deception_config.yaml (schema 0.1.0)
    for proxy consumption.
    """
    schema = _load_schema("deception_plan")
    jsonschema.validate(instance=plan, schema=schema)


def validate_runtime_program(program: dict[str, Any]) -> None:
    """Validate a compiled RuntimeProgram dict."""
    schema = _load_schema("runtime_program")
    jsonschema.validate(instance=program, schema=schema)


def validate_runtime_event(event: dict[str, Any]) -> None:
    """Validate a RuntimeEvent dict."""
    schema = _load_schema("runtime_event")
    jsonschema.validate(instance=event, schema=schema)


def validate_agent_session_event(event: dict[str, Any]) -> None:
    """Validate one canonical agent session event (agent_session_event.v1)."""
    schema = _load_schema("agent_session_event")
    jsonschema.validate(instance=event, schema=schema)


def validate_command_agent_config(cfg: dict[str, Any]) -> None:
    """Validate a command-agent adapter config (command_agent_config.v1)."""
    schema = _load_schema("command_agent_config")
    jsonschema.validate(instance=cfg, schema=schema)


def validate_target_state_fingerprint(fingerprint: dict[str, Any]) -> None:
    schema = _load_schema("target_state_fingerprint")
    jsonschema.validate(instance=fingerprint, schema=schema)


def validate_basket_ownership_fixture_v2(fixture: dict[str, Any]) -> None:
    schema = _load_schema("basket_ownership_fixture_v2")
    jsonschema.validate(instance=fixture, schema=schema)


def validate_normalized_findings(report: dict[str, Any]) -> None:
    """Validate a normalized pentest findings report."""
    schema = _load_schema("normalized_findings")
    jsonschema.validate(instance=report, schema=schema)


def validate_behavioral_review_packet(packet: dict[str, Any]) -> None:
    """Validate a machine-readable behavior-first expert review packet."""
    schema = _load_schema("behavioral_review_packet")
    jsonschema.validate(instance=packet, schema=schema)


@dataclass
class PrimitiveConfig:
    primitive: str
    coupling_variant: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PrimitiveConfig":
        return cls(
            primitive=d["primitive"],
            coupling_variant=d["coupling_variant"],
            params=d.get("params", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"primitive": self.primitive, "coupling_variant": self.coupling_variant, "params": self.params}


@dataclass
class Budget:
    wall_clock_s: int = 900
    max_turns: int = 300
    max_tokens: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Budget":
        return cls(
            wall_clock_s=d["wall_clock_s"],
            max_turns=d["max_turns"],
            max_tokens=d.get("max_tokens"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"wall_clock_s": self.wall_clock_s, "max_turns": self.max_turns, "max_tokens": self.max_tokens}


@dataclass
class AgentSpec:
    driver: str
    llm_backbone: str
    subagent: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentSpec":
        return cls(driver=d["driver"], llm_backbone=d["llm_backbone"], subagent=d.get("subagent"))

    def to_dict(self) -> dict[str, Any]:
        return {"driver": self.driver, "llm_backbone": self.llm_backbone, "subagent": self.subagent}


@dataclass
class EpisodeSpec:
    """Typed wrapper around the EpisodeSpec JSON Schema."""

    schema_version: str = "0.1.0"
    episode_id: str = field(default_factory=lambda: f"ep_{uuid.uuid4().hex[:12]}")
    task_id: str = "T1"
    baseline: str = "B0"
    primitive_config: list[PrimitiveConfig] = field(default_factory=list)
    ablated_primitives: list[str] = field(default_factory=list)
    defense_posture: str = "none"
    budget: Budget = field(default_factory=Budget)
    seed: int = 42
    agent: AgentSpec = field(default_factory=lambda: AgentSpec(driver="claude_code", llm_backbone="glm-5.2"))
    task_overrides: dict[str, Any] = field(default_factory=dict)
    run_idx: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EpisodeSpec":
        validate_episode_spec(d)
        return cls(
            schema_version=d.get("schema_version", "0.1.0"),
            episode_id=d["episode_id"],
            task_id=d["task_id"],
            baseline=d["baseline"],
            primitive_config=[PrimitiveConfig.from_dict(p) for p in d.get("primitive_config", [])],
            ablated_primitives=d.get("ablated_primitives", []),
            defense_posture=d.get("defense_posture", "none"),
            budget=Budget.from_dict(d["budget"]),
            seed=d["seed"],
            agent=AgentSpec.from_dict(d["agent"]) if "agent" in d else AgentSpec(driver="claude_code", llm_backbone="glm-5.2"),
            task_overrides=d.get("task_overrides", {}),
            run_idx=d.get("run_idx"),
        )

    @classmethod
    def from_json(cls, path: str | Path) -> "EpisodeSpec":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        # Strip _comment before validation (schema allows it but the dataclass doesn't carry it)
        d.pop("_comment", None)
        return cls.from_dict(d)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "task_id": self.task_id,
            "baseline": self.baseline,
            "primitive_config": [p.to_dict() for p in self.primitive_config],
            "ablated_primitives": list(self.ablated_primitives),
            "defense_posture": self.defense_posture,
            "budget": self.budget.to_dict(),
            "seed": self.seed,
            "agent": self.agent.to_dict(),
            "task_overrides": dict(self.task_overrides),
            "run_idx": self.run_idx,
        }
        return d

    def to_json(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    def to_env_vars(self) -> dict[str, str]:
        """Materialize the legacy ATOBENCH_* env vars from this spec.

        Used by the env-compat shim so services/_deception_tag.py and the
        episode runner keep reading the same configuration.
        """
        env: dict[str, str] = {
            "ATOBENCH_EPISODE_ID": self.episode_id,
            "ATOBENCH_TASK": self.task_id,
            "ATOBENCH_BASELINE": self.baseline,
            "ATOBENCH_AGENT_DRIVER": self.agent.driver,
            "ATOBENCH_LLM_BACKBONE": self.agent.llm_backbone,
        }
        if self.ablated_primitives:
            env["ATOBENCH_ABLATE_PRIMITIVE"] = ",".join(self.ablated_primitives)
        if self.primitive_config:
            # Legacy mechanism: one global coupling variant per episode
            # (the variant of the first active primitive).
            env["ATOBENCH_COUPLING_VARIANT"] = self.primitive_config[0].coupling_variant
        if self.agent.subagent:
            env["ATOBENCH_SUBAGENT"] = self.agent.subagent
        if self.run_idx is not None:
            env["ATOBENCH_M5_RUN_IDX"] = str(self.run_idx)
        return env


def from_env_vars() -> EpisodeSpec:
    """Materialize an EpisodeSpec from the legacy ATOBENCH_* env vars.

    Inverse of EpisodeSpec.to_env_vars(). Used when a legacy script invokes
    the runner with env vars; the runner can normalize to an EpisodeSpec
    internally without requiring the user to provide JSON.
    """
    task_id = os.environ.get("ATOBENCH_TASK", "T1")
    baseline = os.environ.get("ATOBENCH_BASELINE", "B0")
    ablate_str = os.environ.get("ATOBENCH_ABLATE_PRIMITIVE", "").strip()
    ablated = [p.strip() for p in ablate_str.split(",") if p.strip()] if ablate_str else []
    coupling = os.environ.get("ATOBENCH_COUPLING_VARIANT", "loose")
    episode_id = os.environ.get("ATOBENCH_EPISODE_ID") or f"ep_{uuid.uuid4().hex[:12]}"
    driver = os.environ.get("ATOBENCH_AGENT_DRIVER", "claude_code")
    llm = os.environ.get("ATOBENCH_LLM_BACKBONE", "glm-5.2")
    subagent = os.environ.get("ATOBENCH_SUBAGENT")
    run_idx_str = os.environ.get("ATOBENCH_M5_RUN_IDX")
    run_idx = int(run_idx_str) if run_idx_str and run_idx_str.isdigit() else None

    # Defense posture: legacy --defense-posture flag passed via env
    defense = os.environ.get("ATOBENCH_DEFENSE_POSTURE", "none")

    # Primitive config: legacy mode has at most one (primitive, coupling) active
    # per episode, derived from the ablate list inversion. The actual primitive
    # name is not in env vars (the legacy mechanism ablates everything except
    # the targeted primitive); we record the ablated list and let the runner
    # fill in primitive_config from the sweep config.
    primitive_config: list[PrimitiveConfig] = []
    # If coupling is set and not "loose", assume the un-ablated primitive is the active one.
    # We can't know its name from env vars alone; the caller must patch this.
    primitive_config.append(PrimitiveConfig(primitive="__legacy__", coupling_variant=coupling))

    spec = EpisodeSpec(
        episode_id=episode_id,
        task_id=task_id,
        baseline=baseline,
        primitive_config=primitive_config,
        ablated_primitives=ablated,
        defense_posture=defense,
        seed=int(os.environ.get("ATOBENCH_SEED", "42")),
        agent=AgentSpec(driver=driver, llm_backbone=llm, subagent=subagent),
        run_idx=run_idx,
    )
    return spec
