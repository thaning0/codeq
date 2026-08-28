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
def _python_index(
    path_text: str,
    mtime_ns: int,
) -> tuple[
    ast.AST | None,
    dict[ast.AST, ast.AST],
    dict[tuple[int, str], tuple[tuple[ast.AST, int], ...]],
]:
    del mtime_ns
    try:
        source = Path(path_text).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=path_text)
    except (OSError, SyntaxError, UnicodeError):
        return None, {}, {}
    parents: dict[ast.AST, ast.AST] = {}
    uses: dict[tuple[int, str], list[tuple[ast.AST, int]]] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
        if isinstance(parent, ast.Name) and isinstance(parent.ctx, ast.Load):
            key = (int(getattr(parent, "lineno", -1)), parent.id)
            uses.setdefault(key, []).append((parent, int(getattr(parent, "col_offset", 0))))
        elif isinstance(parent, ast.Attribute) and isinstance(parent.ctx, ast.Load):
            line = int(getattr(parent, "end_lineno", getattr(parent, "lineno", -1)))
            end_col = int(getattr(parent, "end_col_offset", 0))
            uses.setdefault((line, parent.attr), []).append(
                (parent, max(0, end_col - len(parent.attr.encode("utf-8"))))
            )
        elif isinstance(parent, ast.alias):
            imported_name = parent.name.rsplit(".", 1)[-1]
            uses.setdefault((int(getattr(parent, "lineno", -1)), imported_name), []).append(
                (parent, int(getattr(parent, "col_offset", 0)))
            )
    return tree, parents, {key: tuple(value) for key, value in uses.items()}


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


def _lsp_column_to_utf8_offset(path: Path, line: int, column: int) -> int | None:
    lines = _read_lines(str(path.resolve()), _file_version(path))
    if not (1 <= line <= len(lines)):
        return None
    target_units = max(0, column - 1)
    units = 0
    prefix: list[str] = []
    for char in lines[line - 1]:
        if units == target_units:
            break
        char_units = 2 if ord(char) > 0xFFFF else 1
        if units + char_units > target_units:
            return None
        prefix.append(char)
        units += char_units
    if units != target_units:
        return None
    return len("".join(prefix).encode("utf-8"))


def _python_reference_node(
    path: Path,
    line: int,
    column: int,
    symbol_name: str,
) -> tuple[ast.AST, dict[ast.AST, ast.AST]] | None:
    tree, parents, uses = _python_index(str(path.resolve()), _file_version(path))
    if tree is None:
        return None
    target_col = _lsp_column_to_utf8_offset(path, line, column)
    if target_col is None:
        return None
    exact = [node for node, start_col in uses.get((line, symbol_name), ()) if start_col == target_col]
    if len(exact) != 1:
        return None
    return exact[0], parents


def _call_is_in_scope_body(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    current: ast.AST = call
    while current in parents:
        child = current
        current = parents[child]
        if isinstance(current, ast.Lambda):
            return False
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return child in current.body
    return True


def classify_python_call_reference(
    reference: dict[str, Any],
    symbol_name: str,
) -> bool | None:
    """Return True for a direct call, False for a safe non-call, or None to fall back."""
    path = Path(str(reference.get("path") or "")).resolve()
    if path.suffix.lower() not in {".py", ".pyi"}:
        return None
    line = int(reference.get("line") or 0)
    column = int(reference.get("column") or 1)
    matched = _python_reference_node(path, line, column, symbol_name)
    if matched is None:
        return None
    node, parents = matched
    if isinstance(node, ast.alias):
        return False
    parent = parents.get(node)
    if isinstance(parent, ast.Call) and parent.func is node:
        return True if _call_is_in_scope_body(parent, parents) else None
    # Passing, assigning, returning, or decorating with a callable can create
    # aliases that a later call-hierarchy scan may resolve. Preserve the server
    # result for these cases rather than claiming the reference is a non-call.
    if _python_reason_for_node(node, parents) is not None:
        return None
    return False


@lru_cache(maxsize=512)
def _is_python_property_definition(
    path_text: str,
    mtime_ns: int,
    line: int,
    symbol_name: str,
) -> bool:
    tree, _, _ = _python_index(path_text, mtime_ns)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != symbol_name or int(getattr(node, "lineno", -1)) != line:
            continue
        for decorator in node.decorator_list:
            name = _call_name(decorator.func) if isinstance(decorator, ast.Call) else _call_name(decorator)
            if name in {"property", "cached_property", "setter", "getter", "deleter"}:
                return True
    return False


def is_python_property_definition(path: Path, line: int, symbol_name: str) -> bool:
    resolved = path.resolve()
    return _is_python_property_definition(str(resolved), _file_version(resolved), line, symbol_name)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _python_reason_for_node(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
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


def _python_reason(path: Path, line: int, column: int, symbol_name: str) -> str | None:
    matched = _python_reference_node(path, line, column, symbol_name)
    if matched is None:
        return None
    node, parents = matched
    return _python_reason_for_node(node, parents)


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
