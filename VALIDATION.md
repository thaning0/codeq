# Validation

Validated on 2026-08-21 against the real `~/Quant` repository using the installed editable `codeq` executable (`/home/thn/.local/bin/codeq`), version `0.2.1`.

## Static and package checks

- full unit/integration suite: **30/30 pass** with `ResourceWarning` promoted to error;
- `basedpyright --level error src tests`: **0 errors, 0 warnings**;
- stale-daemon Unix-socket integration: pass; client detects version mismatch, replaces the stale same-UID daemon, and retries successfully;
- workspace lifecycle tests: idle eviction and least-recently-used cap eviction pass;
- `uv build`: `codeq-0.2.1.tar.gz` and `codeq-0.2.1-py3-none-any.whl` build successfully;
- all CLI help output checked during the suite remains plain text with no ANSI escape sequences.

## Real `~/Quant` acceptance

All checks were invoked through the installed `codeq` CLI, not implementation classes. `git status --short` was captured before and after the run and remained byte-for-byte unchanged.

| Check | Result |
|---|---:|
| `find 'SSE backtest logs' --limit 8` | top result `streamBacktestLogs` at `frontend/src/features/backtests/api.ts:95`; 1466.1 ms |
| `context backend/src/app/services/backtest_service.py` | 194 symbols, 23 imports, 8 verified importer edges; 2635.6 ms |
| `context frontend/src/features/market/api.ts` | 52 symbols, 2 imports, 13 verified importer edges; 195.2 ms |
| `context quant-cli/src/domain/time.ts` | correctly assigned to `/home/thn/Quant/quant-cli`; 1602.7 ms |
| `context BacktestService.stream_backtest_logs` | deterministic source definition, 4 direct callers; 1962.4 ms |
| incoming `trace` depth 2 | 8 semantic nodes; 770.0 ms |
| `review --base HEAD~1 --limit 8` | 32 changed files = 23 modified + 9 deleted; 4997.9 ms |
| existing independent worktree `find` | success in `/home/thn/Quant-worktrees/591-decision-spike`; 1252.0 ms |

## Review file-status completeness

Git truth for the validation diff was:

```text
32 changed files
23 modified
9 deleted
```

`codeq review --base HEAD~1` returned exactly the same counts and status classes. All 9 deleted files were retained in `file_changes` with:

```text
semantic_status = deleted_not_analyzed
```

A temporary Git-repository regression test separately covers all four status classes:

```text
A  added
M  modified
D  deleted
R  renamed
```

A second regression verifies a pure rename with no content hunk remains visible as `rename_or_copy_without_content_changes`, while a deletion remains visible without trying to analyze nonexistent current source.

## File context and import topology

`context` accepts a source file directly and returns its complete LSP document outline instead of a short prefix list.

Python validation:

```text
backend/src/app/services/backtest_service.py
194 document symbols
23 direct imports
8 verified importer edges
```

TypeScript validation:

```text
frontend/src/features/market/api.ts
52 document symbols
2 direct imports
13 verified importer edges
```

Verified TypeScript importers included alias, relative, re-export, test, and dynamic-import cases, including:

```text
frontend/src/features/backtests/utils/benchmark.ts
frontend/src/features/market/index.ts
frontend/src/features/market/hooks/useMarketChart.ts
frontend/src/features/alpha101/hooks/useAlpha101PostAnchorCloseMetrics.ts
```

Local module resolution is covered by unit tests for Python `src/` layouts and TypeScript `tsconfig.json` path aliases such as `@/* -> ./src/*`. Language-server definition lookup remains a fallback when deterministic source resolution cannot resolve a candidate.

## Search ranking

The earlier diagnostic query `SSE backtest logs` now ranks the intended implementation first:

```text
1  streamBacktestLogs  frontend/src/features/backtests/api.ts:95
```

The improvement comes from:

- ranking production definitions ahead of tests/examples unless tests are explicitly requested;
- retaining token-coverage scoring across source hits;
- mapping documentation/comments immediately preceding a declaration to that following semantic definition.

`find` intentionally does not translate between natural languages. Concept queries should use terms likely to occur in source code or comments. Exact/qualified symbol queries remain the preferred follow-up once a target is discovered.

## TypeScript project roots

The reported `No Project` problem was not reproducible after current project discovery was exercised on real files. Both TypeScript roots resolve independently:

```text
frontend/src/features/market/api.ts -> /home/thn/Quant/frontend
quant-cli/src/domain/time.ts        -> /home/thn/Quant/quant-cli
```

Each uses its own `typescript-language-server` workspace.

## Daemon lifecycle

The daemon protocol now carries an explicit codeq version and protocol version. An integration test starts a deliberately stale Unix-socket daemon and verifies that the client:

1. detects the incompatible response;
2. identifies the peer process using same-UID Unix `SO_PEERCRED`;
3. terminates only that stale daemon;
4. starts the current daemon and retries successfully.

Language workspaces are released after 5 minutes idle by default. The service also keeps at most 4 cached inactive workspaces using least-recently-used eviction; active concurrent workspaces are never evicted. The daemon itself exits after 15 minutes with no workspaces by default. These thresholds can be overridden with internal environment variables for diagnostics.

## Dynamic-reference fallback

Exact language-server references can be classified as explicitly possible callback/registry evidence without promoting them to exact call edges. Regression coverage includes:

- Python callback arguments and mapping/registry values;
- FastAPI-style dependency callback references;
- TypeScript event callbacks and mapping values;
- guards against Python direct calls, method calls, `typing.cast`, TypeScript direct calls, type annotations, and generic type positions.

## Worktree assertion

The linked worktree `/home/thn/Quant-worktrees/591-decision-spike` was queried using a class that actually exists in that historical commit. The query succeeded and the worktree's `.codeq*` state before and after the query was identical.

## Current limits

- runtime-only Python/JavaScript dispatch can still be unknowable to static analysis;
- possible dynamic references are labeled evidence, not proof of a runtime call edge;
- `review` reports deleted files but cannot derive current-worktree semantic edges from source that no longer exists;
- test discovery is based on semantic references/callers plus test-path classification, not coverage data;
- natural-language `find` is source-language lexical/semantic ranking, not multilingual translation or embedding search;
- each Git worktree is a separate language-server workspace and has its own first-query startup cost.
