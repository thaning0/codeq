from __future__ import annotations

import ast
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from .util import language_for, loc

_TS_IMPORT_RE = re.compile(
    r"(?:\b(?:import|export)\b(?:[^\"']*?\bfrom\s*)?|\bfrom\s*|\brequire\s*\(|\bimport\s*\()"
    r"[\"'](?P<specifier>[^\"']+)[\"']"
)

_TS_EXTENSIONS = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".d.ts")
_PY_EXTENSIONS = ("", ".py", ".pyi")


def extract_imports(path: Path) -> list[dict[str, Any]]:
    """Extract direct source-level imports without building a repository graph."""
    path = path.resolve()
    language = language_for(path)
    if language == "python":
        return _python_imports(path)
    if language in {"typescript", "typescriptreact", "javascript", "javascriptreact"}:
        return _typescript_imports(path)
    return []


def _python_imports(path: Path) -> list[dict[str, Any]]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return []
    lines = source.splitlines()
    out: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                text = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
                column = text.find(alias.name)
                out.append(
                    {
                        "specifier": alias.name,
                        "names": [alias.asname or alias.name.split(".")[-1]],
                        "kind": "import",
                        "line": node.lineno,
                        "column": column + 1 if column >= 0 else node.col_offset + 1,
                    }
                )
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            text = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
            needle = node.module or (node.names[0].name if node.names else "")
            column = text.find(needle) if needle else -1
            out.append(
                {
                    "specifier": module,
                    "names": [alias.asname or alias.name for alias in node.names],
                    "kind": "from",
                    "line": node.lineno,
                    "column": column + 1 if column >= 0 else node.col_offset + 1,
                }
            )
    out.sort(key=lambda item: (int(item["line"]), str(item["specifier"])))
    return out


