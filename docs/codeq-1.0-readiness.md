# codeq 1.0 readiness

Status: **READY FOR 1.0 RC**

This document freezes the intended product boundary after the 0.5.x hardening line. `codeq` is the default semantic/text navigation and review primitive for coding agents; it is not intended to become the only code inspection tool or a general static-analysis platform.

## Stable feature surface

The user-facing command surface is frozen to exactly four top-level commands:

```text
find
context
trace
review
```

New top-level commands are a compatibility break and require explicit evidence from repeated real Agent workflows. Minor releases should prefer options or better composition inside these four commands.

## Supported primary workflows

### Navigation and discovery

- Find Python/TypeScript/JavaScript symbols semantically.
- Discover source concepts as ranked files through a workspace-local, contentless
  in-memory SQLite FTS5 index, with bounded representative lexical source lines and
  copyable location-based context commands for returned files.
- Select `find` behavior explicitly with symbol, concept, or text mode when automatic
  identifier-versus-multi-term classification is not desired.
- Search exact runtime/configuration/HTTP/registry contracts across Git-visible text (tracked plus non-ignored untracked files).
- Filter exact-text evidence by repository path, glob, and test/non-test category.
- Resolve `PATH:LINE` to enclosing semantic context.
- Resolve `PATH:LINE:COLUMN` to the cursor definition when the language server can prove one exact repository definition, while preserving request-site context.

### Local semantic context

- Direct callers/callees.
- Semantic references and likely tests.
- Implementations/inheritance.
- Bounded possible dynamic callback/registry evidence.
- Progressive file outlines and opt-in import/importer topology for a target file
  or the file containing a resolved symbol/location.
- Separate lexical evidence that is never silently promoted to a semantic edge.

### Multi-hop tracing

- Incoming/outgoing LSP call hierarchy.
- Explicit depth and node budgets.
- Cycle protection and repository-source filtering.

### Working-tree / PR review

- Git A/M/D/R/U file truth.
- Non-ignored untracked source files.
- Explicit merge-base PR semantics.
- Changed-symbol impact, callers/references/tests, and bounded dynamic evidence.
- Conservative base-side lexical evidence for deleted supported source files.
- Current semantic importer/reference evidence for pure renames.

## Stable machine contract

### JSON

Every daemon-backed result includes:

```json
{
  "schema_version": 1,
  "status": "..."
}
```

Stable top-level status vocabulary:

```text
ok
not_found
ambiguous
unsupported_language
unsupported_target
unsupported_capability
invalid_query
error
```

Nested evidence may use `unavailable` where analysis cannot be produced from the selected base/current worktree.

Stable machine evidence vocabulary:

```text
semantic
lexical
possible_dynamic
base_side_lexical
current_semantic
```

Evidence classes must remain separated. A lexical match must never become an authoritative semantic reference merely because it appears plausible.

Symbol/file `context` and `review` JSON responses also expose diagnostic phase
timing under `_meta.phase_ms`. Context separates `resolution`, `prewarm`, and
`semantic_neighborhood`; review separates `change_discovery` and
`review_analysis`. These additive diagnostics do not expand default plain output.

### Exit codes

```text
0  successful query/result
1  valid query with a query-level failure/status (not_found, ambiguous, unsupported, invalid query)
2  CLI/runtime/tool failure
```

`--json` still emits the structured query-level result before exit status 1.

### Target-resolution contract

- Path-like input is exact and fail-closed; it never falls through to fuzzy symbol search.
- Qualified symbols are fail-closed; an unverified semantic suffix or module/file
  suffix never degrades to an unrelated same-named symbol.
- Semantic `find` and symbolic `context` path constraints filter before target
  selection; ambiguous candidates include copyable exact-location commands.
- Concept `find` is lexical file discovery, never an invented semantic symbol;
  representative lines explain file ranking without being promoted to semantic
  evidence, and path/glob/test constraints filter before the public result limit.
- `PATH:LINE` means enclosing semantic context.
- `PATH:LINE:COLUMN` prefers the exact cursor definition and preserves `requested_location` plus a request-site snippet.

## Disclosure / token contract

`--limit` remains the single public item-count control. Concept `find --files-only`
only suppresses plain-output evidence details; it does not change result selection,
counts, or JSON evidence.

Internally:

