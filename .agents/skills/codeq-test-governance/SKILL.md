---
name: codeq-test-governance
description: Use whenever codeq development creates, changes, deletes, reviews, or is blocked by tests; when deciding whether verification evidence should become a permanent test; when triaging failures after intentional behavior changes; or when an issue explicitly audits existing test debt. Govern both missing tests and excessive, brittle, duplicated, coverage-driven, or wrong-layer tests.
---

# Govern codeq Tests

Treat every committed test as a long-lived repository constraint. Seek high regression signal with the smallest durable constraint surface, not maximum test count or coverage.

Govern which tests should exist. Follow [AGENTS.md](../../../AGENTS.md), the README, and the applicable release or validation instructions for commands and completion gates.

## Separate Verification from Permanent Tests

For every behavior change, obtain enough evidence to show that the implementation works. Do not assume every useful development check belongs in Git.

Use three mechanisms deliberately:

- **Verification evidence:** targeted tests, temporary probes, ad-hoc scripts, CLI runs, integration scenarios, or before/after reproduction used to prove the current change.
- **Permanent behavioral tests:** committed tests that protect a durable contract or a realistic regression risk across future implementations.
- **Mechanical gates:** type checking, build checks, packaging checks, benchmark/readiness gates, Git constraints, and other tools that already prove a mechanical property.

Allow strong verification with zero new permanent tests when no durable behavior needs another constraint. Do not convert temporary evidence into a test merely because it already exists.

## Start from the Approved Contract

Before authoring or changing a test:

1. Identify the behavior the issue or request intends to preserve, add, change, or remove.
2. Resolve authority from acceptance criteria, [codeq 1.0 readiness](../../../docs/codeq-1.0-readiness.md), public CLI/JSON contracts, maintained docs, and explicit runtime invariants.
3. Inspect existing tests as executable projections of that contract, not as the source of truth.
4. Identify the realistic failure mode and the narrowest stable place where it can be observed.

When an intentional product or architecture change invalidates old behavior, update or remove tests for that behavior. Do not distort codeq semantics to keep an obsolete test green.

## Admit a Permanent Test Only When It Earns Its Constraint

Before committing a new test, answer all of these:

1. **Durable behavior:** What stable externally observable behavior, public contract, compatibility invariant, lifecycle guarantee, or previously expensive regression does this test protect?
2. **Realistic regression:** What plausible future defect would make it fail?
3. **Independent signal:** Why would existing tests, typing, build checks, benchmark gates, or other constraints not already catch that defect?
4. **Stable observation:** Would the test normally remain valid if internals were substantially refactored while intended behavior stayed the same?
5. **Appropriate layer:** Is this the narrowest layer that still exercises the real risk, including real CLI, daemon, workspace, LSP, Git, and filesystem boundaries when those are the risk?

If those questions do not have concrete answers, default to not committing the test.

High-value permanent tests commonly protect:

- a reproduced CLI, resolution, worktree, daemon, lifecycle, or user-visible regression;
- the four-command surface, option semantics, plain output, JSON schema/status/evidence vocabulary, and exit codes;
- fail-closed path and qualified-symbol resolution, ambiguity handling, and copyable selection commands;
- bounded disclosure, exact totals, lower bounds, truncation metadata, and separation of semantic, lexical, possible-dynamic, base-side, and current-worktree evidence;
- Git-visible text search, diff status, merge-base behavior, untracked files, deletions, and renames;
- daemon endpoint selection, peer trust, version restart, sandbox/in-process fallback, runtime permissions, idle eviction, cleanup, and worktree isolation;
- LSP framing, request lifecycle, cache invalidation, concurrency, timeouts, or real language-server compatibility;
- package entry points, version synchronization, build artifacts, and installed-tool upgrade behavior.

## Reject Tests That Manufacture Confidence

Default-reject tests whose primary purpose is any of the following:

