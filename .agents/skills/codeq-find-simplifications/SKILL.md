---
name: codeq-find-simplifications
description: Use when working in codeq to find, validate, rank, or implement non-obvious simplifications; review a change for removable complexity; audit dead, duplicated, speculative, over-built, compatibility-only, or hand-rolled code; or consolidate worthwhile cleanup ideas from another branch or pull request.
---

# Find codeq Simplifications

Turn broad requests to simplify codeq into a small set of evidence-backed changes that remove or collapse real surface area. Prefer a few well-proven candidates over a long list of guesses.

## Establish Repository Context

1. Read [AGENTS.md](../../../AGENTS.md), [README.md](../../../README.md), and the maintained document that owns the selected behavior. Start with [codeq 1.0 readiness](../../../docs/codeq-1.0-readiness.md) for the frozen product and compatibility boundary. Treat plans, validation history, benchmark reports, and superseded release-candidate notes as historical evidence unless a maintained document names them as current authority.
2. Use the repository version of `codeq` before broad manual exploration. From the repository root, run `uv run codeq --help`, then choose `find`, `context`, `trace`, or `review`. Use `rg`, Git, and direct source inspection for dynamic, configuration-driven, packaging, daemon, cross-process, and cross-language relationships that semantic analysis cannot establish. Do not infer that no consumer exists from a missing static result.
3. Keep an audit read-only when the user asks only for findings. When implementation is requested, follow the repository's validation and stable-installation rules.

Preserve codeq's intentional boundary: a four-command CLI coordinates a small daemon-backed service; `Workspace` owns repository and language-server analysis; LSP, Git, and exact-text helpers retain distinct evidence semantics. Do not call fail-closed resolution, bounded disclosure, evidence classification, worktree isolation, private runtime state, transport fallback, or daemon compatibility needless complexity unless the user explicitly asks to reconsider the contract and the evidence covers every affected consumer.

Treat generated artifacts, distributions, persisted runtime state, protocol/version compatibility, benchmark fixtures, and packaged entry points as ambiguous until their build, loading, upgrade, or support paths are proven. Tests and documentation are not production consumers, but they may encode a frozen public contract or a previously expensive failure mode.

## Recognize Strong Candidates

Prefer candidates where the current design costs more than it buys:

- A public function, model field, option, configuration key, helper, wrapper, compatibility alias, or extension seam has no production or operational consumer.
- Tests, examples, or docs are the only consumers and no maintained contract requires the behavior.
- Two models, caches, lifecycle flags, result fields, or orchestration paths represent the same fact and can drift.
- CLI rendering, service dispatch, workspace analysis, or helper modules repeat policy that already has a clear owner.
- An abstraction has one implementation, speculative extension points, pass-through layers, or fallback machinery without a supported environment or failure mode.
- A compatibility path, release-candidate workaround, or temporary fallback has met a documented removal condition.
- Defensive validation, retries, locks, sentinels, or lifecycle states duplicate guarantees already enforced by the true trust or ownership boundary.
- Hand-rolled infrastructure can be replaced by the standard library or an already-approved tool with meaningful net deletion.
- Permanent documentation duplicates a contract or procedure whose authoritative owner is already defined.
- The simpler behavior differs slightly but remains an acceptable, explicit product contract.

Do not elevate typo fixes, style-only rewrites, isolated renames, tool-generated dead-code lists without call-site proof, or vague observations that code looks complex.

## Survey Broadly

Start with the largest changed or highly coupled production surfaces. When repository-wide breadth, many candidates, or parallel analysis is explicitly requested, partition the survey by domain:

- CLI parsing, rendering, exit semantics, daemon requests, and the installed command boundary in `src/codeq/cli.py`.
- Daemon endpoints, peer validation, process lifecycle, version upgrades, in-process fallback, and runtime directories in `src/codeq/daemon.py` and `src/codeq/service.py`.
- Workspace discovery, target resolution, semantic find/context/trace/review behavior, LSP sessions, caching, and disclosure metadata in `src/codeq/workspace.py` and `src/codeq/lsp.py`.
- Git diff truth, exact-text search, dynamic-reference classification, topology, base-side analysis, and shared contracts in the remaining `src/codeq` modules.
- Tests, benchmarks, build/release tooling, documentation, JSON compatibility, and committed distributions.

Require every survey result to include source evidence, consumer classification, behavior given up, and a rejection condition. Do not let candidate count substitute for confidence.

## Prove or Reject Every Candidate

For each symbol or behavior:

