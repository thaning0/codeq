# Repository instructions

When exploring, understanding, tracing, or reviewing code, use the `codeq` CLI first; if a command is unfamiliar, run `codeq COMMAND --help`.

## Updating the local `codeq` installation

The globally installed `codeq` command used by other agents must be a stable
snapshot. Build and test release-ready changes before replacing it; development
commands must continue to target Cargo's working-tree binary explicitly.

After release-ready changes have passed validation and the package version has
been updated consistently, refresh the local tool from the repository:

```bash
cargo install --force --locked --path /path/to/codeq
codeq --version
codeq --root /path/to/repository context path/to/file.py:10 --limit 1
```

Keep `Cargo.toml` and `Cargo.lock` in sync before reinstalling. A new version
also lets the client detect and restart an older 2.x daemon on the first normal
query. The isolated 2.x namespace prevents that restart from touching 1.x.

For development against a changing working tree, run the repository version
explicitly instead of replacing the stable global command:

```bash
cargo run --manifest-path /path/to/codeq/Cargo.toml -- \
  --root /path/to/repository context path/to/file.py:10
```

Never commit machine-specific absolute paths in documentation or examples.
