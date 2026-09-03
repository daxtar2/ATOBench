#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atobench_vr.alignment_deterministic import cli


if __name__ == "__main__":
    raise SystemExit(cli())
