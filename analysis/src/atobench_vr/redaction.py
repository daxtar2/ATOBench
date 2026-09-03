from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

REDACTION_VERSION = "atobench.redaction.v2"
DEFAULT_LARGE_BODY_CHARS = 32_768
DEFAULT_PREFIX_CHARS = 4_096

PATTERNS = [
    ("BEARER", re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)(?!\s*[\"']?\s*<)([^\s\"']+)")),
    ("BASIC_AUTH", re.compile(r"(?i)(Authorization\s*:\s*Basic\s+)(?!\s*[\"']?\s*<)([^\s\"']+)")),
    (
        "JWT",
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
            r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}(?![A-Za-z0-9_-])"
        ),
    ),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("COOKIE", re.compile(r"(?i)((?:Set-)?Cookie\s*:\s*)(?!\s*[\"']?\s*<)([^\r\n]+)")),
    (
        "PASSWORD",
        re.compile(
            r"(?i)((?:password|passwd|pwd|passwordHash)"
            r"\s*(?:[\"']?\s*[:=]\s*[\"']?|[\"']\s*:\s*[\"']))"
            r"(?!\s*[\"']?\s*<)([^\"'\s,;&}]+)"
        ),
    ),
    (
        "TOTP_SECRET",
        re.compile(
            r"(?i)((?:totp(?:Secret)?|otpSecret|mfaSecret)"
            r"\s*(?:[\"']?\s*[:=]\s*[\"']?|[\"']\s*:\s*[\"']))"
            r"(?!\s*[\"']?\s*<)([^\"'\s,;&}]+)"
        ),
    ),
    (
        "RESET_ANSWER",
        re.compile(
            r"(?i)((?:resetAnswer|securityAnswer|answer)"
            r"\s*(?:[\"']?\s*[:=]\s*[\"']?|[\"']\s*:\s*[\"']))"
            r"(?!\s*[\"']?\s*<)([^\"'\r\n,;&}]+)"
        ),
    ),
    (
        "API_KEY",
        re.compile(
            r"(?i)((?:api[_-]?key|client[_-]?secret|access[_-]?token)"
            r"\s*(?:[\"']?\s*[:=]\s*[\"']?|[\"']\s*:\s*[\"']))"
            r"(?!\s*[\"']?\s*<)([A-Za-z0-9_.\-/+]{8,})"
        ),
    ),
    (
        "RESOURCE",
        re.compile(
            r"(?i)((?:userId|basketId|resourceId|subjectId|bid)"
            r"\s*(?:[\"']?\s*[:=]\s*[\"']?|[\"']\s*:\s*[\"']))"
            r"(?!\s*[\"']?\s*<)([A-Za-z0-9_-]+)"
        ),
    ),
    (
        "SESSION",
        re.compile(
            r"(?i)((?:sessionId|session_id)"
            r"\s*(?:[\"']?\s*[:=]\s*[\"']?|[\"']\s*:\s*[\"']))"
            r"(?!\s*[\"']?\s*<)([A-Za-z0-9_-]+)"
        ),
    ),
]

PREFIXED_KINDS = {
    "BEARER",
    "BASIC_AUTH",
    "COOKIE",
    "PASSWORD",
    "TOTP_SECRET",
    "RESET_ANSWER",
    "API_KEY",
    "RESOURCE",
    "SESSION",
}


@dataclass
class StableRedactor:
    """Stable within one pair; the private mapping is never exported."""

    salt: str
    mapping: dict[tuple[str, str], str] = field(default_factory=dict)

    def placeholder(self, kind: str, raw: str) -> str:
        key = (kind, raw)
        if key not in self.mapping:
            suffix = hashlib.sha256(
                f"{self.salt}:{kind}:{raw}".encode()
            ).hexdigest()[:8].upper()
            self.mapping[key] = f"<{kind}_{suffix}>"
        return self.mapping[key]

    def redact(self, text: str) -> str:
        value = text
        for kind, pattern in PATTERNS:
            if kind in PREFIXED_KINDS:
                value = pattern.sub(
                    lambda match, current_kind=kind: (
                        match.group(1)
                        + self.placeholder(current_kind, match.group(2))
                    ),
                    value,
                )
            else:
                value = pattern.sub(
                    lambda match, current_kind=kind: self.placeholder(
                        current_kind, match.group(0)
                    ),
                    value,
                )
        return value

    def redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {
                key: self.redact_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        return value


def summarize_large_value(
    value: Any,
    redactor: StableRedactor,
    *,
    threshold: int = DEFAULT_LARGE_BODY_CHARS,
    prefix_chars: int = DEFAULT_PREFIX_CHARS,
) -> Any:
    if value is None:
        return None
    encoded = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True)
    )
    redacted_value = redactor.redact_value(value)
    redacted = (
        redacted_value
        if isinstance(redacted_value, str)
        else json.dumps(redacted_value, ensure_ascii=False, sort_keys=True)
    )
    if len(encoded) <= threshold:
        return redacted_value
    return {
        "redacted_large_body": True,
        "original_char_count": len(encoded),
        "original_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "redacted_prefix": redacted[:prefix_chars],
        "redacted_prefix_char_count": min(len(redacted), prefix_chars),
        "truncated": True,
    }


def residual_sensitive_kinds(text: str) -> list[str]:
    """Return pattern kinds that still match a non-placeholder value."""
    return sorted(
        {
            kind
            for kind, pattern in PATTERNS
            if pattern.search(text)
        }
    )
