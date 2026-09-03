from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import GateError, read_json
from .sessions import iter_jsonl

CONDITION_RE = re.compile(r"_(c[01])_", re.IGNORECASE)


def global_pair_id(campaign_id: str, pair_id: str) -> str:
    prefix = f"{campaign_id}::"
    return pair_id if pair_id.startswith(prefix) else prefix + pair_id


def _model_dir(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _condition_from_episode_id(episode_id: str) -> str | None:
    match = CONDITION_RE.search(episode_id)
    return match.group(1).upper() if match else None


def _raw_episode_rows(root: Path, campaign_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for path in sorted((root / "logs").glob("*/*/episodes.jsonl")):
        model_dir = path.parent.parent.name
        aou = path.parent.name
        for line_number, value in iter_jsonl(path):
            if "_parse_error" in value:
                errors.append(
                    {
                        "campaign_id": campaign_id,
                        "source_path": str(path.resolve()),
                        "source_line_number": line_number,
                        "error": value["_parse_error"],
                    }
                )
                continue
            episode_id = value.get("episode_id")
            if not isinstance(episode_id, str) or not episode_id:
                errors.append(
                    {
                        "campaign_id": campaign_id,
                        "source_path": str(path.resolve()),
                        "source_line_number": line_number,
                        "error": "raw episode row missing episode_id",
                    }
                )
                continue
            row = rows.setdefault(
                episode_id,
                {
                    "episode_id": episode_id,
                    "model_dir": model_dir,
                    "aou": aou,
                    "condition": _condition_from_episode_id(episode_id),
                    "source_path": str(path.resolve()),
                    "source_line_numbers": [],
                    "started_at": None,
                    "ended_at": None,
                    "raw_status": None,
                    "outcome": None,
                    "final_report_text": None,
                },
            )
            row["source_line_numbers"].append(line_number)
            for key in ("started_at", "ended_at", "outcome", "final_report_text"):
                if value.get(key) is not None:
                    row[key] = value[key]
            if value.get("status") is not None:
                row["raw_status"] = value["status"]
    return sorted(rows.values(), key=lambda row: row["episode_id"]), errors


def _match_raw_episodes(
    planned: list[dict[str, Any]],
    started_events: dict[tuple[str, str, str], dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    campaign_id: str,
    errors: list[dict[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    planned_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    raw_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for assignment in planned:
        key = (
            _model_dir(str(assignment.get("model") or assignment.get("model_selector") or "")),
            str(assignment.get("aou") or ""),
            str(assignment.get("condition") or "").upper(),
        )
        event = started_events.get(
            (
                str(assignment.get("pair_id")),
                str(assignment.get("condition")).upper(),
                str(assignment.get("slot")),
            )
        )
        if event:
            planned_groups[key].append(
                {
                    "identity": (
                        str(assignment.get("pair_id")),
                        str(assignment.get("condition")).upper(),
                        str(assignment.get("slot")),
                    ),
                    "event_time": _timestamp(event.get("recorded_at")),
                }
            )
    for row in raw_rows:
        key = (row["model_dir"], row["aou"], str(row.get("condition") or ""))
        raw_groups[key].append(row)

    matched: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, event_rows in planned_groups.items():
        available = sorted(
            raw_groups.get(key, []),
            key=lambda row: (_timestamp(row.get("started_at")) or float("inf"), row["episode_id"]),
        )
        for event_row in sorted(
            event_rows,
            key=lambda row: (row["event_time"] or float("inf"), row["identity"]),
        ):
            event_time = event_row["event_time"]
            choices = [
                (abs(raw_time - event_time), index, raw)
                for index, raw in enumerate(available)
                if event_time is not None
                and (raw_time := _timestamp(raw.get("started_at"))) is not None
            ]
            if not choices:
                continue
            delta, index, raw = min(choices, key=lambda item: (item[0], item[2]["episode_id"]))
            if delta > 300:
                errors.append(
                    {
                        "campaign_id": campaign_id,
                        "error": "nearest raw episode start exceeds 300-second binding window",
                        "planned_identity": "::".join(event_row["identity"]),
                        "nearest_episode_id": raw["episode_id"],
                        "delta_seconds": round(delta, 3),
                    }
                )
                continue
            matched[event_row["identity"]] = raw
            available.pop(index)
    return matched


def _legacy_census(
    root: Path,
    manifest: dict[str, Any],
    campaign_id: str,
    errors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep the tiny scalar smoke fixture supported."""
    episode_rows: dict[str, dict[str, Any]] = {}
    for line_number, event in iter_jsonl(root / "events.jsonl"):
        if "_parse_error" in event:
            errors.append(
                {
                    "campaign_id": campaign_id,
                    "source_line_number": line_number,
                    "error": event["_parse_error"],
                }
            )
            continue
        episode_id = event.get("episode_id")
        if not episode_id:
            continue
        row = episode_rows.setdefault(
            str(episode_id),
            {
                "campaign_id": campaign_id,
                "episode_id": str(episode_id),
                "pair_id": str(event.get("pair_id") or str(episode_id).rsplit("_", 1)[0]),
                "condition": str(event.get("condition") or "UNKNOWN").upper(),
                "event_count": 0,
                "terminal_status": None,
                "execution_valid": False,
                "mapping_basis": "legacy_event_episode_id",
            },
        )
        row["event_count"] += 1
        row["terminal_status"] = event.get("status") or event.get("event") or event.get("type")
        row["execution_valid"] = row["terminal_status"] in {"complete", "ended"}
    episodes = list(episode_rows.values())
    pairs: dict[str, dict[str, Any]] = {}
    for row in episodes:
        gpair = global_pair_id(campaign_id, row["pair_id"])
        row["global_pair_id"] = gpair
        row["global_episode_id"] = f"{gpair}::{row['condition']}::{row['episode_id']}"
        pair = pairs.setdefault(
            gpair,
            {
                "campaign_id": campaign_id,
                "pair_id": row["pair_id"],
                "global_pair_id": gpair,
                "planned_c0": 0,
                "planned_c1": 0,
                "execution_valid_c0": 0,
                "execution_valid_c1": 0,
            },
        )
        pair[f"planned_{row['condition'].lower()}"] += 1
        if row["execution_valid"]:
            pair[f"execution_valid_{row['condition'].lower()}"] += 1
    return episodes, list(pairs.values())


def build_census(
    campaign_roots: list[Path],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    campaigns: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_global_episodes: set[str] = set()

    for root in campaign_roots:
        manifest_path = root / "campaign_manifest.json"
        events_path = root / "events.jsonl"
        if not manifest_path.is_file() or not events_path.is_file():
            raise GateError(f"campaign missing manifest/events: {root}")
        manifest = read_json(manifest_path)
        campaign_id = manifest.get("campaign_id")
        if not campaign_id:
            raise GateError(f"manifest missing campaign_id: {manifest_path}")
        planned_pairs = manifest.get("planned_pairs")
        planned_episodes = manifest.get("planned_episodes")
        if not isinstance(planned_pairs, list) or not isinstance(planned_episodes, list):
            legacy_episodes, legacy_pairs = _legacy_census(
                root, manifest, campaign_id, errors
            )
            episodes.extend(legacy_episodes)
            pairs.extend(legacy_pairs)
            campaigns.append(
                {
                    "campaign_id": campaign_id,
                    "root": str(root.resolve()),
                    "manifest_status": manifest.get("status"),
                    "planned_pair_count": int(planned_pairs or 0),
                    "planned_episode_count": int(planned_episodes or 0),
                    "execution_valid_episode_count": sum(
                        bool(row["execution_valid"]) for row in legacy_episodes
                    ),
                    "manifest_failure_count": len(manifest.get("failures") or []),
                }
            )
            continue

        event_rows: list[dict[str, Any]] = []
        for line_number, event in iter_jsonl(events_path):
            if "_parse_error" in event:
                errors.append(
                    {
                        "campaign_id": campaign_id,
                        "source_path": str(events_path.resolve()),
                        "source_line_number": line_number,
                        "error": event["_parse_error"],
                    }
                )
            else:
                event_rows.append(event)
        started_events = {
            (str(row.get("pair_id")), str(row.get("condition")).upper(), str(row.get("slot"))): row
            for row in event_rows
            if row.get("status") == "started" and row.get("pair_id") and row.get("slot")
        }
        complete_events = {
            (str(row.get("pair_id")), str(row.get("condition")).upper(), str(row.get("slot"))): row
            for row in event_rows
            if row.get("status") == "complete" and row.get("pair_id") and row.get("slot")
        }
        raw_rows, raw_errors = _raw_episode_rows(root, campaign_id)
        errors.extend(raw_errors)
        raw_matches = _match_raw_episodes(
            planned_episodes, started_events, raw_rows, campaign_id, errors
        )

        campaign_episode_rows: list[dict[str, Any]] = []
        for assignment in planned_episodes:
            pair_id = str(assignment["pair_id"])
            condition = str(assignment["condition"]).upper()
            slot = str(assignment["slot"])
            identity = (pair_id, condition, slot)
            gpair = global_pair_id(campaign_id, pair_id)
            global_episode = f"{gpair}::{condition}::{slot}"
            if global_episode in seen_global_episodes:
                raise GateError(f"duplicate global episode: {global_episode}")
            seen_global_episodes.add(global_episode)
            started = started_events.get(identity)
            completed = complete_events.get(identity)
            raw = raw_matches.get(identity)
            report = raw.get("final_report_text") if raw else None
            execution_valid = bool(
                completed
                and completed.get("returncode") == 0
                and raw
                and raw.get("raw_status") == "ended"
                and raw.get("outcome") == "success"
            )
            row = {
                "campaign_id": campaign_id,
                "episode_id": raw.get("episode_id") if raw else "",
                "pair_id": pair_id,
                "global_pair_id": gpair,
                "global_episode_id": global_episode,
                "condition": condition,
                "slot": slot,
                "model": assignment.get("model"),
                "model_selector": assignment.get("model_selector"),
                "aou": assignment.get("aou"),
                "unit_id": assignment.get("unit_id"),
                "block_id": assignment.get("block_id"),
                "planned": True,
                "event_started": bool(started),
                "event_complete": bool(completed),
                "returncode": completed.get("returncode") if completed else None,
                "raw_episode_present": bool(raw),
                "raw_status": raw.get("raw_status") if raw else None,
                "outcome": raw.get("outcome") if raw else None,
                "started_at": raw.get("started_at") if raw else None,
                "ended_at": raw.get("ended_at") if raw else None,
                "raw_episode_source": raw.get("source_path") if raw else "",
                "final_report_sha256": (
                    hashlib.sha256(report.encode()).hexdigest()
                    if isinstance(report, str)
                    else ""
                ),
                "execution_valid": execution_valid,
                "mapping_basis": (
                    "event_start_time_to_raw_episode_within_300s"
                    if raw
                    else "unmatched_planned_assignment"
                ),
                "terminal_status": raw.get("raw_status") if raw else None,
                "event_count": int(bool(started)) + int(bool(completed)),
            }
            campaign_episode_rows.append(row)
            episodes.append(row)

        for planned_pair in planned_pairs:
            pair_id = str(planned_pair["pair_id"])
            gpair = global_pair_id(campaign_id, pair_id)
            pair_eps = [row for row in campaign_episode_rows if row["global_pair_id"] == gpair]
            row = {
                "campaign_id": campaign_id,
                "pair_id": pair_id,
                "global_pair_id": gpair,
                "model": planned_pair.get("model"),
                "aou": planned_pair.get("aou"),
                "unit_id": planned_pair.get("unit_id"),
                "block_id": planned_pair.get("block_id"),
                "planned_c0": sum(item["condition"] == "C0" for item in pair_eps),
                "planned_c1": sum(item["condition"] == "C1" for item in pair_eps),
                "execution_valid_c0": sum(
                    item["condition"] == "C0" and item["execution_valid"] for item in pair_eps
                ),
                "execution_valid_c1": sum(
                    item["condition"] == "C1" and item["execution_valid"] for item in pair_eps
                ),
            }
            pairs.append(row)

        valid_pair_ids = {
            row["global_pair_id"]
            for row in pairs
            if row["campaign_id"] == campaign_id
            and row["execution_valid_c0"] == 1
            and row["execution_valid_c1"] == 1
        }
        for row in campaign_episode_rows:
            row["paired_analysis_eligible"] = (
                row["execution_valid"] and row["global_pair_id"] in valid_pair_ids
            )
        campaigns.append(
            {
                "campaign_id": campaign_id,
                "root": str(root.resolve()),
                "manifest_status": manifest.get("status"),
                "planned_pair_count": len(planned_pairs),
                "planned_episode_count": len(planned_episodes),
                "execution_valid_episode_count": sum(
                    bool(row["execution_valid"]) for row in campaign_episode_rows
                ),
                "paired_analysis_eligible_episode_count": sum(
                    bool(row["paired_analysis_eligible"]) for row in campaign_episode_rows
                ),
                "manifest_failure_count": len(manifest.get("failures") or []),
                "raw_episode_count": len(raw_rows),
                "unmatched_raw_episode_count": len(raw_rows) - len(raw_matches),
            }
        )

    for row in pairs:
        row["pair_complete"] = row["planned_c0"] == 1 and row["planned_c1"] == 1
        row["execution_valid_pair"] = (
            row["execution_valid_c0"] == 1 and row["execution_valid_c1"] == 1
        )
        row["c0_episode_count"] = row["planned_c0"]
        row["c1_episode_count"] = row["planned_c1"]
    return (
        sorted(campaigns, key=lambda row: row["campaign_id"]),
        sorted(pairs, key=lambda row: row["global_pair_id"]),
        sorted(episodes, key=lambda row: row["global_episode_id"]),
        errors,
    )
