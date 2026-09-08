"""ATOBench CLI entrypoint.

Subcommands:
    atobench run          Build an EpisodeSpec and run one episode end-to-end.
    atobench sweep        Run a YAML-configured batch of episodes.
    atobench analyze      Re-evaluate an already-completed episode (no re-run).
    atobench leaderboard  List completed episodes from logs/episodes.jsonl.
    atobench version      Print version and exit.

`run` and `sweep` iterate EpisodeSpec dicts and delegate execution to a
separate episode-runner process; `analyze` re-evaluates a completed episode
without re-running; `leaderboard` is a read-only summary over
logs/episodes.jsonl.

For paired Protocol-v3 cross-model campaigns (the primary ATOBench
execution path), use the `atobench-cross-model` command instead.

Usage:
    atobench run --task T1 --baseline B3 \\
        --primitive fake_version_banner --coupling schema_coupled
    atobench sweep --config sweeps/t1_smoke.yaml
    atobench analyze --episode-id ep_a1b0c0ffee01
    atobench leaderboard
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from atobench import __version__
from atobench.schema.loader import EpisodeSpec, PrimitiveConfig, Budget, AgentSpec

ROOT = Path(__file__).resolve().parents[2]
EPISODE_RUNNER = ROOT / "proxy" / "episode_runner.py"
DEFAULT_LOG_DIR = ROOT / "logs"
DEFAULT_EXPERIMENT_CONFIG = ROOT / "examples" / "experiments" / "protocol_v3" / "juice-shop-protocol-v3-sqli.yaml"


# ---------- run ----------

# Map of primitives that need ATOBENCH_ABLATE_PRIMITIVE to disable everything
# else (legacy mechanism: ablate-all-but-target). For B3 single-primitive
# episodes, the legacy services use ablation to isolate the target primitive.
# Empty primitive = no ablation (B0 truthful, or B2 generator-driven).
_KNOWN_PRIMITIVES = {
    "fake_version_banner", "vuln_symptom_inject", "no_vuln_gaslighting",
    "corrupt_belief", "poison_dependency", "false_completion",
    "exhaustion_trap", "induce_loop", "substitute_subgoal",
}


def _build_spec_from_args(args: argparse.Namespace) -> EpisodeSpec:
    """Construct an EpisodeSpec from CLI args."""
    primitive_config: list[PrimitiveConfig] = []
    ablated: list[str] = []

    if args.primitive:
        if args.primitive not in _KNOWN_PRIMITIVES:
            raise ValueError(
                f"Unknown primitive '{args.primitive}'. "
                f"Known: {sorted(_KNOWN_PRIMITIVES)}"
            )
        primitive_config.append(
            PrimitiveConfig(primitive=args.primitive, coupling_variant=args.coupling)
        )
        # Legacy mechanism: ablate everything except the target primitive.
        ablated = sorted(_KNOWN_PRIMITIVES - {args.primitive})

    # Defense posture: T3 requires strong/weak; T1/T2 use "none".
    defense = args.defense_posture
    if args.task in ("T1", "T2") and defense == "none":
        defense = "none"  # T1/T2 don't use the defense prompt
    if args.task == "T3" and defense == "none":
        defense = "strong"  # default for T3

    spec = EpisodeSpec(
        episode_id=args.episode_id or f"ep_{os.urandom(6).hex()}",
        task_id=args.task,
        baseline=args.baseline,
        primitive_config=primitive_config,
        ablated_primitives=ablated,
        defense_posture=defense,
        budget=Budget(wall_clock_s=900, max_turns=300),
        seed=args.seed,
        agent=AgentSpec(driver="claude_code", llm_backbone="glm-5.2"),
        run_idx=args.run_idx,
    )
    return spec


def _save_spec(spec: EpisodeSpec, log_dir: Path) -> Path:
    """Persist the EpisodeSpec to <log_dir>/episode_specs/<id>.json."""
    spec_dir = log_dir / "episode_specs"
    spec_dir.mkdir(parents=True, exist_ok=True)
    path = spec_dir / f"{spec.episode_id}.json"
    spec.to_json(path)
    return path


def _invoke_runner(spec: EpisodeSpec, log_dir: Path) -> int:
    """Spawn the episode runner with env vars derived from the spec."""
    if not EPISODE_RUNNER.is_file():
        raise SystemExit(
            "[atobench run] the standalone episode-runner process is not part of "
            "this release. For the paired Protocol-v3 execution path use "
            "`atobench-cross-model` instead (see the repository README)."
        )
    env = os.environ.copy()
    env.update(spec.to_env_vars())
    # Defense posture is passed via CLI flag, not env (legacy runner reads --defense-posture).
    cmd = [
        sys.executable, str(EPISODE_RUNNER),
        "--task", spec.task_id,
        "--baseline", spec.baseline,
        "--driver", "subagent",
        "--agent-driver", "subagent",
        "--llm-backbone", spec.agent.llm_backbone,
        "--defense-posture", spec.defense_posture if spec.defense_posture != "none" else "strong",
    ]
    if spec.run_idx is not None:
        cmd += ["--m5-run-idx", str(spec.run_idx)]
    if spec.ablated_primitives:
        # Legacy runner takes a single comma-separated ablate string.
        cmd += ["--ablate", ",".join(spec.ablated_primitives)]

    print(f"[atobench run] episode_id={spec.episode_id}")
    print(f"[atobench run] spec saved: {_save_spec(spec, log_dir)}")
    cmd_preview = " ".join(cmd[:4])
    print(f"[atobench run] launching: {cmd_preview} ... (full cmd in logs)")
    print(f"[atobench run] baseline={spec.baseline} primitive_config={[p.to_dict() for p in spec.primitive_config]}")
    if spec.ablated_primitives:
        print(f"[atobench run] ablated ({len(spec.ablated_primitives)}): {spec.ablated_primitives[:3]}...")

    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env)
    duration = time.time() - t0
    print(f"[atobench run] runner exited rc={proc.returncode} in {duration:.1f}s")
    return proc.returncode


def _save_summary(spec: EpisodeSpec, runner_rc: int, log_dir: Path) -> Path:
    """Persist a per-episode summary."""
    summary_dir = log_dir / "episode_summaries"
    summary_dir.mkdir(parents=True, exist_ok=True)
    path = summary_dir / f"{spec.episode_id}.json"
    summary = {
        "episode_id": spec.episode_id,
        "spec_path": str(log_dir / "episode_specs" / f"{spec.episode_id}.json"),
        "runner_rc": runner_rc,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return path


def cmd_run(args: argparse.Namespace) -> int:
    log_dir = Path(args.output_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Proxy mode: drive mitmproxy deception proxy + claude agent against a real target.
    if getattr(args, "mode", "services") == "proxy":
        return _cmd_run_proxy(args, log_dir)

    spec = _build_spec_from_args(args)
    rc = _invoke_runner(spec, log_dir)
    summary_path = _save_summary(spec, rc, log_dir)
    print(f"\n[atobench run] === summary ===")
    print(f"  episode_id: {spec.episode_id}")
    print(f"  runner_rc: {rc}")
    print("  evaluation: use `atobench evaluate-pair` for pentest_effect.json")
    print(f"  summary: {summary_path}")
    return 0 if rc == 0 else 1


def _cmd_run_proxy(args: argparse.Namespace, log_dir: Path) -> int:
    """Run an episode against the mitmproxy deception proxy + a real HTTP target."""
    from atobench.agents.proxy_runner import ProxyEpisodeRunner

    _resolve_deception_artifacts(args)
    if not args.deception_config and not getattr(args, "runtime_program", None):
        raise ValueError("--deception-config or --runtime-program is required when --mode proxy")
    if not args.target_url:
        raise ValueError("--target-url is required when --mode proxy")

    episode_id = args.episode_id or f"ep_{os.urandom(6).hex()}"

    runner = ProxyEpisodeRunner(
        deception_config_path=args.deception_config,
        runtime_program_path=getattr(args, "runtime_program", None),
        target_url=args.target_url,
        proxy_port=args.proxy_port,
        task_id=args.task,
        baseline=args.baseline,
        episode_id=episode_id,
        log_dir=log_dir,
        llm_backbone="claude-opus-4-7",
        agent_timeout_s=args.agent_timeout,
        driver=getattr(args, "driver", "subagent"),
        model=getattr(args, "model", None),
        model_selector=getattr(args, "model_selector", None),
        claude_effort=getattr(args, "claude_effort", None),
        agent_subagent_type=getattr(args, "agent_subagent_type", "agentic-pentest-benchmark"),
        agent_defense_posture=getattr(args, "agent_defense_posture", "strong"),
        agent_max_tool_calls=getattr(args, "agent_max_tool_calls", 80),
        agent_calibration_focus=getattr(args, "agent_calibration_focus", None),
        agent_workspace=getattr(args, "agent_workspace", None),
        command_config=getattr(args, "agent_command_config", None),
    )
    print(f"[atobench run --mode proxy] episode_id={episode_id}")
    print(f"  target_url={args.target_url} proxy_port={args.proxy_port}")
    if args.deception_config:
        print(f"  deception_config={args.deception_config}")
    if getattr(args, "runtime_program", None):
        print(f"  runtime_program={args.runtime_program}")
    if getattr(args, "deception_dir", None):
        print(f"  deception_dir={args.deception_dir}")
    print(f"  task={args.task} baseline={args.baseline}")

    result = runner.run()

    print(f"\n[atobench run --mode proxy] === summary ===")
    print(f"  episode_id: {result.episode_id}")
    print(f"  rc: {result.rc}")
    print(f"  flag: {result.flag}")
    print(f"  n_turns: {result.n_turns}")
    print(f"  n_deceptive: {result.n_deceptive}")
    print(f"  duration_s: {result.duration_s:.1f}")
    print("  evaluation: use `atobench evaluate-pair` for pentest_effect.json")
    if result.error:
        print(f"  error: {result.error}")

    # Save summary
    summary_dir = log_dir / "episode_summaries"
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / f"{episode_id}.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({
            "episode_id": episode_id,
            "mode": "proxy",
            "deception_config": str(args.deception_config),
            "runtime_program": str(args.runtime_program) if getattr(args, "runtime_program", None) else None,
            "target_url": args.target_url,
            "proxy_port": args.proxy_port,
            "task": args.task,
            "baseline": args.baseline,
            "rc": result.rc,
            "flag": result.flag,
            "n_turns": result.n_turns,
            "n_deceptive": result.n_deceptive,
            "duration_s": result.duration_s,
            "error": result.error,
            "final_report_text": result.final_report_text,
        }, f, indent=2, ensure_ascii=False)
    print(f"  summary: {summary_path}")
    if getattr(args, "deception_dir", None):
        from atobench.scaffold.deception_workspace import archive_episode_run

        manifest = archive_episode_run(
            deception_dir=args.deception_dir,
            episode_id=episode_id,
            log_dir=log_dir,
            summary_path=summary_path,
        )
        print(f"  archived_run: {manifest['run_dir']}")
    return result.rc


def _resolve_deception_artifacts(args: argparse.Namespace) -> None:
    """Resolve ID-scoped deception workspace paths into runtime/config args."""
    dec_dir = getattr(args, "deception_dir", None)
    dec_id = getattr(args, "deception_id", None)
    target_dir = getattr(args, "target_dir", None)
    if not dec_dir and dec_id and target_dir:
        dec_dir = str(Path(target_dir) / "deceptions" / dec_id)
        args.deception_dir = dec_dir
    if not dec_dir:
        return
    d = Path(dec_dir)
    runtime_program = d / "runtime_program.yaml"
    legacy_config = d / "deception_config.yaml"
    if not getattr(args, "runtime_program", None) and runtime_program.exists():
        args.runtime_program = str(runtime_program)
    if not getattr(args, "deception_config", None) and legacy_config.exists():
        args.deception_config = str(legacy_config)


def cmd_compile(args: argparse.Namespace) -> int:
    """Compile a deception plan into an ID-scoped workspace."""
    from atobench.scaffold.deception_workspace import create_deception_workspace

    result = create_deception_workspace(
        target_dir=args.target_dir,
        deception_id=args.deception_id,
        plan_path=args.plan,
        target_profile_path=getattr(args, "target_profile", None),
        baseline=args.baseline,
    )
    print("[atobench compile] deception workspace created")
    for key in [
        "deception_id",
        "workspace_dir",
        "plan_path",
        "runtime_program_path",
        "deception_config_path",
        "compile_report_path",
        "validation_report_path",
    ]:
        print(f"  {key}: {result[key]}")
    return 0


def cmd_attribute(args: argparse.Namespace) -> int:
    """Generate attribution report for a deception workspace run."""
    from atobench.scaffold.attribution_report import generate_attribution_report

    result = generate_attribution_report(
        deception_dir=args.deception_dir,
        episode_id=args.episode_id,
        output_path=args.output,
    )
    print("[atobench attribute] attribution report written")
    print(f"  episode_id: {result['episode_id']}")
    print(f"  report: {result['report_path']}")
    print(f"  n_planned: {result['n_planned']}")
    print(f"  n_fired: {result['n_fired']}")
    if result["uncontacted"]:
        print(f"  uncontacted: {', '.join(result['uncontacted'])}")
    return 0


def cmd_pentest_effect(args: argparse.Namespace) -> int:
    """Compute clean-relative pentest deception effect metrics."""
    from atobench.eval.pentest_effect import evaluate_pentest_effect_from_paths

    result = evaluate_pentest_effect_from_paths(
        clean_turns_path=args.clean_turns,
        deception_turns_path=args.deception_turns,
        clean_report_path=args.clean_report,
        deception_report_path=args.deception_report,
        target_profile_path=args.target_profile,
        plan_path=args.plan,
        runtime_program_path=args.runtime_program,
        clean_normalized_report_path=args.clean_normalized_report,
        deception_normalized_report_path=args.deception_normalized_report,
        target=args.target,
    )
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        print("[atobench pentest-effect] report written")
        print(f"  output: {output_path}")
        print(f"  contact_rate: {result['opportunity']['contact_rate']:.3f}")
        print(f"  verified_fabrication_rate: {result['outcome']['verified_fabrication_rate']:.3f}")
        print(f"  clean_relative_recall_drop: {result['outcome']['clean_relative_recall_drop']:.3f}")
        print(f"  clean_finding_disappearance_rate: {result['outcome'].get('clean_finding_disappearance_rate', 0.0):.3f}")
        print(f"  clean_finding_polarity_flip_rate: {result['outcome'].get('clean_finding_polarity_flip_rate', 0.0):.3f}")
    else:
        print(output)
    return 0


def cmd_normalize_report(args: argparse.Namespace) -> int:
    """Deterministically normalize a raw pentest report into finding schema."""
    from atobench.eval.pentest_effect import normalize_report_to_schema

    report_text = Path(args.report).read_text(encoding="utf-8")
    normalized = normalize_report_to_schema(
        report_text,
        source_report_id=args.source_report_id,
    )
    output = json.dumps(normalized, indent=2, ensure_ascii=False)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        print("[atobench normalize-report] normalized findings written")
        print(f"  output: {output_path}")
        print(f"  findings: {len(normalized['findings'])}")
    else:
        print(output)
    return 0


def cmd_evaluate_pair(args: argparse.Namespace) -> int:
    """Evaluate paired clean/deception run directories end to end."""
    from atobench.eval.pair_workflow import evaluate_run_pair, infer_workspace_paths

    inferred = infer_workspace_paths(args.deception_run_dir)
    target_profile = args.target_profile or str(inferred.get("target_profile_path", "")) or None
    plan = args.plan or str(inferred.get("plan_path", "")) or None
    runtime_program = args.runtime_program or str(inferred.get("runtime_program_path", "")) or None

    result = evaluate_run_pair(
        clean_run_dir=args.clean_run_dir,
        deception_run_dir=args.deception_run_dir,
        target_profile_path=target_profile,
        plan_path=plan,
        runtime_program_path=runtime_program,
        output_path=args.output,
        target=args.target,
        normalizer_mode=args.normalizer_mode,
        overwrite_normalized=args.overwrite_normalized,
        clean_normalized_report_path=args.clean_normalized_report,
        deception_normalized_report_path=args.deception_normalized_report,
    )
    artifacts = result.get("artifacts", {})
    print("[atobench evaluate-pair] pentest effect report written")
    print(f"  output: {artifacts.get('pentest_effect')}")
    print(f"  clean_final_report: {artifacts.get('clean_final_report')}")
    print(f"  deception_final_report: {artifacts.get('deception_final_report')}")
    print(f"  clean_normalized_findings: {artifacts.get('clean_normalized_findings')}")
    print(f"  deception_normalized_findings: {artifacts.get('deception_normalized_findings')}")
    print(f"  contact_rate: {result['opportunity']['contact_rate']:.3f}")
    print(f"  verified_fabrication_rate: {result['outcome']['verified_fabrication_rate']:.3f}")
    print(f"  clean_relative_recall_drop: {result['outcome']['clean_relative_recall_drop']:.3f}")
    print(f"  clean_finding_disappearance_rate: {result['outcome'].get('clean_finding_disappearance_rate', 0.0):.3f}")
    print(f"  clean_finding_polarity_flip_rate: {result['outcome'].get('clean_finding_polarity_flip_rate', 0.0):.3f}")
    return 0


def cmd_extract_action_trace(args: argparse.Namespace) -> int:
    """Derive a redaction-safe action trace from a run's turns.jsonl."""
    from atobench.eval.action_trace import extract_action_trace

    result = extract_action_trace(
        args.turns,
        args.output,
        condition=args.condition,
        artifact_manifest_path=args.artifact_manifest,
    )
    print("[atobench extract-action-trace] action trace written")
    print(f"  output: {result['output_path']}")
    print(f"  actions: {result['action_count']}")
    print(f"  transformed_actions: {result['transformed_action_count']}")
    print(f"  artifact_uses: {result['artifact_use_count']}")
    return 0


