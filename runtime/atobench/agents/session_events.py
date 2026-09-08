"""Canonical agent session events (agent_session_event.v1).

Every driver emits one ``agent_session.jsonl`` per episode into the isolated
episode workspace. The file is the driver-neutral trajectory contract:
whatever the agent's native stream looks like, the adapter translates it at
the edge into the closed event vocabulary defined by
``atobench/schema/agent_session_event.json``.

Two attestation modes:

- ``stream_verified`` — events were derived from the agent's own structured
  event stream (Claude Code ``stream-json``), so the ordering and content are
  attested by the agent process itself.
- ``adapter_declared`` — events were reconstructed by the harness from
  process-level observations (spawn, report artifact, exit code) without
  agent-side structured telemetry.

Redaction rules: text payloads are capped, chain-of-thought is never
emitted (there is no event type for it), and raw HTTP bodies stay in the
proxy-side ``turns.jsonl`` — the session file only carries summaries.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import jsonschema

from atobench.schema.loader import validate_agent_session_event

SCHEMA_VERSION = "atobench.agent_session_event.v1"

MAX_EVENT_TEXT_CHARS = 4096
MAX_REPORT_TEXT_CHARS = 32768
TRUNCATION_MARKER = "\n...[truncated]..."

# Claude Code stream-json block types that must never reach the session file.
# "thinking" is hidden chain-of-thought; "system" entries inside assistant
# content are internal notices, not agent actions.
EXCLUDED_CONTENT_TYPES = {"thinking", "system"}

_TEXT_BLOCK_SUMMARY_LIMIT = 120


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + TRUNCATION_MARKER, True


def _summarize_text(text: str) -> str:
    """Collapse a text block to a one-line digest for tool summaries."""
    collapsed = " ".join(text.split())
    return _cap(collapsed, _TEXT_BLOCK_SUMMARY_LIMIT)[0]


class SessionEventWriter:
    """Append-only, schema-checked writer for ``agent_session.jsonl``.

    Sequence numbers are assigned per writer; a fresh episode starts a fresh
    writer, so a validated file always has strictly increasing ``seq`` values
    starting at 0.
    """

    def __init__(
        self,
        path: Path,
        episode_id: str,
        *,
        attestation_mode: str = "adapter_declared",
    ) -> None:
        if attestation_mode not in {"stream_verified", "adapter_declared"}:
            raise ValueError(f"unknown attestation mode: {attestation_mode!r}")
        self.path = Path(path)
        self.episode_id = episode_id
        self.attestation_mode = attestation_mode
        self._seq = 0

    def append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "schema_version": SCHEMA_VERSION,
            "episode_id": self.episode_id,
            "seq": self._seq,
            "ts": _now(),
            "event_type": event_type,
            "attestation": {"mode": self.attestation_mode},
            "payload": payload,
        }
        validate_agent_session_event(event)
        self._seq += 1
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        return event

    def append_text_event(self, event_type: str, text: str) -> dict[str, Any]:
        """Append a text-carrying event with the schema's truncation contract."""
        capped, truncated = _cap(text, MAX_REPORT_TEXT_CHARS if event_type == "final_report" else MAX_EVENT_TEXT_CHARS)
        return self.append(event_type, {"text": capped, "truncated": truncated})

    def append_session_end(self, exit_code: int | None, duration_s: float) -> dict[str, Any]:
        return self.append(
            "session_end",
            {"exit_code": exit_code, "duration_s": round(float(duration_s), 3)},
        )

    def append_error(self, message: str, *, fatal: bool) -> dict[str, Any]:
        capped, _ = _cap(message, MAX_EVENT_TEXT_CHARS)
        return self.append("error", {"message": capped, "fatal": fatal})

    @property
    def event_count(self) -> int:
        return self._seq


def validate_session_file(path: Path) -> dict[str, Any]:
    """Validate a session file and summarize it without copying content.

    Returns {"event_count", "invalid_line_count", "episode_ids"} — callers
    get attestation-relevant facts, never trajectory bodies.
    """
    event_count = 0
    invalid = 0
    episode_ids: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            validate_agent_session_event(event)
        except (json.JSONDecodeError, jsonschema.ValidationError):
            invalid += 1
            continue
        event_count += 1
        episode_ids.add(str(event.get("episode_id")))
    return {
        "event_count": event_count,
        "invalid_line_count": invalid,
        "episode_ids": sorted(episode_ids),
    }


# ---------- Claude Code stream-json translation ----------