- Execute a line or branch only to raise coverage.
- Assert private helpers, internal state transitions, exact call counts, or mock choreography when those details are not themselves the contract.
- Test trivial models/accessors or Python/library behavior already guaranteed elsewhere.
- Feed impossible same-process values that typed internal models already exclude, unless the value crosses a real dynamic boundary.
- Duplicate the same behavior at several layers without distinct failure signal.
- Snapshot broad internal payloads or plain output that is expected to evolve.
- Enumerate Cartesian combinations without evidence that each dimension carries a distinct regression risk.
- Assemble mocks or fakes that bypass the real CLI, service, daemon, workspace, Git worktree, or LSP path where the defect could occur.
- Preserve temporary issue-specific implementation details after the issue is complete.

Treat coverage as a diagnostic, not an authoring target. If uncovered code appears to need a meaningless test, first ask whether the code is dead, unreachable, duplicated, or at the wrong abstraction boundary.

## Use Test-First as a Proof Technique

For a bug or regression, prefer showing:

```text
target regression present -> relevant evidence fails
fixed implementation      -> relevant evidence passes
```

When practical, demonstrate this with the permanent regression test against the buggy or base behavior. Regard a test never shown capable of failing for the target defect as weaker evidence.

Do not create a permanent test merely to satisfy a ritual. Use a temporary reproduction when the behavior does not merit a durable constraint.

## Prefer Outcomes and Real Boundaries

Observe CLI stdout/stderr and exit semantics, structured JSON, resolved locations, changed-file truth, workspace cleanup, emitted LSP messages, daemon behavior, runtime files, or another durable outcome.

Use mocks and interaction assertions only when the interaction is itself the contract or when a real dependency is external, nondeterministic, destructive, platform-specific, or prohibitively expensive. Examples include preventing an untrusted daemon peer, avoiding duplicate LSP initialization, or enforcing cleanup after a timeout. Otherwise prefer the resulting behavior.

When the risk is command registration, client/service composition, installed-tool upgrades, worktree discovery, packaging, or language-server wiring, exercise the cheapest real entry path that exposes it. Many isolated mock tests do not replace one test through the broken composition path.

## Govern Existing Tests in Scope

Classify a failing or nearby historical test before changing production code:

- **Real regression:** It still protects a current durable contract. Fix the implementation.
- **Intentional contract evolution:** The approved change removes or alters the behavior. Update or delete the test.
- **Implementation pinning:** The behavior is valid but the assertion is coupled to internals. Rewrite it at a stable observation point.
- **Duplicate or low-signal constraint:** Another test or gate already protects the same risk. Merge or delete the redundant test.
- **Flaky or environmental failure:** Establish that it is nondeterministic or environment-owned. Do not weaken codeq behavior or add retries/skips merely to make the current change green.

Before deleting a historical regression test, inspect its name, assertions, nearby docs, Git history, and linked issue or pull request when needed. Preserve the behavioral protection, not necessarily the old test implementation.

Do not mark a test `skip` or `xfail`, loosen assertions, or increase retries merely to silence a required gate.

## Keep Cleanup Bounded

During a normal change:

- Govern tests directly affected by the changed behavior.
- When editing a test file, remove or merge adjacent obvious duplicates only when evidence is strong and the extra diff stays small.
- Do not sweep unrelated modules because similar smells are discovered.
- Record a separate issue for broader debt when it is worth pursuing.

Use a dedicated test-governance issue for wider audits. Rank candidates by future maintenance cost and lost signal: implementation-pinning tests, duplicate scenarios, broad snapshots, mock-only integration tests, chronic flakes, and tests whose protected behavior can no longer be identified.

## Handle Selected-Check Failures

If a required or selected check fails outside the request scope:

1. Reproduce and classify the failure.
2. Determine whether the current change caused it.
3. Do not silently expand the request to repair unrelated behavior.
4. Report or file the unrelated defect according to the repository workflow.
5. Do not claim release or merge readiness while a required gate legitimately fails.

## Review Test Changes in Behavioral Terms

Explain:

- Permanent tests added and the durable risk each protects.
- Existing tests changed, merged, or deleted and why the old constraint was no longer correct or unique.
- Important verification evidence intentionally not committed as a permanent test.

Review in both directions:

- **Missing protection:** Identify durable or high-cost regression risks with no adequate test.
- **Excess protection:** Identify tests that duplicate gates, pin implementation, bypass the real risk, or constrain behavior the approved change no longer requires.

Reject material problems in either direction. Passing tests alone do not establish correctness, and the existing suite does not overrule the current approved contract.
