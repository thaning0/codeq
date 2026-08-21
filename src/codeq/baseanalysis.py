from __future__ import annotations

import ast
import re
from typing import Any

_TS_DECL_RE = re.compile(
    r"^\s*(?P<export>export\s+)?(?:default\s+)?(?:declare\s+)?"
    r"(?:(?P<async>async)\s+)?(?P<kind>function|class|interface|type|enum|const|let|var)\s+"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\b"
)


def extract_base_declarations(text: str, language: str | None) -> list[dict[str, Any]]:
    """Extract conservative top-level declarations from base-side source text.

    This exists only for files unavailable in the current worktree. It intentionally
    avoids pretending to be a full parser/type-checker replacement.
    """
    if language == "python":
        return _python_declarations(text)
    if language in {"typescript", "typescriptreact", "javascript", "javascriptreact"}:
        return _typescript_declarations(text)
    return []


def _python_declarations(text: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append({"name": node.name, "kind": "Function", "line": node.lineno})
        elif isinstance(node, ast.ClassDef):
            out.append({"name": node.name, "kind": "Class", "line": node.lineno})
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets: list[ast.expr]
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            else:
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    out.append({"name": target.id, "kind": "Constant", "line": node.lineno})
    return out


def _typescript_declarations(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    kind_names = {
        "function": "Function",
        "class": "Class",
        "interface": "Interface",
        "type": "TypeParameter",
        "enum": "Enum",
        "const": "Constant",
        "let": "Variable",
        "var": "Variable",
    }
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _TS_DECL_RE.match(line)
        if not match:
            continue
        kind = match.group("kind")
        name = match.group("name")
        exported = bool(match.group("export"))
        if kind in {"const", "let", "var"} and not exported and not name.isupper():
            continue
        out.append({"name": name, "kind": kind_names[kind], "line": line_number})
    return out
