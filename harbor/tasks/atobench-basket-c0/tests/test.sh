#!/usr/bin/env bash
# ATOBench SQLi verifier entrypoint. Deterministic; no network access needed.
set -u
mkdir -p /logs/verifier
python3 /tests/verify.py
# verify.py always writes the reward files; keep exit 0 so Harbor parses them.
exit 0