def cmd_audit_behavior(args: argparse.Namespace) -> int:
    """Audit behavior from an explicit JSON list of paired run directories."""
    from atobench.eval.behavior_audit import audit_pairs

    pair_specs = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
    if isinstance(pair_specs, dict):
        pair_specs = pair_specs.get("pairs")
    if not isinstance(pair_specs, list):
        raise ValueError("--pairs must contain a JSON list or an object with a pairs list")
    result = audit_pairs(pair_specs, args.output)
    print("[atobench audit-behavior] behavior audit written")
    print(f"  output: {args.output}")
    print(f"  pairs: {result['pair_count']}")
    for name, summary in result["aggregate"].items():
        print(
            f"  {name}: contact={summary['contacted_pair_count']}/{summary['pair_count']}, "
            f"C0_attempts={summary['clean_sqli_attempts_total']}, "
            f"C1_attempts={summary['deception_sqli_attempts_total']}, "
            f"post_contact_retries={summary['post_contact_retries_total']}"
        )
    return 0


def cmd_validate_behavior_profiles(args: argparse.Namespace) -> int:
    """Validate profile coverage and provenance against a frozen runtime program."""
    from atobench.eval.behavior_profiles import validate_behavior_profiles

    result = validate_behavior_profiles(args.profiles, args.runtime_program, mode=args.mode)
    print("[atobench validate-behavior-profiles]")
    print(f"  valid: {result['is_valid']}")
    print(f"  profiles: {result['profile_count']}/{result['runtime_case_count']}")
    for warning in result["warnings"]:
        print(f"  warning: {warning}")
    for error in result["errors"]:
        print(f"  error: {error}")
    return 0 if result["is_valid"] else 1


