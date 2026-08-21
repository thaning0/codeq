# Validation

Validated on 2026-08-21 against the real `~/Quant` repository using the installed editable `codeq 0.3.3` CLI.

## Release gates

- `uv run python -W error -m unittest discover -s tests`: **42/42 pass**
- `basedpyright --level error src/codeq tests`: **0 errors, 0 warnings**
- `uv build`: **sdist + wheel pass**
- `git diff --check`: **pass**
- CLI help: plain text, no ANSI color
- installed module version: **0.3.3**
- installed distribution metadata: **0.3.3**

## Correctness blockers from the 0.2.1 evaluation

### Cold exact symbol and qualified target

`find` now has an exact declaration path that does not depend on `workspace/symbol` background indexing. Declaration-looking `rg` hits are mapped back through LSP document symbols before being accepted.

Five independent fresh `Workspace` runs against `~/Quant` all produced:

```text
BacktestService
  -> backend/src/app/services/backtest_service.py:70

BacktestService.stream_backtest_logs
  -> backend/src/app/services/backtest_service.py:673
```

The installed CLI was revalidated after the 0.3.3 version bump; qualified symbol resolution remained exact and the daemon upgrade handshake completed transparently.

A nonexistent qualified member now fails closed:

```text
BacktestService.definitely_missing_member
  -> not_found
  -> qualified member not found in BacktestService: definitely_missing_member
```

It is never degraded to a same-named function in another container.

### Missing explicit paths (Issue #1)

The exact Issue #1 reproductions were run from `~/Quant`, where both requested files were confirmed absent:

```text
scripts/migrate_catalog_1m_to_questdb.py
windata_service/src/QMT_service/tmp_option_probe.py
```

Plain-text `context` now returns only `file not found: ...` and exits **1**. A missing `path:line` query in `--json` mode returns a structured document:

```json
{
  "status": "not_found",
  "target": "scripts/migrate_catalog_1m_to_questdb.py:12",
  "path": "/home/thn/Quant/scripts/migrate_catalog_1m_to_questdb.py",
  "reason": "file not found: ..."
}
```

and also exits **1**. No LSP session is started and fuzzy symbol search is never entered. Existing source-file context, bare symbol search, and qualified symbol context were rechecked and still exit **0** with their original semantics.

### Unsupported existing files

Real repository files were tested:

```text
docs/dxchart-lite/tests/memory-leak/run-memory-leak-test.sh
infra/init-prefect-db.sql
```

`context`, `find`, and `file:line` queries return `unsupported_language`. They do not enter fuzzy symbol search and cannot silently resolve to unrelated Python/TypeScript nodes.

### TypeScript project initialization

Project discovery in `~/Quant` resolves both TypeScript workspaces independently:

```text
frontend/...     -> /home/thn/Quant/frontend
quant-cli/...    -> /home/thn/Quant/quant-cli
```

A fresh-workspace `find 'SSE backtest logs'` was repeated three times. All three runs returned no `No Project` errors and ranked `frontend/src/features/backtests/api.ts::streamBacktestLogs` first.

TypeScript search now boundedly opens relevant lexical-hit documents before `workspace/symbol`; transient `No Project` responses fall back to document-symbol results instead of leaking an unstable error to the agent.

## Progressive disclosure for file context

The file-context contract changed from eager full-file disclosure to staged disclosure.

For:

```text
backend/src/app/services/backtest_service.py
```

LSP reports **194 total symbols**, but the default CLI response now shows only **8 top-level symbols**. Default plain-text output is approximately **15 lines** rather than dumping all 194 symbols.

Default:

```bash
codeq context backend/src/app/services/backtest_service.py
```

returns:

```text
8 top-level symbols
23 direct-import count (summary only)
0 expanded imports
0 expanded importers
topology_loaded=false
```

Further disclosure is explicit:

```bash
codeq context FILE --outline-depth 2
codeq context FILE --container BacktestService
codeq context FILE --kind method --limit 20
codeq context FILE --topology --limit 20
```

Real results:

- `--container BacktestService --limit 12`: **12 shown / 29 matching**, with truncation metadata.
- `--kind method --limit 5`: **5 shown / 25 matching**.
- `frontend/src/features/market/api.ts --topology --limit 20`: **52 total symbols, 2 imports, 13 verified importers**.

Importer scanning is not performed unless `--topology` is requested.

Symbol-level `context` no longer embeds an additional `file_symbols` list; file structure is requested explicitly through `context FILE`.

## Implementations / inheritance

Implementation locations are mapped back to document symbols so agents receive semantic names instead of bare locations.

Real `~/Quant` result:

```text
codeq context SingleAssetFactor
  implementations:
    MomentumFactor  backend/src/app/trade/factors/single/momentum.py:17
```

The queried base location is excluded when LSP reports it alongside implementations.

## Review disclosure

`review` continues to use Git's A/M/D/R status as its complete fact layer. On the real current `HEAD~1` diff:

```text
32 changed files
23 modified
9 deleted
0 renamed
```

This exactly matched `git diff --name-status -M HEAD~1 --`.

Deleted files remain explicit and are marked `deleted_not_analyzed` rather than disappearing.

Detailed review material now follows the same disclosure budget as the rest of the CLI. With `--limit 10`:

```text
12 impacted files total -> 10 returned
115 likely tests total  -> 10 returned
```

Full counts remain present together with truncation flags; increase `--limit` only when deeper review context is needed.

## Other real-repository checks

- `SSE backtest logs` -> `streamBacktestLogs` ranked first.
- `BacktestService.stream_backtest_logs` incoming trace depth 2 -> **8 nodes**, **934.8 ms** in the final run.
- Existing independent historical worktree query succeeded directly with no repository-local `.codeq*` state.
- `~/Quant` `git status --short` was byte-for-byte unchanged before and after the complete acceptance run.

Final acceptance summary:

```text
version                 codeq 0.3.3
cold qualified          BacktestService.stream_backtest_logs:673
exact class             BacktestService:70
unsupported .sh/.sql    explicit unsupported_language
file default            194 total -> 8 shown, topology hidden
file container          12 / 29 shown
file method filter      5 / 25 shown
TS topology             52 symbols, 2 imports, 13 importers
TS project              /home/thn/Quant/quant-cli
concept top             streamBacktestLogs
inheritance             MomentumFactor
trace                    8 nodes
review                   32 files, 9 deleted, bounded detail lists
worktree                 pass
repository mutation      none
ACCEPTANCE                PASS
```

## Remaining boundaries

- Dynamic runtime dispatch can remain unknowable to static analysis. Heuristic callback/registry evidence stays explicitly labeled `possible` and is not promoted to exact call edges.
- Natural-language `find` is lexical + semantic ranking, not translation or embedding search. Use vocabulary likely to occur in the source/comments.
- `review` test discovery is semantic/reference-based guidance, not coverage proof.
- Worktrees remain separate language-server workspaces and therefore retain their own first-query warm-up cost.