def claude_stream_to_session_events(stdout: str) -> list[dict[str, Any]]:
    """Translate a Claude Code ``stream-json`` transcript into canonical events.

    ``thinking`` and internal ``system`` blocks are dropped. Raw tool output
    bodies are reduced to short summaries; full HTTP transcripts remain in the
    proxy-side turns.jsonl. Tool names on ``tool_result`` events are resolved
    from the earlier ``tool_call`` with the same ``tool_use_id``.
    """
    events: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            stream_event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(stream_event, dict):
            continue
        events.extend(_translate_stream_event(stream_event, tool_names))
    return events


def _translate_stream_event(
    stream_event: dict[str, Any], tool_names: dict[str, str]
) -> list[dict[str, Any]]:
    etype = stream_event.get("type")
    if etype == "system":
        return [{"event_type": "session_start", "payload": {"driver": "claude-code"}}]
    if etype == "assistant":
        return _translate_assistant_message(stream_event.get("message") or {}, tool_names)
    if etype == "user":
        return _translate_user_message(stream_event.get("message") or {}, tool_names)
    if etype == "result":
        events: list[dict[str, Any]] = []
        result_text = stream_event.get("result")
        if isinstance(result_text, str) and result_text.strip():
            capped, truncated = _cap(result_text, MAX_REPORT_TEXT_CHARS)
            events.append(
                {
                    "event_type": "final_report",
                    "payload": {"text": capped, "truncated": truncated},
                }
            )
        subtype = stream_event.get("subtype")
        if stream_event.get("is_error") is True or (
            isinstance(subtype, str) and subtype not in {"success", ""}
        ):
            events.append(
                {
                    "event_type": "error",
                    "payload": {
                        "message": str(subtype or "result_error"),
                        "fatal": True,
                    },
                }
            )
        return events
    return []


def _translate_assistant_message(
    message: dict[str, Any], tool_names: dict[str, str]
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in EXCLUDED_CONTENT_TYPES:
            continue
        if block_type == "text":
            text = str(block.get("text") or "")
            if not text.strip():
                continue
            capped, truncated = _cap(text, MAX_EVENT_TEXT_CHARS)
            events.append(
                {"event_type": "assistant_message", "payload": {"text": capped, "truncated": truncated}}
            )
        elif block_type == "tool_use":
            arguments = block.get("input")
            arguments_summary, truncated = _cap(
                _summarize_text(json.dumps(arguments, ensure_ascii=False) if arguments is not None else ""),
                MAX_EVENT_TEXT_CHARS,
            )
            payload: dict[str, Any] = {
                "tool_name": str(block.get("name") or "unknown"),
                "arguments_summary": arguments_summary,
                "truncated": truncated,
            }
            if block.get("id"):
                payload["tool_use_id"] = str(block["id"])
                tool_names[str(block["id"])] = str(block.get("name") or "unknown")
            events.append({"event_type": "tool_call", "payload": payload})
    return events


def _translate_user_message(
    message: dict[str, Any], tool_names: dict[str, str]
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_use_id = str(block.get("tool_use_id") or "")
        payload: dict[str, Any] = {
            "tool_name": tool_names.get(tool_use_id, "tool"),
            "is_error": block.get("is_error") is True,
            "summary": _summarize_text(_tool_result_text(block.get("content"))),
            "truncated": False,
        }
        if tool_use_id:
            payload["tool_use_id"] = tool_use_id
        events.append({"event_type": "tool_result", "payload": payload})
    return events


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, dict) and block.get("type") == "tool_use":
                parts.append(json.dumps(block.get("input") or {}, ensure_ascii=False))
        return "\n".join(parts)
    return ""


def write_session_file(
    path: Path,
    episode_id: str,
    *,
    attestation_mode: str,
    events: Iterable[tuple[str, dict[str, Any]]],
) -> SessionEventWriter:
    """Write a complete session file from pre-built (event_type, payload) pairs."""
    writer = SessionEventWriter(path, episode_id, attestation_mode=attestation_mode)
    for event_type, payload in events:
        payload = dict(payload)
        if event_type == "session_end":
            writer.append_session_end(
                payload.get("exit_code"), float(payload.get("duration_s", 0.0))
            )
        elif event_type == "error":
            writer.append_error(str(payload.get("message")), fatal=bool(payload.get("fatal")))
        elif event_type == "final_report":
            capped, truncated = _cap(str(payload.get("text", "")), MAX_REPORT_TEXT_CHARS)
            final_payload: dict[str, Any] = {"text": capped, "truncated": truncated}
            if payload.get("report_path"):
                final_payload["report_path"] = str(payload["report_path"])
            writer.append(event_type, final_payload)
        elif event_type == "assistant_message":
            writer.append_text_event(event_type, str(payload.get("text", "")))
        else:
            writer.append(event_type, payload)
    return writer
