# Contributing to ATOBench

Thanks for your interest in improving ATOBench. This document covers the
practical rules a contribution must satisfy; the design context is in
[docs/CONCEPTS.md](docs/CONCEPTS.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Setup

```bash
python3 -m pip install -e ./runtime
python3 -m pip install -e './analysis[test]'
```

## Before you open a PR

Run all three gates locally — a PR is only reviewable when they pass:

```bash
python -m pytest runtime/tests -q      # release validation tests
python -m pytest analysis/tests -q     # analysis layer tests
python3 runtime/scripts/release_check.py .
```

`release_check.py` is the whole-tree audit for secrets and private paths
(provider endpoints, internal hostnames, absolute user paths, run-data
artifacts). It must pass with zero findings; the same audit runs on the
committed tree.

## The fail-closed pinning policy

This is the most common way a well-meant change breaks CI, so read it once:
frozen suites, protocol execution specs, and analysis platform configs pin
SHA-256 hashes of the files they depend on. If you edit any pinned file,
recompute the corresponding pins in the same change — see "Hash pinning" in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the three layers and their
scopes. Validation commands fail closed on mismatch by design; do not relax
the check, re-pin instead.

Behavioral rules that go with it:

- Keep fail-closed semantics. Missing inputs, attestation gaps, and hash
  mismatches must stop the pipeline, never fall back silently.
- Deterministic outputs stay deterministic: no wall-clock or randomness
  introduced into artifacts that downstream stages hash or diff.
- Identity blinding is an invariant: judge and semantic-matcher packets must
  never expose model names, conditions, or pair identities.

## What never gets committed

- Campaign run data: turns, mitmproxy dumps, agent workspaces, episode
  outputs, logs (`.gitignore` excludes them; keep it that way).
- Any provider configuration: API keys, tokens, gateway URLs. The agent
  carrier and judges read routing and credentials from your local Claude
  Code configuration only.
- Frozen reference datasets (the analysis layer's reference cohort).

If `release_check.py` flags a file, do not commit it even if the filename
looks harmless — check its contents first.

## Documentation conventions

- The top-level `README.md` and `README.zh-CN.md` mirror each other; change
  both together. Commands and paths stay in English in both.
- Every CLI flag shown in documentation must exist — verify against the
  command's `--help` output rather than another doc page.
- New user-facing docs go in `docs/` and get linked from `README.md`
  and/or `docs/CONCEPTS.md`.

## Reporting problems

Open an issue with the command you ran (redact any private values), the
expected vs. actual behavior, and, for campaign failures, the relevant
`campaign_manifest.json` and artifact-audit output — never raw episode logs.
