---
name: user-facing-reporting
description: Use when delivering a user-facing report or handoff about completed work, reviews, plans or designs, research findings, failures, or blockers; especially when the user asks what changed, why it matters, or for plain language. Do not use for routine one-line answers, raw output, or brief in-progress updates.
---

# User-Facing Reporting

Lead with the answer the user needs, not the code structure, diff, tool history, or internal terminology.

## Lock the Scope

Identify the exact object and question the user asked about before composing the report. Do not broaden a Skill, code, test, or paper request into adjacent product or architecture advice unless that implication directly affects the requested outcome; label optional implications as such. If the user corrects the scope, discard the earlier framing and answer from the corrected goal.

## Choose the Report Shape

- **Implementation result:** outcome → previous practical problem → what changed → what the user can now do → remaining limits → verification.
- **Research, review, or design:** conclusion → strongest evidence → practical implication → uncertainty, trade-off, or open decision.
- **Failure or blocker:** completed progress → exact failure/blocker → user impact → evidence → decision or input required.

Use only the fields that help this report. Do not force implementation language such as “以前/现在” onto research findings or design analysis.

## Write for the User

- Match the user's language and level of detail.
- Preserve exact identifiers and verification details when they matter, but do not make the user reconstruct the outcome from them.
- Put paths, revisions, commands, checks, and other technical evidence after the practical explanation unless they are the answer.
- Use a small mermaid diagram or table only when relationships are genuinely hard to explain in prose.
- If the user says they do not understand, change the explanation model or example; do not merely rephrase the same structure.
- Treat plain language as clearer organization, not permission to omit conflicts, caveats, or uncertainty.
- Stop when the requested conclusion, impact, limits, and evidence are clear; avoid adjacent commentary that does not change the user's decision.
