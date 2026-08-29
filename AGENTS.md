# Repository instructions

When exploring, understanding, tracing, or reviewing code, use the `codeq` CLI first; if a command is unfamiliar, run `codeq COMMAND --help`.

## Updating the local `codeq` installation

The globally installed `codeq` command used by other agents must be a stable
snapshot. Do not install that command with `--editable`, because uncommitted
source changes and branch switches would immediately affect unrelated agent
sessions.

After release-ready changes have passed validation and the package version has
been updated consistently, refresh the local tool from the repository:

```bash
uv tool install --force /path/to/codeq
codeq --version
codeq --root /path/to/repository context path/to/file.py:10 --limit 1
```

Keep the versions in `pyproject.toml`, `src/codeq/__init__.py`, and `uv.lock` in
sync before reinstalling. A new version also lets the client detect and restart
an older running daemon on the first normal query.

For development against a changing working tree, run the repository version
explicitly instead of replacing the stable global command:

```bash
uv run --project /path/to/codeq codeq --root /path/to/repository context path/to/file.py:10
```

Never commit machine-specific absolute paths in documentation or examples.
