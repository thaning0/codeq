# codeq

`codeq` is a deliberately small CLI-first code-intelligence tool for coding agents.

Its job is not to build another code graph. Its job is to collapse common agent exploration/review loops into a few deterministic shell commands by composing mature language-server semantics with `rg` and `git`.

## Scope

Exactly four user-facing commands:

```text
codeq find QUERY
codeq context TARGET
codeq trace TARGET --in|--out [--depth N]
codeq review [--base REF]
```

There is no MCP server, skill package, graph database, embedding model, visualization layer, or per-worktree `build`/`init` step.

## What each command replaces

### `find`

Find a symbol or related code from a name or a short natural-language query.

```bash
codeq find BacktestService --kind class
codeq find 'report summary freshness policy evidence' --limit 8
```

`--kind` is optional. Useful values include `function`, `method`, `class`, and `test`.

The implementation combines:

- LSP workspace/document symbols for semantic entities;
- `rg` source hits for fast lexical discovery and cold-index fallback;
- deterministic ranking that prefers real definitions over imports/aliases.

### `context`

Return the local semantic neighborhood of one symbol in one call:

- exact definition/location;
- hover/signature;
- bounded source snippet;
- direct callers;
- direct callees;
- implementations;
- source references;
- references from test files;
- symbols in the containing file.

Targets can be a symbol or a file location:

```bash
codeq context BacktestService.stream_backtest_logs
codeq context backend/src/app/services/backtest_service.py:684
```

A `file:line[:column]` target is promoted to its enclosing function/method/type when possible.

### `trace`

Trace the LSP call hierarchy without constructing a persistent graph:

```bash
codeq trace BacktestService.stream_backtest_logs --in --depth 2
codeq trace fetchBars --out --depth 3
```

Traversal is depth-bounded, cycle-protected, result-bounded, and restricted to repository source (for example, `node_modules` is not emitted).

### `review`

Turn a Git diff into compact semantic review context:

```bash
codeq review --base HEAD~1
codeq review --base master --limit 15 --json
```

It performs:

```text
git diff
  -> changed line ranges
  -> enclosing semantic symbols
  -> callers / references
  -> likely tests
  -> affected source files
```

Changed-symbol selection prefers the innermost function/method/type instead of flooding the result with local variables and containing classes.

## Global options

```text
--root PATH       repository or worktree root; defaults to cwd
--json            emit one JSON document
--limit N         bound returned symbols/results
--timeout SEC     language-server request timeout
```

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

The daemon exists for one reason: keep language servers warm between short CLI invocations. It does not own a second code index.

For a monorepo, `codeq` discovers language subprojects and starts only the relevant server. For example, in `~/Quant`, Python analysis runs with `backend/` as its project root and TypeScript analysis runs with `frontend/` as its project root rather than treating the entire Git checkout as one language workspace.

## Worktrees

A Git worktree needs no `codeq init` or `codeq build`.

On first query in a worktree, `codeq` lazily starts the relevant language server for that worktree/project. Subsequent CLI calls reuse it. No `.codeq` directory or semantic database is written into the repository/worktree.

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

The language server remains the semantic authority. `codeq` only composes and bounds queries for agent consumption.

## Known limitations

- Python/JavaScript dynamic dispatch cannot always be resolved statically. Missing edges are preferable to invented edges.
- `find` natural-language behavior is lightweight lexical + semantic ranking, not embedding search.
- `review` test discovery uses language-server references/callers plus test-path classification; it is useful context, not a formal coverage proof.
- The first query for a symbol can take a few seconds because `codeq` deliberately prewarms only a bounded set of likely reference files instead of indexing/building a separate whole-repository graph.
- Different Git worktrees are different language-server workspaces. `codeq` avoids a second graph build, but the underlying language server still has its own workspace startup cost.
