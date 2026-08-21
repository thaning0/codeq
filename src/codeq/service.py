from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
            return {"status": "ok", "workspaces": len(roots), "roots": roots}

        root = str(request.get("root") or ".")
        timeout = float(request.get("timeout") or 15.0)
        limit = int(request.get("limit") or 20)
        _, entry = self._acquire_workspace(root, timeout)
        ws = entry.workspace
        before = ws.session_stats()
        try:
            data: dict[str, Any]
            if command == "find":
                data = ws.find(
                    str(request.get("query") or ""),
                    limit=limit,
                    kind=str(request.get("kind") or "") or None,
                )
            elif command == "context":
                data = ws.context(
                    str(request.get("target") or ""),
                    limit=limit,
                    outline_depth=max(0, int(request.get("outline_depth") or 1)),
                    outline_kind=str(request.get("outline_kind") or "") or None,
                    container=str(request.get("container") or "") or None,
                    include_topology=bool(request.get("include_topology")),
                )
            elif command == "trace":
                data = ws.trace(
                    str(request.get("target") or ""),
                    direction=str(request.get("direction") or "in"),
                    depth=int(request.get("depth") or 3),
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
            data["_meta"] = {
                "root": str(ws.root),
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "lsp_sessions_before": before,
                "lsp_sessions": after,
            }
            return data
        finally:
            self._release_workspace(entry)
