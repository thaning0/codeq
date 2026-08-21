from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse


SYMBOL_KINDS = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
    6: "Method", 7: "Property", 8: "Field", 9: "Constructor",
    10: "Enum", 11: "Interface", 12: "Function", 13: "Variable",
    14: "Constant", 15: "String", 16: "Number", 17: "Boolean",
    18: "Array", 19: "Object", 20: "Key", 21: "Null",
    22: "EnumMember", 23: "Struct", 24: "Event", 25: "Operator",
    26: "TypeParameter",
}

SOURCE_LANG = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".mjs": "javascript",
    ".cjs": "javascript",
}


def git_root(path: str | Path) -> Path:
    path = Path(path).expanduser().resolve()
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode == 0:
        return Path(proc.stdout.strip()).resolve()
    return path if path.is_dir() else path.parent


def path_to_uri(path: str | Path) -> str:
    p = Path(path).resolve()
    return "file://" + quote(str(p), safe="/:")


def uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"unsupported URI: {uri}")
    return Path(unquote(parsed.path)).resolve()


def language_for(path: str | Path) -> str | None:
    return SOURCE_LANG.get(Path(path).suffix.lower())


def is_test_path(path: str | Path) -> bool:
    p = Path(path)
    s = p.as_posix().lower()
    name = p.name.lower()
    return (
        "/tests/" in f"/{s}/"
        or "/test/" in f"/{s}/"
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
        or "__tests__" in s
    )


def loc(path: str | Path, line: int, column: int = 1, **extra: Any) -> dict[str, Any]:
    out = {"path": str(Path(path).resolve()), "line": int(line), "column": int(column)}
    out.update(extra)
    return out


def lsp_location(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    uri = raw.get("uri") or raw.get("targetUri")
    rng = raw.get("range") or raw.get("targetSelectionRange") or raw.get("targetRange")
    if not uri and isinstance(raw.get("location"), dict):
        return lsp_location(raw["location"])
    if not uri:
        return None
    start = (rng or {}).get("start", {})
    try:
        path = uri_to_path(uri)
    except ValueError:
        return None
    return loc(path, int(start.get("line", 0)) + 1, int(start.get("character", 0)) + 1)


def symbol_kind(value: Any) -> str:
    try:
        return SYMBOL_KINDS.get(int(value), f"Kind{value}")
    except (TypeError, ValueError):
        return str(value or "Unknown")


def identifier_tokens(query: str) -> list[str]:
    # Python's Unicode-aware \w keeps short natural-language queries useful when
    # source comments/docstrings contain non-ASCII terms, while preserving normal
    # identifier behavior for Python/TypeScript names.
    tokens = re.findall(r"[^\W\d_]\w*|_[A-Za-z0-9_]+", query, flags=re.UNICODE)
    seen: set[str] = set()
    out: list[str] = []
    for token in sorted(tokens, key=len, reverse=True):
        low = token.lower()
        if low not in seen and len(token) >= 2:
            seen.add(low)
            out.append(token)
    return out


def fuzzy_score(query: str, name: str, container: str = "", path: str = "") -> int:
    q = query.strip().lower()
    n = name.lower()
    c = container.lower()
    p = path.lower()
    combined = f"{c}.{n}".strip(".")
    if not q:
        return 0
    if q == combined or q == n:
        return 10000
    if combined.endswith(q):
        return 9000
    if n.startswith(q):
        return 8000
    if q in n:
        return 7000
    tokens = identifier_tokens(q)
    if not tokens:
        return 0
    hay = f"{combined} {p}".lower()
    coverage = sum(1 for token in tokens if token.lower() in hay)
    if coverage == 0:
        return 0
    return coverage * 1000 + min(len(n), 200)


def source_snippet(
    path: str | Path,
    line: int,
    before: int = 2,
    after: int = 10,
    *,
    max_chars: int | None = None,
    max_line_chars: int | None = None,
) -> dict[str, Any]:
    p = Path(path)
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"start_line": line, "text": "", "truncated": False}
    start = max(1, line - before)
    end = min(len(lines), line + after)
    rendered: list[str] = []
    line_truncated = False
    for idx in range(start, end + 1):
        content = lines[idx - 1]
        if max_line_chars is not None and len(content) > max_line_chars:
            keep = max(0, max_line_chars - 3)
            content = content[:keep] + ("..." if max_line_chars >= 3 else "")
            line_truncated = True
        rendered.append(f"{idx:>5}  {content}")
    numbered = "\n".join(rendered)
    payload_truncated = False
    if max_chars is not None and len(numbered) > max_chars:
        keep = max(0, max_chars - 3)
        numbered = numbered[:keep] + ("..." if max_chars >= 3 else "")
        payload_truncated = True
    return {
        "start_line": start,
        "end_line": end,
        "text": numbered,
        "truncated": line_truncated or payload_truncated,
    }


