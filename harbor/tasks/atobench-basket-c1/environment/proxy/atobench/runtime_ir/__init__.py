"""Runtime IR helpers for the ATOBench deception proxy.

The runtime IR is the compiled, machine-readable form between the high-level
deception plan and the mitmproxy execution layer.
"""

from atobench.runtime_ir.models import (
    build_legacy_runtime_program,
    event_to_legacy_primitive_entry,
    make_event,
)
from atobench.runtime_ir.recipes import load_primitive_recipes, query_primitive_recipes

__all__ = [
    "build_legacy_runtime_program",
    "event_to_legacy_primitive_entry",
    "load_primitive_recipes",
    "make_event",
    "query_primitive_recipes",
]
