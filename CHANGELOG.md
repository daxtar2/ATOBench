# Changelog

## 0.1.0 — initial open-source release

First public release of ATOBench, the evaluation framework for tracing how
autonomous penetration-testing agents verify vulnerabilities when target
evidence lies.

### runtime/ (package `atobench`)

- mitmproxy response-transformation engine: registered selectors,
  transforms, and application rules (the ATO mechanism).
- Protocol-v3 paired cross-model campaign runner: per-model configs,
  balanced C0/C1 assignment schedule, model provenance attestation, route
  attestation, deterministic source snapshots; `--dry-run` materializes the
  full plan without Docker or model calls.
- Three frozen Juice Shop AOUs: SQLi proof, Basket ownership
  (persistent-K2), JWT artifact.
- Claude Code agent carrier with stream parsing and route attestation.
- Agent-driven AOU design loop (`scaffold` → `make-deception` → `compile`
  → `freeze-suite`) with the deception-methodology corpus and
  schema-validated primitive library.
- Eval layer: action traces, paired behavior audits, pentest-effect
  metrics.

### analysis/ (package `atobench_vr`)

- Evidence reconstruction with identity-blinded judging: verification
  control, stop decision, report grounding.
- Native/ATO pair profiling, missingness-aware resilience statistics with
  worst–best bounds, release QA gate.
- Learning-data exports (records, transitions, audit packets,
  counterfactual trajectory dataset) with redaction enforced by design.

### Not included

- Frozen reference datasets and all campaign run data (regenerable from a
  frozen campaign via the export commands).
- Provider configuration of any kind.

See [README.md](README.md) for installation and the quick start.
