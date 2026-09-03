"""Configuration model for reproducible ATOBench experiment cycles."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class TargetConfig:
    name: str
    target_dir: Path
    target_url: str
    compose_file: Path | None = None
    health_url: str | None = None
    health_expected_status: int = 200
    architecture: str = "monolith"
    stack_hint: str = "unknown"
    source_repo: str | None = None
    discovery_paths: list[str] = field(default_factory=list)
    known_real_vulns: list[dict[str, Any]] = field(default_factory=list)
    fake_value_sources: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentConfig:
    driver: str = "claude-code"
    subagent_type: str = "agentic-pentest-benchmark"
    model: str | None = None
    model_selector: str | None = None
    claude_effort: str | None = None
    timeout_s: int = 600
    defense_posture: str = "strong"
    max_tool_calls: int = 80
    calibration_focus: str | None = None


@dataclass
class RuntimeConfig:
    deception_id: str
    clean_episode_id: str
    deception_episode_id: str
    clean_proxy_port: int = 8100
    deception_proxy_port: int = 8101
    task: str = "T3"
    log_dir: Path = Path("logs")


@dataclass
class BenchmarkSuiteConfig:
    suite_id: str | None = None
    suite_dir: Path | None = None
    active_program: str = "c1_core"
    c0_runtime_program: Path | None = None
    c1_runtime_program: Path | None = None
    c2_runtime_program: Path | None = None
    enforce_frozen: bool = False


@dataclass
class SubagentConfig:
    executor: str = "prompt_files"
    planning_mode: str = "trajectory_aware"
    non_contact_paths: list[str] = field(default_factory=list)
    required: list[str] = field(
        default_factory=lambda: [
            "deception-recon",
            "deception-planner",
            "deception-consistency",
            "deception-validator",
            "report-normalizer",
        ]
    )
    optional: list[str] = field(default_factory=lambda: ["deception-static-analyzer"])
    experimental: list[str] = field(
        default_factory=lambda: ["deception-coverage-planner", "deception-content-generator"]
    )


@dataclass
class ProtocolV3Config:
    execution_spec: Path | None = None
    lock_output: Path | None = None
    target_state_contract: Path | None = None
    role: str = "confirmatory_collection"
    unit_id: str | None = None
    validation_episodes_are_excluded: bool = True


@dataclass
class ExperimentConfig:
    experiment_id: str
    target: TargetConfig
    runtime: RuntimeConfig
    agent: AgentConfig = field(default_factory=AgentConfig)
    subagents: SubagentConfig = field(default_factory=SubagentConfig)
    benchmark_suite: BenchmarkSuiteConfig = field(default_factory=BenchmarkSuiteConfig)
    protocol_v3: ProtocolV3Config = field(default_factory=ProtocolV3Config)

    @property
    def work_dir(self) -> Path:
        return self.target.target_dir / "experiments" / self.experiment_id

    @property
    def deception_dir(self) -> Path:
        return self.target.target_dir / "deceptions" / self.runtime.deception_id

    @property
    def clean_run_dir(self) -> Path:
        return self.target.target_dir / "scaffold_work" / "clean_run"


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"experiment config root must be a mapping: {config_path}")

    target_raw = _mapping(raw.get("target"), "target")
    runtime_raw = _mapping(raw.get("runtime"), "runtime")
    agent_raw = _mapping(raw.get("agent", {}), "agent")
    subagents_raw = _mapping(raw.get("subagents", {}), "subagents")
    suite_raw = raw.get("benchmark_suite", {})
    if suite_raw is None:
        suite_raw = {}
    suite_raw = _mapping(suite_raw, "benchmark_suite")
    protocol_v3_raw = raw.get("protocol_v3", {})
    if protocol_v3_raw is None:
        protocol_v3_raw = {}
    protocol_v3_raw = _mapping(protocol_v3_raw, "protocol_v3")

    target_dir = _path(target_raw["target_dir"], config_path.parent)
    compose_file = target_raw.get("compose_file")

    known_real_vulns = list(target_raw.get("known_real_vulns", []))
    known_real_vulns_file = target_raw.get("known_real_vulns_file")
    if known_real_vulns_file:
        source_path = _path(known_real_vulns_file, config_path.parent)
        with open(source_path, "r", encoding="utf-8") as handle:
            source_raw = yaml.safe_load(handle) or {}
        known_real_vulns = list(_mapping(source_raw.get("target"), "known_real_vulns_file.target").get("known_real_vulns", []))

    target = TargetConfig(
        name=str(target_raw["name"]),
        target_dir=target_dir,
        target_url=str(target_raw["target_url"]),
        compose_file=_path(compose_file, config_path.parent) if compose_file else target_dir / "docker-compose.yml",
        health_url=str(target_raw.get("health_url") or target_raw["target_url"]),
        health_expected_status=int(target_raw.get("health_expected_status", 200)),
        architecture=str(target_raw.get("architecture", "monolith")),
        stack_hint=str(target_raw.get("stack_hint", "unknown")),
        source_repo=target_raw.get("source_repo"),
        discovery_paths=list(target_raw.get("discovery_paths", [])),
        known_real_vulns=known_real_vulns,
        fake_value_sources=dict(target_raw.get("fake_value_sources", {})),
    )
    runtime = RuntimeConfig(
        deception_id=str(runtime_raw["deception_id"]),
        clean_episode_id=str(runtime_raw.get("clean_episode_id") or f"ep_b0_{target.name}"),
        deception_episode_id=str(runtime_raw.get("deception_episode_id") or f"ep_b3_{target.name}"),
        clean_proxy_port=int(runtime_raw.get("clean_proxy_port", 8100)),
        deception_proxy_port=int(runtime_raw.get("deception_proxy_port", 8101)),
        task=str(runtime_raw.get("task", "T3")),
        log_dir=_path(runtime_raw.get("log_dir", "../../logs"), config_path.parent),
    )
    agent = AgentConfig(
        driver=str(agent_raw.get("driver", "claude-code")),
        subagent_type=str(agent_raw.get("subagent_type", "agentic-pentest-benchmark")),
        model=agent_raw.get("model"),
        model_selector=agent_raw.get("model_selector"),
        claude_effort=agent_raw.get("claude_effort"),
        timeout_s=int(agent_raw.get("timeout_s", 600)),
        defense_posture=str(agent_raw.get("defense_posture", "strong")),
        max_tool_calls=int(agent_raw.get("max_tool_calls", 80)),
        calibration_focus=agent_raw.get("calibration_focus"),
    )
    if agent.claude_effort and agent.claude_effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("agent.claude_effort must be one of low, medium, high, xhigh, max")
    subagents = SubagentConfig(
        executor=str(subagents_raw.get("executor", "prompt_files")),
        planning_mode=str(subagents_raw.get("planning_mode", "trajectory_aware")),
        non_contact_paths=list(subagents_raw.get("non_contact_paths", [])),
        required=list(subagents_raw.get("required", SubagentConfig().required)),
        optional=list(subagents_raw.get("optional", SubagentConfig().optional)),
        experimental=list(subagents_raw.get("experimental", SubagentConfig().experimental)),
    )
    if subagents.planning_mode not in {"trajectory_aware", "static_inventory", "non_contact_control"}:
        raise ValueError("subagents.planning_mode must be trajectory_aware, static_inventory, or non_contact_control")
    if subagents.planning_mode == "non_contact_control" and not subagents.non_contact_paths:
        raise ValueError("non_contact_control requires subagents.non_contact_paths")

    suite_id = suite_raw.get("suite_id")
    suite_dir = None
    if suite_raw.get("suite_dir"):
        suite_dir = _path(suite_raw["suite_dir"], config_path.parent)
    elif suite_id:
        suite_dir = target.target_dir / "benchmark_suites" / str(suite_id)

    def suite_path(key: str, default_program: str) -> Path | None:
        value = suite_raw.get(key)
        if value:
            return _path(value, config_path.parent)
        if suite_dir:
            return suite_dir / "programs" / default_program / "runtime_program.yaml"
        return None

    benchmark_suite = BenchmarkSuiteConfig(
        suite_id=str(suite_id) if suite_id else None,
        suite_dir=suite_dir,
        active_program=str(suite_raw.get("active_program", "c1_core")),
        c0_runtime_program=suite_path("c0_runtime_program", "c0_identity"),
        c1_runtime_program=suite_path("c1_runtime_program", "c1_core"),
        c2_runtime_program=suite_path("c2_runtime_program", "c2_selected"),
        enforce_frozen=bool(suite_raw.get("enforce_frozen", False)),
    )
    execution_spec = protocol_v3_raw.get("execution_spec")
    lock_output = protocol_v3_raw.get("lock_output")
    target_state_contract = protocol_v3_raw.get("target_state_contract")
    protocol_v3 = ProtocolV3Config(
        execution_spec=_path(execution_spec, config_path.parent) if execution_spec else None,
        lock_output=_path(lock_output, config_path.parent) if lock_output else None,
        target_state_contract=_path(target_state_contract, config_path.parent) if target_state_contract else None,
        role=str(protocol_v3_raw.get("role", "confirmatory_collection")),
        unit_id=str(protocol_v3_raw["unit_id"]) if protocol_v3_raw.get("unit_id") else None,
        validation_episodes_are_excluded=bool(protocol_v3_raw.get("validation_episodes_are_excluded", True)),
    )
    return ExperimentConfig(
        experiment_id=str(raw.get("experiment_id") or runtime.deception_id),
        target=target,
        runtime=runtime,
        agent=agent,
        subagents=subagents,
        benchmark_suite=benchmark_suite,
        protocol_v3=protocol_v3,
    )


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"experiment config requires mapping field: {name}")
    return value


def _path(value: Any, base_dir: Path) -> Path:
    p = Path(str(value)).expanduser()
    if p.is_absolute():
        return p
    return (base_dir / p).resolve()