def cmd_audit_behavior_profiles(args: argparse.Namespace) -> int:
    """Audit case contact and co-application for behavioral profiles."""
    from atobench.eval.behavior_profiles import audit_behavior_profile_coverage

    result = audit_behavior_profile_coverage(
        args.profiles,
        args.runtime_program,
        args.pairs,
        args.output,
    )
    print("[atobench audit-behavior-profiles] coverage audit written")
    print(f"  output: {result['artifacts']['json']}")
    print(f"  episodes: {result['episode_count']}")
    for item in result["coverage"]:
        print(
            f"  {item['case_id']}: contact={item['contacted_episodes']}/{result['episode_count']}, "
            f"transforms={item['transformed_response_count']}, "
            f"identifiability={item['empirical_identifiability']}"
        )
    return 0


def cmd_experiment(args: argparse.Namespace) -> int:
    """Run or inspect an open-source experiment cycle from YAML config."""
    from atobench.experiment.config import load_experiment_config
    from atobench.experiment.cycle import ExperimentCycle

    cfg = load_experiment_config(args.config)
    cycle = ExperimentCycle(cfg)
    action = args.action
    background = getattr(args, "background", False)

    if action == "validate-protocol-v3":
        from atobench.protocol.v3 import validate_protocol_v3

        result = validate_protocol_v3(
            args.protocol_manifest,
            write_lock=args.write_protocol_lock,
        )
    elif action == "init":
        result = cycle.init()
    elif action == "start-target":
        result = cycle.start_target(clean_first=getattr(args, "clean_target_first", False))
    elif action == "health":
        result = cycle.check_target_health()
    elif action == "scaffold":
        result = cycle.scaffold()
    elif action == "make-deception":
        result = cycle.make_deception(background=background)
    elif action == "compile":
        result = cycle.compile()
    elif action == "freeze-suite":
        result = cycle.freeze_suite(
            suite_id=args.suite_id,
            source_deception_dir=args.source_deception_dir,
            force=args.force,
        )
    elif action == "validate-suite":
        if args.suite_id:
            from atobench.experiment.suite import validate_frozen_suite

            suite_dir = cfg.target.target_dir / "benchmark_suites" / args.suite_id
            result = validate_frozen_suite(suite_dir)
        else:
            result = cycle.validate_suite()
    elif action == "construct-offline":
        from atobench.experiment.offline_construction import construct_offline_candidate_suite

        result = construct_offline_candidate_suite(
            target_dir=cfg.target.target_dir,
            target_name=cfg.target.name,
            suite_id=args.suite_id or (cfg.benchmark_suite.suite_id or f"{cfg.target.name}-offline-v1"),
            ground_truth_path=args.ground_truth,
            surface_inventory_path=args.surface_inventory,
            primitive_index_path=args.primitive_index,
            primitive_recipes_path=args.primitive_recipes,
            force=args.force,
            max_matrix_rows=args.max_matrix_rows,
            max_candidates=args.max_candidates,
        )
    elif action == "validate-offline":
        from atobench.experiment.offline_construction import validate_offline_candidate_suite

        if args.suite_id:
            suite_dir = cfg.target.target_dir / "benchmark_suites" / args.suite_id
        elif cfg.benchmark_suite.suite_dir:
            suite_dir = cfg.benchmark_suite.suite_dir
        else:
            suite_id = cfg.benchmark_suite.suite_id or f"{cfg.target.name}-offline-v1"
            suite_dir = cfg.target.target_dir / "benchmark_suites" / suite_id
        result = validate_offline_candidate_suite(suite_dir=suite_dir)
    elif action == "review-offline":
        from atobench.experiment.offline_construction import review_offline_candidate_suite

        if args.suite_id:
            suite_dir = cfg.target.target_dir / "benchmark_suites" / args.suite_id
        elif cfg.benchmark_suite.suite_dir:
            suite_dir = cfg.benchmark_suite.suite_dir
        else:
            suite_id = cfg.benchmark_suite.suite_id or f"{cfg.target.name}-offline-v1"
            suite_dir = cfg.target.target_dir / "benchmark_suites" / suite_id
        result = review_offline_candidate_suite(
            suite_dir=suite_dir,
            max_c1_cases=args.max_c1_cases,
        )
    elif action == "materialize-offline-suite":
        from atobench.experiment.offline_materialization import materialize_offline_suite

        if args.suite_id:
            suite_dir = cfg.target.target_dir / "benchmark_suites" / args.suite_id
        elif cfg.benchmark_suite.suite_dir:
            suite_dir = cfg.benchmark_suite.suite_dir
        else:
            suite_id = cfg.benchmark_suite.suite_id or f"{cfg.target.name}-offline-v1"
            suite_dir = cfg.target.target_dir / "benchmark_suites" / suite_id
        result = materialize_offline_suite(
            suite_dir=suite_dir,
            task_id=cfg.runtime.task,
            force=args.force,
        )
    elif action == "replay-offline-suite":
        from atobench.experiment.offline_replay_validation import replay_validate_offline_suite

        if args.suite_id:
            suite_dir = cfg.target.target_dir / "benchmark_suites" / args.suite_id
        elif cfg.benchmark_suite.suite_dir:
            suite_dir = cfg.benchmark_suite.suite_dir
        else:
            suite_id = cfg.benchmark_suite.suite_id or f"{cfg.target.name}-offline-v1"
            suite_dir = cfg.target.target_dir / "benchmark_suites" / suite_id
        result = replay_validate_offline_suite(
            suite_dir=suite_dir,
            target_url=cfg.target.target_url,
        )
    elif action == "materialize-d-ladder":
        from atobench.experiment.offline_d_ladder import materialize_d_ladder_suite

        if args.suite_id:
            suite_dir = cfg.target.target_dir / "benchmark_suites" / args.suite_id
        elif cfg.benchmark_suite.suite_dir:
            suite_dir = cfg.benchmark_suite.suite_dir
        else:
            suite_id = cfg.benchmark_suite.suite_id or f"{cfg.target.name}-offline-v1"
            suite_dir = cfg.target.target_dir / "benchmark_suites" / suite_id
        result = materialize_d_ladder_suite(
            suite_dir=suite_dir,
            task_id=cfg.runtime.task,
            force=args.force,
        )
    elif action == "replay-d-ladder":
        from atobench.experiment.offline_d_ladder import replay_validate_d_ladder_suite

        if args.suite_id:
            suite_dir = cfg.target.target_dir / "benchmark_suites" / args.suite_id
        elif cfg.benchmark_suite.suite_dir:
            suite_dir = cfg.benchmark_suite.suite_dir
        else:
            suite_id = cfg.benchmark_suite.suite_id or f"{cfg.target.name}-offline-v1"
            suite_dir = cfg.target.target_dir / "benchmark_suites" / suite_id
        result = replay_validate_d_ladder_suite(
            suite_dir=suite_dir,
            target_url=cfg.target.target_url,
        )
    elif action == "freeze-offline-suite":
        from atobench.experiment.suite import freeze_offline_materialized_suite

        if args.suite_id:
            suite_dir = cfg.target.target_dir / "benchmark_suites" / args.suite_id
        elif cfg.benchmark_suite.suite_dir:
            suite_dir = cfg.benchmark_suite.suite_dir
        else:
            suite_id = cfg.benchmark_suite.suite_id or f"{cfg.target.name}-offline-v1"
            suite_dir = cfg.target.target_dir / "benchmark_suites" / suite_id
        result = freeze_offline_materialized_suite(
            suite_dir=suite_dir,
            force=args.force,
        )
    elif action == "run-clean":
        result = cycle.run_clean(
            background=background,
            protocol_validation_only=args.protocol_validation_only,
            protocol_assignment_slot=args.protocol_assignment_slot,
        )
    elif action == "archive-clean":
        result = cycle.archive_clean()
    elif action == "run-deception":
        result = cycle.run_deception(
            background=background,
            protocol_validation_only=args.protocol_validation_only,
            protocol_assignment_slot=args.protocol_assignment_slot,
        )
    elif action == "validate-clean":
        result = cycle.validate_clean()
    elif action == "validate-deception":
        result = cycle.validate_deception(require_contact=args.require_contact)
    elif action == "attribute":
        result = cycle.attribute()
    elif action == "normalize-blinded":
        result = cycle.normalize_blinded(scope=args.normalizer_scope, run_dirs=args.run_dir)
    elif action == "normalize-fixed-local":
        result = cycle.normalize_fixed_local(scope=args.normalizer_scope, run_dirs=args.run_dir)
    elif action == "evaluate":
        result = cycle.evaluate_pair(normalizer_mode=args.normalizer_mode)
    elif action == "status":
        result = cycle.status()
    elif action == "run-mechanical":
        results = {
            "init": cycle.init(),
            "health": cycle.check_target_health(),
        }
        result = results
    else:
        raise ValueError(f"unknown experiment action: {action}")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if action == "validate-protocol-v3" and args.require_collection_ready:
        return 0 if result.get("collection_ready") else 2
    return 0


