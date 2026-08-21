from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def git_resolve_commit(root: Path, ref: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"cannot resolve Git ref {ref}")
    return proc.stdout.strip()


def git_merge_base(root: Path, base: str, head: str = "HEAD") -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "merge-base", base, head],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"cannot resolve merge base for {base} and {head}")
    value = proc.stdout.strip()
    if not value:
        raise RuntimeError(f"no merge base found for {base} and {head}")
    return value


def git_show_file(root: Path, commit: str, path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    proc = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:{relative}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_untracked_files(root: Path) -> list[dict[str, Any]]:
    """Return untracked files, respecting Git ignore/exclude rules."""
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git ls-files failed")
    out: list[dict[str, Any]] = []
    for rel in proc.stdout.split("\0"):
        if not rel:
            continue
        path = (root / rel).resolve()
        if not path.is_file():
            continue
        out.append({"status": "U", "similarity": None, "old_path": None, "path": str(path)})
    return out


def whole_file_range(path: Path) -> dict[str, Any]:
    try:
        line_count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        line_count = 0
    return {"path": str(path.resolve()), "start": 1, "end": max(1, line_count)}


def git_changed_files(root: Path, base: str) -> list[dict[str, Any]]:
    """Return Git's authoritative A/M/D/R/C file status for a diff base."""
    proc = subprocess.run(
        ["git", "-C", str(root), "diff", "--no-ext-diff", "--name-status", "-z", "-M", base, "--"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git diff failed for base {base}")

    fields = proc.stdout.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    out: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        raw_status = fields[index]
        index += 1
        if not raw_status:
            continue
        status = raw_status[0]
        if status in {"R", "C"}:
            if index + 1 >= len(fields):
                break
            old_rel = fields[index]
            new_rel = fields[index + 1]
            index += 2
            out.append(
                {
                    "status": status,
                    "similarity": raw_status[1:] or None,
                    "old_path": str((root / old_rel).resolve()),
                    "path": str((root / new_rel).resolve()),
                }
            )
            continue
        if index >= len(fields):
            break
        rel = fields[index]
        index += 1
        out.append(
            {
                "status": status,
                "similarity": None,
                "old_path": None,
                "path": str((root / rel).resolve()),
            }
        )
    return out


def git_changed_ranges(root: Path, base: str) -> list[dict[str, Any]]:
    proc = subprocess.run(
        [
            "git", "-C", str(root), "diff", "--no-ext-diff", "--unified=0",
            "--find-renames", "--diff-filter=AMRC", base, "--",
        ],
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
