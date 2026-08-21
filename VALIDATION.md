# Validation

Validated on 2026-08-22 against the real `~/Quant` repository using the installed editable `codeq 1.0.0rc2` CLI.

## Release gates

- `uv run python -W error -m unittest discover -s tests`: **82/82 pass**
- `basedpyright --level error src/codeq tests`: **0 errors, 0 warnings**
- `uv build`: **sdist + wheel pass**
- `git diff --check`: **pass**
- CLI help: plain text, no ANSI color
- installed module version: **1.0.0rc2**
- installed distribution metadata: **1.0.0rc2**

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

The installed CLI was revalidated after the 1.0.0rc2 release cut; qualified symbol resolution remained exact and the daemon upgrade handshake completed transparently.

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

### PATH:LINE:COLUMN parsing

`PATH:LINE:COLUMN` parsing is now performed from the right rather than with a greedy path regex. The reported regression:

```text
backend/src/app/api/backtest.py:175:17
```

now resolves the real file `/home/thn/Quant/backend/src/app/api/backtest.py` with `line=175`, `column=17`. In 0.4.0 the column-aware resolver follows the cursor definition to `BacktestService.stream_backtest_logs` at `backend/src/app/services/backtest_service.py:673`, while preserving `requested_location` and a request-site snippet containing `service.stream_backtest_logs(...)`. The line-only form `backtest.py:175` deliberately remains on the enclosing API function at line 158. A missing `missing.py:175:17` still returns `not_found` with exit status **1**.

Regression tests also cover a path containing an internal colon so the right-side numeric suffix parsing remains unambiguous.

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

## Exact working-tree text contracts (0.4.0–0.5.0)

`find --text` uses exact literal matching without semantic interpretation. Since 0.4.1, its file set is Git-visible working-tree text: tracked files plus non-ignored untracked files. Tracked files are searched with `git grep`; untracked candidates come from `git ls-files --others --exclude-standard`, so Git-ignored files remain excluded. Real `~/Quant` validation for:

```text
BACKTEST_QUESTDB_QUERY_TARGET_ROWS
```

returned **21 exact occurrences across 18 lines**, including Python config/runtime usage, docs, `.env.example`, Compose YAML, and Shell tests; **8 matching lines were tests**. With `--limit 12`, only 12 lines were emitted while the full counts remained available and `truncated=true`.

`context ... --lexical-references TEXT` reuses the same evidence shape. On the real FastAPI call site:

```text
backend/src/app/api/backtest.py:175:17
```

using the exact override `/logs/stream` returned **68 matching lines**, including **36 test lines**, while semantic context independently resolved the cursor to `BacktestService.stream_backtest_logs`.

A second exact-text probe for `eod_post_close_pipeline_flow` returned **34 occurrences / 33 lines / 14 test lines**, surfacing deployment metadata/tests/docs that are not connected by ordinary LSP callers.

The 0.4.1 untracked-file acceptance created three temporary, non-ignored files in `~/Quant` (YAML, Shell, SQL) containing the same unique marker. Default text search returned exactly **3 matches / 3 lines**, all marked `untracked`; `--glob '*.yaml'`, `--glob '*.sh'`, and `--glob '*.sql'` each reduced the result to the corresponding single file. The temporary files were then removed and `git status --short` returned to empty.

Path/category filtering was validated against the real `/logs/stream` contract:

```text
--path frontend --exclude-tests
  -> 3 matches / 3 lines / 0 test lines
  -> frontend/src/features/backtests/api.ts
  -> frontend/src/features/logs/api.ts
  -> frontend/src/features/uploaded-strategies/api.ts

--path quant-cli/src --glob '*.ts' --exclude-tests
  -> 1 match / 1 line / 0 test lines
  -> quant-cli/src/cli/commands/backtests.ts:894
```

`--path` and `--glob` are repeatable OR filters; `--exclude-tests` affects both returned lines and aggregate counts. The same filters are available through `context --lexical-references`.

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

## Issue #2: trace node cap

`--node-limit` is now the sole hard cap for trace output and is independent of the global `--limit`. Real `~/Quant` validation against the same 8-node incoming trace produced:

```text
--node-limit 1 -> node_count=1, truncated=true
--node-limit 2 -> node_count=2, truncated=true
--node-limit 5 -> node_count=5, truncated=true
```

Plain-text and JSON output both reported the same bounded node count.

## Issue #5: trace depth zero

