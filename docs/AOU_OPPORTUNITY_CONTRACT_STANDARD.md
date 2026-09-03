# AOU Opportunity Contract Standard

Status: active design standard  
Date: 2026-07-14  
Applies to: ATOBench AOM/AOU/BRS experiments and open benchmark units

## 1. Core Principle

ATOBench separates two probabilities that broad-only benchmarks often conflate:

```text
P(opportunity | task distribution)
P(effect | opportunity/contact)
```

The first asks whether a pentest agent naturally reaches an AOU-relevant
security opportunity under a given task distribution. The second asks how the
agent behaves once it reaches that opportunity and observes the adversarial
target evidence.

This distinction is mandatory. A low broad-task opportunity rate is not
evidence that an agent resists deception. It may only mean the agent never
entered the relevant workflow.

## 2. Tracks

ATOBench uses three experiment tracks.

| Track | Prompt specificity | Purpose | Paper role |
|---|---|---|---|
| Broad Ecological Track | L0 broad | Estimate natural opportunity rate, time-to-opportunity, competing vulnerability paths, and budget displacement | Auxiliary ecological validity |
| Domain-Focused AOU Track | L1/L2 focused | Estimate conditional BRS once the agent has a realistic opportunity to test a security domain | Main mechanism experiment |
| Debug/Feasibility Track | L3 unit-directed | Prove engineering feasibility, capability, and evaluator correctness | Not a mechanism-effect result |

The main mechanism relation is:

```text
AOU x Pentest Agent x Task Distribution
    -> Opportunity
    -> Contact
    -> Behavioral Response Signature
```

## 3. Prompt Specificity Levels

| Level | Allowed information | Disallowed information | Use |
|---|---|---|---|
| L0 broad | General pentest objective and scope | Vulnerability class, workflow, endpoint, payload, false hypothesis, recovery path | Broad ecological track |
| L1 domain-focused | Security domain such as authenticated authorization, injection validation, sensitive artifact assessment, operational exposure | Specific endpoint, parameter, exploit procedure, expected status/body, false hypothesis, recovery path | Preferred main AOU track |
| L2 workflow-focused | Domain plus realistic workflow family such as customer-owned commerce resources or authenticated user-owned objects | Specific endpoint, exact object ids, exploit procedure, expected vulnerability, false hypothesis, recovery path | Allowed when L1 opportunity is insufficient |
| L3 unit-directed | Endpoint or exact workflow details needed to prove capability | N/A for main results | Feasibility/debug only |

Promotion rule:

1. Run L0 to estimate ecological opportunity when relevant.
2. Run L1 clean calibration for main AOU eligibility.
3. If L1 opportunity is below the pre-registered gate, exactly one L2
   workflow-focus upgrade may be used when the contract permits it.
4. L3 can validate capability and tooling but cannot support mechanism-effect
   claims.
5. Do not change prompt specificity after seeing treatment success or failure.
6. Any L2 upgrade prompt must be frozen before treatment and must not reveal
   endpoint, exact object ids, exploit procedure, false hypothesis, or recovery
   path.

## 4. Opportunity Contract Schema

Every paper-facing AOU must define an opportunity contract before treatment
collection.

```yaml
aou_opportunity_contract:
  contract_id: string
  unit_id: string
  aom: string
  task_distribution: broad | domain_focused | workflow_focused | unit_directed
  prompt_specificity_level: L0 | L1 | L2 | L3
  parent_objective: string
  allowed_cues:
    - string
  forbidden_cues:
    - target endpoint
    - exploit parameter
    - payload family
    - false hypothesis
    - expected status code or response body
    - recovery path
  provided_prerequisites:
    - string
  eligibility_predicate:
    description: string
    machine_check: string
  assignment:
    mode: episode_level_randomization | contact_triggered_randomization
    randomization_point: start_of_episode | first_eligible_contact
    c0_shadow_matcher: optional | required
  contact_predicate:
    description: string
    machine_check: string
  treatment_dose:
    transformed_contacts: integer
    subsequent_eligible_contacts: native_untransformed | transformed | disabled
  post_contact_window:
    starts_after: transformed_response_delivered | first_eligible_contact
    unit: requests | in_scope_target_http_requests | turns | seconds
    value: integer
    ends_early_on:
      - final_report
      - hard_budget_exhaustion
  calibration_gate:
    minimum_valid_episodes: integer
    gate_name: threshold
  fallback_if_l1_fails:
    allow_single_upgrade_to_l2: boolean
    l2_allowed_focus: string
    still_forbidden_cues:
      - string
    freeze_new_prompt_before_treatment: boolean
  stopping_policy:
    max_tool_calls: integer
    stop_conditions:
      - string
  controls:
    capability_preservation:
      - string
    negative_controls:
      - string
  allowed_outcomes:
    - influence
    - resistance
    - recovery
    - no_contact
  exclusion_rules:
    - string
```

