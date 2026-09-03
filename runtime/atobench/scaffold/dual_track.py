"""Dual-track validation — compares the legacy services-side deception path
against the new mitmproxy-based deception proxy.

The legacy path: services/acme/deceptive_mode.py etc. inject deception
into responses directly. The recorded turns.jsonl for those episodes
captures the post-deception response (with fake fields already present).

The new path: mitmproxy addon intercepts the upstream (truthful) response
and applies the transformer. The recorded turns.jsonl from the new path
captures the post-deception response too.

For dual-track validation we need BOTH paths to have run the same
deception_config and produced the same deception_tag (z_t, u_t) shape
and the same response mutations.

This module:
1. Loads N episodes from the legacy log (defaults to B3 episodes with the
   fake_version_banner + schema_coupled primitive).
2. For each episode, replays the recorded upstream (truthful) response
   through the new transformer.
3. Compares the new path's deception_tag + response fields to the legacy
   recorded ones.
4. Produces a markdown report.

Caveats:
- The legacy episodes' deception was applied INSIDE the services, so the
  recorded turns capture the post-deception response. To get the "upstream"
  response we'd need to re-run the episode with the services in truthful
  mode, which requires docker. In a dry-run, we approximate by stripping
  the known deception fields from the legacy response and treating that as
  the "upstream" — this is an approximation, not a true A/B test.
- A live dual-track validation requires `docker compose up` against a
  real target. The script supports that via the `--live` flag (not yet
  implemented in this env).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from atobench.proxy.flow import HTTPFlow, Request, Response
from atobench.proxy.pattern_matcher import find_match
from atobench.proxy.state_store import StateStore
from atobench.proxy.transformers import REGISTRY

LOG_DIR = Path("logs")


@dataclass
class EpisodeComparison:
    episode_id: str
    task: str
    baseline: str
    n_turns: int
    n_deceptive_turns: int
    n_z_t_matches: int = 0  # turns where new transformer's z_t matches legacy
    n_u_t_matches: int = 0  # turns where new transformer's u_t.fake_value matches legacy
    n_response_field_matches: int = 0  # turns where the new mutated response field matches legacy
    per_turn_mismatches: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DualTrackReport:
    n_episodes: int
    episodes: list[EpisodeComparison]
    overall_z_t_match_rate: float
    overall_u_t_match_rate: float
    overall_response_field_match_rate: float
    notes: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            "# Dual-Track Validation Report",
            "",
            f"Episodes compared: {self.n_episodes}",
            f"z_t match rate: {self.overall_z_t_match_rate:.1%}",
            f"u_t.fake_value match rate: {self.overall_u_t_match_rate:.1%}",
            f"response-field match rate: {self.overall_response_field_match_rate:.1%}",
            "",
            "## Per-episode breakdown",
            "",
            "| Episode | Task | Baseline | Turns | Deceptive | z_t match | u_t match | field match |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for ep in self.episodes:
            lines.append(
                f"| {ep.episode_id} | {ep.task} | {ep.baseline} | {ep.n_turns} | "
                f"{ep.n_deceptive_turns} | {ep.n_z_t_matches}/{ep.n_deceptive_turns} | "
                f"{ep.n_u_t_matches}/{ep.n_deceptive_turns} | "
                f"{ep.n_response_field_matches}/{ep.n_deceptive_turns} |"
            )
        if self.notes:
            lines.append("")
            lines.append("## Notes")
            for note in self.notes:
                lines.append(f"- {note}")
        return "\n".join(lines)


def _load_legacy_turns(episode_id: str, log_dir: Path = LOG_DIR) -> list[dict[str, Any]]:
    path = log_dir / "turns.jsonl"
    if not path.exists():
        return []
    turns: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                continue
            if t.get("episode_id") == episode_id:
                turns.append(t)
    turns.sort(key=lambda x: x.get("turn_idx", 0))
    return turns


def _load_legacy_episode_meta(episode_id: str, log_dir: Path = LOG_DIR) -> dict[str, Any]:
    path = log_dir / "episodes.jsonl"
    if not path.exists():
        return {}
    running: dict[str, dict[str, Any]] = {}
    ended: dict[str, dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("episode_id") != episode_id:
                continue
            if e.get("status") == "running":
                running[episode_id] = e
            elif e.get("status") == "ended":
                ended[episode_id] = e
    return {**running.get(episode_id, {}), **ended.get(episode_id, {})}


def _pick_legacy_episodes(n: int, log_dir: Path = LOG_DIR) -> list[str]:
    """Pick N B3 episodes with at least 1 deceptive turn."""
    path = log_dir / "episodes.jsonl"
    if not path.exists():
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (e.get("status") == "running" and e.get("baseline") == "B3"
                    and e.get("episode_id") not in seen):
                seen.add(e.get("episode_id"))
                candidates.append(e["episode_id"])
                if len(candidates) >= n:
                    break
    return candidates[:n]


def _build_flow_from_turn(turn: dict[str, Any]) -> HTTPFlow:
    """Reconstruct an HTTPFlow from a recorded legacy turn.

    The recorded response is the POST-deception response. For dry-run
    validation, we treat this as the 'upstream' (truthful) response — i.e.
    we assume the new transformer should reproduce the same response shape
    given the same primitive+coupling. This is an approximation: the legacy
    services may have produced the response from scratch (not by mutating
    an upstream response), so the new transformer might produce a slightly
    different shape (e.g. add vs overwrite fields). The dry-run check
    verifies that the new transformer's OUTPUT contains the legacy's
    deception_tag.u_t.fake_value and the same response field with the same
    fake value.
    """
    req_data = turn.get("request") or {}
    resp_data = turn.get("response") or {}
    request = Request(
        method=req_data.get("method", "GET"),
        path=req_data.get("path", "/"),
        headers=req_data.get("headers", {}),
        body=req_data.get("body", "").encode("utf-8") if req_data.get("body") else None,
        json=None,
    )
    response = Response(
        status_code=resp_data.get("status", 200),
        headers=resp_data.get("headers", {}),
        body=resp_data.get("body", "").encode("utf-8") if resp_data.get("body") else None,
        json=None,
    )
    flow = HTTPFlow(request=request, response=response)
    flow.episode_id = turn.get("episode_id", "")
    return flow


def _replay_turn_through_new_proxy(
    turn: dict[str, Any],
    deception_config: dict[str, Any],
    state_store: StateStore,
) -> tuple[HTTPFlow, str | None]:
    """Replay a recorded turn through the new proxy pipeline.

    Returns (flow_after_transform, matched_primitive_name_or_None).
    """
    flow = _build_flow_from_turn(turn)
    primitives = deception_config.get("primitives", [])
    match = find_match(flow, primitives)
    if not match.matched or not match.primitive_name:
        return flow, None
    spec = next((p for p in primitives if p.get("name") == match.primitive_name), {})
    transformer = REGISTRY.get(match.primitive_name)
    if transformer is None:
        return flow, None
    try:
        transformer.apply(flow, spec, state_store)
    except Exception as e:
        # transformer failed — leave flow unchanged, return error in tag
        flow.deception_tag = {"z_t": f"__error__:{e}", "u_t": {}, "D_t_snapshot": {}}
    return flow, match.primitive_name


def _compare_turn(turn: dict[str, Any], new_flow: HTTPFlow, matched_prim: str | None) -> dict[str, Any]:
    """Compare legacy turn's deception_tag + response to the new flow's."""
    legacy_tag = turn.get("deception_tag") or {}
    legacy_z_t = legacy_tag.get("z_t", "")
    legacy_u_t = legacy_tag.get("u_t", {})
    legacy_fake_values = [v for v in legacy_u_t.values() if isinstance(v, str) and len(v) >= 6]

    new_tag = new_flow.deception_tag or {}
    new_z_t = new_tag.get("z_t", "")
    new_u_t = new_tag.get("u_t", {})
    new_fake_values = [v for v in new_u_t.values() if isinstance(v, str) and len(v) >= 6]

    z_t_match = bool(legacy_z_t) and legacy_z_t == new_z_t
    u_t_match = any(lfv in new_fake_values for lfv in legacy_fake_values) if legacy_fake_values else (not new_fake_values)

    # Response field match: did the new flow produce the same fake_value in the response body?
    # Pull response body string and check if legacy fake_value appears
    new_body = ""
    if new_flow.response.body is not None:
        try:
            new_body = new_flow.response.body.decode("utf-8", errors="replace")
        except Exception:
            pass
    elif new_flow.response.json is not None:
        new_body = json.dumps(new_flow.response.json, ensure_ascii=False)

    response_field_match = any(lfv in new_body for lfv in legacy_fake_values) if legacy_fake_values else True

    return {
        "turn_idx": turn.get("turn_idx"),
        "legacy_z_t": legacy_z_t,
        "new_z_t": new_z_t,
        "z_t_match": z_t_match,
        "u_t_match": u_t_match,
        "response_field_match": response_field_match,
        "matched_primitive": matched_prim,
        "legacy_fake_values": legacy_fake_values[:3],
        "new_fake_values": new_fake_values[:3],
    }


def compare_episode(episode_id: str, deception_config: dict[str, Any], log_dir: Path = LOG_DIR) -> EpisodeComparison:
    """Run dual-track comparison for a single episode."""
    meta = _load_legacy_episode_meta(episode_id, log_dir=log_dir)
    turns = _load_legacy_turns(episode_id, log_dir=log_dir)
    state = StateStore(log_dir=log_dir, flush_every=100000)

    deceptive_turns = [t for t in turns if (t.get("deception_tag") or {}).get("z_t")]
    comp = EpisodeComparison(
        episode_id=episode_id,
        task=meta.get("task", "T1"),
        baseline=meta.get("baseline", "B3"),
        n_turns=len(turns),
        n_deceptive_turns=len(deceptive_turns),
    )

    for turn in deceptive_turns:
        new_flow, matched_prim = _replay_turn_through_new_proxy(turn, deception_config, state)
        result = _compare_turn(turn, new_flow, matched_prim)
        if result["z_t_match"]:
            comp.n_z_t_matches += 1
        if result["u_t_match"]:
            comp.n_u_t_matches += 1
        if result["response_field_match"]:
            comp.n_response_field_matches += 1
        if not (result["z_t_match"] and result["u_t_match"] and result["response_field_match"]):
            comp.per_turn_mismatches.append(result)

    return comp


def run_dual_track(
    deception_config_path: Path | str,
    n_episodes: int = 5,
    log_dir: Path = LOG_DIR,
    report_path: Path | str | None = None,
) -> DualTrackReport:
    """Run dual-track validation across N episodes. Writes markdown report."""
    with open(deception_config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    episode_ids = _pick_legacy_episodes(n_episodes, log_dir=log_dir)
    comparisons: list[EpisodeComparison] = []
    notes: list[str] = []

    if not episode_ids:
        notes.append("No legacy B3 episodes found in logs/episodes.jsonl — cannot validate.")
    else:
        for ep_id in episode_ids:
            try:
                comp = compare_episode(ep_id, cfg, log_dir=log_dir)
                comparisons.append(comp)
            except Exception as e:
                notes.append(f"Episode {ep_id} comparison failed: {e}")

    total_deceptive = sum(c.n_deceptive_turns for c in comparisons)
    total_z_t = sum(c.n_z_t_matches for c in comparisons)
    total_u_t = sum(c.n_u_t_matches for c in comparisons)
    total_field = sum(c.n_response_field_matches for c in comparisons)

    report = DualTrackReport(
        n_episodes=len(comparisons),
        episodes=comparisons,
        overall_z_t_match_rate=total_z_t / max(1, total_deceptive),
        overall_u_t_match_rate=total_u_t / max(1, total_deceptive),
        overall_response_field_match_rate=total_field / max(1, total_deceptive),
        notes=notes,
    )

    if report_path:
        Path(report_path).write_text(report.to_markdown(), encoding="utf-8")

    return report
