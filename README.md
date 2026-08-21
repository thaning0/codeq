# codeq

`codeq` is a deliberately small CLI-first code-intelligence tool for coding agents.

Its job is to collapse common agent exploration/review loops into a few deterministic shell commands by composing mature language-server semantics with `rg` and `git`.

## Scope

Exactly four user-facing commands:

```text
codeq find QUERY
codeq context TARGET
codeq trace TARGET --in|--out [--depth N]
codeq review [--base REF]
```

## Agent integration

`codeq` is intentionally self-describing. An agent should not need a codeq skill file, copied prompt, or repository-specific wrapper.

The only repository instruction needed is one line in `AGENTS.md`:

```text
When exploring, understanding, tracing, or reviewing code, use the `codeq` CLI first; run `codeq --help` for usage.
```

From that point the intended discovery path is:

```text
AGENTS.md says "use codeq"
  -> codeq --help
  -> choose find / context / trace / review
  -> codeq COMMAND --help when an argument is unclear
```

Command selection is deliberately simple:

| Question | Command |
| --- | --- |
| Where is this code / what is it called? | `codeq find QUERY` |
| What is this symbol and what directly surrounds it? | `codeq context TARGET` |
| Who calls this / what does it call across multiple hops? | `codeq trace TARGET --in/--out` |
| What does this branch/diff affect and which tests are relevant? | `codeq review --base REF` |

All human-readable CLI output and help are plain text without ANSI colors. Use `--json` only when structured machine consumption is useful.

## What each command replaces

### `find`

Find a symbol or related code from a name or a short natural-language query, or switch to exact working-tree text mode for runtime/configuration contracts.

```bash
codeq find BacktestService --kind class
codeq find 'report summary freshness policy evidence' --limit 8
codeq find --text 'BACKTEST_QUESTDB_QUERY_TARGET_ROWS' --limit 20
codeq find --text '/logs/stream' --path frontend --exclude-tests
codeq find --text '/logs/stream' --path quant-cli/src --glob '*.ts' --exclude-tests
```

Arguments:

| Argument | Meaning |
| --- | --- |
| `QUERY` | Exact symbol, qualified-name fragment, or short source-code description. For concepts, use vocabulary likely to appear in source/comments; queries are not automatically translated between natural languages. |
| `--kind KIND` | Optional semantic result filter such as `function`, `method`, `class`, `interface`, or `test`. |
| `--text` | Treat `QUERY` as an exact literal and search Git-visible working-tree text: tracked files plus untracked files not excluded by Git ignore rules. Results mark `tracked`/`untracked` and keep full exact-match/line/file counts. |
| `--path PREFIX` | Text mode only. Restrict to a repository-relative path prefix; repeat for OR matching. |
| `--glob PATTERN` | Text mode only. Restrict to a shell-style path glob; repeat for OR matching. `*.ts` matches by basename as well as repository-relative path. |
| `--exclude-tests` | Text mode only. Exclude test paths from both returned lines and aggregate counts. |
| `--limit N` | Maximum number of result lines/symbols returned; full text-match counts remain available even when text output is truncated. Global option, default `20`. |

The default semantic mode combines LSP workspace/document symbols, bounded `rg` discovery, and deterministic ranking. `--text` is deliberately separate: tracked files come from `git grep`, non-ignored untracked files come from Git's working-tree file view, and raw YAML/Shell/SQL/docs lines are never reinterpreted as semantic symbols.

### `context`

Return context for a symbol, source location, or whole source file.

For a symbol or source-position target it returns:

- exact definition/location;
- hover/signature;
- bounded definition source snippet;
- direct callers and callees;
- implementations and source references;
- references from test files;
- possible dynamic callback/registry references when detected.

Position semantics are intentionally different by precision:

- `PATH:LINE` keeps the enclosing semantic context (function/method/type).
- `PATH:LINE:COLUMN` first asks the language server for the symbol under the cursor and follows its definition when one exact repository definition is available. The response also preserves `requested_location` and a small `request_source` snippet around the original call site.