An explicit `--depth 0` now survives the CLI/service round trip and means root-only traversal as documented. Real `~/Quant` validation returned:

```text
depth=0
node_count=1
children=0
truncated=false
```

Plain-text output contained only the root node and reported `depth=0`. `--depth 1` still returned the four direct incoming callers. Negative depth is rejected by argparse with exit status `2` and `argument --depth: must be >= 0`; the service also rejects negative values defensively.

## Issues #3 and #4: working-tree and PR review semantics

Working-tree review now includes untracked files from `git ls-files --others --exclude-standard`, so Git-ignored files remain excluded. The untracked `tests/test_review_worktree.py` was reported as `U/analyzed`, and four of its functions appeared in `changed_symbols`.

PR/feature-branch review now has explicit merge-base mode:

```bash
codeq review --base origin/main --merge-base
```

A real temporary divergent Git history verified the distinction. With a base-only commit after divergence, an unstaged feature edit, and an untracked Python file:

```text
merge-base mode: A feature.py, U untracked.py
 direct mode:    D base_only.py, A feature.py, U untracked.py
```

The merge-base result recorded `requested_base`, `base_mode=merge-base`, and the exact `resolved_base` SHA, which matched `git merge-base BASE HEAD`. Supported untracked files receive whole-file semantic analysis.

## Review disclosure and base-side edges

`review` uses Git's A/M/D/R/U status as its complete fact layer. Tracked diff status comes from Git, and untracked files are appended from Git's ignore-aware working-tree view. On the final real `~/Quant` `HEAD~1` validation, codeq reported **15 changed files / 5 deleted**, matching Git's file-status view.

Deleted supported source files now receive base-side analysis instead of stopping at file status. For a deleted Python test file in the real Quant diff, codeq loaded the file from `resolved_base`, extracted **6 base symbols**, and attached current-worktree exact residual-reference evidence. Unsupported deleted docs/config files remain explicit with `deleted_base_unavailable` rather than being guessed.

A separate real temporary Python repository validated pure rename behavior end-to-end through the globally installed CLI. A `R100 old.py -> new.py` plus updated consumer/test imports produced:

```text
importers = 2   (consumer.py, test_consumer.py)
renamed_api current semantic references = 4
test references = 2
```

Deleted-file evidence is labeled `base-side lexical`; pure-rename evidence is labeled `current semantic`. Neither is silently promoted into the other evidence class.

Detailed review lists continue to follow `--limit` while complete file/count facts remain available.

## Other real-repository checks

- `SSE backtest logs` -> `streamBacktestLogs` ranked first.
- `BacktestService.stream_backtest_logs` incoming trace depth 2 -> **8 nodes**, **934.8 ms** in the final run.
- Existing independent historical worktree query succeeded directly with no repository-local `.codeq*` state.
- `~/Quant` `git status --short` was byte-for-byte unchanged before and after the complete acceptance run.

## 0.5.0 correctness contract hardening

Every daemon-backed JSON result now carries `schema_version=1`. Stable machine evidence enums use underscore-separated values (`semantic`, `lexical`, `possible_dynamic`, `base_side_lexical`, `current_semantic`) instead of presentation strings.

A unified internal `QueryBudget` derives all disclosure limits from the single public `--limit` knob: top-level lists follow `--limit`, nested per-symbol details cap at five, hover/source snippets are character-bounded, and exact-text result lines are capped at 500 characters while preserving full match/line/file counts. A real `quant-cli/README.md` `/logs/stream` hit was truncated to exactly **500 characters** with `text_truncated=true`; the response still reported **17 complete matches / 17 matching lines**.

Quant acceptance checks confirmed:

```text
find BacktestService:                schema_version=1
context backtest.py:175:17:          schema_version=1, cursor definition preserved
review HEAD~1:                       base-side evidence=base_side_lexical
text /logs/stream --path quant-cli:  full counts preserved with bounded payload
```

Invariant/contract tests now cover schema attachment, evidence enum shape, monotone query budgets, hard snippet/text payload limits, and count-preserving truncation.

## 0.5.1 performance hardening

Document symbols now use a safe per-Workspace LRU cache keyed by `(path, mtime_ns, size)` and capped at 256 entries. Unit tests cover cache hits, edit invalidation, and LRU eviction. Cross-file references/definitions remain uncached to avoid stale semantic results.

