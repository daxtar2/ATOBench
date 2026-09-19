"""Logging — writes turns.jsonl with canonical runtime_events.

For the standalone proxy container, the writer is inlined here so the
container is self-contained (no legacy proxy/ package needed).

Shape produced (one JSON object per line):
    {
        "episode_id": ...,
        "turn_idx": ...,
        "ts": ...,
        "proxy_service": "deception_proxy",
        "tool": "http",
        "tool_args": {"method": ..., "url": ...},
        "assistant_text": null,
        "request": {"method": ..., "path": ..., "headers": ..., "body": ...},
        "response": {"status": ..., "headers": ..., "body": ...},
        "runtime_events": [...],
        "deception_tag": {z_t, u_t, D_t_snapshot},
        "post_turn_signals": {},
    }

`runtime_events` is canonical for attribution and pentest-effect evaluation.
`deception_tag` is a compatibility projection for older turn-log consumers.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atobench.proxy.flow import HTTPFlow

PROXY_SERVICE = "deception_proxy"
DEFAULT_LOG_DIR = "logs"
DEFAULT_BODY_LIMIT = 8000

_write_lock = threading.Lock()


def _log_dir() -> str:
    return os.environ.get("ATOBENCH_LOG_DIR", DEFAULT_LOG_DIR)


def _turns_path() -> Path:
    return Path(_log_dir()) / "turns.jsonl"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _body_limit() -> int | None:
    """Return configured body logging limit.

    `ATOBENCH_LOG_BODY_LIMIT=0` or `none` disables truncation. The default remains
    conservative for exploratory runs, while formal campaigns can opt into full
    body capture explicitly.
    """
    raw = os.environ.get("ATOBENCH_LOG_BODY_LIMIT")
    if raw is None or raw == "":
        return DEFAULT_BODY_LIMIT
    value = raw.strip().lower()
    if value in {"0", "none", "unlimited", "full"}:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return DEFAULT_BODY_LIMIT
    return None if parsed <= 0 else parsed


def _truncate(s: str | bytes | None, limit: int | None = None) -> str | None:
    if s is None:
        return None
    if isinstance(s, bytes):
        try:
            s = s.decode("utf-8", errors="replace")
        except Exception:
            return None
    if limit is None:
        return s
    return s[:limit]


def _body_as_str(body: bytes | None) -> str | None:
    if body is None:
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("latin-1", errors="replace")


def _append_turn(record: dict[str, Any]) -> None:
    """Append a turn record to turns.jsonl. Thread-safe, immediate flush."""
    path = _turns_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with _write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()


def log_flow(
    flow: HTTPFlow,
    episode_id: str,
    turn_idx: int,
    proxy_service: str = PROXY_SERVICE,
    tool: str = "http",
    extra_post_turn_signals: dict[str, Any] | None = None,
) -> None:
    """Emit a turn record to turns.jsonl in the legacy shape."""
    limit = _body_limit()
    request_body = _body_as_str(flow.request.body)
    response_body = _body_as_str(flow.response.body)
    req_payload = {
        "method": flow.request.method,
        "path": flow.request.path,
        "headers": dict(flow.request.headers),
        "body": _truncate(request_body, limit),
        "body_meta": _body_meta(request_body, limit),
    }
    resp_payload = {
        "status": flow.response.status_code,
        "headers": dict(flow.response.headers),
        "body": _truncate(response_body, limit),
        "body_meta": _body_meta(response_body, limit),
    }
    runtime_events = list(getattr(flow, "runtime_events", []) or [])
    tool_args = {"method": flow.request.method, "url": flow.request.path}
    record = {
        "episode_id": episode_id,
        "turn_idx": turn_idx,
        "ts": _ts(),
        "proxy_service": proxy_service,
        "tool": tool,
        "tool_args": tool_args,
        "assistant_text": None,
        "request": req_payload,
        "response": resp_payload,
        "runtime_events": runtime_events,
        "deception_tag": flow.deception_tag or {},
        "post_turn_signals": extra_post_turn_signals or {},
    }
    _append_turn(record)


def _body_meta(body: str | None, limit: int | None) -> dict[str, Any]:
    if body is None:
        return {
            "chars_original": 0,
            "chars_logged": 0,
            "truncated": False,
            "limit": limit,
        }
    logged_len = len(body) if limit is None else min(len(body), limit)
    return {
        "chars_original": len(body),
        "chars_logged": logged_len,
        "truncated": limit is not None and len(body) > limit,
        "limit": limit,
    }
