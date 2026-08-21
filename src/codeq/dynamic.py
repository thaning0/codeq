from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .contracts import EVIDENCE_POSSIBLE_DYNAMIC


_DYNAMIC_CONFIDENCE = "possible"


@lru_cache(maxsize=256)
def _read_lines(path_text: str, mtime_ns: int) -> tuple[str, ...]:
    del mtime_ns
    try:
        return tuple(Path(path_text).read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return ()


@lru_cache(maxsize=128)
def _python_index(path_text: str, mtime_ns: int) -> tuple[ast.AST | None, dict[ast.AST, ast.AST]]:
    del mtime_ns
    try:
        source = Path(path_text).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=path_text)
    except (OSError, SyntaxError, UnicodeError):
        return None, {}
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return tree, parents


def _file_version(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _line_text(path: Path, line: int) -> str:
    lines = _read_lines(str(path.resolve()), _file_version(path))
    if 1 <= line <= len(lines):
        return lines[line - 1].rstrip()
    return ""


def _node_contains(root: ast.AST, target: ast.AST) -> bool:
    return any(node is target for node in ast.walk(root))


def _python_symbol_start(node: ast.AST, line: int, symbol_name: str) -> int | None:
    if isinstance(node, ast.Name):
        if (
            node.id == symbol_name
            and isinstance(node.ctx, ast.Load)
            and getattr(node, "lineno", -1) == line
        ):
            return int(getattr(node, "col_offset", 0))
        return None
    if isinstance(node, ast.Attribute):
        if (
            node.attr == symbol_name
            and isinstance(node.ctx, ast.Load)
            and getattr(node, "end_lineno", getattr(node, "lineno", -1)) == line
        ):
            end_col = int(getattr(node, "end_col_offset", 0))
            return max(0, end_col - len(symbol_name))
    return None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _python_reason(path: Path, line: int, column: int, symbol_name: str) -> str | None:
    tree, parents = _python_index(str(path.resolve()), _file_version(path))
    if tree is None:
        return None
    target_col = max(0, column - 1)
    candidates: list[tuple[ast.AST, int]] = []
    for candidate in ast.walk(tree):
        start = _python_symbol_start(candidate, line, symbol_name)
        if start is not None:
            candidates.append((candidate, start))
    if not candidates:
        return None
    node, start_col = min(candidates, key=lambda item: abs(item[1] - target_col))
    if not (start_col <= target_col <= start_col + len(symbol_name)) and abs(start_col - target_col) > 1:
        return None

    non_callback_calls = {
        "Annotated",
        "Literal",
        "cast",
        "isinstance",
        "issubclass",
        "type",
    }
    current: ast.AST = node
    while current in parents:
        parent = parents[current]
        if isinstance(parent, ast.Call):
            if _node_contains(parent.func, node):
                return None
            if _call_name(parent.func) in non_callback_calls:
                return None
            return "callback_argument"
        if isinstance(parent, ast.Dict):
            if any(value is not None and _node_contains(value, node) for value in parent.values):
                return "mapping_value"
        if isinstance(parent, (ast.List, ast.Tuple, ast.Set)):
            return "collection_member"
        if isinstance(parent, ast.AnnAssign):
            if parent.annotation is not None and _node_contains(parent.annotation, node):
                return None
            if parent.value is not None and _node_contains(parent.value, node):
                if isinstance(parent.target, ast.Subscript):
                    return "registry_assignment"
                return "assigned_callable"
        if isinstance(parent, (ast.Assign, ast.NamedExpr)):
            value = parent.value
            if _node_contains(value, node):
                targets = list(parent.targets) if isinstance(parent, ast.Assign) else [parent.target]
                if any(isinstance(target, ast.Subscript) for target in targets):
                    return "registry_assignment"
                return "assigned_callable"
        if isinstance(parent, ast.Return):
            return "returned_callable"
        if isinstance(parent, ast.arg):
            return None
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if parent.returns is not None and _node_contains(parent.returns, node):
                return None
            if any(_node_contains(decorator, node) for decorator in parent.decorator_list):
                return "decorator_reference"
        if isinstance(parent, ast.ClassDef):
            if any(_node_contains(decorator, node) for decorator in parent.decorator_list):
                return "decorator_reference"
            if any(_node_contains(base, node) for base in parent.bases):
                return None
        current = parent
    return None


def _typescript_reason(path: Path, line: int, column: int, symbol_name: str) -> str | None:
    text = _line_text(path, line)
    if not text:
        return None
    if re.match(r"^(?:import|export)\b", text):
        return None
    escaped = re.escape(symbol_name)
    if re.search(rf"\b(?:function|class|interface|type|enum)\s+{escaped}\b", text):
        return None
    if re.search(rf"\b(?:const|let|var)\s+{escaped}\b", text):
        return None

    position = max(0, column - 1)
    nearby = text[position:] if position < len(text) else text
    match = re.search(rf"\b{escaped}\b", nearby)
    if match:
        absolute = position + match.start()
    else:
        match = re.search(rf"\b{escaped}\b", text)
        if not match:
            return None
        absolute = match.start()
    after = text[absolute + len(symbol_name):]
    before = text[:absolute]

    if re.match(r"\s*\(", after):
        return None
    if re.search(r"(?:[{,])\s*[^,{}:]+\s*:\s*$", before):
        return "mapping_value"
    if re.search(r":\s*$", before):
        return None
    # Type positions such as `value: Map<string, Foo[]>`, `): Promise<Foo[]>`,
    # `as Foo`, and generic constraints are references, but not dynamic dispatch.
    if re.search(r":\s*[A-Za-z_$][\w$]*(?:\s*<[^{};=]*)?$", before):
        return None
    if re.search(r"\b(?:as|satisfies|extends|implements)\s*$", before):
        return None
    if re.search(r"\[[^\]]+\]\s*=\s*$", before) or re.search(r"\.\w+\s*=\s*$", before):
        return "registry_assignment"
    if re.search(r"=\s*$", before):
        return "assigned_callable"
    if re.search(r"\breturn\s*$", before):
        return "returned_callable"
    if "(" in before and ")" not in before.rsplit("(", 1)[-1]:
        return "callback_argument"
    if any(char in before + after for char in "[]{}"):
        return "collection_member"
    return None


def classify_dynamic_reference(reference: dict[str, Any], symbol_name: str) -> dict[str, Any] | None:
    path = Path(str(reference.get("path") or "")).resolve()
    line = int(reference.get("line") or 0)
    column = int(reference.get("column") or 1)
    suffix = path.suffix.lower()
    if suffix in {".py", ".pyi"}:
        reason = _python_reason(path, line, column, symbol_name)
    elif suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        reason = _typescript_reason(path, line, column, symbol_name)
    else:
        return None
    if reason is None:
        return None
    return {
        **reference,
        "reason": reason,
        "confidence": _DYNAMIC_CONFIDENCE,
        "evidence": EVIDENCE_POSSIBLE_DYNAMIC,
        "text": _line_text(path, line).strip(),
    }


def classify_dynamic_references(
    references: list[dict[str, Any]],
    symbol_name: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, str]] = set()
    for reference in references:
        classified = classify_dynamic_reference(reference, symbol_name)
        if classified is None:
            continue
        key = (
            str(classified["path"]),
            int(classified["line"]),
            int(classified.get("column") or 1),
            str(classified["reason"]),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(classified)
        if limit is not None and len(out) >= limit:
            break
    return out