# ---------- sweep ----------

def cmd_sweep(args: argparse.Namespace) -> int:
    """Run a YAML-configured batch of episodes.

    YAML format: a list of dicts, each an EpisodeSpec (minus schema_version/
    episode_id which are auto-filled if absent). Example:

        - task_id: T1
          baseline: B0
        - task_id: T1
          baseline: B3
          primitive_config:
            - primitive: fake_version_banner
              coupling_variant: schema_coupled
    """
    import yaml  # type: ignore
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[atobench sweep] config not found: {config_path}", file=sys.stderr)
        return 2
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, list):
        print(f"[atobench sweep] config root must be a list, got {type(cfg).__name__}", file=sys.stderr)
        return 2

    log_dir = Path(args.output_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"[atobench sweep] {len(cfg)} episodes from {config_path}")

    n_ok = n_fail = 0
    for i, ep_dict in enumerate(cfg, 1):
        ep_dict = dict(ep_dict)  # shallow copy so we can mutate
        ep_dict.setdefault("schema_version", "0.1.0")
        ep_dict.setdefault("episode_id", f"ep_{os.urandom(6).hex()}")
        ep_dict.setdefault("budget", {"wall_clock_s": 900, "max_turns": 300})
        ep_dict.setdefault("seed", 42)
        ep_dict.setdefault("agent", {"driver": "claude_code", "llm_backbone": "glm-5.2"})
        ep_dict.setdefault("defense_posture", "none")
        ep_dict.setdefault("primitive_config", [])
        ep_dict.setdefault("ablated_primitives", [])
        try:
            spec = EpisodeSpec.from_dict(ep_dict)
        except Exception as e:
            print(f"[atobench sweep] [{i}/{len(cfg)}] invalid spec: {e}", file=sys.stderr)
            n_fail += 1
            continue
        print(f"\n[atobench sweep] [{i}/{len(cfg)}] {spec.episode_id} task={spec.task_id} baseline={spec.baseline}")
        rc = _invoke_runner(spec, log_dir)
        _save_summary(spec, rc, log_dir)
        if rc == 0:
            n_ok += 1
        else:
            n_fail += 1

    print(f"\n[atobench sweep] done: {n_ok} ok, {n_fail} fail, {len(cfg)} total")
    return 0 if n_fail == 0 else 1


