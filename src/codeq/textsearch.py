from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from typing import Any

from .contracts import EVIDENCE_LEXICAL, QueryBudget
from .util import is_test_path


def _invalid_result(query: str, reason: str) -> dict[str, Any]:
    return {
        "status": "invalid_query",
        "mode": "text",
        "query": query,
        "reason": reason,
        "results": [],
        "match_count": 0,
        "matching_line_count": 0,
        "matching_file_count": 0,
        "returned_line_count": 0,
        "returned_match_count": 0,
        "test_line_count": 0,
        "tracked_line_count": 0,
        "untracked_line_count": 0,
        "truncated": False,
    }


def _normalize_path_prefix(root: Path, value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root.resolve()).as_posix().rstrip("/")
        except ValueError:
            return "__outside_repository__"
    normalized = raw.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _path_matches(
    root: Path,
    rel: str,
    *,
    path_prefixes: tuple[str, ...],
    globs: tuple[str, ...],
    exclude_tests: bool,
    only_tests: bool,
) -> bool:
    test_path = is_test_path(root / rel)
    if exclude_tests and test_path:
        return False
    if only_tests and not test_path:
        return False
    if path_prefixes:
        normalized = tuple(_normalize_path_prefix(root, value) for value in path_prefixes)
        if not any(prefix and (rel == prefix or rel.startswith(prefix + "/")) for prefix in normalized):
            return False
    if globs:
        basename = Path(rel).name
        if not any(fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(basename, pattern) for pattern in globs):
            return False
    return True


def _tracked_hits(
    root: Path,
    query: str,
    *,
    path_prefixes: tuple[str, ...],
    globs: tuple[str, ...],
    exclude_tests: bool,
    only_tests: bool,
) -> list[dict[str, Any]]:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "grep",
            "-n",
            "-I",
            "-F",
            "-z",
            "-e",
            query,
            "--",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode not in {0, 1}:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or "git grep failed")

    hits: list[dict[str, Any]] = []
    for raw in proc.stdout.splitlines():
        parts = raw.split(b"\0", 2)
        if len(parts) != 3:
            continue
        rel_raw, line_raw, text_raw = parts
        try:
            line = int(line_raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            continue
        rel = rel_raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        if not _path_matches(
            root,
            rel,
            path_prefixes=path_prefixes,
            globs=globs,
            exclude_tests=exclude_tests,
            only_tests=only_tests,
        ):
            continue
        text = text_raw.decode("utf-8", errors="replace")
        occurrences = text.count(query)
        if occurrences <= 0:
            continue
        path = (root / rel).resolve()
        hits.append(
            {
                "path": str(path),
                "relative_path": rel,
                "line": line,
                "column": text.find(query) + 1,
                "text": text,
                "occurrences": occurrences,
                "is_test": is_test_path(path),
                "tracked": True,
                "git_status": "tracked",
                "source": "git-grep",
                "evidence": EVIDENCE_LEXICAL,
            }
        )
    return hits


def _untracked_files(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or "git ls-files failed")
    return [
        raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for raw in proc.stdout.split(b"\0")
        if raw
    ]


def _untracked_hits(
    root: Path,
    query: str,
    *,
    path_prefixes: tuple[str, ...],
    globs: tuple[str, ...],
    exclude_tests: bool,
    only_tests: bool,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for rel in _untracked_files(root):
        if not _path_matches(
            root,
            rel,
            path_prefixes=path_prefixes,
            globs=globs,
            exclude_tests=exclude_tests,
            only_tests=only_tests,
        ):
            continue
        path = (root / rel).resolve()
        if not path.is_file():
            continue
        try:
            with path.open("rb") as binary:
                if b"\x00" in binary.read(8192):
                    continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    text = raw_line.rstrip("\r\n")
                    occurrences = text.count(query)
                    if occurrences <= 0:
                        continue
                    hits.append(
                        {
                            "path": str(path),
                            "relative_path": rel,
                            "line": line_number,
                            "column": text.find(query) + 1,
                            "text": text,
                            "occurrences": occurrences,
                            "is_test": is_test_path(path),
                            "tracked": False,
                            "git_status": "untracked",
                            "source": "untracked-scan",
                            "evidence": EVIDENCE_LEXICAL,
                        }
                    )
        except OSError:
            continue
    return hits


def _bounded_hit(item: dict[str, Any], query: str, max_chars: int) -> dict[str, Any]:
    text = str(item.get("text") or "")
    if len(text) <= max_chars:
        return {**item, "text_truncated": False, "text_start_column": 1}
    match_index = max(0, int(item.get("column") or 1) - 1)
    context = max(0, max_chars // 3)
    start = max(0, match_index - context)
    if start + max_chars > len(text):
        start = max(0, len(text) - max_chars)
    end = min(len(text), start + max_chars)
    window = text[start:end]
    if start > 0 and max_chars >= 3:
        window = "..." + window[3:]
    if end < len(text) and max_chars >= 3:
        window = window[:-3] + "..."
    return {
        **item,
        "text": window,
        "text_truncated": True,
        "text_start_column": start + 1,
    }


def git_text_search(
    root: Path,
    query: str,
    limit: int = 20,
    *,
    paths: tuple[str, ...] = (),
    globs: tuple[str, ...] = (),
    exclude_tests: bool = False,
    include_untracked: bool = True,
    only_tests: bool = False,
) -> dict[str, Any]:
    """Search an exact literal across Git-visible working-tree text.

    Tracked files are searched through `git grep`. Untracked files are added from
    `git ls-files --others --exclude-standard`, so Git-ignored files remain outside
    the search contract. Results stay raw/textual and are never promoted to semantic
    references.
    """
    if not query:
        return _invalid_result(query, "text query must not be empty")
    if "\x00" in query or "\n" in query or "\r" in query:
        return _invalid_result(query, "text query must be a single line without NUL bytes")

    path_prefixes = tuple(value for value in paths if value.strip())
    glob_filters = tuple(value for value in globs if value.strip())
    hits = _tracked_hits(
        root,
        query,
        path_prefixes=path_prefixes,
        globs=glob_filters,
        exclude_tests=exclude_tests,
        only_tests=only_tests,
    )
    if include_untracked:
        hits.extend(
            _untracked_hits(
                root,
                query,
                path_prefixes=path_prefixes,
                globs=glob_filters,
                exclude_tests=exclude_tests,
                only_tests=only_tests,
            )
        )
    hits.sort(key=lambda item: (item["relative_path"], int(item["line"]), not bool(item["tracked"])))

    budget = QueryBudget.from_limit(limit)
    bounded = [_bounded_hit(item, query, budget.text_line_chars) for item in hits[: budget.items]]
    matching_files = {str(item["path"]) for item in hits}
    return {
        "status": "ok",
        "mode": "text",
        "evidence": EVIDENCE_LEXICAL,
        "query": query,
        "results": bounded,
        "match_count": sum(int(item["occurrences"]) for item in hits),
        "matching_line_count": len(hits),
        "matching_file_count": len(matching_files),
        "returned_line_count": len(bounded),
        "returned_match_count": sum(int(item["occurrences"]) for item in bounded),
        "test_line_count": sum(1 for item in hits if item["is_test"]),
        "tracked_line_count": sum(1 for item in hits if item["tracked"]),
        "untracked_line_count": sum(1 for item in hits if not item["tracked"]),
        "truncated": len(hits) > len(bounded),
        "filters": {
            "paths": list(path_prefixes),
            "globs": list(glob_filters),
            "exclude_tests": exclude_tests,
            "include_untracked": include_untracked,
            "only_tests": only_tests,
        },
    }
