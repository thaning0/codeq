# codeq

A small, CLI-first code-intelligence tool for coding agents.

`codeq` gives an agent a fast semantic first hop for unfamiliar code: locate an implementation, inspect its neighborhood, trace multi-hop calls, review a branch, or search exact runtime/configuration contracts — without building or maintaining a repository graph.

> **Status:** `1.0.0rc1` is the active release candidate. The four-command surface is feature-frozen for 1.0.

## Why codeq

Coding agents repeatedly ask the same questions:

- Where is this implementation?
- Who calls it, and what does it call?
- Which tests and references are related?
- What does this branch affect?
- Where else does this URL, environment variable, registry key, or SQL name appear?

A language server can answer many of these questions, but driving it directly takes multiple low-level requests and process-management work. Repository graph systems can answer more, but introduce another index, database, rebuild lifecycle, and worktree state.

`codeq` takes a narrower approach:

- **CLI first** — four shell commands, designed for agents and humans.
- **Semantic when possible** — Python and TypeScript/JavaScript relationships come from mature language servers.
- **Exact text when semantics stop** — URLs, environment variables, YAML, Shell, SQL, registry keys, and other runtime contracts stay raw textual evidence.
- **No persistent code graph** — no graph database, embedding index, or per-worktree rebuild.
- **Fail closed** — explicit paths and qualified symbols are never silently degraded into unrelated fuzzy matches.
- **Bounded output** — one `--limit` budget keeps agent context predictable while preserving full counts and truncation metadata.

## Quick start

### Requirements

`codeq` itself has no Python runtime dependencies. You need:

- Python 3.12+
- Git
- ripgrep (`rg`)
- a language server for the languages you want to analyze semantically

For Python, install `basedpyright` (preferred) or `pyright`. For TypeScript/JavaScript, install `typescript-language-server` and TypeScript.

```bash
uv tool install basedpyright
npm install -g typescript typescript-language-server
```

### Install codeq

```bash
git clone https://github.com/thaning0/codeq.git
cd codeq
uv tool install .
```

If the uv tool directory is not already on `PATH`:

```bash
uv tool update-shell
```

Verify the installation:

```bash
codeq --version
codeq --help
```

The current release candidate reports:

```text
codeq 1.0.0rc1
```

## The workflow

Start in any Git repository or worktree:

```text
find implementation
      ↓
context definition + direct neighborhood
      ↓
trace multi-hop callers/callees when needed
      ↓
review branch/worktree impact
      ↓
rg / direct source inspection for the remaining runtime edge cases
```

Choose a command by question:

| Question | Command |
| --- | --- |
| Where is this code? | `codeq find QUERY` |
| What surrounds this symbol/location/file? | `codeq context TARGET` |
| Who calls it / what does it call across multiple hops? | `codeq trace TARGET --in/--out` |
| What does this branch or worktree affect? | `codeq review --base REF` |

## Commands

### `find` — locate code or exact text

Semantic discovery:

```bash
codeq find RetryPolicy
codeq find 'request retry policy' --limit 8
codeq find RetryPolicy --kind class
```

Exact working-tree text search:

```bash
codeq find --text 'DATABASE_URL'
codeq find --text '/api/orders' --path frontend --exclude-tests
codeq find --text 'DEPLOYMENTS' --glob '*.py' --glob '*.yaml'
```

Text mode searches **tracked plus non-ignored untracked files**, so newly created YAML, Shell, SQL, and configuration files are visible before `git add`. `--path` and `--glob` are repeatable filters; `--exclude-tests` removes test paths from both results and counts.

### `context` — inspect one semantic neighborhood

```bash
codeq context RetryPolicy.should_retry
codeq context src/api/orders.py:84
codeq context src/api/orders.py:84:21
```

For symbols and locations, `context` returns bounded definition source, hover/signature, direct callers and callees, implementations, references, tests, and possible dynamic callback/registry evidence.

Position semantics are deliberate:

- `PATH:LINE` → enclosing function/method/type context.
- `PATH:LINE:COLUMN` → prefer the definition of the symbol under the cursor, while preserving a small snippet around the requested call site.

Whole-file context uses progressive disclosure:

```bash
codeq context src/services/orders.py
codeq context src/services/orders.py --container OrderService
codeq context src/services/orders.py --kind method --limit 20
codeq context src/services/orders.py --topology
```

Runtime/text contracts can be attached without mixing them into semantic references:

```bash
codeq context OrderService.stream_logs \
  --lexical-references '/logs/stream' \
  --path frontend \
  --exclude-tests
```

### `trace` — follow a call hierarchy

```bash
codeq trace RetryPolicy.should_retry --in --depth 2
codeq trace OrderService.submit --out --depth 3
```

- `--in` walks toward callers and entry points.
- `--out` walks toward callees and implementation.
- `--depth 0` returns the root only.
- `--node-limit N` is a hard cap on emitted nodes.

Traversal is cycle-protected and restricted to repository source.

### `review` — analyze the current diff/worktree

