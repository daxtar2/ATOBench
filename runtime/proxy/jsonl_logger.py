"""JSONL writers for episodes, turns, and orchestrator logs.

All writers append; no caching; flush immediately.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
LOG_DIR = os.environ.get("ATOBENCH_LOG_DIR") or os.environ.get("ATOBENCH_LOG_DIR") or str(DEFAULT_LOG_DIR)
EPISODES_PATH = os.path.join(LOG_DIR, "episodes.jsonl")
TURNS_PATH = os.path.join(LOG_DIR, "turns.jsonl")
ORCHESTRATOR_PATH = os.path.join(LOG_DIR, "orchestrator.jsonl")

_write_lock = threading.Lock()


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _append(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with _write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()


def new_episode_id() -> str:
    return f"ep_{uuid.uuid4().hex[:12]}"


def start_episode(task: str, baseline: str, agent_driver: str, llm_backbone: str,
                  m5_run_idx: int | None = None,
                  ablation_primitive: str | None = None) -> str:
    episode_id = new_episode_id()
    rec = {
        "episode_id": episode_id,
        "task": task,
        "baseline": baseline,
        "agent_driver": agent_driver,
        "llm_backbone": llm_backbone,
        "started_at": _ts(),
        "status": "running",
    }
    if m5_run_idx is not None:
        rec["m5_run_idx"] = m5_run_idx
    if ablation_primitive:
        rec["ablation_primitive"] = ablation_primitive
    _append(EPISODES_PATH, rec)
    orchestrator_log(f"episode_start {episode_id} task={task} baseline={baseline}"
                     + (f" run_idx={m5_run_idx}" if m5_run_idx is not None else "")
                     + (f" ablate={ablation_primitive}" if ablation_primitive else ""))
    return episode_id


def end_episode(episode_id: str, outcome: str, flag_obtained, grader_success: bool,
                steps: int, http_tool_calls: int, total_tool_calls: int,
                token_cost: int, wrong_branch_count: int, loop_incidence: int,
                false_claim_adoption_count: int, parse_failure_count: int,
                consistency_violation_count: int, stealth_penalty_score: float,
                final_report_text: str = None) -> None:
    """Write the episode summary record. final_report_text is the agent's final
    assistant message — used by L3 directed-adoption detector (rev4 replacement
    for per-turn assistant_text, which was infeasible to capture).

    Note: start record carries task/baseline/m5_run_idx/agent_driver/llm_backbone.
    This end record only carries metrics + outcome. evaluate.py merges via
    dict.update with start, so end's omitted fields preserve start's values.
    """
    rec = {
        "episode_id": episode_id,
        "ended_at": _ts(),
        "status": "ended",
        "outcome": outcome,
        "flag_obtained": flag_obtained,
        "grader_success": grader_success,
        "steps": steps,
        "http_tool_calls": http_tool_calls,
        "total_tool_calls": total_tool_calls,
        "token_cost": token_cost,
        "wrong_branch_count": wrong_branch_count,
        "loop_incidence": loop_incidence,
        "false_claim_adoption_count": false_claim_adoption_count,
        "parse_failure_count": parse_failure_count,
        "consistency_violation_count": consistency_violation_count,
        "stealth_penalty_score": stealth_penalty_score,
        "final_report_text": final_report_text,
    }
    _append(EPISODES_PATH, rec)
    orchestrator_log(f"episode_end {episode_id} outcome={outcome} grader={grader_success}")


def log_turn(episode_id: str, turn_idx: int, tool: str, tool_args: dict,
             request: dict, response: dict, deception_tag: dict,
             post_turn_signals: dict, assistant_text: str = None,
             proxy_service: str = None) -> None:
    rec = {
        "episode_id": episode_id,
        "turn_idx": turn_idx,  # NOTE: per-proxy counter — use (proxy_service, turn_idx) or ts for global ordering
        "ts": _ts(),
        "proxy_service": proxy_service,
        "tool": tool,
        "tool_args": tool_args,
        "assistant_text": assistant_text,  # deprecated rev4 — kept for backward compat, always null
        "request": request,
        "response": response,
        "deception_tag": deception_tag,
        "post_turn_signals": post_turn_signals,
    }
    _append(TURNS_PATH, rec)


def orchestrator_log(detail: str, level: str = "info") -> None:
    rec = {"ts": _ts(), "source": "orchestrator", "level": level, "detail": detail}
    _append(ORCHESTRATOR_PATH, rec)


def decision_log(detail: str) -> None:
    orchestrator_log(detail, level="decision")