Prewarm is budget-adaptive: it probes every two opened candidate files and stops early only after the current requested result budget is already satisfied. Synthetic tests verify both early-stop and no-premature-stop behavior.

JSON `_meta` now exposes per-request LSP/cache/prewarm deltas. The fixed Quant benchmark (`benchmarks/quant_benchmark.py`, final 0.5.1 run with 2 reps) produced:

```text
                         cold P50   cold P95   warm P50   warm P95
context symbol            3753.7     3825.9      157.2      172.3 ms
context cursor            3845.9     3958.1      125.8      127.5 ms
context + lexical         3844.8     3938.6      302.6      311.0 ms
trace incoming depth 2    3858.2     3872.3      325.0      329.8 ms
find concept              1168.3     1179.0      936.2     1021.7 ms
```

There were no 10 s+ semantic outliers. Warm context samples showed document-symbol cache hits with zero misses. The fully warmed representative Workspace reached about **1.36 GB combined LSP RSS**; this is dominated by basedpyright/typescript-language-server, so memory remains governed by existing workspace idle eviction rather than by an unbounded Python cache.

Full samples are stored in `benchmarks/results/0.5.1-quant.json`; the human-readable baseline is `benchmarks/0.5.1-quant.md`.

## 0.5.2 historical workflow replay

The replay parser scanned actual local Codex/Pi session JSONL tool-call records and found **327 Quant-related CRG workflows / 6216 CRG calls**. The committed 100-workflow sample is stratified as **50 navigation / 30 review / 20 complex** with **71 Codex / 29 Pi** sources. Artifacts are anonymized: no user prompts, raw session paths, or concrete private targets are stored.

The sample contained **1130 historical CRG calls**. Excluding graph-maintenance calls that codeq eliminates entirely, **93.3%** of actionable CRG calls map directly or approximately onto current `find/context/trace/review`. Those 1130 CRG observations compress to **281 mapped codeq observations (4.02x)**. All **50/50 navigation workflows** map without an unsupported fallback.

The only repeated unsupported family is historical **architecture/community abstraction** usage (39/100 workflows). Named affected-flow calls are intentionally classified as approximate `review/trace` coverage rather than as a mandatory fallback because the underlying impact/caller questions are already exposed by current codeq. This does not justify reintroducing a persistent graph.

Thirty anonymized concrete historical query probes (16 `find`, 14 `context`) were executed against the current Quant tree with the final 0.5.2 tool: **30/30 returned `ok`**, P50 **484.2 ms**, max **2785.5 ms**. The replay explicitly does not invent a counterfactual pure-rg/read success/time baseline; historical companion grep/read/git observations are reported as observed pressure only.

Parser regression tests cover Codex `exec -> mcporter`, Pi direct/wrapped CRG calls, injected-doc exclusion, affected-flow mapping, and committed-artifact anonymization.

Full anonymized data: `benchmarks/results/0.5.2-workflows.json`. Human report: `benchmarks/0.5.2-workflows.md`.

## 0.5.3 1.0 readiness / feature freeze

0.5.2 exposed no repeated capability blocker that justified another analysis feature. 0.5.3 therefore freezes the four-command surface and turns the accumulated evidence into executable release gates rather than expanding codeq.

Compatibility tests now assert that the only top-level commands are exactly `find`, `context`, `trace`, and `review`. Existing schema/status/evidence/exit-code/fail-closed tests remain part of the release gate.

`benchmarks/readiness_gate.py` consumes the committed 0.5.1 performance artifact and 0.5.2 historical replay artifact. The final 0.5.3 run passed **9/9** readiness checks:

```text
warm context P95          172.3 ms   <= 3000
warm trace P95            329.8 ms   <= 3000
cold context P95         3825.9 ms   <= 5000
cold trace P95           3872.3 ms   <= 5000
max semantic sample      3958.1 ms   < 10000
historical workflows         100     >= 100
mapping coverage             93.3%   >= 90%
navigation fallback           0/50   required 0
extracted query validation   30/30   >= 95% ok
```

The compatibility/readiness policy is now documented in `docs/codeq-1.0-readiness.md`. It explicitly keeps `rg`, Git, and direct source inspection as expected partners, freezes the four-command surface for the path to 1.0, and rejects speculative graph/community/embedding expansion without repeated real-workflow evidence.

Machine readiness artifact: `benchmarks/results/0.5.3-readiness.json`; human gate report: `benchmarks/0.5.3-readiness.md`.

