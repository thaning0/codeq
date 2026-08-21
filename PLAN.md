# codeq MVP implementation plan

## Goal
Build a small CLI-first code-intelligence tool for coding agents. Replace the high-frequency parts of code-review-graph without a graph database, MCP server, skills, embeddings, or per-worktree graph builds.

The four recurring tasks are: find related code; understand one symbol; trace callers/callees; review a git diff with impact and likely tests.

## CLI surface

    codeq find QUERY [--kind ...]
    codeq context TARGET
    codeq trace TARGET --in|--out [--depth N]
    codeq review [--base REF]

Global options: --root PATH, --json, --limit N, --timeout SEC.
Targets accept path:line[:column], QualifiedClass.method, or symbol_name. Ambiguity must be returned explicitly rather than guessed.

## Architecture

    codeq CLI
      -> per-user Unix-socket daemon (auto-started)
          -> one workspace per git worktree root
              -> basedpyright/pyright LSP for Python
              -> typescript-language-server LSP for TS/JS
          -> git diff for changed ranges
          -> ripgrep fallback for lexical discovery/test heuristics

No persistent semantic graph is built. Language servers own parsing, type resolution, references, symbols, implementations, and call hierarchy. codeq only composes those primitives and bounds output for agent consumption.

## MVP behavior
- find: workspace symbols, fuzzy ranking, rg fallback.
- context: definition, hover, source snippet, direct callers/callees, implementations, references split source/tests, file symbols.
- trace: bounded call-hierarchy traversal with cycle protection.
- review: git changed ranges -> enclosing symbols -> callers/references/tests -> compact impact summary.

## Daemon lifecycle
CLI connects to a Unix socket under XDG runtime or `/tmp/codeq-$UID`. The daemon is auto-spawned. Language servers are lazy-started per (root, language) and reused. codeq maintains no separate source index.

## Language server discovery
Python: basedpyright-langserver, then pyright-langserver.
TypeScript/JavaScript: typescript-language-server on PATH, then project-local node_modules/.bin/typescript-language-server, then a codeq-local managed install if present.

## Correctness principles
Do not invent call edges. Preserve source locations. Mark lexical fallbacks. Return ambiguity. Bound result counts/depth/source. JSON mode emits one stable document.

## Validation
Unit-test LSP framing/dispatch, symbol ranking, target parsing, trace cycle/depth, git diff parsing, and output contracts. Then validate on ~/Quant for Python and TypeScript, review a real diff, verify warm daemon reuse, and verify a fresh Git worktree needs no codeq build/index step.
