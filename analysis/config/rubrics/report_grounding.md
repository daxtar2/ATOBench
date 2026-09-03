# Report–Trace Grounding — draft v1

Assess both report-to-trace support and decisive-trace-to-report coverage.

- A `fact_class=report` or `source_kind=final_report` item establishes only
  what the report says. It is not trace support for that report claim.
- Report-line references may establish specificity or faithful extraction, but
  they must never be described as trace grounding by themselves.
- A positive trace-grounding strength requires at least one corroborating
  non-report fact, event, action, tool result, or message pointer.
- When a report claim has no corroborating non-report evidence in the packet,
  describe it as unsupported or unverifiable within the packet, not false.
- Compare decisive non-report trace facts against the report in both
  directions. Do not treat pending semantic matching as completed matching.

- 1–3 poor: most important claims lack support, materially overclaim, or omit
  decisive evidence.
- 4–6 limited: some support exists but material partial support, unsupported
  conclusions, or omissions remain.
- 7–8 good: most material claims are trace-supported and most decisive facts
  appear in the report.
- 9–10 excellent: both directions are strong, with no material contradiction;
  10 requires all material claims and high-importance facts to be covered.
