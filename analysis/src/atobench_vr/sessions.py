from __future__ import annotations

import json
import re
import shlex
import hashlib
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from .common import sha256_file

CONTENT_TYPES = {"thinking", "text", "tool_use", "tool_result"}
URL_RE = re.compile(r"https?://[^\s\"'`]+")
EPISODE_ID_RE = re.compile(r"\bep_[A-Za-z0-9_]+\b")


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                yield line_number, {
                    "_parse_error": str(exc),
                    "_raw_prefix": raw[:200],
                }
                continue
            yield line_number, value


def discover_session_files(project_root: Path) -> list[Path]:
    """Return canonical transcripts only, including subagent JSONL files."""
    return sorted(path for path in project_root.rglob("*.jsonl") if path.is_file())


def session_tree_root(path: Path) -> Path:
    if path.parent.name == "subagents":
        return path.parent.parent
    sibling_tree = path.parent / path.stem
    return sibling_tree if sibling_tree.is_dir() else path.parent


def discover_candidate_session_trees(
    project_root: Path,
    episode_ids: set[str],
    campaign_ids: set[str],
) -> dict[Path, dict[str, Any]]:
    """Find frozen-campaign candidates while retaining exact-ID evidence.

    Path matches are discovery hints only. Canonical episode mapping is performed
    later at session-tree level using exact episode IDs or exact report hashes.
    """
    campaign_tokens = {
        token
        for campaign_id in campaign_ids
        for token in (campaign_id.lower(), campaign_id.lower().replace("_", "-"))
    }
    trees: dict[Path, dict[str, Any]] = {}
    for path in discover_session_files(project_root):
        path_lower = str(path).lower()
        path_campaign_match = any(token in path_lower for token in campaign_tokens)
        text = path.read_text(encoding="utf-8", errors="replace")
        exact_ids = sorted(set(EPISODE_ID_RE.findall(text)) & episode_ids)
        if not path_campaign_match and not exact_ids:
            continue
        root = session_tree_root(path)
        tree = trees.setdefault(
            root,
            {
                "root": root,
                "files": [],
                "episode_ids": set(),
                "path_campaign_match": False,
            },
        )
        tree["files"].append(path)
        tree["episode_ids"].update(exact_ids)
        tree["path_campaign_match"] = tree["path_campaign_match"] or path_campaign_match
    for tree in trees.values():
        root = tree["root"]
        sibling_main = root.parent / f"{root.name}.jsonl"
        tree["files"] = sorted(
            {
                *tree["files"],
                *([sibling_main] if sibling_main.is_file() else []),
                *root.glob("*.jsonl"),
                *(root / "subagents").glob("*.jsonl"),
            }
        )
    return trees


def _all_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _all_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_strings(item)


def session_tree_inventory_row(root: Path, files: list[Path]) -> dict[str, Any]:
    sibling_main = root.parent / f"{root.name}.jsonl"
    main_files = [
        path
        for path in files
        if path == sibling_main or (path.parent == root and path.parent.name != "subagents")
    ]
    subagent_files = [path for path in files if path.parent.name == "subagents"]
    main_ids = sorted(path.stem for path in main_files)
    subagent_ids = sorted(path.stem.removeprefix("agent-") for path in subagent_files)
    exact_text_hashes: set[str] = set()
    parse_errors = 0
    record_count = 0
    episode_ids: set[str] = set()
    for path in files:
        for _, record in iter_jsonl(path):
            record_count += 1
            if "_parse_error" in record:
                parse_errors += 1
                continue
            for value in _all_strings(record):
                exact_text_hashes.add(hashlib.sha256(value.encode()).hexdigest())
                episode_ids.update(EPISODE_ID_RE.findall(value))
    file_hashes = {str(path.resolve()): sha256_file(path) for path in files}
    sidecar_count = len(
        {
            sidecar
            for path in files
            for sidecar in optional_sidecar_paths(path)
        }
    )
    return {
        "canonical_session_tree_id": hashlib.sha256(
            str(root.resolve()).encode()
        ).hexdigest()[:20],
        "tree_root": str(root.resolve()),
        "main_session_id": main_ids[0] if len(main_ids) == 1 else "",
        "main_session_count": len(main_ids),
        "subagent_ids": subagent_ids,
        "session_file_paths": sorted(file_hashes),
        "file_sha256": file_hashes,
        "record_count": record_count,
        "parse_error_count": parse_errors,
        "optional_sidecar_output_count": sidecar_count,
        "output_coverage_status": (
            "canonical_jsonl_with_optional_sidecar"
            if sidecar_count
            else "canonical_jsonl_inline_only"
        ),
        "missing_output_inferred": False,
        "engagement_ids": sorted(episode_ids),
        "_exact_text_hashes": exact_text_hashes,
    }