For a source-file target it uses progressive disclosure. The default returns only a bounded top-level outline. Expand deliberately:

```bash
codeq context backend/src/app/services/backtest_service.py
codeq context backend/src/app/services/backtest_service.py --outline-depth 2
codeq context backend/src/app/services/backtest_service.py --container BacktestService
codeq context backend/src/app/services/backtest_service.py --kind method --limit 20
codeq context frontend/src/features/market/api.ts --topology --limit 20
```

`--topology` is opt-in because imports/importers are much less frequently needed than a file outline. Without it, codeq does not scan importer candidates.

Exact textual evidence is also opt-in:

```bash
# Search the resolved symbol/file name literally across Git-visible text
codeq context BacktestService.stream_backtest_logs --lexical-references

# Override the literal for a runtime/HTTP/config contract and filter the evidence
codeq context backend/src/app/api/backtest.py:175:17 \
  --lexical-references '/logs/stream' --path frontend --exclude-tests
```

`lexical_references` uses the same non-semantic Git-visible text result shape and the same `--path` / `--glob` / `--exclude-tests` filters as `find --text`; it is kept separate from LSP `references` so agents can distinguish semantic edges from exact textual evidence.

Symbol/location examples remain:

```bash
codeq context BacktestService.stream_backtest_logs
codeq context backend/src/app/services/backtest_service.py:684
codeq context backend/src/app/api/backtest.py:175:17
```

A `PATH:LINE` target is promoted to its enclosing function/method/type when possible; `PATH:LINE:COLUMN` instead prefers the cursor symbol's definition and falls back to enclosing context when no definition is available. Line/column suffixes are parsed from the right, so the syntax remains unambiguous even when the path itself contains a colon.

Arguments:

| Argument | Meaning |
| --- | --- |
| `TARGET` | Qualified symbol, bare symbol, source file, or `PATH:LINE[:COLUMN]`. Qualified symbols are preferred when known. |
| `--outline-depth N` | File targets only. Maximum nesting depth; default `1` (top-level only). |
| `--kind KIND` | File targets only. Select one symbol kind across the file, such as `method` or `class`. |
| `--container NAME` | File targets only. Reveal one class/container and its children. |
| `--topology` | File targets only. Additionally resolve bounded direct imports and importers. |
| `--lexical-references [TEXT]` | Also return exact Git-visible text evidence. Without `TEXT`, search the resolved symbol/file name; with `TEXT`, search that exact contract string. |
| `--path PREFIX` | Lexical-reference mode only. Restrict text evidence to a repository-relative prefix; repeat for OR matching. |
| `--glob PATTERN` | Lexical-reference mode only. Restrict text evidence to a shell-style path glob; repeat for OR matching. |
| `--exclude-tests` | Lexical-reference mode only. Exclude test paths from text evidence and its counts. |
| `--limit N` | Bounds returned symbols/references/topology/text lines; full exact-text counts remain available. Global option, default `20`. |
| `--json` | Return the same context as one JSON document. |

### `trace`

Trace a semantic call hierarchy:

```bash
codeq trace BacktestService.stream_backtest_logs --in --depth 2
codeq trace fetchBars --out --depth 3
```

Traversal is depth-bounded, cycle-protected, result-bounded, and restricted to repository source (for example, `node_modules` is not emitted).

Arguments:

| Argument | Meaning |
| --- | --- |
| `TARGET` | Qualified symbol, bare symbol, or `PATH:LINE[:COLUMN]`. |
| `--in` | Walk incoming calls toward callers/entry points; use for impact radius. |
| `--out` | Walk outgoing calls toward callees/implementation; use for execution flow. |
| `--depth N` | Maximum number of call edges. `0` = root only, `1` = direct neighbors; default `3`. Must be non-negative. |
| `--node-limit N` | Hard cap on emitted call-tree nodes; default `100`. This cap is independent of global `--limit`. |

