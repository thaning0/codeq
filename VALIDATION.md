# Validation

Validated on 2026-08-21 against the real `~/Quant` repository using the globally installed editable `codeq` executable (`/home/thn/.local/bin/codeq`).

## Static / package checks

- `python3 -m py_compile src/codeq/*.py`: pass
- `uv run python -m unittest discover -s tests -v`: 7/7 pass
- `basedpyright --level error src/codeq tests`: 0 errors, 0 warnings
- `uv build`: source distribution and wheel build successfully

## Real `~/Quant` acceptance

All commands below were invoked from `/home/thn/Quant` through the installed `codeq` command, not by importing implementation classes directly.

| Check | Result |
|---|---:|
| Python `find BacktestService --kind class` | 852.0 ms |
| Python `context BacktestService.stream_backtest_logs` cold | 3608.7 ms |
| Same Python `context` warm | 705.7 ms |
| Python cold/warm language-server process | same PID, reused |
| `context backend/src/app/services/backtest_service.py:684` | 159.5 ms; promoted to `BacktestService.stream_backtest_logs` |
| Python incoming trace depth 2 | 1055.6 ms; 8 semantic nodes |
| Natural-language `find` | 704.4 ms; 8 bounded results |
| TypeScript `context fetchBars` | 2974.0 ms; 3 direct repository callers |
| TypeScript incoming trace depth 2 | 194.5 ms; 7 semantic nodes |
| `review --base HEAD~1 --limit 5` after warmup | 667.9 ms |
| Review scope | 23 changed files, 5 returned changed symbols, 9 impacted files, 77 likely test references |
| Existing independent worktree cold `find` | 679.1 ms |

### Python semantic assertions

`BacktestService.stream_backtest_logs` resolved deterministically to:

```text
/home/thn/Quant/backend/src/app/services/backtest_service.py:673
```

Direct callers included:

```text
backend/src/app/api/backtest.py::stream_backtest_logs
backend/tests/api/test_backtest_api.py::_collect
```

Three direct test references were returned, and a depth-2 incoming trace produced 8 nodes.

The same qualified target was queried repeatedly during development after fixing resolver drift; it remained pinned to the same class/method definition.

### TypeScript semantic assertions

`fetchBars` resolved to:

```text
/home/thn/Quant/frontend/src/features/market/api.ts:38
```

Direct callers included:

```text
useMarketChart
load (useAlpha101Diagnostics)
fetchBenchmarkData
```

External `node_modules` call edges were filtered from agent-facing output.

### Worktree assertion

The existing linked worktree:

```text
/home/thn/Quant/Quant-worktrees/issue-624-agent-verification
```

was queried directly with `codeq --root <worktree> find ...`.

- no `codeq init` was run;
- no `codeq build` was run;
- no `.codeq*` state existed before the query;
- no `.codeq*` state existed after the query;
- the worktree got its own lazy Python LSP process and returned the expected symbol.

### Repository mutation assertion

`git status --short` for `~/Quant` was captured before and after the full final acceptance run and compared byte-for-byte. It was unchanged.

## Interpretation

The MVP demonstrates the intended architectural tradeoff:

- no persistent source graph is required;
- no per-worktree graph rebuild is required;
- cold semantic queries pay only bounded language-server startup/prewarm cost;
- subsequent agent queries reuse the same language-server process;
- the four CLI primitives cover symbol discovery, local context, multi-hop call tracing, and diff-oriented review context.

The validation does not claim static completeness for dynamic Python/JavaScript dispatch. Missing dynamic edges remain an explicit limitation rather than being filled with heuristic graph edges.
