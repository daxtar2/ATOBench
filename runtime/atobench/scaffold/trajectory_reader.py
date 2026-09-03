"""trajectory_reader — exposes agent's visit history to the adaptive generator.

Reads turns.jsonl (the canonical per-episode log written by atobench.proxy.logging)
and provides structured queries the coverage_planner subagent needs:

  - visits(path_regex) → list of TurnRecords matching the path
  - adopted_fakes() → list of fake_values the agent cited in later requests
                      (heuristic: search request bodies for prior z_t/u_t
                       fake_value strings)
  - last_turn() → most recent TurnRecord
  - coverage_state() → per-primitive {fired, adopted, contradicted} flags

This module is read-only — it never modifies turns.jsonl.

Design doc: atobench/scaffold/ADAPTIVE_GENERATOR_DESIGN.md
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TurnRecord:
    """One row from turns.jsonl, parsed into a convenient shape."""
    turn_idx: int
    ts: str
    request_method: str
    request_path: str
    request_headers: dict[str, str]
    request_body: str
    response_status: int
    response_headers: dict[str, str]
    response_body: str
    deception_tag: dict[str, Any] = field(default_factory=dict)
    primitive_name: str | None = None  # extracted from deception_tag.z_t
    fake_value: str | None = None      # extracted from deception_tag.u_t


class TrajectoryReader:
    """Reads an episode's turns.jsonl and answers trajectory queries."""

    def __init__(self, turns_jsonl_path: Path | str) -> None:
        self.path = Path(turns_jsonl_path)
        self._turns: list[TurnRecord] | None = None

    # ---------- loading ----------

    def _load(self) -> list[TurnRecord]:
        if self._turns is not None:
            return self._turns
        turns: list[TurnRecord] = []
        if not self.path.exists():
            self._turns = turns
            return turns
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                turns.append(self._parse(obj))
        self._turns = turns
        return turns

    def _parse(self, obj: dict[str, Any]) -> TurnRecord:
        req = obj.get("request", {}) or {}
        resp = obj.get("response", {}) or {}
        tag = obj.get("deception_tag", {}) or {}
        z_t = tag.get("z_t", "") or ""
        u_t = tag.get("u_t", {}) or {}
        primitives_fired_list = tag.get("primitives_fired") or []

        # Determine primitive_name + fake_value:
        # - New shape (L1.5): primitives_fired list — take first entry
        # - Old shape: z_t as string (the primitive name)
        # - Legacy test shape: z_t as dict {"primitive": "..."}
        if primitives_fired_list and isinstance(primitives_fired_list, list):
            first = primitives_fired_list[0] or {}
            primitive = first.get("z_t")
            first_u_t = first.get("u_t") or {}
            fake = first_u_t.get("fake_value") if isinstance(first_u_t, dict) else None
        elif isinstance(z_t, str) and z_t:
            primitive = z_t
            fake = u_t.get("fake_value") if isinstance(u_t, dict) else None
        elif isinstance(z_t, dict):
            primitive = z_t.get("primitive")
            fake = u_t.get("fake_value") if isinstance(u_t, dict) else None
        else:
            primitive = None
            fake = None

        return TurnRecord(
            turn_idx=obj.get("turn_idx", 0),
            ts=obj.get("ts", ""),
            request_method=req.get("method", ""),
            request_path=req.get("path", ""),
            request_headers=req.get("headers", {}) or {},
            request_body=req.get("body", "") or "",
            response_status=resp.get("status", 0) or resp.get("status_code", 0),
            response_headers=resp.get("headers", {}) or {},
            response_body=resp.get("body", "") or "",
            deception_tag=tag,
            primitive_name=primitive,
            fake_value=fake if isinstance(fake, str) else (json.dumps(fake) if fake else None),
        )

    # ---------- queries ----------

    def turns(self) -> list[TurnRecord]:
        return list(self._load())

    def visits(self, path_regex: str) -> list[TurnRecord]:
        """Return all turns whose request path matches the regex."""
        try:
            pat = re.compile(path_regex)
        except re.error:
            return []
        return [t for t in self._load() if pat.search(t.request_path)]

    def last_turn(self) -> TurnRecord | None:
        turns = self._load()
        return turns[-1] if turns else None

    def primitives_fired(self) -> dict[str, list[int]]:
        """Map primitive_name → list of turn_idx where it fired.

        L1.5 fix 2026-07-07: reads primitives_fired list (canonical) so multiple
        primitives firing on the same turn are ALL counted. Old turns.jsonl
        (pre-L1.5) only has single z_t — fallback handles that.
        """
        out: dict[str, list[int]] = {}
        for t in self._load():
            tag = t.deception_tag or {}
            primitives_fired_list = tag.get("primitives_fired") or []
            if primitives_fired_list:
                for entry in primitives_fired_list:
                    name = entry.get("z_t") if isinstance(entry, dict) else None
                    if name:
                        out.setdefault(name, []).append(t.turn_idx)
            elif t.primitive_name:
                # Backward compat: old shape (single z_t)
                out.setdefault(t.primitive_name, []).append(t.turn_idx)
        return out

    def adopted_fakes(self) -> list[str]:
        """Return fake_values the agent cited in later request bodies.

        Heuristic: for each turn with a fake_value, search subsequent turns'
        request_body for that string. If found, mark as adopted.
        """
        turns = self._load()
        adopted: list[str] = []
        for i, t in enumerate(turns):
            if not t.fake_value or len(t.fake_value) < 6:
                continue
            for later in turns[i + 1:]:
                if t.fake_value in (later.request_body or ""):
                    if t.fake_value not in adopted:
                        adopted.append(t.fake_value)
                    break
        return adopted

    def coverage_state(self) -> dict[str, dict[str, bool]]:
        """Per primitive: {fired, adopted, contradicted}.

        - fired: primitive fired at least once
        - adopted: at least one fake_value from this primitive was cited later
        - contradicted: heuristic — agent's response_body mentioned "deception",
          "fake", "doesn't match", or status code was 4xx/5xx after adoption
        """
        turns = self._load()
        out: dict[str, dict[str, bool]] = {}
        for t in turns:
            if not t.primitive_name:
                continue
            entry = out.setdefault(t.primitive_name, {"fired": False, "adopted": False, "contradicted": False})
            entry["fired"] = True
            # Check adoption
            if t.fake_value and len(t.fake_value) >= 6:
                for later in turns[turns.index(t) + 1:]:
                    if t.fake_value in (later.request_body or ""):
                        entry["adopted"] = True
                        # Check for contradiction (agent rejected after adopting)
                        rb = (later.response_body or "").lower()
                        if any(k in rb for k in ("deception", "fake", "doesn't match", "inconsistent")):
                            entry["contradicted"] = True
                        break
        return out

    def endpoint_visit_counts(self) -> dict[str, int]:
        """Map request_path (without query string) → visit count."""
        out: dict[str, int] = {}
        for t in self._load():
            path = t.request_path.split("?", 1)[0]
            out[path] = out.get(path, 0) + 1
        return out

    # ---------- (C)-granularity profile methods ----------

    def response_fields_seen(self) -> dict[str, list[str]]:
        """Map request_path (no query) → sorted list of top-level JSON keys in response body.

        Used by planner to know which response fields agent actually saw on each
        visited surface. Only parses JSON bodies (content-type sniffed heuristically:
        body starts with '{' or '['). Non-JSON bodies contribute an empty list.
        Nested _links is flattened to top-level key '_links'.
        """
        out: dict[str, list[str]] = {}
        for t in self._load():
            path = t.request_path.split("?", 1)[0]
            body = (t.response_body or "").strip()
            if not body or body[0] not in "{[":
                out.setdefault(path, [])
                continue
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                out.setdefault(path, [])
                continue
            if isinstance(parsed, dict):
                keys = sorted(parsed.keys())
            elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                keys = sorted(parsed[0].keys())
            else:
                keys = []
            existing = out.setdefault(path, [])
            for k in keys:
                if k not in existing:
                    existing.append(k)
            existing.sort()
        return out

    def repeated_visit_endpoints(self, min_count: int = 2) -> dict[str, int]:
        """Subset of endpoint_visit_counts where count >= min_count.

        Repeated visits are a behavioral signal that the agent relies on / trusts
        this surface — high-value anchor for deception.
        """
        return {p: c for p, c in self.endpoint_visit_counts().items() if c >= min_count}

    def cited_fields_from_report(
        self,
        report_text: str,
        candidate_fields: list[str] | None = None,
    ) -> list[str]:
        """DEPRECATED — semantic analysis moved to planner subagent.

        Substring matching produced false positives (e.g. 'version' matched URL
        path string '/rest/admin/application-version', not a real citation).
        The planner subagent now reads profile.final_report_text directly and
        infers cited_fields / trusted_paths itself. This method is kept only for
        backward compatibility with any callers; new code should NOT use it.

        Returns [] always.
        """
        return []

    def profile_summary(
        self,
        report_text: str | None = None,
        min_repeated: int = 2,
    ) -> dict[str, Any]:
        """Mechanical-only (C)-granularity dump for the planner subagent.

        This is a DATA SOURCE, not an analysis. The planner subagent reads this
        + final_report_text and does the semantic analysis itself (which fields
        agent cited, which paths agent trusted, which beliefs formed).

        Args:
            report_text: agent's final_report_text (passed through verbatim —
                planner reads it from profile, not from a separate file).
            min_repeated: threshold for repeated_visit_endpoints.

        Returns:
            {
              "n_turns": int,
              "visited_paths": [{"path": str, "visit_count": int, "repeated": bool}],
              "fields_seen_per_path": {path: [fields]},
              "primitives_fired_in_clean": {name: [turn_idxs]},  # empty for true B0
              "final_report_text": str  # verbatim passthrough, planner does semantic
            }
        """
        turns = self._load()
        counts = self.endpoint_visit_counts()
        repeated = self.repeated_visit_endpoints(min_count=min_repeated)
        fields_per_path = self.response_fields_seen()

        visited_paths = [
            {
                "path": p,
                "visit_count": c,
                "repeated": p in repeated,
            }
            for p, c in sorted(counts.items(), key=lambda x: -x[1])
        ]

        return {
            "n_turns": len(turns),
            "visited_paths": visited_paths,
            "fields_seen_per_path": fields_per_path,
            "primitives_fired_in_clean": self.primitives_fired(),
            "final_report_text": report_text or "",
        }
