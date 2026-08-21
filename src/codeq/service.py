from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import attach_schema
from .util import git_root
from .workspace import Workspace


@dataclass
class _WorkspaceEntry:
    workspace: Workspace
    last_used: float
    active: int = 0


class CodeqService:
    def __init__(self, max_workspaces: int = 4) -> None:
        self._workspaces: dict[Path, _WorkspaceEntry] = {}
        self._lock = threading.RLock()
        self._last_activity = time.monotonic()
        self._max_workspaces = max(1, max_workspaces)

    def close(self) -> None:
        with self._lock:
            workspaces = [entry.workspace for entry in self._workspaces.values()]
            self._workspaces.clear()
        for workspace in workspaces:
            workspace.close()

    def workspace_count(self) -> int:
        with self._lock:
            return len(self._workspaces)

    def idle_seconds(self) -> float:
        with self._lock:
            return max(0.0, time.monotonic() - self._last_activity)

    def evict_idle(self, max_idle_seconds: float) -> list[str]:
        """Close inactive workspaces without exposing lifecycle controls to the CLI."""
        now = time.monotonic()
        evicted: list[Workspace] = []
        roots: list[str] = []
        with self._lock:
            for root, entry in list(self._workspaces.items()):
                if entry.active != 0 or now - entry.last_used < max_idle_seconds:
                    continue
                self._workspaces.pop(root, None)
                evicted.append(entry.workspace)
                roots.append(str(root))
        for workspace in evicted:
            workspace.close()
        return roots

    def _acquire_workspace(self, root: str, timeout: float) -> tuple[Path, _WorkspaceEntry]:
        resolved = git_root(root)
        now = time.monotonic()
        victim: Workspace | None = None
        with self._lock:
            entry = self._workspaces.get(resolved)
            if entry is None:
                if len(self._workspaces) >= self._max_workspaces:
                    idle = [
                        (candidate_root, candidate)
                        for candidate_root, candidate in self._workspaces.items()
                        if candidate.active == 0
                    ]
                    if idle:
                        victim_root, victim_entry = min(idle, key=lambda item: item[1].last_used)
                        self._workspaces.pop(victim_root, None)
                        victim = victim_entry.workspace
                entry = _WorkspaceEntry(Workspace(resolved, timeout=timeout), last_used=now)
                self._workspaces[resolved] = entry
            else:
                entry.workspace.timeout = timeout
            entry.active += 1
            entry.last_used = now
            self._last_activity = now
        if victim is not None:
            victim.close()
        return resolved, entry

    def _release_workspace(self, entry: _WorkspaceEntry) -> None:
        with self._lock:
            entry.active = max(0, entry.active - 1)
            entry.last_used = time.monotonic()
            self._last_activity = entry.last_used

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        command = request.get("command")
        if command == "_status":
            with self._lock:
                self._last_activity = time.monotonic()
                roots = sorted(str(root) for root in self._workspaces)
            return attach_schema({"status": "ok", "workspaces": len(roots), "roots": roots})

        root = str(request.get("root") or ".")
        timeout = float(request.get("timeout") or 15.0)
        limit = int(request.get("limit") or 20)
        _, entry = self._acquire_workspace(root, timeout)
        ws = entry.workspace
        before = ws.session_stats()
        metrics_before = ws.metrics_snapshot()
        try:
            data: dict[str, Any]
            if command == "find":
                data = ws.find(
                    str(request.get("query") or ""),
                    limit=limit,
                    kind=str(request.get("kind") or "") or None,
                    text=bool(request.get("text")),
                    text_paths=tuple(str(value) for value in (request.get("text_paths") or [])),
                    text_globs=tuple(str(value) for value in (request.get("text_globs") or [])),
                    text_exclude_tests=bool(request.get("text_exclude_tests")),
                )
            elif command == "context":
                data = ws.context(
                    str(request.get("target") or ""),
                    limit=limit,
                    outline_depth=max(0, int(request.get("outline_depth") or 1)),
                    outline_kind=str(request.get("outline_kind") or "") or None,
                    container=str(request.get("container") or "") or None,
                    include_topology=bool(request.get("include_topology")),
                    lexical_references=bool(request.get("lexical_references")),
                    lexical_query=str(request.get("lexical_query") or "") or None,
                    lexical_paths=tuple(str(value) for value in (request.get("lexical_paths") or [])),
                    lexical_globs=tuple(str(value) for value in (request.get("lexical_globs") or [])),
                    lexical_exclude_tests=bool(request.get("lexical_exclude_tests")),
                )
            elif command == "trace":
                raw_depth = request.get("depth")
                depth = 3 if raw_depth is None else int(raw_depth)
                if depth < 0:
                    raise ValueError("trace depth must be >= 0")
                data = ws.trace(
                    str(request.get("target") or ""),
                    direction=str(request.get("direction") or "in"),
                    depth=depth,
                    limit=max(1, int(request.get("node_limit") or 100)),
                )
            elif command == "review":
                data = ws.review(
                    str(request.get("base") or "HEAD~1"),
                    limit=limit,
                    merge_base=bool(request.get("merge_base")),
                )
            else:
                raise ValueError(f"unknown command: {command}")
            after = ws.session_stats()
            metrics_after = ws.metrics_snapshot()

            def delta(name: str) -> int:
                return max(0, int(metrics_after.get(name, 0)) - int(metrics_before.get(name, 0)))

            data["_meta"] = {
                "root": str(ws.root),
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "lsp_sessions_before": before,
                "lsp_sessions": after,
                "lsp_started": delta("sessions_started") > 0,
                "lsp_request_count": delta("lsp_request_count"),
                "prewarm_files": delta("prewarm_files"),
                "prewarm_probes": delta("prewarm_probes"),
                "prewarm_early_stops": delta("prewarm_early_stops"),
                "cache": {
                    "document_symbols_hit": delta("document_symbols_hit"),
                    "document_symbols_miss": delta("document_symbols_miss"),
                    "document_symbols_evicted": delta("document_symbols_evicted"),
                    "document_symbol_entries": int(metrics_after.get("document_symbol_cache_entries", 0)),
                },
            }
            if data.get("mode") == "text":
                data["_meta"]["text"] = {
                    "matching_file_count": int(data.get("matching_file_count", 0)),
                    "tracked_matching_lines": int(data.get("tracked_line_count", 0)),
                    "untracked_matching_lines": int(data.get("untracked_line_count", 0)),
                }
            elif isinstance(data.get("lexical_references"), dict):
                lexical = data["lexical_references"]
                data["_meta"]["text"] = {
                    "matching_file_count": int(lexical.get("matching_file_count", 0)),
                    "tracked_matching_lines": int(lexical.get("tracked_line_count", 0)),
                    "untracked_matching_lines": int(lexical.get("untracked_line_count", 0)),
                }
            return attach_schema(data)
        finally:
            self._release_workspace(entry)
