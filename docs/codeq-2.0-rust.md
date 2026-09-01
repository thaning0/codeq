# codeq 2.0 Rust migration

Development happens only on the long-lived `2.0-rust` branch. The behavioral
oracle is `1.0.0rc13` at commit
`56fadc0a3485531da83851fbde69f2dc1126463b`; `main` remains frozen until the
final cutover.

The active development toolchain is pinned by `rust-toolchain.toml`. Cargo owns
the CodeQ build, dependency lock, tests, and release artifacts. Git, ripgrep,
SQLite FTS5, and external language servers remain explicit product boundaries.

During migration, the Rust daemon namespace is `codeq-2.0-rust-dev` and an
explicit development runtime directory is selected with `CODEQ2_RUNTIME_DIR`.
It must never connect to, replace, shut down, or reuse a stable 1.x daemon.

The Rust CLI currently fails closed for unimplemented semantic commands. A
slice is considered complete only when black-box fixtures pass against both an
explicit RC13 oracle executable and the Rust candidate. Transitional Python
implementation and harness files remain only until their Rust replacements and
the acceptance matrix are proven; they are removed before the 2.0 cutover.

Development checks:

```bash
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test
cargo build --release
```

Run the committed corpus against the Rust candidate itself as part of ordinary
tests. To compare the frozen Python oracle with the current Rust binary, pass
the oracle explicitly; the candidate defaults to Cargo's just-built `codeq`:

```bash
cargo test --test parity -- --oracle /path/to/frozen/codeq
```

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
