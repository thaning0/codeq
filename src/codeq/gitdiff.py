from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

_HUNK = re.compile(r"^@@ -\d+(?:\d+,?\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def git_changed_ranges(root: Path, base: str) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["git", "-C", str(root), "diff", "--no-ext-diff", "--unified=0", "--find-renames", base, "--"],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git diff failed for base {base}")
    changes: list[dict[str, Any]] = []
    current: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            continue
        if line.startswith("+++ /dev/null"):
            current = None
            continue
        if not current or not line.startswith("@@"):
            continue
        match = _HUNK.match(line)
        if not match:
            continue
        start = int(match.group("start"))
        count = int(match.group("count") or "1")
        end = start if count == 0 else start + count - 1
        changes.append({"path": str((root / current).resolve()), "start": start, "end": end})
    return changes


def merge_ranges(ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[int, int]]] = {}
    for item in ranges:
        grouped.setdefault(item["path"], []).append((item["start"], item["end"]))
    out: list[dict[str, Any]] = []
    for path, spans in grouped.items():
        spans.sort()
        merged: list[list[int]] = []
        for start, end in spans:
            if not merged or start > merged[-1][1] + 1:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        out.append({"path": path, "ranges": [{"start": s, "end": e} for s, e in merged]})
    return sorted(out, key=lambda x: x["path"])
