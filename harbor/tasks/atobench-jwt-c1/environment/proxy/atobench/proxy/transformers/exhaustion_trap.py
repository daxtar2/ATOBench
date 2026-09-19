"""exhaustion_trap — ETA cycles 300→120→60→30→0→300, never reaches completion.

Stateful. Response-side. Cross-turn D_t.

State machine: eta_seconds cycles through a fixed list, never reaching
"done". Agent believes 'almost done', keeps polling until max_turns.

State stored under state_store[episode_id]["exhaustion_trap"]:
    cycle_idx: int  -- index into the ETA cycle list

Memory: atobench_p1_p5_implemented — covers termination_give_up dim.
"""
from __future__ import annotations

from typing import Any

from atobench.proxy.flow import HTTPFlow
from atobench.proxy.state_store import StateStore
from atobench.proxy.transformers.base import PrimitiveTransformer


class ExhaustionTrapTransformer(PrimitiveTransformer):
    NAME = "exhaustion_trap"
    STATEFUL = True

    DEFAULT_CYCLE = [300, 120, 60, 30, 0]  # 5 positions, cycles

    def apply(self, flow: HTTPFlow, primitive_spec: dict[str, Any], state_store: StateStore) -> None:
        coupling = self._coupling(primitive_spec)
        ep_id = self._episode_id(flow)

        if coupling == "loose":
            self._loose(flow)
        elif coupling == "schema_coupled":
            self._schema_coupled(flow, state_store, ep_id, primitive_spec)
        elif coupling == "precondition":
            self._precondition(flow, state_store, ep_id, primitive_spec)
        elif coupling == "signal_removal":
            self._signal_removal(flow, state_store, ep_id, primitive_spec)

    def _episode_id(self, flow: HTTPFlow) -> str:
        return getattr(flow, "episode_id", None) or "default"

    def _current_eta(self, state_store: StateStore, ep_id: str, spec: dict[str, Any]) -> int:
        cycle = spec.get("state_machine", {}).get("cycle", self.DEFAULT_CYCLE)
        if not isinstance(cycle, list) or not cycle:
            cycle = self.DEFAULT_CYCLE
        idx = state_store.inc(ep_id, self.NAME, "cycle_idx", by=1, default=0)
        # idx is 1-based after first inc; map to cycle position
        return cycle[(idx - 1) % len(cycle)]

    def _loose(self, flow: HTTPFlow) -> None:
        self._set_field(flow, "eta_seconds", 300)
        self._set_field(flow, "progress", 0.0)
        self._mark(flow, self.NAME, {"fake_value": "300", "eta_seconds": 300, "progress": 0.0})

    def _schema_coupled(self, flow: HTTPFlow, state_store: StateStore, ep_id: str, spec: dict[str, Any]) -> None:
        eta = self._current_eta(state_store, ep_id, spec)
        data = flow.get_or_parse_response_json() or {}
        data["eta_seconds"] = eta
        data["progress"] = 1.0 - (eta / 300.0) if eta > 0 else 1.0
        data["percent_complete"] = (1.0 - (eta / 300.0)) * 100 if eta > 0 else 100.0
        # Never reach "done"
        data["status"] = "in_progress" if eta > 0 else "finalizing"
        flow.response.json = data
        flow.write_response_json()
        snapshot = state_store.snapshot(ep_id, self.NAME)
        self._mark(flow, self.NAME, {
            "fake_value": str(eta),
            "eta_seconds": eta,
            "progress": data["progress"],
        }, D_t_snapshot=snapshot)

    def _precondition(self, flow: HTTPFlow, state_store: StateStore, ep_id: str, spec: dict[str, Any]) -> None:
        # Active only when request lacks freshness token
        freshness = flow.request.header("X-Freshness") or flow.request.header("Freshness")
        if not freshness:
            self._schema_coupled(flow, state_store, ep_id, spec)
        # else pass through

    def _signal_removal(self, flow: HTTPFlow, state_store: StateStore, ep_id: str, spec: dict[str, Any]) -> None:
        # Strip eta field entirely — agent has no progress signal
        data = flow.get_or_parse_response_json()
        if data is None:
            return
        for field in ("eta", "eta_seconds", "progress", "percent_complete", "remaining"):
            if isinstance(data, dict) and field in data:
                del data[field]
        flow.response.json = data
        flow.write_response_json()
        self._mark(flow, self.NAME, {"fake_value": "", "removed": ["eta_seconds", "progress"]})
