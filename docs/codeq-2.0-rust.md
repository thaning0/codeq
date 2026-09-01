# codeq 2.0 Rust migration

Development happens only on the long-lived `2.0-rust` branch. The behavioral
oracle is the immutable `v1.0.0-rc13` tag at commit
`56fadc0a3485531da83851fbde69f2dc1126463b`; `main` remains frozen until the
final cutover.

The active development toolchain is pinned by `rust-toolchain.toml`. Cargo owns
the CodeQ build, dependency lock, tests, and release artifacts. Git, ripgrep,
SQLite FTS5, and external language servers remain explicit product boundaries.

During migration, the Rust daemon namespace is `codeq-2.0-rust-dev` and an
explicit development runtime directory is selected with `CODEQ2_RUNTIME_DIR`.
It must never connect to, replace, shut down, or reuse a stable 1.x daemon.
The default Linux transport is an isolated abstract Unix socket; an explicit
runtime selects a private `0700` directory and `0600` filesystem socket.
`CODEQ2_DAEMON_LOG` opts into a private daemon log; no log is created by
default.

A slice is considered complete only when black-box fixtures pass against both
an explicit RC13 oracle executable and the Rust candidate. The normalized
oracle results are committed in `compat/expected.json`, so ordinary Rust tests
remain independent of the Python environment. Transitional Python
implementation and harness files remain only until their Rust replacements and
the acceptance matrix are proven; they are removed before the 2.0 cutover.

Development checks:

```bash
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test
cargo build --release
```

The runtime contract can also target an arbitrary built executable. It covers
filesystem and abstract sockets, same-UID peer validation, protocol mismatch,
automatic daemon startup/restart, `--no-daemon`, idle exit, signals, and socket
cleanup:

```bash
cargo test --test runtime_contract -- --codeq /path/to/codeq
```

Ordinary tests compare the Rust candidate with the committed, normalized RC13
results and need no Python runtime. To perform a live dual-run against the
frozen Python oracle, pass it explicitly; the candidate defaults to Cargo's
just-built `codeq`:

```bash
cargo test --test parity -- --oracle /path/to/frozen/codeq
```

Only after verifying that executable against `v1.0.0-rc13`, refresh the
committed results with `--update-expected`. The snapshot records the frozen Git
commit and rejects a different oracle identity.

Both `--oracle` and `--candidate` accept arbitrary executable paths. Add
`--report PATH` to retain the JSON difference report, or `--verbose` to print
it directly. Executable paths are runtime inputs and must not be committed.

The end-to-end readiness workload is also executable-agnostic:

```bash
cargo bench --bench readiness -- \
  --codeq /path/to/codeq \
  --root /path/to/representative/repository \
  --reps 3 \
  --output /path/to/result.json
```

Cold cases use `--no-daemon`; warm cases reuse a private filesystem runtime.
The report measures the query process, daemon, and child language-server RSS
separately, then fails if any process carrying that private runtime survives the
cleanup window.