def _typescript_imports(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for lineno, line in enumerate(lines, start=1):
        for match in _TS_IMPORT_RE.finditer(line):
            specifier = match.group("specifier")
            start = match.start("specifier")
            prefix = line[: match.start()]
            kind = "export" if re.search(r"\bexport\b", prefix + match.group(0)[:20]) else "import"
            out.append(
                {
                    "specifier": specifier,
                    "names": [],
                    "kind": kind,
                    "line": lineno,
                    "column": start + 1,
                }
            )
    return out


def resolve_import_specifier(importer: Path, specifier: str, project_root: Path) -> list[Path]:
    """Resolve local Python/TypeScript module specifiers to repository files."""
    importer = importer.resolve()
    project_root = project_root.resolve()
    language = language_for(importer)
    if language == "python":
        return _resolve_python_specifier(importer, specifier, project_root)
    if language in {"typescript", "typescriptreact", "javascript", "javascriptreact"}:
        return _resolve_typescript_specifier(importer, specifier, project_root)
    return []


def _existing_module_paths(base: Path, extensions: tuple[str, ...], index_names: tuple[str, ...]) -> list[Path]:
    candidates: list[Path] = []
    for extension in extensions:
        candidate = Path(str(base) + extension) if extension else base
        if candidate.is_file():
            candidates.append(candidate.resolve())
    if base.is_dir():
        for name in index_names:
            candidate = base / name
            if candidate.is_file():
                candidates.append(candidate.resolve())
    out: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _resolve_python_specifier(importer: Path, specifier: str, project_root: Path) -> list[Path]:
    if not specifier:
        return []
    level = len(specifier) - len(specifier.lstrip("."))
    module = specifier.lstrip(".")
    parts = [part for part in module.split(".") if part]
    bases: list[Path] = []
    if level:
        base = importer.parent
        for _ in range(max(0, level - 1)):
            base = base.parent
        bases.append(base.joinpath(*parts))
    else:
        bases.append(project_root.joinpath(*parts))
        bases.append((project_root / "src").joinpath(*parts))
    out: list[Path] = []
    for base in bases:
        out.extend(_existing_module_paths(base, _PY_EXTENSIONS, ("__init__.py", "__init__.pyi")))
    return list(dict.fromkeys(out))


@lru_cache(maxsize=32)
def _tsconfig_paths(project_root_text: str, mtime_ns: int) -> tuple[str, dict[str, list[str]]]:
    del mtime_ns
    project_root = Path(project_root_text)
    config = project_root / "tsconfig.json"
    try:
        payload = json.loads(config.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return ".", {}
    compiler = payload.get("compilerOptions") or {}
    base_url = str(compiler.get("baseUrl") or ".")
    paths = compiler.get("paths") or {}
    normalized = {
        str(pattern): [str(value) for value in values]
        for pattern, values in paths.items()
        if isinstance(values, list)
    }
    return base_url, normalized


def _resolve_typescript_specifier(importer: Path, specifier: str, project_root: Path) -> list[Path]:
    bases: list[Path] = []
    if specifier.startswith("."):
        bases.append((importer.parent / specifier).resolve())
    else:
        config = project_root / "tsconfig.json"
        try:
            mtime = config.stat().st_mtime_ns
        except OSError:
            mtime = 0
        base_url, paths = _tsconfig_paths(str(project_root), mtime)
        for pattern, replacements in paths.items():
            wildcard = ""
            if "*" in pattern:
                prefix, suffix = pattern.split("*", 1)
                if not (specifier.startswith(prefix) and specifier.endswith(suffix)):
                    continue
                wildcard = specifier[len(prefix) : len(specifier) - len(suffix) if suffix else None]
            elif specifier != pattern:
                continue
            for replacement in replacements:
                mapped = replacement.replace("*", wildcard)
                bases.append((project_root / base_url / mapped).resolve())
    out: list[Path] = []
    for base in bases:
        out.extend(
            _existing_module_paths(
                base,
                _TS_EXTENSIONS,
                ("index.ts", "index.tsx", "index.js", "index.jsx", "index.d.ts"),
            )
        )
    return list(dict.fromkeys(out))


def importer_candidate_hits(root: Path, target: Path, limit: int = 400) -> list[dict[str, Any]]:
    """Find import-looking lines likely to reference target; LSP verifies them later."""
    target = target.resolve()
    tokens: list[str] = []
    if target.stem not in {"index", "__init__"}:
        tokens.append(target.stem)
    if target.parent.name and target.parent.name not in tokens:
        tokens.append(target.parent.name)
    if not tokens:
        return []

    cmd = [
        "rg", "--json", "-n", "--hidden", "--max-count", "8",
        "-g", "*.py", "-g", "*.pyi", "-g", "*.ts", "-g", "*.tsx",
        "-g", "*.js", "-g", "*.jsx", "-g", "!node_modules/**",
        "-g", "!.git/**", "-g", "!.next/**", "-g", "!dist/**", "-g", "!build/**",
        "-g", "!Quant-worktrees/**", "-g", "!worktrees/**", "-g", "!.worktrees/**",
    ]
    for token in tokens:
        cmd.extend(["-e", re.escape(token)])
    cmd.append(".")
    try:
        proc = subprocess.run(cmd, cwd=str(root), text=True, capture_output=True, check=False, timeout=8.0)
    except (OSError, subprocess.TimeoutExpired):
        return []

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw in proc.stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data") or {}
        rel = (data.get("path") or {}).get("text")
        lineno = data.get("line_number")
        text = str((data.get("lines") or {}).get("text") or "").rstrip()
        if not rel or not lineno or not _looks_like_import(text):
            continue
        path = (root / rel).resolve()
        if path == target:
            continue
        key = (str(path), int(lineno))
        if key in seen:
            continue
        seen.add(key)
        lowered = text.lower()
        token = next((candidate for candidate in tokens if candidate.lower() in lowered), tokens[0])
        column = lowered.find(token.lower()) + 1
        out.append(loc(path, int(lineno), max(1, column), text=text, token=token))
        if len(out) >= limit:
            break
    return out


def _looks_like_import(line: str) -> bool:
    stripped = line.strip()
    return bool(
        re.search(r"\b(?:from|import|export|require)\b", stripped)
        or re.search(r"\bimport\s*\(", stripped)
    )
