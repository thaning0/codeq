# CodeQ 2.0 dependency audit

CodeQ remains one Rust package and one shipping binary. `cargo tree
--duplicates` reports no duplicate package versions. The release uses a pinned
`Cargo.lock`; the toolchain is pinned by `rust-toolchain.toml`.

| Dependency | Why it remains |
| --- | --- |
| `clap` | Typed public CLI parsing and generated validation. Replacing it would hand-roll a large, compatibility-sensitive parser. |
| `serde`, `serde_json` | Stable JSON machine contract and the minimal JSON-RPC/LSP messages CodeQ consumes. |
| `globset` | Compiled repeated `--glob` filters with established glob semantics. |
| `nix` | Linux peer credentials, UID/process/signal/socket operations that require correct OS bindings. Only the needed features are enabled. |
| `rusqlite` with `bundled` | Contentless in-memory SQLite FTS5. Bundling gives the native artifact a known FTS5 capability without a system SQLite or Python dependency. |
| `signal-hook` | Safe signal registration for daemon shutdown. |
| `tempfile` (development only) | Private, automatically cleaned black-box test/benchmark workspaces and runtimes. It is not linked into the shipping binary. |

The direct crates are established ecosystem primitives and point to maintained
public repositories through their crates.io metadata. At the audit date, the
declared major/minor lines use current releases (`clap 4.6`, `globset 0.4`,
`nix 0.31`, `rusqlite 0.40`, `signal-hook 0.4`, and `tempfile 3`). Serde remains
on its stable `1.x` line.

External product boundaries are deliberately not Rust dependencies:

- Git owns repository and diff semantics.
- ripgrep owns exact working-tree text search.
- BasedPyright/Pyright and TypeScript Language Server own language semantics.
- SQLite owns FTS; CodeQ uses it in memory and does not introduce a persistent
  index.

There is no parser framework, async runtime, HTTP stack, persistent database,
graph engine, embedding client, or language-specific Rust parser in the
dependency graph. Adding one requires a concrete product or ownership boundary,
not anticipation of future features.
