# ATOBench: Blind Review to Evaluation Release Workflow

## Purpose

This document records the reproducible path from frozen traces and blinded
review material to canonical trajectory facts. The release workflow separates
two kinds of human judgment from deterministic analysis:

1. Predicate precision review checks whether trace-derived machine predicates
   are sufficiently precise before they are frozen.
2. Condition/model-blind outcome adjudication labels final reports without
   access to condition, model, episode identity, trace features, or pair
   membership.

Everything around those judgments is automated and hash-bound. Human reviewers
choose labels; software prepares evidence, enforces coverage, detects
disagreement, validates schemas and hashes, joins outcomes only after the trace
lock, computes paired statistics, and emits auditable facts.

## Canonical sequence

| Gate | Automated work | Human/operator boundary | Fail-closed evidence |
|---|---|---|---|
| 1. Trace contract | Census, normalization, typed graph, predicate packets, Phase 0--3 QA | None | QA_REPORT.json |
| 2. Predicate review preparation | Redaction, deterministic positive/negative/unknown sampling, review clustering, role-isolated workbench, packet hashes | None | WORKBENCH_QA_REPORT.json |
| 3. Predicate precision review | Submission parsing, reviewer-role checks, full coverage, disagreement and override validation | Primary and independent secondary reviewers export decisions | HUMAN_REVIEW_VALIDATION_REPORT.json |
| 4. Registry freeze | Verify review provenance, unresolved disagreements, integrity governance and three registry boundaries; freeze registry and trace contract | Project owner explicitly authorizes freeze | CANONICAL_REGISTRY_FREEZE_RECEIPT.json |
| 5. Outcome-blind evaluation | Run monitors, lock features, build frozen pairs and pathway signatures; no outcome access | None | PHASE_4_5_QA_REPORT.json |
| 6. Report adjudication preparation | Discover every materialized report from frozen census, redact it, split public/private mappings, generate dual-review queue | None | Outcome WORKBENCH_QA_REPORT.json |
| 7. Report adjudication | Validate two independent decision sets, reject missing/duplicate/disagreeing rows, preserve uncertain | Reviewers label only anonymous reports | OUTCOME_BLIND_REVIEW_VALIDATION_REPORT.json |
| 8. Outcome join and statistics | Join through private mapping after trace lock; retain frozen pairs; compute paired RD, CI, McNemar and Holm | Project owner explicitly authorizes outcome access | PHASE_6_QA_REPORT.json |
| 9. Result facts | Emit definition-, population-, lineage-, registry-, input- and code-hash-bound confirmatory/supporting/diagnostic facts | Project owner authorizes facts stage | PHASE_7_QA_REPORT.json |
| 10. Trajectory facts | Compute contact, exact downstream use, same-outcome composite/component discordance; preserve unavailable states | Project owner authorizes additive trajectory layer | PHASE_7_1_QA_REPORT.json |
| 11. Semantic boundary review | Generate fixed SQLi B4 evidence queue and validate reviewer CSV separately from machine packets | Reviewer decides whether audited B4 cases are behaviorally distinct | SEMANTIC_REVIEW_VALIDATION_REPORT.json |
| 12. Evaluation readiness | Verify every prior receipt and hash, report the first incomplete gate, produce a machine-readable status manifest | None | workflow_status.json |
| 13. Release inventory | Enumerate allowlisted code/artifacts, exclude private classes, hash every candidate and scan public artifacts for credential literals | None | release_inventory.json |

## What is and is not automated

Automated:

- discovery from frozen assignments rather than directory order;
- secret redaction and public/private separation;
- deterministic sampling and complete required-case coverage;
- packet, submission, source and registry hashing;
- reviewer-role separation and disagreement detection;
- preservation of missing, uncertain and unavailable states;
- pair-locked joins and paired statistical units;
- stage manifests, code/input hashes and QA;
- resume from the first incomplete gate.

Never automated:

- filling reviewer decisions;
- resolving a substantive disagreement without an adjudicator;
- asserting human provenance;
- issuing project-owner authorizations;
- changing predicates after outcome access;
- rematching pairs, imputing missing episodes or converting unavailable to zero.

## Open-source release boundary

Publish code, schemas, registry templates, public redacted packets, review
rubrics, synthetic fixtures, stage-manifest schemas and aggregate/fact outputs.
Keep raw credentials, unredacted final reports, private blind mappings, bearer
tokens, cookies and provider secrets outside the public bundle. Reviewer
submissions should be released only under the project's consent and provenance
policy; the public release can instead include their hashes, validation
receipts and aggregate agreement statistics.

## Unified commands

From the repository root:

    python3 -m atobench.experiment.trajectory_release_workflow_v1 status
    python3 -m atobench.experiment.trajectory_release_workflow_v1 plan
    python3 -m atobench.experiment.trajectory_release_workflow_v1 audit
    python3 -m atobench.experiment.trajectory_release_workflow_v1 run --dry-run

The run command is resume-safe. It skips passed gates, executes deterministic
missing stages, and stops at the first human or operator gate unless the
corresponding submission or authorization is present.

## Current release audit

The canonical cohort passes all 16 evaluation gates and is ready for release
packaging. It is not yet safe to publish the existing directory verbatim. The
inventory audit found 46 legacy condition-blind report copies with
password-like literals: 18 SQLi, 12 Basket and 16 JWT. The audit therefore
returns `FAIL_RELEASE_INVENTORY_AUDIT` and `release_bundle_ready=false`.

This finding does not invalidate the blind adjudication or canonical results.
It constrains only the public packaging projection. A later packager must
re-redact or omit those report copies, record source and released hashes, and
rerun the same audit. It must not overwrite the hash-bound canonical packets.