### `review`

Turn a Git diff into compact semantic review context:

```bash
codeq review --base HEAD~1
codeq review --base origin/main --merge-base
codeq review --base master --merge-base --limit 15 --json
```

Working-tree review includes tracked staged/unstaged changes plus untracked files reported by `git ls-files --others --exclude-standard`; ignored files stay excluded. Untracked files are marked `U` and supported source files receive whole-file semantic analysis.

For PR/feature-branch review, add `--merge-base`. codeq resolves `git merge-base BASE HEAD` and compares that commit against the current worktree, so base-only commits after divergence are excluded while current staged/unstaged/untracked worktree edits remain visible.

It first reports Git's added/modified/deleted/renamed/untracked file set, then analyzes current changed source:

```text
git diff
  -> A/M/D/R/U file status
  -> current changed line ranges
  -> enclosing semantic symbols
  -> callers / references
  -> possible dynamic callback / registry references
  -> likely tests
  -> affected source files
```

Deleted files receive conservative base-side analysis: codeq reads the file from `resolved_base`, extracts top-level functions/classes and important constants, then performs exact Git-visible working-tree text search for residual references and tests. This evidence is explicitly labeled lexical rather than being promoted to an LSP call edge.

Pure renames (no content hunk in the renamed file) are analyzed on the new path using current import topology plus bounded LSP references/tests for top-level symbols. Changed-symbol selection for ordinary modified files still prefers the innermost function/method/type instead of flooding the result with local variables and containing classes.

Arguments:

| Argument | Meaning |
| --- | --- |
| `--base REF` | Requested Git base ref; default `HEAD~1`. |
| `--merge-base` | Resolve `merge-base(BASE, HEAD)` before diffing; recommended for PR/feature-branch review. |
| `--limit N` | Bounds detailed changed symbols, affected files, and likely tests; file status/counts remain complete. Global option, default `20`. |

Review JSON records `requested_base`, `base_mode`, and `resolved_base` for auditability.

## Global options

| Argument | Default | Meaning |
| --- | --- | --- |
| `--version` | — | Print the installed codeq version and exit. |
| `--root PATH` | `.` | Repository or worktree path. codeq resolves the containing Git root. |
| `--json` | off | Emit one machine-readable JSON document instead of compact plain text. |
| `--limit N` | `20` | Bound matches/symbols where the selected command uses a result limit. |
| `--timeout SEC` | `20` | Language-server request timeout in seconds. |

Query outcomes such as `not_found`, `ambiguous`, and `unsupported_language` exit with status `1`. Runtime/tool failures exit with status `2`. In `--json` mode the structured error document is still emitted before the nonzero exit.

### JSON contract (schema version 1)

All daemon-backed command results include `schema_version: 1`. The stable top-level status vocabulary is:

```text
ok
not_found
ambiguous
unsupported_language
unsupported_target
invalid_query
error
```

Nested review analysis may additionally use `unavailable` for evidence that cannot be produced from the selected base/current worktree.

Machine-readable evidence values use underscore-separated enums rather than presentation strings:

```text
semantic
lexical
possible_dynamic
base_side_lexical
current_semantic
```

`--limit` is the single public disclosure knob. Internally codeq derives a bounded query budget: top-level items follow `--limit`, per-symbol nested details are capped at five, hover/source/text-line payloads have hard character budgets, and any bounded list keeps its complete count plus truncation metadata when available. Exact-text matching counts are computed before payload truncation.

For agent ergonomics, global options work before or after the subcommand:

```bash
codeq --json --root ~/Quant find fetchBars
codeq find fetchBars --root ~/Quant --json
```

## Architecture

```text
Agent
  |
  | shell
  v
codeq CLI
  |
  | local Unix socket
  v
small persistent daemon
  |
  +-- Python project ------> basedpyright / pyright LSP
  |
  +-- TypeScript project --> typescript-language-server / tsserver
  |
  +-- rg ------------------> lexical discovery / targeted prewarm
  |
  +-- git -----------------> changed ranges for review
```

