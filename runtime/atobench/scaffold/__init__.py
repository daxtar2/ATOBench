"""ATOBench scaffold package — multi-agent orchestration for new targets.

Public API:
- ScaffoldOrchestrator — coordinates the 4 deception-* subagents
- ScaffoldResult — artifact paths + subagent outputs
"""
from atobench.scaffold.orchestrator import ScaffoldOrchestrator, ScaffoldResult
from atobench.scaffold.dual_track import (
    DualTrackReport,
    EpisodeComparison,
    compare_episode,
    run_dual_track,
)

__all__ = [
    "ScaffoldOrchestrator",
    "ScaffoldResult",
    "DualTrackReport",
    "EpisodeComparison",
    "compare_episode",
    "run_dual_track",
]