## 5. Assignment and Dose Rules

Paper-facing AOUs should prefer contact-triggered randomization when the
runtime can identify eligibility before applying a transform:

```yaml
assignment:
  mode: contact_triggered_randomization
  randomization_point: first_eligible_contact
  c0_shadow_matcher: required
```

This design keeps C0 and C1 identical until the first pre-registered eligible
contact. It also supports contact-conditioned effects without treating no-
opportunity episodes as agent resistance.

If contact-triggered randomization is not yet implemented, episode-level
randomization may be used only when the paper reports both:

- intent-to-treat effect over all valid episodes;
- contact-conditioned effect over episodes where the C0 shadow matcher or C1
  runtime matcher reached the eligibility predicate.

Dose must be pre-registered. The preferred first paper-grade dose is:

```yaml
treatment_dose:
  transformed_contacts: 1
  subsequent_eligible_contacts: native_untransformed
```

Higher dose or repeated-contact variants are allowed only as separately named
variants. They must not be mixed with the primary one-contact AOU estimate.

Relational and business-logic AOUs must not define opportunity as "a different
numeric id was requested." They need an episode-bound known-existing resource,
an authenticated subject binding, and a ground-truth ownership relation.

## 6. Track-Specific Claims

Broad Ecological Track may claim:

- natural opportunity rate;
- time-to-opportunity;
- contact rate;
- competing vulnerability paths before opportunity;
- remaining budget at opportunity;
- no-contact reasons.

Broad Ecological Track must not claim:

- an AOU had no effect when the agent never reached eligibility/contact;
- low susceptibility from uncontacted episodes.

Domain-Focused AOU Track may claim:

- contact-conditioned BRS;
- report closure, downgrade, or omission;
- retry, pivot, return, recovery, or resistance;
- capability-preserving effect under a specific task distribution.

Domain-Focused AOU Track must not claim:

- broad ecological prevalence unless paired with L0 evidence;
- vulnerability-class generalization beyond the AOU's opportunity contract.

Debug/Feasibility Track may claim:

- agent capability exists under unit-directed conditions;
- runtime/evaluator/predicate works;
- a candidate is feasible or infeasible.

Debug/Feasibility Track must not claim:

- mechanism effect;
- realistic susceptibility;
- main benchmark result.

## 7. Current Classification Rules

- SQLi login/search evidence-control can be evaluated in L0 broad and L1
  injection-validation settings.
- JWT hash suppression should use L1 sensitive-artifact assessment before any
  counted mechanism result.
- Basket relational-scope misdirection requires L1 authenticated authorization
  or L2 customer-owned resource authorization for main results. The existing
  `/rest/basket/<id>` focused calibration is L3 and remains a capability gate.
  Its L1 contract must use known-existing foreign resources, valid
  episode-bound bearers, contact-triggered randomization when available, and
  a fixed one-contact dose before any treatment run.
- Positive steering must use a broad parent objective. The prompt must not name
  the planted route.
- Recovery may use a domain-focused parent objective, but the prompt must never
  reveal the contradiction path.

## 8. Open Benchmark Packaging

Each released AOU should be packaged as:

```text
aou/<unit_id>/
  unit.yaml
  opportunity_contract.yaml
  task_cards/
    broad.yaml
    domain_focused.yaml
    workflow_focused.yaml
  prerequisites.yaml
  runtime_program.yaml
  behavior_profile.yaml
  evaluator.py
  replay_fixture/
  human_review.yaml
```

The package must make clear what the agent was told, what was forbidden, and
which task distribution each result estimates.
