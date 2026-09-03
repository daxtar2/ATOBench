#!/usr/bin/env python3
"""Lightweight guardrail for accidental secrets/private-machine paths."""

from __future__ import annotations

import re
import sys
from pathlib import Path


PATTERNS = {
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    "Anthropic token": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b"),
    "Bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}", re.IGNORECASE),
    "private macOS path": re.compile(r"/Users/[a-z0-9._-]+/"),
    "Claude home config path": re.compile(r"(?:~|/Users/[a-z0-9._-]+)/\.claude/"),
    "root home path": re.compile(r"(?:^|[^A-Za-z0-9])/root/"),
    "private MaaS domain": re.compile(r"aliyuncs|maas\.|llm-bieh477ezo46n8mz|cn-beijing"),
    "internal cloud vendor reference": re.compile(
        r"(?i:alibaba(?!\s+cloud\s+model\s+studio))|(?i:aliyun)|alibaba-inc"
    ),
    "internal infrastructure address": re.compile(r"121\.199\.29\.76"),
    "developer username": re.compile(r"doppel"),
    "developer workspace path": re.compile(r"Gen/"),
    "conference venue string": re.compile(r"aaai", re.IGNORECASE),
}
SKIP_PARTS = {".git", ".venv", "build", "dist", "__pycache__", "node_modules", ".pytest_cache"}
FORBIDDEN_NAMES = {".DS_Store"}
FORBIDDEN_PARTS = {".mitmproxy", "__pycache__"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    checker_path = Path(__file__).resolve()
    findings: list[str] = []
    for path in root.rglob("*"):
        if (
            path.resolve() == checker_path
            or not path.is_file()
            or any(part in SKIP_PARTS for part in path.parts)
        ):
            continue
        if path.name in FORBIDDEN_NAMES or path.suffix == ".pyc":
            findings.append(f"forbidden generated artifact: {path.relative_to(root)}")
            continue
        if any(part in FORBIDDEN_PARTS for part in path.parts):
            findings.append(f"forbidden path component: {path.relative_to(root)}")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            if path.suffix.lower() in IMAGE_SUFFIXES:
                continue
            findings.append(f"binary file requires manual review: {path.relative_to(root)}")
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append(f"{name}: {path.relative_to(root)}")
    if findings:
        print("release check failed:")
        print("\n".join(f"- {finding}" for finding in findings))
        return 1
    print(f"release check passed: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