1. Identify its exact qualified symbol, CLI spelling, wire value, environment variable, schema field, file, or lifecycle state.
2. Use `uv run codeq context` and `uv run codeq trace` to inspect definitions, callers, callees, references, implementations, and likely tests. Use file context with `--topology` for import relationships and `uv run codeq review` for a change-focused audit. Then use `rg` for dynamic references: CLI strings, environment variables, serialized fields, shell, TOML/YAML/JSON, process entry points, resource loading, and documentation contracts.
3. Read every relevant call site. Classify consumers as:
   - **Production/runtime:** CLI execution, service dispatch, daemon lifecycle, workspace analysis, language-server I/O, Git/text helpers, and installed `codeq` behavior.
   - **Operational/compatibility:** builds, releases, package entry points, protocol and schema versions, runtime directories, daemon upgrades, supported platforms, and performance gates.
   - **Non-production:** tests, docs, examples, snapshots, fixtures, comments, and historical benchmark records.
   - **Ambiguous:** generated, packaged, loader-discovered, persisted, platform-specific, or externally invoked surfaces. Resolve the loading path before judging them.
4. Find the rationale in the README, the active 1.0 compatibility policy, source contracts, and applicable repository instructions. Use historical plans and validation logs as discovery evidence, not automatic authority.
5. State the proposed end state and the capability or behavior it removes. Distinguish cleanup from a product or compatibility decision.
6. Calculate net simplification: implementation, tests, fixtures, docs, configuration, and branches deleted minus replacement glue, migration work, new dependency footprint, and unrelated churn.
7. Name a validation path and a fact that would disprove the candidate.

Downgrade or reject a candidate when a current production or operational consumer exists; stable CLI/JSON/exit semantics, worktree correctness, daemon safety, bounded disclosure, or performance depends on it; the proposal weakens an intentional boundary; the replacement only moves the same complexity; or the change is an unapproved product decision.

## Audit Ownership, Trust, and Lifecycle Boundaries

For each parser, validator, retry, lock, cache, fallback, and process transition, name the data source, trust boundary, owner, mutability, process boundary, platform assumptions, and next consumer. Preserve validation for CLI input, daemon messages, LSP responses, Git output, files, sockets, subprocesses, and JSON unless another verified owner enforces the same invariant.

For daemon and workspace lifecycle code, map each endpoint choice, peer check, version check, retry, fallback, eviction threshold, cache key, shutdown path, and cleanup action to a distinct state transition or owner. Consolidate mechanisms only when they mirror the same fact. Preserve separate mechanisms when they protect same-UID trust, cross-namespace behavior, stale-daemon recovery, sandbox fallback, worktree isolation, cache invalidation, or resource cleanup.

Before moving logic between modules, keep CLI concerns in parsing/rendering/transport, request orchestration and workspace lifetime in the service, semantic repository behavior in `Workspace`, LSP protocol behavior in `LspProcess`, and Git/text/platform facts in focused helpers. Avoid moving policy into a lower-level helper merely to shorten a caller.

## Evaluate Dependency Substitutions

Prefer the standard library and dependencies already approved by the project. Before proposing a new dependency:

- Match its supported surface against the hand-rolled behavior and list residual glue.
- Verify current maintenance, licensing, Python/platform support, and transitive footprint from primary sources.
- Include deleted implementation, dedicated tests, and docs in the net-deletion calculation.
- Reject wrappers that retain the original complexity or add a second abstraction.

Treat a runtime dependency as a major product tradeoff because codeq currently has no Python runtime dependencies.

## Report the Outcome

Default to a ranked shortlist of up to five strong candidates. Return fewer rather than lowering the evidence bar. For each candidate, provide:

- A concrete action-oriented title and confidence level.
- Current surface and source paths.
- Production, operational, non-production, and ambiguous consumer evidence.
- Proposed removal, merge, demotion, or replacement.
- Net simplification and behavior or flexibility given up.
- Risks, affected flows, required tests/docs, and rejection condition.

Also list representative rejected or downgraded ideas so the survey boundary is visible. Distinguish "no consumer found" from "no consumer exists" and name the searches performed.

Keep proposal-only results in the response, pull request, issue, or a user-requested report. Do not create a new normative document, add TODO/FIXME/XXX comments, edit code, or mutate issues and pull requests unless the user asks for that change.

## Implement an Approved Simplification

When implementation is requested:

1. Re-run the relevant `uv run codeq context`, `trace`, or `review` query and inspect dynamic, packaged, persisted, platform-specific, and operational consumers that static analysis cannot establish.
2. Apply `codeq-test-governance` to decide which retained behaviors warrant permanent tests, then make the smallest root-cause deletion or collapse. Do not add tests mechanically for code being removed or for guarantees already enforced elsewhere.
3. Update the authoritative document when a supported contract changes. Search for stale copies and replace duplicated detail with links.
4. Run the repository version during development. Refresh the stable global installation only for release-ready, consistently versioned changes, as directed by [AGENTS.md](../../../AGENTS.md).
5. Follow the applicable repository instructions for verification.
6. Report actual deletions, remaining glue, behavior changes, affected consumers, verification, and rollback considerations.

Drop a proposal when new call-site, contract, platform, or runtime evidence invalidates it.

## Fold Findings from Another Branch or Pull Request

Compare each branch with its approved base or merge base so its independent contribution is visible; do not assume the base branch name. Port only non-overlapping findings that still meet the evidence bar, consolidate duplicates under the current owner, and retain provenance in the pull request or issue. Do not close or rewrite another branch or pull request unless the user explicitly asks.