- top-level item lists follow `--limit`;
- per-symbol nested details are capped at five;
- hover/source snippets have hard character budgets;
- exact-text lines are capped at 500 returned characters;
- complete counts are retained when payload details are truncated;
- truncation is explicit.

The CLI should continue favoring progressive disclosure over eager repository dumps.

## Performance baseline

Source: `benchmarks/results/1.0.0rc6-quant.json` (`~/Quant`, two measured runs).

| Query | Cold P95 | Warm P95 |
| --- | ---: | ---: |
| context symbol | 3950.4 ms | 362.2 ms |
| context reference store | 3452.1 ms | 675.1 ms |
| context cursor | 3242.7 ms | 152.2 ms |
| context + lexical | 3231.7 ms | 339.0 ms |
| trace incoming depth 2 | 3557.1 ms | 410.1 ms |
| find concept | 1074.6 ms | 1621.3 ms |
| broad review (`HEAD~4`, 28 files) | 7476.3 ms | 3243.3 ms |

Readiness thresholds:

- warm context/trace P95 <= 3 s;
- cold context/trace P95 <= 5 s;
- no representative semantic/review sample >= 10 s;
- live-workload-shaped complex context and broad review cases must be present in both cold and warm samples.

Current baseline passes all thresholds.

An earlier fully warmed baseline reached roughly 1.36 GB combined LSP RSS. This is primarily basedpyright/typescript-language-server memory. codeq's document-symbol cache is bounded to 256 entries per Workspace, and existing workspace idle eviction remains the process-memory boundary.

## Historical Agent workflow evidence

Source: `benchmarks/results/0.5.2-workflows.json`.

The anonymized replay parsed actual local Codex/Pi sessions containing code-review-graph calls:

```text
Quant-related historical CRG workflows: 327
Quant-related CRG calls:                6216
Stratified replay sample:                100
  navigation:                             50
  review:                                 30
  complex:                                20
```

Results:

```text
Historical CRG calls in sample:        1130
Mapped codeq observations:              281
Observation compression:               4.02x
Actionable direct/approx coverage:      93.3%
Navigation unsupported fallback:        0 / 50
Current extracted query validation:    30 / 30 ok
```

The remaining repeated unsupported abstraction is architecture/community analysis. Historical named affected-flow calls are adequately represented as approximate `review`/`trace` coverage for the underlying impact questions.

This evidence does **not** justify adding architecture communities, named flows, embeddings, or a persistent graph back into codeq.

## Expected fallback tools

A strong default workflow remains:

```text
find
-> context
-> trace (when multi-hop is needed)
-> review (for branch/diff work)
-> rg / direct source read / git for runtime-specific or repository-specific boundaries
```

Using `rg`, Git, and direct source inspection is expected and healthy. 1.0 success does not require zero fallback.

## Explicit non-goals

1.0 does not add:

```text
persistent graph/index
embeddings/vector database
architecture/community detection
named flow database
risk score
universal dependency graph
AST framework competing with LSP/type checkers
cross-language synthetic call graph
runtime tracing
MCP server
agent skill framework
automatic refactoring
```

## 1.0 compatibility policy

For 1.x minor releases:

- do not add/remove top-level commands casually;
- do not change the meaning of existing status/evidence enum values;
- do not change exit-code semantics;
- do not weaken fail-closed target resolution;
- do not remove stable JSON fields without a schema-version change;
- additive optional JSON fields are allowed;
- performance optimizations must preserve semantic results and cache invalidation correctness;
- new analysis capabilities require repeated real-workflow evidence, not speculative platform completeness.

## Automated readiness gate

Run:

```bash
uv run python benchmarks/readiness_gate.py
```

The gate consumes the committed active-version performance artifact and the 0.5.2
historical replay artifact. The refreshed `1.0.0rc12` gate passes all ten checks,
including mandatory live-workload cases and the no-10-second semantic/review limit.
RC12 keeps the shared daemon and one-shot fallback, coalesces concurrent cold work,
and replaces heuristic multi-token semantic promotion with ephemeral FTS5 file
discovery.

During the RC period, only release blockers should change analysis/runtime behavior: silent correctness bugs, compatibility-contract regressions, or repeated severe performance/lifecycle failures. New analysis capabilities stay deferred. If the RC workload remains clean, the next stable release is `1.0.0`.
