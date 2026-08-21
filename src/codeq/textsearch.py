from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .util import is_test_path


def git_tracked_text_search(root: Path, query: str, limit: int = 20) -> dict[str, Any]:
    """Search an exact literal across Git-tracked text files.

    Git is the file-set authority: ignored and untracked files are excluded, while
    current working-tree contents of tracked files are searched. Binary files are
    skipped with `git grep -I`.
    """
    if not query:
        return {
            "status": "invalid_query",
            "mode": "text",
            "query": query,
            "reason": "text query must not be empty",
            "results": [],
            "match_count": 0,
            "matching_line_count": 0,
            "returned_line_count": 0,
            "returned_match_count": 0,
            "test_line_count": 0,
            "truncated": False,
        }
    if "\x00" in query or "\n" in query or "\r" in query:
        return {
            "status": "invalid_query",
            "mode": "text",
            "query": query,
            "reason": "text query must be a single line without NUL bytes",
            "results": [],
            "match_count": 0,
            "matching_line_count": 0,
            "returned_line_count": 0,
            "returned_match_count": 0,
            "test_line_count": 0,
            "truncated": False,
        }

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
    match_count = 0
    test_line_count = 0
    for raw in proc.stdout.splitlines():
        parts = raw.split(b"\0", 2)
        if len(parts) != 3:
            continue
        rel_raw, line_raw, text_raw = parts
        try:
            line = int(line_raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            continue
        rel = rel_raw.decode("utf-8", errors="surrogateescape")
        text = text_raw.decode("utf-8", errors="replace")
        occurrences = text.count(query)
        if occurrences <= 0:
            continue
        path = (root / rel).resolve()
        test = is_test_path(path)
        if test:
            test_line_count += 1
        match_count += occurrences
        hits.append(
            {
                "path": str(path),
                "line": line,
                "column": text.find(query) + 1,
                "text": text,
                "occurrences": occurrences,
                "is_test": test,
                "source": "git-grep",
            }
        )

    bounded = hits[: max(1, limit)]
    return {
        "status": "ok",
        "mode": "text",
        "query": query,
        "results": bounded,
        "match_count": match_count,
        "matching_line_count": len(hits),
        "returned_line_count": len(bounded),
        "returned_match_count": sum(int(item["occurrences"]) for item in bounded),
        "test_line_count": test_line_count,
        "truncated": len(hits) > len(bounded),
    }