```bash
codeq review --base HEAD~1
codeq review --base origin/main --merge-base
codeq review --base origin/main --merge-base --limit 15 --json
```

`review` combines Git truth with semantic analysis:

- added / modified / deleted / renamed files
- staged, unstaged, and non-ignored untracked files
- changed semantic owners
- callers and references
- possible dynamic references
- likely tests
- affected files

For feature branches, prefer `--merge-base`; codeq records both the requested base and the resolved merge-base SHA.

Deleted source files receive conservative **base-side lexical** residual-reference analysis. Pure renames are analyzed on the new path using current importers and semantic references. These evidence types remain explicit instead of being presented as the same thing.

## Agent setup

`codeq` is self-describing; it does not require an MCP server or a codeq-specific skill.

A repository can opt in with one line in `AGENTS.md`:

```text
When exploring, understanding, tracing, or reviewing code, use the `codeq` CLI first; run `codeq --help` for usage.
```

Agents can discover the rest from:

```bash
codeq --help
codeq find --help
codeq context --help
codeq trace --help
codeq review --help
```

Plain text is the default. Use `--json` when the caller wants a stable machine-readable response.

## Supported analysis

| Source | Semantic analysis | Exact text search |
| --- | --- | --- |
| Python / `.py`, `.pyi` | basedpyright or pyright | Yes |
| TypeScript / JavaScript | typescript-language-server | Yes |
| YAML / Shell / SQL / docs / config | No | Yes |
| Other Git-visible text | No | Yes |

Language servers remain the semantic authority for definitions, references, implementations, and call hierarchy. `codeq` does not invent a second semantic graph.

## Safety and correctness contracts

These behaviors are part of the 1.0 compatibility boundary:

- Qualified targets such as `Class.method` are **fail-closed**.
- Explicit path-like targets never fall back to fuzzy symbol search.
- Missing files return `not_found`.
- Unsupported source-file languages return `unsupported_language` rather than an unrelated symbol.
- `PATH:LINE` and `PATH:LINE:COLUMN` keep their distinct semantics described above.
- Text/lexical evidence is kept separate from LSP semantic references.
- Worktrees are independent language-server workspaces; no repository-local graph state is created.

JSON responses use `schema_version: 1`. Query outcomes use exit code `0` for success, `1` for query outcomes such as `not_found`/`ambiguous`, and `2` for CLI/runtime failures.

See [1.0 readiness and compatibility policy](docs/codeq-1.0-readiness.md) for the full frozen contract.

## How it works

```text
Agent / shell
     │
     ▼
 codeq CLI
     │  Unix socket
     ▼
 small daemon
  ├─ basedpyright / pyright
  ├─ typescript-language-server
  ├─ rg
  └─ git
```

The daemon keeps relevant language servers warm, caches only safe file-local document symbols, and releases inactive workspaces. It does **not** maintain a persistent repository graph or embedding index.

## Performance and validation

The release candidate is validated against a large Python/TypeScript monorepo and historical real-agent workflows.

Representative committed results:

- warm semantic `context` P95: **172.3 ms**
- warm incoming `trace` P95: **329.8 ms**
- cold semantic context/trace P95: **< 4 s**
- historical actionable CRG-call mapping: **93.3%**
- sampled navigation workflows with unsupported fallback: **0 / 50**
- anonymized historical concrete query validation: **30 / 30 `ok`**

These are project-specific benchmark results, not universal latency guarantees.

Details:

- [Quant cold/warm benchmark](benchmarks/0.5.1-quant.md)
- [Historical workflow replay](benchmarks/0.5.2-workflows.md)
- [RC1 readiness gate](benchmarks/1.0.0rc1-readiness.md)
- [Full validation record](VALIDATION.md)

## Boundaries

`codeq` is intentionally not a complete static-analysis platform. It does not try to provide:

- a persistent dependency/call graph database
- embeddings or vector search
- architecture/community detection
- named flow databases
- generic risk scores
- runtime tracing
- automatic refactoring
- cross-language synthetic call graphs
- MCP or agent framework integration

Dynamic dispatch can still be unknowable statically. Configuration, reflection, HTTP strings, registries, SQL names, and other runtime relationships are why exact text search and direct source inspection remain first-class fallbacks.

## Development

```bash
git clone https://github.com/thaning0/codeq.git
cd codeq
uv sync
uv run python -W error -m unittest discover -s tests
uv run basedpyright --level error src/codeq tests benchmarks
uv build
uv run python benchmarks/readiness_gate.py
```

The 1.0 RC policy is blocker-only: before stable 1.0, runtime/analysis behavior should change only for silent correctness problems, compatibility-contract regressions, or repeated severe performance/lifecycle failures.

## Documentation

- [1.0 readiness and compatibility policy](docs/codeq-1.0-readiness.md)
- [Validation history](VALIDATION.md)
- [Initial design plan](PLAN.md)
- [Benchmarks](benchmarks/)

For usage details, prefer the CLI help because it is tested together with the implementation:

```bash
codeq COMMAND --help
```