def guess_symbol_column(path: str | Path, line: int, preferred: str | None = None) -> int:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[line - 1]
    except (OSError, IndexError):
        return 0
    if preferred:
        idx = text.find(preferred)
        if idx >= 0:
            return idx
    match = re.search(r"\b(?:async\s+def|def|class|function|interface|type|const|let|var)\s+([A-Za-z_$][\w$]*)", text)
    if match:
        return match.start(1)
    match = re.search(r"[A-Za-z_$][\w$]*", text)
    return match.start() if match else 0


_TARGET_RE = re.compile(r"^(?P<path>.+?\.(?:pyi?|tsx?|jsx?|mjs|cjs)):(?P<line>\d+)(?::(?P<col>\d+))?$")
_PATH_LIKE_SUFFIXES = set(SOURCE_LANG) | {
    ".sh", ".bash", ".zsh", ".fish", ".sql", ".ps1", ".psm1",
    ".go", ".rs", ".java", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".kt", ".kts", ".scala", ".lua",
    ".r", ".jl", ".vue", ".svelte", ".md", ".json", ".toml", ".yaml", ".yml",
}


def _split_path_position(target: str) -> tuple[str, int | None, int | None]:
    """Split a trailing :LINE[:COLUMN] position from a path-like target.

    Parsing from the right avoids ambiguity with colons that are part of the path
    itself (for example Windows drive prefixes) and with the two numeric suffixes.
    """
    three = target.rsplit(":", 2)
    if len(three) == 3 and three[1].isdigit() and three[2].isdigit():
        return three[0], int(three[1]), int(three[2])
    two = target.rsplit(":", 1)
    if len(two) == 2 and two[1].isdigit():
        return two[0], int(two[1]), 1
    return target, None, None


def path_target_intent(target: str, root: str | Path) -> dict[str, Any] | None:
    """Classify explicit path-like input without requiring the target to exist.

    This is deliberately conservative around dotted symbols: `Class.method` is not
    treated as a path unless it contains a path separator or a known file suffix.
    """
    raw_path, line, column = _split_path_position(target)

    candidate = Path(raw_path).expanduser()
    suffix = candidate.suffix.lower()
    path_like = (
        candidate.is_absolute()
        or "/" in raw_path
        or "\\" in raw_path
        or raw_path.startswith(("./", "../", "~/"))
        or suffix in _PATH_LIKE_SUFFIXES
    )
    if not path_like:
        return None
    if not candidate.is_absolute():
        candidate = Path(root) / candidate
    return {
        "path": str(candidate.resolve()),
        "line": line,
        "column": column,
    }


def parse_target(target: str, root: str | Path) -> dict[str, Any]:
    match = _TARGET_RE.match(target)
    if not match:
        return {"kind": "name", "name": target}
    path = Path(match.group("path")).expanduser()
    if not path.is_absolute():
        path = Path(root) / path
    line = int(match.group("line"))
    col = int(match.group("col") or 1)
    return {"kind": "location", "path": str(path.resolve()), "line": line, "column": col}


def compact_location(item: dict[str, Any]) -> str:
    return f"{item['path']}:{item['line']}:{item.get('column', 1)}"


