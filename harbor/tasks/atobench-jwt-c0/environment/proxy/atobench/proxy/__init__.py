"""ATOBench deception proxy package.

mitmproxy-based deception proxy that sits in front of any HTTP target and
applies the 9-primitive × 4-coupling strategy space to the agent's traffic.

Public API:
- flow.HTTPFlow / Request / Response — internal abstraction
- pattern_matcher.find_match — primitive selection
- state_store.StateStore — per-episode D_t state
- transformers.REGISTRY — name -> PrimitiveTransformer
- addon — mitmproxy addon entrypoint (mitmdump -s atobench/proxy/addon.py)
- logging.log_flow — turns.jsonl writer (legacy-compatible shape)
"""
from atobench.proxy.flow import HTTPFlow, Request, Response
from atobench.proxy.pattern_matcher import find_match, matches, MatchResult
from atobench.proxy.state_store import StateStore
from atobench.proxy.transformers import REGISTRY, PrimitiveTransformer
from atobench.proxy.logging import log_flow

__all__ = [
    "HTTPFlow",
    "Request",
    "Response",
    "find_match",
    "matches",
    "MatchResult",
    "StateStore",
    "REGISTRY",
    "PrimitiveTransformer",
    "log_flow",
]
