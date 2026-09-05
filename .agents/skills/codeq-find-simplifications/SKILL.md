---
name: codeq-find-simplifications
description: Use when working in codeq to find, validate, rank, or implement non-obvious simplifications; review a change for removable complexity; audit dead, duplicated, speculative, over-built, added-then-removed, compatibility-only, or hand-rolled code; or consolidate worthwhile cleanup ideas from another branch or pull request.
---

# Finding codeq Simplifications

Find a small number of evidence-backed changes that remove or collapse real surface area. Prefer one proven simplification over several plausible guesses.

## Core Procedure

1. **Fix the scope.** Read [AGENTS.md](../../../AGENTS.md), [README.md](../../../README.md), and the maintained document that owns the selected behavior. Use [2.x development and validation](../../../docs/codeq-2.0-rust.md) for the current validation boundary. Treat historical plans, migration parity, validation reports, benchmarks, and superseded release-candidate notes as discovery evidence, not current authority.
2. **Locate the real surface.** Use the repository version of `codeq` before broad manual exploration: `find` when the location is unknown, `context` for one symbol or file, `trace` for call chains, and `review` for a change. Use `rg`, Git, and direct inspection for strings and dynamic, configuration-driven, packaging, daemon, cross-process, or cross-language references that semantic analysis can miss.
3. **Prove each candidate.** Resolve its definitions, callers, CLI and wire spellings, configuration names, runtime or persisted forms, package loading, tests, documentation, and current rationale. Read every relevant call site; a missing static result is not proof of no consumer.
4. **Describe the smaller end state.** State what disappears, what behavior or flexibility is given up, which owner remains, and whether the change is cleanup or a product/compatibility decision.
5. **Calculate net simplification.** Count deleted implementation, branches, tests, fixtures, configuration, and duplicate documentation against replacement glue, migrations, dependencies, and unrelated churn.
6. **Name disproof and validation.** State the fact that would reject the candidate and the smallest check that would validate an approved change.

Stop searching when the consumers, authority, end state, trade-off, and disproof condition are established. Do not repeat equivalent searches or collect evidence that cannot change the decision.

Keep an audit read-only unless implementation was requested.

## Preserve Intentional Boundaries

Treat codeq's four-command CLI and small daemon-backed service as intentional. Keep parsing, rendering, and transport in the CLI; request orchestration and `Workspace` lifetime in the service; semantic repository behavior in `Workspace`; LSP protocol behavior in `LspProcess`; and Git, exact-text, and platform facts in focused helpers. Do not label fail-closed resolution, bounded disclosure, evidence classification, worktree isolation, private runtime state, transport fallback, or daemon compatibility as needless complexity unless the user explicitly reopens that contract and the evidence covers affected consumers.

Treat generated artifacts, distributions, persisted runtime state, protocol/version compatibility, benchmark fixtures, loader-discovered resources, and packaged entry points as ambiguous until their build, loading, upgrade, and support paths are proven. Tests and docs are not production consumers, but may protect a frozen contract or an expensive historical failure.

## Prefer Strong Candidates

Prioritize:

- surfaces with no production or operational consumer and no maintained contract;
- duplicate representations of the same fact that can drift;
- repeated policy that already has one clear owner;
- single-implementation or pass-through abstractions with no supported extension case;
- compatibility, release-candidate, or fallback paths whose documented removal condition is satisfied;
- defensive machinery that duplicates a guarantee enforced at the same trust boundary;
- hand-rolled infrastructure replaceable with the standard library or an existing dependency with meaningful net deletion;
- permanent documentation that duplicates its authoritative contract.

Do not elevate style changes, isolated renames, raw dead-code output, or a vague sense of complexity.

## Classify Consumers and Risks

Classify every relevant consumer:

- **Production/runtime:** CLI execution, service dispatch, daemon lifecycle, workspace analysis, language-server I/O, Git/text helpers, and installed `codeq` behavior.
- **Operational/compatibility:** builds, releases, package entry points, protocol and schema versions, runtime directories, daemon upgrades, supported platforms, and performance gates.
- **Non-production:** tests, docs, examples, snapshots, fixtures, comments, and historical benchmark records.
- **Ambiguous:** generated, packaged, loader-discovered, persisted, platform-specific, or externally invoked surfaces; resolve them before deciding.

For parsers, validators, retries, locks, caches, fallbacks, and process transitions, identify the source, trust boundary, owner, mutability, process boundary, platform assumptions, and next consumer. Preserve a mechanism when it uniquely protects external input, daemon trust, LSP or Git responses, stale-process recovery, sandbox fallback, worktree isolation, cache invalidation, compatibility, or resource cleanup.

For a dependency substitution, verify the supported surface, maintenance, license, Python/platform support, transitive footprint, and residual glue. Prefer the standard library and existing dependencies. Treat a runtime dependency as a major trade-off because codeq currently has no Python runtime dependencies.

Reject or downgrade a candidate when it has a current production or operational consumer, protects stable CLI/JSON/exit semantics, worktree correctness, daemon safety, bounded disclosure, compatibility, or performance, weakens an intentional boundary, merely moves complexity, or requires an unapproved product decision.

## Report the Result

Return at most five ranked strong candidates, and fewer when the evidence bar is not met. For each include:

- action-oriented title and confidence;
- current surface and consumer evidence;
- proposed removal, merge, demotion, or replacement;
- net simplification and behavior given up;
- risks, affected flows, validation, and rejection condition.

List representative rejected or downgraded ideas so the survey boundary is visible. Distinguish “no consumer found” from “no consumer exists” and name the searches performed. Keep proposal-only findings in the response, issue, pull request, or a user-requested report; do not create or mutate project artifacts without authorization.

## Implement an Approved Simplification

Reconfirm the relevant semantic, textual, packaged, persisted, platform-specific, and operational consumers, then make the smallest root-cause deletion or collapse. Apply `codeq-test-governance` to retained behaviors instead of adding tests mechanically for removed code or duplicate guarantees. Update only the authoritative documentation whose supported contract changed. Run the repository version during development; refresh the stable global installation only for release-ready, consistently versioned changes as directed by [AGENTS.md](../../../AGENTS.md). Report actual deletions, remaining glue, behavior changes, affected consumers, verification, and rollback considerations.

When folding findings from another branch or pull request, compare with its approved base or merge base, port only independent findings that still meet this evidence bar, and do not rewrite or close the source without authorization.