The final globally installed 0.5.3 CLI was then exercised through the normal Quant sequence:

```text
find 'SSE backtest logs'      -> ok; frontend streamBacktestLogs ranked first
context backtest.py:175:17    -> ok; cursor definition stream_backtest_logs; 3 filtered frontend lexical lines / 0 tests
trace incoming depth 2        -> ok; 8 nodes; not truncated
review HEAD~1                 -> ok; 15 changed / 5 deleted / 0 untracked
```

All four results carried `schema_version=1`, and `~/Quant` remained clean after the run.

## 1.0.0rc1 release cut

The RC cut changes release metadata and release-contract tests only; it does not add or change analysis capabilities. Python package metadata uses the PEP 440 version `1.0.0rc1`; the Git release tag is `v1.0.0-rc1`.

The RC1 release gate passed:

```text
81 / 81 tests
basedpyright: 0 errors, 0 warnings
uv build: codeq-1.0.0rc1 sdist + wheel
readiness gate: 9 / 9 PASS
global module version: 1.0.0rc1
global distribution metadata: 1.0.0rc1
```

The globally installed RC1 then passed the normal Quant smoke:

```text
find 'SSE backtest logs'      -> ok; frontend streamBacktestLogs ranked first
context backtest.py:175:17    -> ok; cursor definition stream_backtest_logs; 3 filtered frontend lexical lines / 0 tests
trace incoming depth 2        -> ok; 8 nodes; not truncated
review HEAD~1                 -> ok; 15 changed / 5 deleted / 0 untracked
```

All four responses retained `schema_version=1`; the Quant working tree was unchanged before/after. The release-candidate readiness artifacts are `benchmarks/1.0.0rc1-readiness.md` and `benchmarks/results/1.0.0rc1-readiness.json`.

RC policy is now active: only silent correctness blockers, compatibility-contract regressions, or repeated severe performance/lifecycle failures should change runtime/analysis behavior before stable 1.0. New capabilities remain deferred.

## 1.0.0rc2 stream-contract fix

RC2 fixes a CLI integration issue observed in an Agent shell wrapper: successful human-readable output mixed normal result lines on stdout with success summaries/notes on stderr. Some execution environments merge or replay the two streams in ways that can duplicate or reorder the visible command result.

The plain-text stream contract is now explicit:

```text
success results / summaries / notes / truncation notices -> stdout
query failures / ambiguity / runtime errors               -> stderr
JSON mode                                                  -> stdout
```

Regression tests cover all four successful renderers (`find`, `context`, `trace`, `review`) and require `stderr == ""` on successful execution. Existing failure tests continue to require nonzero exit status and stderr diagnostics.

The exact reported Quant command was re-run with the globally installed `1.0.0rc2` CLI:

```text
codeq find 'automatic factor submission slug auto-factor' --limit 20
```

Validation result:

```text
20 result lines
20 unique result lines
1 success summary
stderr = 0 bytes
```

A missing explicit path still produced `rc=1`, zero stdout bytes, and the error only on stderr. RC2 passed **82/82 tests**, basedpyright with zero errors/warnings, build, and the **9/9** readiness gate. Artifacts: `benchmarks/1.0.0rc2-readiness.md` and `benchmarks/results/1.0.0rc2-readiness.json`.

Final acceptance summary:

```text
version                 codeq 1.0.0rc2
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
text contracts           tracked + untracked; path/glob/test filters validated
cursor context            call site -> service definition + request snippet
trace                    node-limit enforced; depth 0=root only
review                   deleted base-side + pure-rename current analysis
worktree                 pass
repository mutation      none
ACCEPTANCE                PASS
```

## Remaining boundaries

- Dynamic runtime dispatch can remain unknowable to static analysis. Heuristic callback/registry evidence stays explicitly labeled `possible` and is not promoted to exact call edges.
- `find --text` / lexical-reference evidence includes tracked and non-ignored untracked working-tree text; Git-ignored files remain outside that exact-text contract.
- Deleted-file residual analysis is exact textual evidence from conservative base declarations, not a reconstructed historical LSP call graph; common identifiers can therefore be noisy.
- Natural-language `find` is lexical + semantic ranking, not translation or embedding search. Use vocabulary likely to occur in the source/comments.
- `review` test discovery is semantic/reference-based guidance, not coverage proof.
- Worktrees remain separate language-server workspaces and therefore retain their own first-query warm-up cost.
