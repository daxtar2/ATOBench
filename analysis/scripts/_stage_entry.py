from __future__ import annotations

import sys
from pathlib import Path


def main(stage: str) -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from atobench_vr.pipeline import cli

    return cli(["stage", stage, *sys.argv[1:]])

