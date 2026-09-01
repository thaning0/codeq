# CodeQ 2.0 Rust cutover

CodeQ 2.0 is one Rust package and one self-contained native executable. The
active implementation, tests, benchmarks, build, and release workflows are
Cargo-owned. The only tracked Python files are inert language-analysis inputs
under `compat/corpus/`; CodeQ never executes them as project tooling.

The immutable 1.x compatibility oracle is `v1.0.0-rc13` at commit
`56fadc0a3485531da83851fbde69f2dc1126463b`. Its normalized black-box outcomes
are committed in `compat/expected.json`, so ordinary 2.0 validation does not
need a Python environment. Git history and the tag retain the former Python
implementation.

## Toolchain and boundaries

`rust-toolchain.toml` pins Rust 1.98. Cargo owns dependency resolution through
`Cargo.toml` and `Cargo.lock`. SQLite FTS5 is bundled in the native executable.
CodeQ continues to delegate language semantics to BasedPyright/Pyright and
TypeScript Language Server, exact text to ripgrep, and repository/diff behavior
to Git.

There is one crate, no generic LSP framework, no persistent index or graph, and
no migration dispatch between Python and Rust. See
`docs/dependency-audit.md` for the direct-dependency rationale.

## 1.x and 2.x daemon compatibility

The 2.x default abstract socket namespace is `codeq-2-$UID-p1`; the frozen 1.x
namespace is `codeq-$UID-p1`. They cannot discover, reuse, replace, or stop each
other. This separation is permanent, so upgrade, downgrade, and side-by-side
validation are safe:

- Installing 2.x leaves a running 1.x daemon untouched. The first 2.x query
  starts or reuses only a 2.x daemon.
- Reinstalling 1.x leaves a running 2.x daemon untouched and reconnects only to
  the 1.x namespace.
- A 2.x executable/version mismatch can restart only a daemon in the 2.x
  namespace.
- `CODEQ2_RUNTIME_DIR` explicitly selects a private 2.x filesystem socket for
  restricted or isolated network namespaces. It must not point at a 1.x
  runtime directory.

Runtime directories use mode `0700`, filesystem sockets use `0600`, and both
ends validate the peer UID. Restricted runners that reject Unix sockets fall
back to one-shot execution. `CODEQ2_DAEMON_LOG` opts into a private diagnostic
log; no daemon log is created by default.

## Validation

The normal release matrix is entirely Rust/Cargo based:

```bash
cargo fmt --all -- --check
cargo clippy --locked --all-targets --all-features -- -D warnings
cargo test --locked --all-targets --all-features
cargo build --locked --release
```

The parity harness accepts arbitrary executables. Without `--oracle`, it checks
the just-built candidate against the committed normalized RC13 snapshot:

```bash
cargo test --test parity
cargo test --test parity -- \
  --oracle /path/to/frozen/rc13/codeq \
  --candidate /path/to/rust/codeq \
  --report /path/to/parity-report.json
```

The exact oracle commit and executable identity are recorded; only timing,
PIDs, temporary roots, and runtime paths are normalized. Ordering and semantic
content remain contractual.

Lifecycle and workspace contracts can target an installed artifact:

```bash
cargo test --test runtime_contract -- --codeq /path/to/codeq
cargo test --test workspace_contract -- --codeq /path/to/codeq
```

The end-to-end performance and agent workflow tools also accept an arbitrary
executable:

```bash
cargo run --release --example readiness -- \
  --codeq /path/to/codeq --root /path/to/representative/repository \
  --reps 3 --output /path/to/readiness.json

cargo run --release --example workflow-replay -- \
  --codeq /path/to/codeq --root /path/to/representative/repository \
  --workload benchmarks/workflows.json --output /path/to/workflows.json

cargo run --release --example readiness-gate -- \
  --performance /path/to/readiness.json \
  --workflow-replay /path/to/workflows.json \
  --output /path/to/gate.json --markdown /path/to/gate.md
```

Cold readiness cases use one-shot execution; warm cases reuse a private 2.x
runtime. Reports separate the Rust query process, daemon, and child language
server RSS, enforce hard timeouts, and fail if a process carrying the private
runtime survives cleanup.

`docs/test-migration-ledger.md` records the durable replacement or explicit
retirement decision for every removed Python test and benchmark program.

## Release

CI builds and tests from the lockfile. A `v*` tag builds the supported
`x86_64-unknown-linux-gnu` artifact used on Linux and WSL, packages the native
binary with its SHA-256 checksum, and attaches both to the GitHub release. The
`2.0.0` tag is created only after the `2.0-rust -> main` cutover is accepted.