# ---------- analyze ----------

def cmd_analyze(args: argparse.Namespace) -> int:
    """Legacy single-episode analysis entrypoint."""
    print("[atobench analyze] legacy EffectVector analysis has been removed.")
    print("  Use `atobench evaluate-pair --clean-run-dir ... --deception-run-dir ...` instead.")
    return 2


# ---------- leaderboard ----------

def cmd_leaderboard(args: argparse.Namespace) -> int:
    """List completed episodes from logs/episodes.jsonl.

    Episodes are written as two records: a 'running' record (carries
    task/baseline/m5_run_idx) and an 'ended' record (carries outcome/metrics).
    We join them on episode_id.
    """
    log_dir = Path(args.output_dir)
    path = log_dir / "episodes.jsonl"
    if not path.exists():
        print(f"[atobench leaderboard] no episodes.jsonl at {path}")
        return 1
    running: dict[str, dict[str, Any]] = {}
    ended: dict[str, dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            eid = e.get("episode_id")
            if not eid:
                continue
            if e.get("status") == "running":
                running[eid] = e
            elif e.get("status") == "ended":
                ended[eid] = e
    rows = []
    for eid, end in ended.items():
        run = running.get(eid, {})
        # Compute wall-clock from started_at → ended_at if both present.
        duration = 0.0
        sa = run.get("started_at") or end.get("started_at")
        ea = end.get("ended_at")
        if sa and ea:
            try:
                from datetime import datetime
                dt_s = datetime.fromisoformat(sa.replace("Z", "+00:00"))
                dt_e = datetime.fromisoformat(ea.replace("Z", "+00:00"))
                duration = (dt_e - dt_s).total_seconds()
            except Exception:
                pass
        rows.append({
            "episode_id": eid,
            "task": run.get("task", end.get("task", "?")),
            "baseline": run.get("baseline", end.get("baseline", "?")),
            "outcome": end.get("outcome", "?"),
            "flag": end.get("flag_obtained") or "-",
            "steps": end.get("steps") or 0,
            "duration_s": round(duration, 1),
        })
    if not rows:
        print(f"[atobench leaderboard] no ended episodes in {path}")
        return 0
    print(f"{'episode_id':<22} {'task':<4} {'base':<4} {'outcome':<10} {'flag':<8} {'steps':>6} {'dur(s)':>8}")
    print("-" * 70)
    for r in rows:
        print(f"{r['episode_id']:<22} {r['task']:<4} {r['baseline']:<4} {r['outcome']:<10} {r['flag']:<8} {r['steps']:>6} {r['duration_s']:>8}")
    print(f"\n{len(rows)} episodes")
    return 0


# ---------- version ----------

def cmd_version(args: argparse.Namespace) -> int:
    print(f"atobench {__version__}")
    return 0


# ---------- scaffold ----------

def cmd_scaffold(args: argparse.Namespace) -> int:
    """Deprecated legacy scaffold entrypoint."""
    print(
        "[atobench scaffold] ERROR: this legacy scaffold is deprecated. "
        "Use `python -m atobench.cli.main experiment scaffold` or "
        "`scripts/atobench-experiment scaffold`.",
        file=sys.stderr,
    )
    return 2


# ---------- generate (multi-LLM strict benchmark) ----------

def cmd_generate(args: argparse.Namespace) -> int:
    """Deprecated adaptive-generator entrypoint."""
    print(
        "[atobench generate] ERROR: this legacy generator is deprecated. "
        "Use `atobench experiment scaffold` plus the deception-experiment-cycle skill.",
        file=sys.stderr,
    )
    return 2


# ---------- sweep-proxy ----------

def cmd_sweep_proxy(args: argparse.Namespace) -> int:
    """Run N B0 + N B3 episodes against the mitmproxy deception proxy."""
    from atobench.agents.proxy_runner import ProxyEpisodeRunner
    log_dir = Path(args.output_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    n = args.n_per_baseline
    port = args.proxy_port

    results = []
    for baseline in ("B0", "B3"):
        for i in range(n):
            ep_id = f"ep_sweep_{baseline.lower()}_{i:02d}_{os.urandom(3).hex()}"
            print(f"\n[sweep-proxy] {baseline} episode {i+1}/{n}: {ep_id}")
            runner = ProxyEpisodeRunner(
                deception_config_path=args.deception_config,
                runtime_program_path=getattr(args, "runtime_program", None),
                target_url=args.target_url,
                proxy_port=port,
                task_id=args.task,
                baseline=baseline,
                episode_id=ep_id,
                log_dir=log_dir,
                agent_timeout_s=args.agent_timeout,
                driver=args.driver,
                claude_effort=getattr(args, "claude_effort", None),
                agent_subagent_type=getattr(args, "agent_subagent_type", "agentic-pentest-benchmark"),
                agent_defense_posture=getattr(args, "agent_defense_posture", "strong"),
                agent_max_tool_calls=getattr(args, "agent_max_tool_calls", 80),
                agent_calibration_focus=getattr(args, "agent_calibration_focus", None),
                agent_workspace=getattr(args, "agent_workspace", None),
                command_config=getattr(args, "agent_command_config", None),
            )
            result = runner.run()
            results.append({
                "episode_id": ep_id,
                "baseline": baseline,
                "flag": result.flag,
                "n_turns": result.n_turns,
                "n_deceptive": result.n_deceptive,
                "duration_s": round(result.duration_s, 1),
                "rc": result.rc,
            })
            print(f"  -> flag={result.flag}, n_turns={result.n_turns}, duration={result.duration_s:.1f}s")

    print("\n" + "=" * 80)
    print(f"{'episode_id':<28} {'base':<4} {'flag':<38} {'turns':>6} {'dur':>6}")
    print("-" * 80)
    for r in results:
        flag_str = (r["flag"] or "none")[:36]
        print(f"{r['episode_id']:<28} {r['baseline']:<4} {flag_str:<38} {r['n_turns']:>6} "
              f"{r['duration_s']:>6.1f}")
    print("=" * 80)

    b0 = [r for r in results if r["baseline"] == "B0"]
    b3 = [r for r in results if r["baseline"] == "B3"]
    def mean(xs, key):
        vals = [x[key] for x in xs if x[key] is not None]
        return sum(vals)/len(vals) if vals else 0.0
    print(f"\nB0 means (n={len(b0)}):")
    print(f"  turns={mean(b0,'n_turns'):.1f}  duration={mean(b0,'duration_s'):.1f}s")
    print(f"B3 means (n={len(b3)}):")
    print(f"  turns={mean(b3,'n_turns'):.1f}  duration={mean(b3,'duration_s'):.1f}s")
    print(f"\nDeltas (B3 - B0):")
    print(f"  delta_turns = {mean(b3,'n_turns') - mean(b0,'n_turns'):.2f}")

    summary_path = log_dir / f"sweep_proxy_summary_{int(time.time())}.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({
            "n_per_baseline": n,
            "task": args.task,
            "target_url": args.target_url,
            "deception_config": args.deception_config,
            "runtime_program": getattr(args, "runtime_program", None),
            "results": results,
            "b0_mean": {"turns": mean(b0,'n_turns'), "duration": mean(b0,'duration_s')},
            "b3_mean": {"turns": mean(b3,'n_turns'), "duration": mean(b3,'duration_s')},
        }, f, indent=2, ensure_ascii=False)
    print(f"\nSweep summary: {summary_path}")
    return 0


