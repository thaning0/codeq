# Validation

Validated on 2026-08-21 against the real `~/Quant` repository using the globally installed editable `codeq` executable (`/home/thn/.local/bin/codeq`).

## Static / package checks

- `python3 -m py_compile src/codeq/*.py`: pass
- focused core/dynamic suite (`test_core.py` + `test_dynamic.py`): 18/18 pass; full current working-tree discovery: 21/21 pass
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

### Dynamic-reference fallback assertions

The fallback classifies **exact LSP references** by their local source context; it does not perform repository-wide heuristic graph construction.

Real Python validation used FastAPI dependency injection:

```text
backend/src/app/api/backtest.py:38  _get_backtest_service
```

LSP call hierarchy returned no callers, while `codeq context` identified 8 references such as:

```text
Depends(_get_backtest_service)
```

as `callback_argument` with `confidence=possible`.

Real TypeScript validation targeted the local callback precisely by source position:

```text
frontend/src/hooks/use-mobile.ts:11:11  onChange
```

`codeq context` classified these as possible callback references:

```text
mql.addEventListener('change', onChange)
mql.removeEventListener('change', onChange)
```

while excluding the direct call `onChange()` from the dynamic-reference set.

False-positive guards were validated for Python direct method calls, `typing.cast(...)`, TypeScript direct calls, and TypeScript type annotations.

A temporary Git repository verified positive `review` integration: modifying a function registered as `HANDLERS = {"x": handler}` produced one `mapping_value` dynamic reference in `codeq review --base HEAD`.

### Expanded real-repository query matrix

A second acceptance matrix deliberately varied query wording, symbol kind, class, language, and dispatch style. The run used one freshly started daemon, so the first Python/TypeScript requests include each language server's cold-start cost while later rows are warm queries.

#### Different `find` queries

| Query | Filter | Top result / observation | Time |
|---|---|---|---:|
| `BacktestService` | `class` | `BacktestService` at `backend/src/app/services/backtest_service.py:70` | 880.1 ms |
| `verify_factor_price_semantics` | `function` | exact implementation at `semantic_verification.py:565` | 114.7 ms |
| `factor price semantic verification` | `function` | test functions ranked above implementation | 280.8 ms |
| `industry classification data handler` | `class` | `ClassificationDomainDataHandler` at `backtest_classification_domain.py:585` | 262.3 ms |
| `map bar candle` | `function` | `mapBarToCandle` at `frontend/src/features/market/mappers.ts:36` | 4721.1 ms (TS cold start) |
| `market bars fetch` | `function` | several backend `MarketDataQueryService._fetch_*bars*` methods ranked above frontend `fetchBars` | 2799.6 ms |

This confirms two distinct behaviors rather than treating them as equivalent:

- exact/near-exact symbol queries are deterministic and high precision;
- natural-language `find` is bounded lexical ranking, not embedding-based semantic search. Broad wording can legitimately rank tests or another subsystem first, so agents should prefer exact symbols once discovered.

#### Different classes and symbol kinds

| Target | Element kind | Result highlights | Time |
|---|---|---|---:|
| `FactorStreamRunner` | Python class | callers include `from_factors`, benchmark/test runtime constructors; callees include `CanonicalFactorStreamEngine` | 922.2 ms |
| `ClassificationDomainDataHandler` | Python class | caller `build_compatibility_domain_registry` resolved | 320.4 ms |
| `DividendFactorLoadCoordinator._cleanup_flush_task` | Python method | exact caller/callee plus `add_done_callback(self._cleanup_flush_task)` classified `callback_argument` | 340.0 ms |
| `StStatusStateActor.on_data` | Python bound method | msgbus registration `handler=self.on_data` classified `callback_argument` | 531.7 ms |
| `_get_backtest_service` | Python bare function | correctly returned `ambiguous`: definitions exist in both `backtest.py` and `uploaded_strategies.py` | 253.5 ms |
| `backend/src/app/api/backtest.py:38:5` | Python function by location | disambiguated `_get_backtest_service`; 8 FastAPI `Depends(...)` references classified `callback_argument` | 147.8 ms |
| `require_admin_role` | Python function | exact unit-test callers plus admin/log API `Depends(require_admin_role)` dynamic references | 286.1 ms |
| `mapBarToCandle` | TypeScript function | `bars.map(mapBarToCandle)` classified `callback_argument` | 287.7 ms |
| `frontend/src/hooks/use-mobile.ts:11:11` | TypeScript local constant callback | selected local `onChange`; add/remove event listeners classified dynamic, direct `onChange()` excluded | 207.1 ms |
| `frontend/src/components/ui/sidebar.tsx:93:11` | TSX local constant callback | selected `handleKeyDown`; both event-listener references classified dynamic | 56.1 ms |
| `frontend/src/features/market/types.ts:11:13` | TypeScript type alias | selected `BarData`; no call edges and **0 dynamic references** | 104.6 ms |

The `BarData` row caught a real false positive during the first matrix run: generic/type usages such as `Map<string, BarData[]>` were initially misclassified as `collection_member`. The classifier was tightened and two regression tests were added for generic parameter and generic return-type positions; the repeated real-repo query now returns zero dynamic references.

#### Multi-hop traces on different languages

| Target | Direction/depth | Result | Time |
|---|---|---:|---:|
| `BacktestService.stream_backtest_logs` | incoming, depth 2 | 8 semantic nodes | 1385.8 ms |
| `fetchBars` | incoming, depth 2 | 7 semantic nodes | 209.1 ms |

The matrix therefore covers classes, methods, top-level functions, local callbacks, a type alias, exact and ambiguous symbols, file/line/column targets, callback-based dispatch, and ordinary multi-hop call hierarchy in both Python and TypeScript.

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

The validation does not claim static completeness for dynamic Python/JavaScript dispatch. `possible_dynamic_references` are deliberately separated from exact call edges and marked `confidence=possible`; runtime-only dispatch can still remain unknowable to static analysis.
