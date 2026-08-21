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
- Search exact runtime/configuration/HTTP/registry contracts across Git-visible text (tracked plus non-ignored untracked files).
- Filter exact-text evidence by repository path, glob, and test/non-test category.
- Resolve `PATH:LINE` to enclosing semantic context.
- Resolve `PATH:LINE:COLUMN` to the cursor definition when the language server can prove one exact repository definition, while preserving request-site context.

### Local semantic context

- Direct callers/callees.
- Semantic references and likely tests.
- Implementations/inheritance.
- Bounded possible dynamic callback/registry evidence.
- Progressive file outlines and opt-in import/importer topology.
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

### Exit codes

```text
0  successful query/result
1  valid query with a query-level failure/status (not_found, ambiguous, unsupported, invalid query)
2  CLI/runtime/tool failure
```

`--json` still emits the structured query-level result before exit status 1.

### Target-resolution contract

- Path-like input is exact and fail-closed; it never falls through to fuzzy symbol search.
- Qualified symbols are fail-closed; an unverified container/member relation never degrades to an unrelated same-named symbol.
- `PATH:LINE` means enclosing semantic context.
- `PATH:LINE:COLUMN` prefers the exact cursor definition and preserves `requested_location` plus a request-site snippet.

## Disclosure / token contract

`--limit` remains the single public disclosure control.

Internally:

- top-level item lists follow `--limit`;
- per-symbol nested details are capped at five;
- hover/source snippets have hard character budgets;
- exact-text lines are capped at 500 returned characters;
- complete counts are retained when payload details are truncated;
- truncation is explicit.

The CLI should continue favoring progressive disclosure over eager repository dumps.

## Performance baseline

Source: `benchmarks/results/0.5.1-quant.json` (`~/Quant`, two measured runs).

| Query | Cold P95 | Warm P95 |
| --- | ---: | ---: |
| context symbol | 3825.9 ms | 172.3 ms |
| context cursor | 3958.1 ms | 127.5 ms |
| context + lexical | 3938.6 ms | 311.0 ms |
| trace incoming depth 2 | 3872.3 ms | 329.8 ms |
| find concept | 1179.0 ms | 1021.7 ms |

Readiness thresholds:

- warm context/trace P95 <= 3 s;
- cold context/trace P95 <= 5 s;
- no representative semantic sample >= 10 s.

Current baseline passes all thresholds.

The warmed representative Workspace reached roughly 1.36 GB combined LSP RSS. This is primarily basedpyright/typescript-language-server memory. codeq's document-symbol cache is bounded to 256 entries per Workspace, and existing workspace idle eviction remains the process-memory boundary.

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

The gate consumes the committed 0.5.1 performance artifact and 0.5.2 historical replay artifact. At 0.5.3 all readiness checks pass.

The next release should be `1.0.0-rc1` after a short soak period; additional 0.x feature expansion is not required by the current evidence.