The daemon keeps language servers warm between short CLI invocations, automatically restarts across incompatible codeq upgrades, and releases inactive language workspaces after an idle period.

For a monorepo, `codeq` discovers language subprojects and starts only the relevant server. For example, in `~/Quant`, Python analysis runs with `backend/` as its project root and TypeScript analysis runs with `frontend/` as its project root rather than treating the entire Git checkout as one language workspace.

## Worktrees

Run `codeq` directly from any Git worktree. Worktrees are treated as separate language workspaces, and inactive workspaces are released automatically.

Runtime socket location:

1. `$XDG_RUNTIME_DIR/codeq.sock` when `XDG_RUNTIME_DIR` exists;
2. otherwise `/tmp/codeq-$UID/codeq.sock`.

## Runtime requirements

Required:

- Python 3.12+
- `git`
- `rg` (ripgrep)

For Python semantic analysis, install one of:

```bash
basedpyright --version
# or
pyright --version
```

`codeq` prefers `basedpyright-langserver`, then `pyright-langserver`.

For TypeScript/JavaScript semantic analysis, install `typescript-language-server`. During development this repository can keep an isolated copy under `.vendor`:

```bash
npm install --prefix ~/codeq/.vendor --no-save \
  typescript-language-server@6.0.0 typescript@7.0.2
```

Discovery order is:

1. `typescript-language-server` on `PATH`;
2. target project's `node_modules/.bin/typescript-language-server`;
3. `~/codeq/.vendor/node_modules/.bin/typescript-language-server` for this checkout.

## Development / installation

```bash
cd ~/codeq
uv sync
uv run python -m unittest discover -s tests -v
uv tool install --editable ~/codeq
```

After editable installation, `codeq` can be called directly from any repository.

## Design constraints

`codeq` intentionally does **not** implement:

- a parser suite;
- symbol-resolution heuristics that compete with the compiler/type checker;
- a graph schema or graph database;
- community detection;
- embeddings/vector search;
- refactor application;
- code visualization;
- MCP or agent-specific skills.

Language servers remain the semantic authority for symbols, references, and call edges. File import topology uses deterministic source/module resolution with language-server fallback. `codeq` composes and bounds these queries for agent consumption.

## Known limitations

- Python/JavaScript dynamic dispatch cannot always be resolved statically. `codeq` may surface bounded callback/registry references as explicitly labeled "possible" evidence, but it does not promote heuristic matches to exact call edges.
- Exact text evidence (`find --text`, `context --lexical-references`) searches tracked plus non-ignored untracked working-tree text. Git-ignored files remain outside the contract, and raw text hits do not claim semantic linkage.
- Deleted-file review evidence is base-side declaration extraction plus exact current-worktree text search, not a reconstructed historical LSP graph; common names can therefore be noisy.
- Pure-rename analysis uses current-path importers/references and can still miss runtime-only loaders.
- Qualified targets such as `Class.method` are fail-closed: if the container/member relationship cannot be verified exactly, codeq returns `not_found`/`ambiguous` rather than falling back to an unrelated same-named symbol.
- Explicit path targets are exact even when the file is missing. A missing `file.py`, `path/to/file.ts`, or `path:line[:column]` returns `not_found` and never enters fuzzy symbol search.
- Existing source files outside the currently supported Python/TypeScript/JavaScript families return `unsupported_language`; they are never reinterpreted as fuzzy symbol queries.
- `find` natural-language behavior is lightweight lexical + semantic ranking, not translation or embedding search; use terms likely to occur in the repository source/comments.
- `review` test discovery uses language-server references/callers plus test-path classification; it is useful context, not a formal coverage proof.
- The first query for a symbol can take a few seconds while the relevant language workspace warms and a bounded set of likely reference files is opened.
- Different Git worktrees are different language-server workspaces and therefore have independent first-query startup costs.
