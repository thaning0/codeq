from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeq.service import CodeqService


class _FakeWorkspace:
    def __init__(self, root: Path, timeout: float = 15.0):
        self.root = root
        self.timeout = timeout
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def session_stats(self):
        return []

    def find(self, query: str, limit: int = 20, kind: str | None = None):
        return {"query": query, "results": [], "result_count": 0, "total_candidates": 0, "errors": []}


class ServiceLifecycleTests(unittest.TestCase):
    def test_idle_workspace_is_evicted_and_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _FakeWorkspace):
            service = CodeqService()
            result = service.handle({"command": "find", "root": tmp, "query": "Foo", "timeout": 1, "limit": 5})
            self.assertEqual(result["result_count"], 0)
            self.assertEqual(service.workspace_count(), 1)
            entry = next(iter(service._workspaces.values()))
            workspace = entry.workspace
            entry.last_used = 0.0
            evicted = service.evict_idle(0.01)
            self.assertEqual(evicted, [str(Path(tmp).resolve())])
            self.assertTrue(bool(getattr(workspace, "closed", False)))
            self.assertEqual(service.workspace_count(), 0)

    def test_workspace_cap_evicts_least_recently_used_inactive_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _FakeWorkspace):
            root = Path(tmp)
            roots = [root / name for name in ("one", "two", "three")]
            for item in roots:
                item.mkdir()
            service = CodeqService(max_workspaces=2)
            service.handle({"command": "find", "root": str(roots[0]), "query": "Foo"})
            first_entry = service._workspaces[roots[0].resolve()]
            first_entry.last_used = 0.0
            service.handle({"command": "find", "root": str(roots[1]), "query": "Foo"})
            service.handle({"command": "find", "root": str(roots[2]), "query": "Foo"})
            self.assertEqual(service.workspace_count(), 2)
            self.assertNotIn(roots[0].resolve(), service._workspaces)
            self.assertTrue(bool(getattr(first_entry.workspace, "closed", False)))


if __name__ == "__main__":
    unittest.main()
