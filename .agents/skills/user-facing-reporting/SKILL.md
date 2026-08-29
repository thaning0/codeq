---
name: user-facing-reporting
description: Use when reporting work to the user, including issues, plans, designs, pull requests, implementation results, research findings, failures, and blockers; especially when the user asks for plain language, what changed, or why it matters. Make the explanation user-centered and easy to follow without dropping required precision or verification.
---

# Write User-Facing Reports

Lead with the answer the user needs, not the code structure, diff, tool history, or internal terminology.

Use the relevant parts of this order:

1. State the conclusion.
2. Explain the previous practical problem.
3. Explain what changed and what the user can now do differently.
4. State remaining limits, risks, or decisions.
5. Put technical evidence, such as paths, revisions, commands, and checks, after the explanation.

Use this compact form when useful: `以前……；现在……；所以你可以……；还没解决的是……。`

- Match the user's language.
- Preserve exact identifiers and verification details when they matter, but do not make the user reconstruct the outcome from them.
- Use a small diagram or table only when relationships are genuinely hard to explain in prose.
- If the user says they do not understand, change the explanation model or example; do not merely rephrase the same structure.
- Treat plain language as clearer organization, not permission to omit conflicts, caveats, or uncertainty.
