#!/usr/bin/env python3
"""Export a Harbor job directory as an RL rollout batch (JSONL).

One line per trial:

    {
      "trial_name": ...,
      "task": ...,              # task dir name, e.g. atobench-sqli-c1
      "condition": ...,         # C0 | C1 (from task metadata / name)
      "trajectory": {...},      # ATIF v1.7 document (agent-side view)
      "reward": 0.0 | 1.0,      # primary endpoint (G chain)
      "reward_shaped": float,   # dense process reward for RL
      "metrics": {...},         # full reward.json (all numeric signals)
      "wire_turns": int,        # proxy-side ground-truth turn count
    }

Fail-closed: trials without a reward file or without a trajectory are
skipped and counted, never silently emitted.

Usage:
    python3 harbor/scripts/export_rollout_batch.py jobs/<job-dir> [--out rollouts.jsonl]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CONDITION_RE = re.compile(r"-c([01])$", re.IGNORECASE)


def export_trial(trial_dir: Path) -> dict | None:
    reward_path = trial_dir / "verifier" / "reward.json"
    traj_path = trial_dir / "agent" / "trajectory.json"
    config_path = trial_dir / "config.json"
    if not reward_path.is_file() or not traj_path.is_file() or not config_path.is_file():
        return None

    reward = json.loads(reward_path.read_text(encoding="utf-8"))
    trajectory = json.loads(traj_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    task_name = Path(str((config.get("task") or {}).get("path") or trial_dir.name)).name
    match = CONDITION_RE.search(task_name)
    condition = f"C{match.group(1)}" if match else "?"

    turns_path = trial_dir / "artifacts" / "logs" / "proxy" / "turns.jsonl"
    wire_turns = (
        sum(1 for line in turns_path.read_text(encoding="utf-8").splitlines() if line.strip())
        if turns_path.is_file()
        else 0
    )

    return {
        "trial_name": trial_dir.name,
        "task": task_name,
        "condition": condition,
        "trajectory": trajectory,
        "reward": reward.get("reward"),
        "reward_shaped": reward.get("reward_shaped"),
        "metrics": reward,
        "wire_turns": wire_turns,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="Output JSONL (default: stdout)")
    args = parser.parse_args()

    trial_dirs = sorted(
        p for p in args.job_dir.iterdir() if p.is_dir() and (p / "config.json").is_file()
    )
    records = []
    skipped = []
    for trial_dir in trial_dirs:
        record = export_trial(trial_dir)
        (records if record else skipped).append(record or trial_dir.name)

    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    if args.out:
        args.out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    else:
        print("\n".join(lines))

    summary = {
        "job_dir": str(args.job_dir),
        "trials_found": len(trial_dirs),
        "exported": len(records),
        "skipped": skipped,
        "out": str(args.out) if args.out else None,
    }
    print(json.dumps(summary, indent=2), file=sys.stderr)
    return 0 if records or not trial_dirs else 2


if __name__ == "__main__":
    raise SystemExit(main())
