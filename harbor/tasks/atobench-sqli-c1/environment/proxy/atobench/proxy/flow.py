"""Lightweight HTTP flow abstraction for the deception proxy.

mitmproxy-agnostic. The `addon.py` adapter translates between mitmproxy's
HTTPFlow and these dataclasses at runtime; RuntimePipeline rules operate on
these types so they can be unit-tested without mitmproxy installed.

`runtime_events` is attached dynamically by RuntimePipeline and is the
canonical execution/attribution record. `deception_tag` remains as a legacy
projection for old evaluators and compatibility tests.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Request:
    method: str
    path: str  # full path including query string, e.g. "/api/v1/users?limit=10"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    json: dict | None = None  # parsed body, set when Content-Type is JSON

    def header(self, name: str) -> str | None:
        # case-insensitive header lookup
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return None


@dataclass
class Response:
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    json: dict | None = None  # parsed body, set by pattern_matcher or transformer

    def header(self, name: str) -> str | None:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return None

    def set_header(self, name: str, value: str) -> None:
        # remove existing case-variant then set
        for k in list(self.headers.keys()):
            if k.lower() == name.lower():
                del self.headers[k]
        self.headers[name] = value


@dataclass
class HTTPFlow:
    request: Request
    response: Response
    deception_tag: dict[str, Any] = field(default_factory=dict)
    matched_primitive: str | None = None  # set by pattern_matcher
    matched_coupling: str | None = None  # set by pattern_matcher

    def get_or_parse_response_json(self) -> dict | None:
        """Return response.json if already parsed; else try to parse body."""
        if self.response.json is not None:
            return self.response.json
        if self.response.body is None:
            return None
        try:
            data = json.loads(self.response.body.decode("utf-8"))
            if isinstance(data, dict):
                self.response.json = data
                return data
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        return None

    def write_response_json(self) -> None:
        """Serialize response.json back to response.body (utf-8).

        NOTE: do NOT set Content-Length here. mitmproxy auto-computes it from
        resp.content on the wire; setting it explicitly can cause truncation
        if a later transformer grows the body further.
        """
        if self.response.json is None:
            return
        self.response.body = json.dumps(self.response.json, ensure_ascii=False).encode("utf-8")
        self.response.set_header("Content-Type", "application/json")
