---
name: codeq-test-governance
description: Use whenever codeq development creates, changes, deletes, reviews, or is blocked by tests; when deciding whether verification evidence should become a permanent test; when triaging failures after intentional behavior changes; or when an issue explicitly audits existing test debt. Governs both missing tests and excessive, brittle, duplicated, coverage-driven, or wrong-layer tests.
---

# codeq Test Governance

Treat every committed test as a long-lived repository constraint. Seek high regression signal with the smallest durable constraint surface. This Skill decides **which tests should exist**; follow [AGENTS.md](../../../AGENTS.md) and the applicable readiness or release instructions for verification commands and completion gates.

## Choose the Evidence Mechanism

For every behavior change, obtain enough evidence to show that it works, then choose deliberately:

- **Verification evidence:** targeted tests, temporary probes, CLI runs, integration scenarios, or before/after reproductions for the current work.
- **Permanent behavioral test:** committed protection for a durable contract or realistic regression.
- **Mechanical gate:** types, builds, packaging, version checks, benchmark/readiness gates, or another mechanism that already proves the property.

Strong verification may justify zero permanent tests. Do not commit a temporary check merely because it already exists.

## Decide from the Contract

1. Identify the behavior the issue or request preserves, adds, changes, or removes.
2. Resolve authority from acceptance criteria, [codeq 1.0 readiness](../../../docs/codeq-1.0-readiness.md), public CLI/JSON contracts, maintained documentation, and explicit runtime invariants.
3. Treat existing tests as executable projections, not the source of truth.
4. Identify the realistic failure and the narrowest stable place that exposes it.

Admit a permanent test only when all five answers are concrete:

1. **Durable behavior:** What public behavior, compatibility or lifecycle guarantee, or expensive regression does it protect?
2. **Realistic regression:** What plausible future defect would make it fail?
3. **Independent signal:** Why would existing tests or mechanical gates miss that defect?
4. **Stable observation:** Would the test survive an internal refactor that preserves behavior?
5. **Appropriate layer:** Is this the narrowest layer that still crosses the real risk boundary?

If an answer is missing, use verification evidence or an existing gate instead. Stop adding tests once each distinct durable risk has one adequate independent protection.

## Prefer Outcomes and Real Boundaries

Observe CLI output and exit semantics, structured JSON, resolved locations, changed-file truth, workspace cleanup, emitted LSP messages, daemon behavior, runtime files, or another durable outcome. High-value tests protect the four-command surface; option and output semantics; schema, status, evidence, and exit-code vocabulary; fail-closed resolution; bounded disclosure; Git/worktree truth; daemon trust, upgrade, fallback, and isolation; LSP lifecycle and compatibility; and package, version, build, or installed-tool behavior.

When the risk is command registration, client/service composition, installed-tool upgrades, worktree discovery, packaging, or language-server wiring, exercise the cheapest real entry path that can expose it. Several isolated mock tests do not replace one boundary test.

Use mocks or interaction assertions only when the interaction is itself the contract or the real dependency is external, nondeterministic, destructive, platform-specific, or prohibitively expensive.

## Reject Low-Signal Constraints

Default to no permanent test when it would only:

- execute a line for coverage;
- assert private helpers, internal state, exact call counts, or mock choreography that is not contractual;
- retest trivial models, accessors, Python behavior, or an invariant already enforced mechanically;
- inject impossible same-process values that never cross a dynamic boundary;
- duplicate the same behavior at another layer without distinct failure signal;
- snapshot broad internals or enumerate combinations without distinct regression risks;
- bypass the real CLI, service, daemon, workspace, Git worktree, or LSP path with hand-built fakes;
- preserve temporary implementation details after the issue ends.

Coverage is diagnostic. If uncovered code seems to need a meaningless test, first ask whether the code is dead, unreachable, duplicated, or at the wrong boundary.

## Use Test-First as Proof

For a reproduced bug, prefer:

```text
buggy behavior -> relevant evidence fails
fixed behavior -> relevant evidence passes
```

Show that a permanent regression test can fail for the target defect when practical. Use a temporary reproduction when the behavior does not deserve a durable test; test-first is a proof technique, not a quota.

## Govern Existing Tests

Classify a failing or nearby historical test before changing production code:

- **Real regression:** it protects a current contract; fix the implementation.
- **Intentional evolution:** the approved behavior changed; update or delete the test.
- **Implementation pinning:** retain the behavior but rewrite the assertion at a stable outcome.
- **Duplicate/low signal:** merge or delete it when another independent protection exists.
- **Flaky/environmental:** establish nondeterminism or external ownership; do not weaken behavior or add retries/skips to silence it.

Before deleting a historical regression test, inspect its assertions, nearby authority, and history when needed. Preserve unique behavioral protection, not necessarily its implementation. Never use `skip`, `xfail`, weaker assertions, or extra retries merely to pass a required gate.

Keep cleanup within the affected behavior. Remove adjacent obvious duplication only when evidence is strong and the diff stays small; use a separate issue for broader debt. A dedicated debt audit may rank implementation-pinning tests, duplicate scenarios, broad snapshots, mock-only integration tests, chronic flakes, and tests with no identifiable contract.

## Handle Check Failures and Review

When a selected check fails outside scope, reproduce and classify it, determine whether the current change caused it, and do not silently expand the request. Report the unrelated defect through the repository workflow, and do not claim readiness while a required gate legitimately fails.

Report test work in behavioral terms:

- permanent tests added and the durable risk each protects;
- tests changed, merged, or deleted and why the old constraint was not current or unique;
- important verification evidence intentionally not committed.

Review both directions: missing protection for a durable/high-cost risk and excess protection that duplicates gates, pins internals, bypasses the real boundary, or preserves obsolete behavior. Passing tests alone do not establish correctness, and historical tests do not overrule the approved contract.
