"""Stage-chain analysis over Harbor trial directories (on-demand).

The evaluation report of the Harbor-era framework: per (AOU, condition,
difficulty) cohort, the stage-conditional probabilities of the verification
chain

    contact → detect → adapt → recover → close ∧ support

plus GV rate and outcome distribution, with Wilson 95% intervals. C0 cohorts
use the verifier-computed shadow anchor, so both arms of a pair report the
same chain. Difficulty levels order into the dose-response view.

Reads `verifier/atobench_signals.json` + `config.json` straight from Harbor
trial directories — no legacy import step required.

Fail-closed: reading job output is real-data access and requires
--allow-real-data.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .common import require_real_data_authorization

SIGNALS_REL = Path("verifier/atobench_signals.json")
CONDITION_RE = re.compile(r"-c([01])$", re.IGNORECASE)

STAGE_ORDER = ["contact", "detection", "adaptation", "recovery", "closure"]
LEVEL_ORDER = {"D0": 0, "D1": 1, "D2": 2, "D3": 3, "D4": 4}


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (p, max(0.0, center - half), min(1.0, center + half))


def _rate(num: int, den: int) -> dict[str, Any]:
    p, lo, hi = _wilson(num, den)
    return {"k": num, "n": den, "rate": round(p, 4), "ci95": [round(lo, 4), round(hi, 4)]}


def discover_trials(job_dirs: list[Path]) -> list[dict[str, Any]]:
    trials = []
    for job_dir in job_dirs:
        for trial_dir in sorted(job_dir.iterdir()):
            signals_path = trial_dir / SIGNALS_REL
            config_path = trial_dir / "config.json"
            if not trial_dir.is_dir() or not signals_path.is_file() or not config_path.is_file():
                continue
            signals = json.loads(signals_path.read_text(encoding="utf-8"))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            task_name = Path(str((config.get("task") or {}).get("path") or trial_dir.name)).name
            match = CONDITION_RE.search(task_name)
            if match:
                condition = f"C{match.group(1)}"
            elif re.search(r"-d\d$", task_name, re.IGNORECASE):
                condition = "C1"  # ladder rungs are deception arms by construction
            else:
                condition = "?"
            difficulty = signals.get("difficulty") or {}
            # The C0 arm is the no-deception control by definition, regardless
            # of what contract metadata the (shared) verifier echoes.
            level = "D0" if condition == "C0" else str(difficulty.get("level") or "D2")
            aou = CONDITION_RE.sub("", task_name).replace("atobench-", "")
            aou = re.sub(r"-d\d$", "", aou)  # ladder rungs fold into their AOU
            trials.append(
                {
                    "trial": trial_dir.name,
                    "aou": aou,
                    "condition": condition,
                    "level": level,
                    "signals": signals,
                }
            )
    return trials


def cohort_stats(trials: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trials)
    sig = [t["signals"] for t in trials]

    def count(pred) -> int:
        return sum(1 for s in sig if pred(s))

    contact = count(lambda s: s.get("contact"))
    detect_given_contact = count(lambda s: s.get("contact") and s.get("detection_proxy"))
    adapt_given_contact = count(lambda s: s.get("contact") and s.get("adaptive_verification"))
    recover_given_adapt = count(
        lambda s: s.get("contact") and s.get("adaptive_verification") and s.get("evidence_recovery")
    )
    adapt_and_contact = count(lambda s: s.get("contact") and s.get("adaptive_verification"))
    recover_given_contact = count(lambda s: s.get("contact") and s.get("evidence_recovery"))
    close_given_recover = count(
        lambda s: s.get("evidence_recovery") and s.get("report_closure") and s.get("trace_support")
    )
    recover = count(lambda s: s.get("evidence_recovery"))
    grounded = count(lambda s: s.get("reward") == 1.0)

    outcomes = Counter(int(s.get("outcome", -1)) for s in sig)
    outcome_names = {
        "3": "grounded",
        "2": "unsupported_closure",
        "1": "unreported",
        "0": "unresolved",
        "-1": "unknown",
    }

    return {
        "n": n,
        "stages": {
            "contact": _rate(contact, n),
            "detect|contact": _rate(detect_given_contact, contact),
            "adapt|contact": _rate(adapt_given_contact, contact),
            "recover|adapt": _rate(recover_given_adapt, adapt_and_contact),
            "recover|contact": _rate(recover_given_contact, contact),
            "close+support|recover": _rate(close_given_recover, recover),
        },
        "gv_rate": _rate(grounded, n),
        "outcomes": {outcome_names[str(k)]: v for k, v in sorted(outcomes.items(), reverse=True)},
        "mean_turns": round(sum(float(s.get("turns", 0)) for s in sig) / n, 1) if n else 0.0,
        "mean_reward_shaped": (
            round(sum(float(s.get("reward_shaped", 0.0)) for s in sig) / n, 4) if n else 0.0
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ATOBench stage-chain report",
        "",
        "Stage-conditional verification rates per cohort (Wilson 95% CI in brackets).",
        "C0 chains are computed from shadow anchors (selector-matched, untransformed).",
        "",
    ]
    for aou, cohorts in sorted(report["cohorts"].items()):
        lines.append(f"## {aou}")
        lines.append("")
        header = "| cohort | n | contact | detect\\|contact | adapt\\|contact | recover\\|adapt | close+support\\|recover | GV |"
        lines.append(header)
        lines.append("|---|---|---|---|---|---|---|---|")
        for key in sorted(cohorts, key=lambda k: (LEVEL_ORDER.get(k.split("@")[1], 9), k)):
            stats = cohorts[key]

            def cell(stage: str) -> str:
                r = stats["stages"][stage]
                return f"{r['rate']:.2f} [{r['ci95'][0]:.2f},{r['ci95'][1]:.2f}] ({r['k']}/{r['n']})"

            gv = stats["gv_rate"]
            lines.append(
                f"| {key} | {stats['n']} | {cell('contact')} | {cell('detect|contact')} | "
                f"{cell('adapt|contact')} | {cell('recover|adapt')} | {cell('close+support|recover')} | "
                f"{gv['rate']:.2f} ({gv['k']}/{gv['n']}) |"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dirs", nargs="+", type=Path, help="Harbor job directories")
    parser.add_argument("--out", type=Path, required=True, help="Output directory for the report")
    parser.add_argument("--allow-real-data", action="store_true", help="Authorize reading real job output")
    args = parser.parse_args(argv)

    job_dirs = [p.resolve() for p in args.job_dirs]
    require_real_data_authorization(job_dirs, args.allow_real_data)

    trials = discover_trials(job_dirs)
    if not trials:
        print(json.dumps({"status": "fail", "error": "no trials with atobench signals found"}))
        return 2

    cohorts: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for trial in trials:
        key = f"{trial['condition']}@{trial['level']}"
        cohorts.setdefault(trial["aou"], {}).setdefault(key, []).append(trial)

    report = {
        "schema_version": "atobench.stage_chain_report.v1",
        "reward_spec": "atobench.reward.v3",
        "n_trials": len(trials),
        "cohorts": {
            aou: {key: cohort_stats(group) for key, group in cohort_map.items()}
            for aou, cohort_map in cohorts.items()
        },
    }

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "stage_chain_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "STAGE_CHAIN_REPORT.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": "pass", "n_trials": len(trials), "out": str(out_dir)}, indent=2))
    return 0


cli = main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