def optional_sidecar_paths(session_file: Path) -> set[Path]:
    """Find optional large-output sidecars for main or subagent transcripts."""
    candidates = [
        session_file.parent / "tasks",
        session_file.parent.parent / "tasks",
        session_file.parent / session_file.stem / "tasks",
    ]
    return {
        path.resolve()
        for directory in candidates
        if directory.is_dir()
        for path in directory.glob("*.output")
        if path.exists()
    }


def count_optional_sidecars(session_file: Path) -> int:
    """Count optional sidecars without treating their absence as missing data."""
    return len(optional_sidecar_paths(session_file))


def session_inventory_row(path: Path) -> dict[str, Any]:
    session_id = path.stem.removeprefix("agent-")
    is_subagent = path.parent.name == "subagents" or path.stem.startswith("agent-")
    sidecars = count_optional_sidecars(path)
    parse_errors = 0
    records = 0
    engagement_ids: set[str] = set()
    for _, record in iter_jsonl(path):
        records += 1
        if "_parse_error" in record:
            parse_errors += 1
        text = json.dumps(record, ensure_ascii=False)
        engagement_ids.update(EPISODE_ID_RE.findall(text))
    return {
        "session_id": session_id,
        "source_path": str(path.resolve()),
        "source_sha256": sha256_file(path),
        "is_subagent": is_subagent,
        "record_count": records,
        "parse_error_count": parse_errors,
        "optional_sidecar_output_count": sidecars,
        "output_coverage_status": (
            "canonical_jsonl_with_optional_sidecar"
            if sidecars
            else "canonical_jsonl_inline_only"
        ),
        "missing_output_inferred": False,
        "engagement_ids": sorted(engagement_ids),
    }


def _content_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    if not isinstance(message, dict):
        content = record.get("content")
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    return []


