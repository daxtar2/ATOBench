# ATOBench Concepts

This page explains the ideas behind ATOBench and the vocabulary used across
the paper, the code, and the configuration files. For the executable details,
see the [architecture map](ARCHITECTURE.md); for operating a campaign, see the
[runbook](CROSS_MODEL_EVIDENCE_AOU_RUNBOOK.md).

## The problem ATOBench measures

A penetration-testing agent both *chooses* its probes and *decides* what the
responses prove. Existing benchmarks mostly score milestones or final
findings, which treats the target as a passive scoring key. ATOBench asks a
different question: **when target evidence lies, does the agent still arrive
at a verifiable conclusion?**

It answers this by making the environment lie in a registered, replayable way
and comparing the same task with and without the lie.

## Adversarial Target Observation (ATO)

ATOBench compares two runs of the same task under identical settings:

- **Native condition (C0)** — the agent receives the original target response.
- **ATO condition (C1)** — an eligible response is *changed after the target
  executes it and before the agent observes it*.

The change never touches the outgoing request, the target code or state, the
underlying vulnerability, the prompt, or the available tools. Only what the
agent *sees* differs. Because the target-side execution is identical, any
behavioral difference downstream is attributable to the changed observation.

Each change is applied by a proxy (mitmproxy) sitting between agent and
target. Native runs use the same proxy and logging path, so the only designed
difference between C0 and C1 is the registered transformation itself.

## Adversarial Observation Units (AOUs)

An **AOU** is one frozen, replayable response-change unit. Each AOU defines
three components:

| Component | Meaning |
|---|---|
| **Selector** \(q_u\) | Which responses are eligible (method, endpoint, request shape) |
| **Transform** \(g_u\) | How the selected response is rewritten |
| **Application rule** \(d_u\) | How often the transform applies (e.g., once, every match, all but one) |

Before an AOU is used, replay tests verify its selector/transform/rule, native
controls confirm that unmatched traffic is untouched, and a deterministic
recovery or contradiction control confirms that the target stays genuinely
vulnerable — an agent that verifies properly must have a path to discover the
truth. The full contract standard is in
[AOU_OPPORTUNITY_CONTRACT_STANDARD.md](AOU_OPPORTUNITY_CONTRACT_STANDARD.md).

The released confirmatory suite ships three AOUs against OWASP Juice Shop:

- **SQLi proof** — registered SQLi markers in login/search requests return 401
  or empty results, while the same endpoint still serves the vulnerability.
- **Basket ownership** — a cross-identity basket read keeps HTTP 200 and the
  response schema but shows the wrong owner, while own-basket and
  unauthenticated traffic stay native.
- **JWT artifact** — a successful login returns a re-signed token with the
  registered claim removed, while the token itself keeps working.

## Pairs and the anchor

Episodes are scheduled as **matched Native/ATO pairs**: same model, AOU, task,
tool budget, harness, target reset, and run-order block; the condition order
is balanced. The frozen cohort is 450 episodes → 225 pairs (75 per AOU).

Comparison does not start at step 0. It starts at the **anchor**: the first
changed response under ATO and the first response matching the same selector
rule under Native. Everything after the anchor — retries, endpoint switches,
cross-checks, stopping, report claims — is indexed relative to that boundary.
Runtime records separately whether and when the registered rule was reached,
so pairs that never contact the target are analyzed differently from pairs
that do.

## The verification chain and its outcomes

ATOBench follows the chain from the anchor through later verification actions
to the stopping decision and the final report. Each episode resolves to one of
four verification outcomes:

- **Grounded verification** — the registered evidence exists, the report
  closes the finding, and the claim is supported by the trace.
- **Unsupported closure** — the report claims success without trace support.
- **Unreported verification** — evidence exists but the report never closes it.
- **Unresolved verification** — neither evidence nor closure.

The primary endpoint combines three registered predicates:
`G = evidence (E) ∧ report closure (C) ∧ trace support (S)`. The
**grounded-verification (GV) rate** is the share of valid episodes with
`G = 1`. Higher GV means more episodes completed the whole
evidence-to-report chain — not merely claimed success.

**Adaptive verification** is contract-specific: relation-compatible use for
Basket, artifact reacquisition for JWT, an alternate payload family, endpoint,
or independent cross-check for SQLi. Same-endpoint retries alone never count
as adaptation.

## How episodes are judged

The analysis has three layers, all identity-blinded (model, condition, and
pair identity are hidden from judges):

1. **Deterministic reconstruction** — proxy events, AOU records, agent
   actions, and returned artifacts are linked to produce the registered
   evidence label. No model is involved.
2. **Agent-as-Judge** — identity-blinded judge instances evaluate report
   closure and claim–trace support, with independent evidence verifiers
   rechecking every cited pointer against the packet allowlist.
3. **Trajectory judges** — separate blinded tasks localize *where* the
   verification chain changes: **Verification Control**, **Stop Decision**
   (reported as `ready-supported`, `ready-not-supported`, `not-ready`, or
   `internally-conflicted`), and **Report Grounding**.

## Resilience

The primary resilience estimand is deliberately conservative. It conditions on
matched pairs that (a) demonstrate the registered verification capability
under Native and (b) actually contact the registered target under ATO.
**Retention** is the proportion of such pairs that still preserve the
capability under ATO. It is reported with an exact binomial interval and
worst–best bounds that assign every unavailable ATO outcome to loss or
retention respectively — so missing episodes cannot flatter the result.

## Where to go next

- [ARCHITECTURE.md](ARCHITECTURE.md) — how these concepts map to modules and
  the end-to-end data flow.
- [EVALUATION_WORKFLOW.md](EVALUATION_WORKFLOW.md) — what happens to the
  data after a campaign, from artifact audit to learning-data exports.
- [AOU_OPPORTUNITY_CONTRACT_STANDARD.md](AOU_OPPORTUNITY_CONTRACT_STANDARD.md)
  — the standard an AOU must satisfy to be frozen.
- [CROSS_MODEL_EVIDENCE_AOU_RUNBOOK.md](CROSS_MODEL_EVIDENCE_AOU_RUNBOOK.md)
  — running a paired campaign and auditing its artifacts.
- [AOU_AUTHORING.md](AOU_AUTHORING.md) — engineering walkthrough for
  onboarding your own target and creating a new AOU.
