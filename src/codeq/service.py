from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .util import git_root
from .workspace import Workspace


class CodeqService:
    def __init__(self) -> None:
        self._workspaces: dict[Path, Workspace] = {}
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            workspaces = list(self._workspaces.values())
            self._workspaces.clear()
        for workspace in workspaces:
            workspace.close()

    def _workspace(self, root: str, timeout: float) -> Workspace:
        resolved = git_root(root)
        with self._lock:
            ws = self._workspaces.get(resolved)
            if ws is None:
                ws = Workspace(resolved, timeout=timeout)
                self._workspaces[resolved] = ws
            else:
                ws.timeout = timeout
            return ws

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        command = request.get("command")
        root = request.get("root") or "."
        timeout = float(request.get("timeout") or 15.0)
        limit = int(request.get("limit") or 20)
        ws = self._workspace(root, timeout)
        before = ws.session_stats()
        data: dict[str, Any]
        if command == "find":
            data = ws.find(
                str(request.get("query") or ""),
                limit=limit,
                kind=str(request.get("kind") or "") or None,
            )
        elif command == "context":
            data = ws.context(str(request.get("target") or ""), limit=limit)
        elif command == "trace":
            data = ws.trace(
                str(request.get("target") or ""),
                direction=str(request.get("direction") or "in"),
                depth=int(request.get("depth") or 3),
                limit=max(limit, int(request.get("node_limit") or 100)),
            )
        elif command == "review":
            data = ws.review(str(request.get("base") or "HEAD~1"), limit=limit)
        elif command == "_status":
            data = {"status": "ok", "workspaces": len(self._workspaces)}
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