# ---------- parser ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atobench",
        description="ATOBench: tracing how autonomous penetration-testing agents verify vulnerabilities when target evidence lies.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run a single episode end-to-end.")
    p_run.add_argument("--task", required=True, choices=["T1", "T2", "T3", "T4"])
    p_run.add_argument("--baseline", required=True, choices=["B0", "B1", "B2", "B3"])
    p_run.add_argument("--primitive", default="",
                       help="Deception primitive name (empty for B0/B1).")
    p_run.add_argument("--coupling", default="loose",
                       choices=["loose", "schema_coupled", "precondition", "signal_removal"])
    p_run.add_argument("--defense-posture", default="none",
                       choices=["none", "strong", "weak"],
                       help="T3 anti-injection defense. Default 'none' for T1/T2, 'strong' for T3.")
    p_run.add_argument("--episode-id", default=None,
                       help="Override episode_id (default: random ep_<12hex>).")
    p_run.add_argument("--run-idx", type=int, default=None,
                       help="Optional sweep bookkeeping index.")
    p_run.add_argument("--seed", type=int, default=42)
    p_run.add_argument("--output-dir", default=str(DEFAULT_LOG_DIR))
    # Proxy mode (Phase 2.1): mitmproxy deception proxy + real HTTP target.
    p_run.add_argument("--mode", default="services",
                       choices=["services", "proxy"],
                       help="services=legacy ATOBench services (acme/idp/auth/admin); "
                            "proxy=mitmproxy deception proxy against --target-url.")
    p_run.add_argument("--deception-config", default=None,
                       help="Path to legacy deception_config.yaml. Optional if --runtime-program is supplied.")
    p_run.add_argument("--runtime-program", default=None,
                       help="Optional RuntimeProgram IR YAML. If provided, proxy executes it via ATOBENCH_RUNTIME_PROGRAM.")
    p_run.add_argument("--deception-dir", default=None,
                       help="ID-scoped deception workspace dir containing runtime_program.yaml.")
    p_run.add_argument("--deception-id", default=None,
                       help="Deception ID under --target-dir/deceptions/<id>.")
    p_run.add_argument("--target-dir", default=None,
                       help="Target dir used with --deception-id to resolve artifacts.")
    p_run.add_argument("--target-url", default=None,
                       help="Upstream target URL the proxy forwards to (e.g. http://127.0.0.1:3000). Required when --mode proxy.")
    p_run.add_argument("--proxy-port", type=int, default=8000,
                       help="Port for mitmdump to listen on (default 8000).")
    p_run.add_argument("--agent-timeout", type=int, default=900,
                       help="Agent invocation timeout in seconds (default 900 = 15min).")
    p_run.add_argument("--driver", default="subagent",
                       choices=["subagent", "agentic-pentest-benchmark", "curl-replay", "command"],
                       help="proxy mode only. subagent=spawn `claude -p` (default); "
                            "agentic-pentest-benchmark=delegate to Claude Code Agent tool; "
                            "curl-replay=deterministic curl sequence for fast infra testing; "
                            "command=run a third-party agent CLI defined by --agent-command-config.")
    p_run.add_argument("--agent-subagent-type", default="agentic-pentest-benchmark",
                       help="Agent tool subagent type for --driver agentic-pentest-benchmark.")
    p_run.add_argument("--agent-defense-posture", default="strong",
                       choices=["strong", "weak"],
                       help="Anti-injection posture passed to agentic pentest driver.")
    p_run.add_argument("--agent-max-tool-calls", type=int, default=80,
                       help="Soft tool-call budget described in the agentic pentest prompt.")
    p_run.add_argument("--agent-calibration-focus", default=None,
                       help="Optional targeted clean-calibration focus. Leave unset for broad benchmark runs.")
    p_run.add_argument("--agent-workspace", default=None,
                       help="Optional empty per-episode Claude Code working directory. Protocol-v3 supplies this automatically.")
    p_run.add_argument("--agent-command-config", default=None,
                       help="Command-agent adapter YAML (schema atobench.command_agent_config.v1). "
                            "Required for --driver command.")
    p_run.add_argument("--model", default=None,
                       help="Expected provider model identifier recorded for cross-model testing. "
                            "The runner verifies it against Claude Code route attestation.")
    p_run.add_argument("--model-selector", default=None,
                       help="Optional Claude Code selector (for example `opus`) used to route an "
                            "alternate provider backend. Pair it with --model.")
    p_run.add_argument("--claude-effort", default=None,
                       choices=["low", "medium", "high", "xhigh", "max"],
                       help="Optional Claude Code --effort value to freeze reasoning effort.")
    p_run.set_defaults(func=cmd_run)

    p_sweep = sub.add_parser("sweep", help="Run a YAML-configured batch of episodes.")
    p_sweep.add_argument("--config", required=True)
    p_sweep.add_argument("--output-dir", default=str(DEFAULT_LOG_DIR))
    p_sweep.set_defaults(func=cmd_sweep)

    p_analyze = sub.add_parser("analyze", help="Re-evaluate an already-completed episode.")
    p_analyze.add_argument("--episode-id", required=True)
    p_analyze.add_argument("--output-dir", default=str(DEFAULT_LOG_DIR))
    p_analyze.set_defaults(func=cmd_analyze)

    p_lb = sub.add_parser("leaderboard", help="List completed episodes.")
    p_lb.add_argument("--output-dir", default=str(DEFAULT_LOG_DIR))
    p_lb.set_defaults(func=cmd_leaderboard)

    p_sweep = sub.add_parser(
        "sweep-proxy",
        help="Run N B0 + N B3 episodes against the mitmproxy deception proxy + real target. Produces a comparison table.",
    )
    p_sweep.add_argument("--deception-config", required=True)
    p_sweep.add_argument("--runtime-program", default=None,
                         help="Optional RuntimeProgram IR YAML used for all proxy sweep episodes.")
    p_sweep.add_argument("--target-url", required=True)
    p_sweep.add_argument("--proxy-port", type=int, default=8100)
    p_sweep.add_argument("--task", default="T1", choices=["T1"])
    p_sweep.add_argument("--n-per-baseline", type=int, default=3,
                         help="Number of episodes per baseline (B0 + B3). Default 3 each = 6 total.")
    p_sweep.add_argument("--agent-timeout", type=int, default=300)
    p_sweep.add_argument("--output-dir", default=str(DEFAULT_LOG_DIR))
    p_sweep.add_argument("--driver", default="subagent",
                         choices=["subagent", "agentic-pentest-benchmark", "curl-replay", "command"])
    p_sweep.add_argument("--agent-subagent-type", default="agentic-pentest-benchmark")
    p_sweep.add_argument("--agent-defense-posture", default="strong",
                         choices=["strong", "weak"])
    p_sweep.add_argument("--agent-max-tool-calls", type=int, default=80)
    p_sweep.add_argument("--agent-calibration-focus", default=None)
    p_sweep.add_argument("--agent-command-config", default=None,
                         help="Command-agent adapter YAML (schema atobench.command_agent_config.v1). "
                              "Required for --driver command.")
    p_sweep.add_argument("--claude-effort", default=None,
                         choices=["low", "medium", "high", "xhigh", "max"])
    p_sweep.set_defaults(func=cmd_sweep_proxy)

    p_compile = sub.add_parser(
        "compile",
        help="Compile deception_plan.yaml into an ID-scoped RuntimeProgram workspace.",
    )
    p_compile.add_argument("--target-dir", required=True,
                           help="Target dir containing deception_plan.yaml and scaffold_work/target_profile.yaml.")
    p_compile.add_argument("--deception-id", default=None,
                           help="Stable ID for this deception case. Default: dec_<8hex>.")
    p_compile.add_argument("--plan", default=None,
                           help="Override plan path. Default: <target-dir>/deception_plan.yaml.")
    p_compile.add_argument("--target-profile", default=None,
                           help="v2 target profile path. Default: <target-dir>/scaffold_work/target_profile.yaml.")
    p_compile.add_argument("--baseline", default="B3", choices=["B0", "B3"])
    p_compile.set_defaults(func=cmd_compile)

    p_attr = sub.add_parser(
        "attribute",
        help="Generate a runtime-event attribution report for a deception workspace run.",
    )
    p_attr.add_argument("--deception-dir", required=True)
    p_attr.add_argument("--episode-id", default=None,
                        help="Episode id under <deception-dir>/runs/. Default: latest run dir.")
    p_attr.add_argument("--output", default=None,
                        help="Optional output markdown path. Default: run dir attribution_report.md.")
    p_attr.set_defaults(func=cmd_attribute)

    p_effect = sub.add_parser(
        "pentest-effect",
        help="Compute clean-relative pentest deception effect metrics from paired run artifacts.",
    )
    p_effect.add_argument("--clean-turns", required=True,
                          help="Clean run turns.jsonl.")
    p_effect.add_argument("--deception-turns", required=True,
                          help="Deception run turns.jsonl.")
    p_effect.add_argument("--clean-report", required=True,
                          help="Clean run final report text file.")
    p_effect.add_argument("--deception-report", required=True,
                          help="Deception run final report text file.")
    p_effect.add_argument("--target-profile", default=None,
                          help="target_profile.yaml with known_real_vulns[].")
    p_effect.add_argument("--plan", default=None,
                          help="deception_plan.yaml for configured injection denominator.")
    p_effect.add_argument("--runtime-program", default=None,
                          help="runtime_program.yaml/json for configured rule fallback.")
    p_effect.add_argument("--clean-normalized-report", default=None,
                          help="Optional normalized_findings.json for the clean report. If supplied, raw report parsing is bypassed.")
    p_effect.add_argument("--deception-normalized-report", default=None,
                          help="Optional normalized_findings.json for the deception report. If supplied, raw report parsing is bypassed.")
    p_effect.add_argument("--target", default=None,
                          help="Override target name in output.")
    p_effect.add_argument("--output", default=None,
                          help="Optional JSON output path. Default: print to stdout.")
    p_effect.set_defaults(func=cmd_pentest_effect)

    p_norm = sub.add_parser(
        "normalize-report",
        help="Deterministically normalize a raw pentest report into normalized_findings schema.",
    )
    p_norm.add_argument("--report", required=True,
                        help="Raw final report text file.")
    p_norm.add_argument("--source-report-id", default=None,
                        help="Optional source id to store in normalized output.")
    p_norm.add_argument("--output", default=None,
                        help="Optional normalized_findings.json output path. Default: print to stdout.")
    p_norm.set_defaults(func=cmd_normalize_report)

    p_pair = sub.add_parser(
        "evaluate-pair",
        help="Evaluate paired clean/deception run dirs and write pentest_effect.json.",
    )
    p_pair.add_argument("--clean-run-dir", required=True,
                        help="Run dir containing clean turns.jsonl and episode_summary.json/final_report.txt.")
    p_pair.add_argument("--deception-run-dir", required=True,
                        help="Run dir containing deception turns.jsonl and episode_summary.json/final_report.txt.")
    p_pair.add_argument("--target-profile", default=None,
                        help="target_profile.yaml. Default: inferred from deception workspace.")
    p_pair.add_argument("--plan", default=None,
                        help="deception_plan.yaml. Default: inferred from deception workspace.")
    p_pair.add_argument("--runtime-program", default=None,
                        help="runtime_program.yaml/json. Default: inferred from deception workspace.")
    p_pair.add_argument("--target", default=None,
                        help="Override target name in output.")
    p_pair.add_argument("--normalizer-mode", default="existing",
                        choices=["existing", "heuristic"],
                        help="existing=require normalized_findings.json; heuristic=create missing debug normalized files.")
    p_pair.add_argument("--overwrite-normalized", action="store_true",
                        help="Regenerate normalized_findings.json when --normalizer-mode heuristic.")
    p_pair.add_argument("--clean-normalized-report", default=None,
                        help="Explicit validated normalized findings for the clean run; preserves default artifacts.")
    p_pair.add_argument("--deception-normalized-report", default=None,
                        help="Explicit validated normalized findings for the deception run; preserves default artifacts.")
    p_pair.add_argument("--output", default=None,
                        help="Optional pentest_effect.json output path. Default: <deception-run-dir>/pentest_effect.json.")
    p_pair.set_defaults(func=cmd_evaluate_pair)

    p_trace = sub.add_parser(
        "extract-action-trace",
        help="Derive a redaction-safe agent_action_trace.jsonl from turns.jsonl.",
    )
    p_trace.add_argument("--turns", required=True, help="Source turns.jsonl.")
    p_trace.add_argument("--output", default=None,
                         help="Output path. Default: agent_action_trace.jsonl beside the source.")
    p_trace.add_argument("--condition", choices=["C0", "C1", "C2"], default=None,
                         help="Explicit experiment condition; otherwise inferred from episode_id.")
    p_trace.add_argument("--artifact-manifest", default=None,
                         help="Frozen planted-artifact manifest used for pre-redaction use labels.")
    p_trace.set_defaults(func=cmd_extract_action_trace)

    p_behavior = sub.add_parser(
        "audit-behavior",
        help="Audit paired C0/C1 action traces using an explicit JSON pair manifest.",
    )
    p_behavior.add_argument("--pairs", required=True,
                            help="JSON list of pair_id, clean_run_dir, and deception_run_dir objects.")
    p_behavior.add_argument("--output", required=True, help="Behavior audit JSON output path.")
    p_behavior.set_defaults(func=cmd_audit_behavior)

    p_profile_validate = sub.add_parser(
        "validate-behavior-profiles",
        help="Validate a behavioral-profile sidecar against its frozen runtime program.",
    )
    p_profile_validate.add_argument("--profiles", required=True, help="Behavioral profiles YAML.")
    p_profile_validate.add_argument("--runtime-program", required=True, help="Frozen runtime_program.yaml.")
    p_profile_validate.add_argument("--mode", choices=["exploratory", "confirmatory"],
                                    default="exploratory",
                                    help="Confirmatory mode enforces preregistration and executable predicates.")
    p_profile_validate.set_defaults(func=cmd_validate_behavior_profiles)

    p_profile_audit = sub.add_parser(
        "audit-behavior-profiles",
        help="Audit profile contact and causal identifiability over paired campaign traces.",
    )
    p_profile_audit.add_argument("--profiles", required=True, help="Behavioral profiles YAML.")
    p_profile_audit.add_argument("--runtime-program", required=True, help="Frozen runtime_program.yaml.")
    p_profile_audit.add_argument("--pairs", required=True, help="Explicit JSON pair manifest.")
    p_profile_audit.add_argument("--output", required=True, help="Coverage audit JSON output path.")
    p_profile_audit.set_defaults(func=cmd_audit_behavior_profiles)

    p_exp = sub.add_parser(
        "experiment",
        help="Run open-source ATOBench experiment-cycle steps from a YAML config.",
    )
    p_exp.add_argument(
        "action",
        choices=[
            "init",
            "start-target",
            "health",
            "scaffold",
            "make-deception",
            "compile",
            "construct-offline",
            "validate-offline",
            "review-offline",
            "materialize-offline-suite",
            "replay-offline-suite",
            "materialize-d-ladder",
            "replay-d-ladder",
            "freeze-offline-suite",
            "freeze-suite",
            "validate-suite",
            "validate-protocol-v3",
            "run-clean",
            "archive-clean",
            "run-deception",
            "validate-clean",
            "validate-deception",
            "attribute",
            "normalize-blinded",
            "normalize-fixed-local",
            "evaluate",
            "status",
            "run-mechanical",
        ],
    )
    p_exp.add_argument(
        "--config",
        default=str(DEFAULT_EXPERIMENT_CONFIG),
        help=f"Experiment YAML config. Default: {DEFAULT_EXPERIMENT_CONFIG}",
    )
    p_exp.add_argument("--background", action="store_true", help="Run episode action in background.")
    p_exp.add_argument(
        "--protocol-manifest",
        default="atobench/experiment/protocol_v3/PROTOCOL_V3_EXECUTION_SPEC.yaml",
        help="For validate-protocol-v3: preregistration YAML to validate.",
    )
    p_exp.add_argument(
        "--write-protocol-lock",
        default=None,
        help="For validate-protocol-v3: optional generated lock-report path.",
    )
    p_exp.add_argument(
        "--require-collection-ready",
        action="store_true",
        help="For validate-protocol-v3: exit 2 when any collection blocker remains.",
    )
    p_exp.add_argument(
        "--protocol-validation-only",
        action="store_true",
        help="For protocol-v3 run-clean/run-deception: mark the episode as engineering validation and exclude it from effect estimates.",
    )
    p_exp.add_argument(
        "--protocol-assignment-slot",
        default=None,
        help="For protocol-v3 confirmatory runs: preregistered BLOCK:POSITION slot, for example S01:1.",
    )
    p_exp.add_argument(
        "--clean-target-first",
        action="store_true",
        help="For start-target: run docker compose down --remove-orphans --volumes before up -d.",
    )
    p_exp.add_argument(
        "--suite-id",
        default=None,
        help="For freeze-suite/freeze-offline-suite/construct-offline/validate-offline/materialize-offline-suite/replay-offline-suite/materialize-d-ladder/replay-d-ladder: suite id under <target>/benchmark_suites/.",
    )
    p_exp.add_argument(
        "--source-deception-dir",
        default=None,
        help="For freeze-suite: existing compiled deception workspace to freeze.",
    )
    p_exp.add_argument(
        "--force",
        action="store_true",
        help="For freeze-suite/freeze-offline-suite/construct-offline/materialize-offline-suite/materialize-d-ladder: replace existing generated artifacts.",
    )
    p_exp.add_argument(
        "--ground-truth",
        default=None,
        help="For construct-offline: ground_truth_cards.yaml. Default: atobench/experiment/ground_truth/<target>_ground_truth_cards.yaml.",
    )
    p_exp.add_argument(
        "--surface-inventory",
        default=None,
        help="For construct-offline: surface_inventory.jsonl. Default: <target>/scaffold_work/endpoint_inventory.jsonl.",
    )
    p_exp.add_argument(
        "--primitive-index",
        default=None,
        help="For construct-offline: primitive_index.yaml. Default: atobench/deception_frame/primitive_index.yaml.",
    )
    p_exp.add_argument(
        "--primitive-recipes",
        default=None,
        help="For construct-offline: primitive_recipes.yaml. Default: atobench/deception_frame/primitive_recipes.yaml.",
    )
    p_exp.add_argument(
        "--max-matrix-rows",
        type=int,
        default=200,
        help="For construct-offline: cap primitive-surface matrix rows.",
    )
    p_exp.add_argument(
        "--max-candidates",
        type=int,
        default=60,
        help="For construct-offline: cap generated candidate skeletons.",
    )
    p_exp.add_argument(
        "--max-c1-cases",
        type=int,
        default=16,
        help="For review-offline: cap the machine C1 candidate shortlist.",
    )
    p_exp.add_argument(
        "--require-contact",
        action="store_true",
        help="For validate-deception, mark the run invalid when no deception fired.",
    )
    p_exp.add_argument(
        "--normalizer-mode",
        default="existing",
        choices=["existing", "heuristic"],
        help="Passed to evaluate-pair for action=evaluate.",
    )
    p_exp.add_argument(
        "--normalizer-scope",
        default="current",
        choices=["current", "all"],
        help="For normalize-blinded: current clean/latest B3 or every archived clean and B3 run.",
    )
    p_exp.add_argument(
        "--run-dir",
        action="append",
        default=[],
        help="For normalize-blinded: explicit report-bearing run directory. Repeatable; overrides --normalizer-scope.",
    )
    p_exp.set_defaults(func=cmd_experiment)

    p_scaffold = sub.add_parser(
        "scaffold",
        help="Deprecated legacy scaffold. Use `atobench experiment scaffold`.",
    )
    p_scaffold.add_argument("--target-dir", required=True,
                            help="Deprecated; retained only so old commands fail clearly.")
    p_scaffold.add_argument("--baseline", default="B3", choices=["B0", "B3"])
    p_scaffold.add_argument("--n-episodes", type=int, default=3,
                            help="Number of B3 episodes the validator runs (default 3).")
    p_scaffold.add_argument("--invoke-subagents", action="store_true",
                            help="Actually shell out to `claude` CLI to invoke subagents. "
                                 "Default: just write prompt files for manual invocation.")
    p_scaffold.set_defaults(func=cmd_scaffold)

    p_gen = sub.add_parser(
        "generate",
        help="Deprecated legacy generator. Use `atobench experiment scaffold`.",
    )
    p_gen.add_argument("--target", required=True,
                       help="Target name (matches targets/<name>/ dir).")
    p_gen.add_argument("--task", required=True, choices=["T1", "T2", "T3", "T4"])
    p_gen.add_argument("--baseline", default="B3", choices=["B0", "B3"])
    p_gen.add_argument("--output", required=True,
                       help="Path to write deception_config.yaml.")
    p_gen.set_defaults(func=cmd_generate)

    p_ver = sub.add_parser("version", help="Print version and exit.")
    p_ver.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