def flatten_session(
    path: Path,
    episode_id: str = "UNMAPPED",
    session_tree_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    flattened: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    source_hash = sha256_file(path)
    session_id = path.stem.removeprefix("agent-")
    for line_number, record in iter_jsonl(path):
        if "_parse_error" in record:
            errors.append(
                {
                    "source_file": str(path.resolve()),
                    "source_line_number": line_number,
                    "error": record["_parse_error"],
                }
            )
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        role = message.get("role") or record.get("type") or "unknown"
        for item_index, item in enumerate(_content_items(record)):
            content_type = item.get("type", "text")
            if content_type not in CONTENT_TYPES:
                continue
            text = item.get("thinking") or item.get("text") or item.get("content")
            message_uuid = record.get("uuid") or f"{session_id}:{line_number}"
            row = {
                "episode_id": episode_id,
                "session_tree_id": session_tree_id or session_id,
                "session_id": record.get("sessionId") or session_id,
                "agent_id": record.get("agentId"),
                "message_uuid": message_uuid,
                "content_item_index": item_index,
                "message_content_id": (
                    f"{source_hash[:16]}:line:{line_number}:content:{item_index}"
                ),
                "parent_uuid": record.get("parentUuid"),
                "source_file_sha256": source_hash,
                "source_file_path_private": str(path.resolve()),
                "source_line_number": line_number,
                "timestamp": record.get("timestamp"),
                "sequence_index": len(flattened),
                "role": role,
                "content_type": content_type,
                "recorded_rationale_text": text if content_type == "thinking" else None,
                "visible_text": text if content_type in {"text", "tool_result"} else None,
                "tool_name": item.get("name") if content_type == "tool_use" else None,
                "tool_use_id": item.get("id") if content_type == "tool_use" else None,
                "tool_result_for_id": item.get("tool_use_id") if content_type == "tool_result" else None,
                "tool_input_redacted": item.get("input") if content_type == "tool_use" else None,
                "tool_result_redacted": item.get("content") if content_type == "tool_result" else None,
                "model_route_raw": message.get("model"),
                "claude_code_version": record.get("version"),
                "entrypoint": record.get("entrypoint"),
                "is_sidechain": bool(record.get("isSidechain", False)),
            }
            flattened.append(row)
    return flattened, errors


def canonical_route(url: str) -> str:
    path = urlsplit(url).path or "/"
    path = re.sub(
        r"/(?:(?:\d+)|(?:[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}))(?=/|$)",
        "/{id}",
        path,
    )
    return path


def _body_hash(body: str | None) -> str | None:
    if body is None:
        return None
    return hashlib.sha256(body.encode()).hexdigest()


def parse_http_commands(tool_name: str | None, tool_input: Any) -> list[dict[str, Any]]:
    """Conservatively parse curl commands; unparsed commands remain observable."""
    if not isinstance(tool_input, dict):
        return []
    command = tool_input.get("command")
    if not isinstance(command, str) or "curl" not in command:
        return []
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    requests: list[dict[str, Any]] = []
    curl_indexes = [index for index, token in enumerate(tokens) if token == "curl"]
    for position, start in enumerate(curl_indexes):
        end = curl_indexes[position + 1] if position + 1 < len(curl_indexes) else len(tokens)
        chunk = tokens[start + 1 : end]
        method: str | None = None
        body: str | None = None
        url: str | None = None
        headers: list[str] = []
        index = 0
        while index < len(chunk):
            token = chunk[index]
            if token in {"-X", "--request"} and index + 1 < len(chunk):
                method = chunk[index + 1].upper()
                index += 2
                continue
            if token in {"-d", "--data", "--data-raw", "--data-binary", "--json"} and index + 1 < len(chunk):
                body = chunk[index + 1]
                index += 2
                continue
            if token in {"-H", "--header"} and index + 1 < len(chunk):
                headers.append(chunk[index + 1])
                index += 2
                continue
            if token.startswith("http://") or token.startswith("https://"):
                url = token
            index += 1
        if url is None:
            match = URL_RE.search(command)
            url = match.group(0) if match else None
        if url is None:
            continue
        method = method or ("POST" if body is not None else "GET")
        requests.append(
            {
                "request_index": len(requests),
                "method": method,
                "url_redacted": url,
                "canonical_route": canonical_route(url),
                "request_body_sha256": _body_hash(body),
                "has_authorization_header": any(
                    header.lower().startswith("authorization:") for header in headers
                ),
                "parse_status": "parsed_curl",
            }
        )
    return requests


def build_action_cycles(flattened: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cycles: list[dict[str, Any]] = []
    episode_rows: dict[str, list[dict[str, Any]]] = {}
    for row in flattened:
        episode_rows.setdefault(str(row.get("episode_id")), []).append(row)
    for episode_id, rows in sorted(episode_rows.items()):
        ordered = sorted(rows, key=lambda row: int(row.get("sequence_index") or 0))
        by_tool_result: dict[tuple[str, str], list[dict[str, Any]]] = {}
        tool_use_positions: dict[tuple[str, str], list[int]] = {}
        for row in ordered:
            source_id = row.get("tool_result_for_id")
            if source_id:
                key = (str(row.get("session_id")), str(source_id))
                by_tool_result.setdefault(key, []).append(row)
            if row.get("content_type") == "tool_use" and row.get("tool_use_id"):
                key = (str(row.get("session_id")), str(row["tool_use_id"]))
                tool_use_positions.setdefault(key, []).append(
                    int(row.get("sequence_index") or 0)
                )
        preceding_rationale: dict[str, list[dict[str, Any]]] = {}
        for index, row in enumerate(ordered):
            if row.get("content_type") in {"thinking", "text"}:
                text = row.get("recorded_rationale_text") or row.get("visible_text")
                if text:
                    preceding_rationale[str(row.get("session_id"))] = [row]
                continue
            if row.get("content_type") != "tool_use":
                continue
            tool_id = row.get("tool_use_id")
            session_id = str(row.get("session_id"))
            key = (session_id, str(tool_id))
            current_sequence = int(row.get("sequence_index") or 0)
            next_sequence = next(
                (
                    position
                    for position in tool_use_positions.get(key, [])
                    if position > current_sequence
                ),
                None,
            )
            result_rows = [
                item
                for item in by_tool_result.get(key, [])
                if int(item.get("sequence_index") or 0) > current_sequence
                and (
                    next_sequence is None
                    or int(item.get("sequence_index") or 0) < next_sequence
                )
            ]
            rationale_rows = preceding_rationale.get(session_id, [])
            cycles.append(
                {
                    "schema_version": "atobench.action_cycle.v1",
                    "episode_id": episode_id,
                    "global_episode_id": row.get("global_episode_id"),
                    "global_pair_id": row.get("global_pair_id"),
                    "action_cycle_id": (
                        f"{episode_id}:{row['source_file_sha256'][:16]}:"
                        f"{row['source_line_number']}:{row.get('content_item_index', 0)}"
                    ),
                    "session_tree_id": row.get("session_tree_id"),
                    "session_id": row.get("session_id"),
                    "agent_id": row.get("agent_id"),
                    "tool_name": row.get("tool_name"),
                    "tool_use_id": tool_id,
                    "timestamp": row.get("timestamp"),
                    "sequence_index": row.get("sequence_index"),
                    "preceding_recorded_message_ids": [
                        item["message_uuid"] for item in rationale_rows
                    ],
                    "preceding_recorded_content_ids": [
                        item["message_content_id"] for item in rationale_rows
                    ],
                    "tool_result_message_ids": [item["message_uuid"] for item in result_rows],
                    "tool_result_content_ids": [
                        item["message_content_id"] for item in result_rows
                    ],
                    "tool_input_redacted": row.get("tool_input_redacted"),
                    "tool_result_redacted": [
                        item.get("tool_result_redacted") or item.get("visible_text")
                        for item in result_rows
                    ],
                    "parsed_http_requests": parse_http_commands(
                        row.get("tool_name"), row.get("tool_input_redacted")
                    ),
                }
            )
    return cycles