def run_json_lines(cmd: list[str], cwd: str | Path, timeout: float = 10.0) -> list[dict[str, Any]]:
    proc = subprocess.run(
        cmd, cwd=str(cwd), text=True, capture_output=True, check=False, timeout=timeout,
    )
    out: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def exact_definition_hits(root: str | Path, name: str, limit: int = 80) -> list[dict[str, Any]]:
    """Find declaration-looking lines for one exact identifier.

    This is a cold-start safety primitive, not a semantic authority: callers map
    returned files/lines back through document symbols before accepting a result.
    """
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
        return []
    escaped = re.escape(name)
    patterns = [
        rf"^\s*(?:async\s+def|def|class)\s+{escaped}\b",
        rf"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?(?:function|class|interface|type|enum)\s+{escaped}\b",
        rf"^\s*(?:export\s+)?(?:const|let|var)\s+{escaped}\b",
    ]
    cmd = [
        "rg", "--json", "-n", "--hidden",
        "-g", "*.py", "-g", "*.pyi", "-g", "*.ts", "-g", "*.tsx",
        "-g", "*.js", "-g", "*.jsx", "-g", "!node_modules/**",
        "-g", "!.git/**", "-g", "!.next/**", "-g", "!dist/**", "-g", "!build/**",
        "-g", "!Quant-worktrees/**", "-g", "!worktrees/**", "-g", "!.worktrees/**",
    ]
    for pattern in patterns:
        cmd.extend(["-e", pattern])
    cmd.append(".")
    try:
        events = run_json_lines(cmd, root, timeout=8.0)
    except (OSError, subprocess.TimeoutExpired):
        return []
    hits: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "match":
            continue
        data = event.get("data", {})
        rel = data.get("path", {}).get("text")
        line = data.get("line_number")
        if not rel or not line:
            continue
        path = (Path(root) / rel).resolve()
        text = data.get("lines", {}).get("text", "").rstrip()
        hits.append(loc(path, int(line), 1, source="exact_definition", text=text))
        if len(hits) >= limit:
            break
    return hits


def lexical_hits(root: str | Path, query: str, limit: int = 40) -> list[dict[str, Any]]:
    tokens = identifier_tokens(query)
    if not tokens:
        return []
    patterns = tokens[:3]
    cmd = [
        "rg", "--json", "-n", "--hidden", "--max-count", "20",
        "-g", "*.py", "-g", "*.pyi", "-g", "*.ts", "-g", "*.tsx",
        "-g", "*.js", "-g", "*.jsx", "-g", "!node_modules/**",
        "-g", "!.git/**", "-g", "!.next/**", "-g", "!dist/**", "-g", "!build/**",
        "-g", "!Quant-worktrees/**", "-g", "!worktrees/**", "-g", "!.worktrees/**",
    ]
    for token in patterns:
        cmd.extend(["-e", re.escape(token)])
    cmd.append(".")
    try:
        events = run_json_lines(cmd, root, timeout=8.0)
    except (OSError, subprocess.TimeoutExpired):
        return []
    hits: list[dict[str, Any]] = []
    query_lower = query.lower()
    token_lowers = [token.lower() for token in tokens]
    for event in events:
        if event.get("type") != "match":
            continue
        data = event.get("data", {})
        rel = data.get("path", {}).get("text")
        line = data.get("line_number")
        if not rel or not line:
            continue
        path = (Path(root) / rel).resolve()
        text = data.get("lines", {}).get("text", "").rstrip()
        lowered = text.lower()
        coverage = sum(1 for token in token_lowers if token in lowered)
        phrase_bonus = 3 if query_lower and query_lower in lowered else 0
        hits.append(
            loc(
                path,
                int(line),
                1,
                source="rg",
                text=text,
                match_score=coverage + phrase_bonus,
            )
        )
    hits.sort(key=lambda item: (-int(item.get("match_score", 0)), item["path"], item["line"]))
    return hits[:limit]
