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

    def metrics_snapshot(self):
        return {
            "sessions_started": 0,
            "lsp_request_count": 0,
            "document_symbols_hit": 0,
            "document_symbols_miss": 0,
            "document_symbol_cache_entries": 0,
            "prewarm_files": 0,
            "prewarm_probes": 0,
            "prewarm_early_stops": 0,
        }

    def find(
        self,
        query: str,
        limit: int = 20,
        kind: str | None = None,
        *,
        text: bool = False,
        paths: tuple[str, ...] = (),
        globs: tuple[str, ...] = (),
        exclude_tests: bool = False,
    ):
        return {
            "query": query,
            "results": [],
            "result_count": 0,
            "total_candidates": 0,
            "errors": [],
            "text_seen": text,
            "paths_seen": paths,
            "globs_seen": globs,
            "exclude_tests_seen": exclude_tests,
        }

    def trace(self, target: str, direction: str, depth: int = 3, limit: int = 100):
        return {"status": "ok", "target": target, "direction": direction, "depth": depth, "node_count": 1, "node_limit": limit, "tree": {}, "root": {}}

    def review(self, base: str, limit: int = 20, *, merge_base: bool = False):
        return {"status": "ok", "base": base, "limit_seen": limit, "merge_base_seen": merge_base}


class ServiceLifecycleTests(unittest.TestCase):
    def test_idle_workspace_is_evicted_and_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _FakeWorkspace):
            service = CodeqService()
            result = service.handle({"command": "find", "root": tmp, "query": "Foo", "timeout": 1, "limit": 5})
            self.assertEqual(result["result_count"], 0)
            self.assertEqual(result["_meta"]["lsp_request_count"], 0)
            self.assertFalse(result["_meta"]["lsp_started"])
            self.assertIn("cache", result["_meta"])
            self.assertEqual(result["_meta"]["cache"]["document_symbols_hit"], 0)
            self.assertEqual(service.workspace_count(), 1)
            entry = next(iter(service._workspaces.values()))
            workspace = entry.workspace
            entry.last_used = 0.0
            evicted = service.evict_idle(0.01)
            self.assertEqual(evicted, [str(Path(tmp).resolve())])
            self.assertTrue(bool(getattr(workspace, "closed", False)))
            self.assertEqual(service.workspace_count(), 0)

    def test_text_scope_reaches_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _FakeWorkspace):
            service = CodeqService()
            result = service.handle({
                "command": "find",
                "root": tmp,
                "query": "TOKEN",
                "text": True,
                "paths": ["frontend", "quant-cli/src"],
                "globs": ["*.ts"],
                "exclude_tests": True,
            })
            self.assertTrue(result["text_seen"])
            self.assertEqual(result["paths_seen"], ("frontend", "quant-cli/src"))
            self.assertEqual(result["globs_seen"], ("*.ts",))
            self.assertTrue(result["exclude_tests_seen"])

    def test_semantic_scope_reaches_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _FakeWorkspace):
            service = CodeqService()
            result = service.handle({
                "command": "find",
                "root": tmp,
                "query": "Candidate",
                "paths": ["packages/research-core"],
                "globs": ["*.py"],
                "exclude_tests": True,
            })
            self.assertFalse(result["text_seen"])
            self.assertEqual(result["paths_seen"], ("packages/research-core",))
            self.assertEqual(result["globs_seen"], ("*.py",))
            self.assertTrue(result["exclude_tests_seen"])

    def test_trace_node_limit_is_not_raised_by_global_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _FakeWorkspace):
            service = CodeqService()
            result = service.handle({
                "command": "trace",
                "root": tmp,
                "target": "Foo.run",
                "direction": "in",
                "depth": 2,
                "limit": 20,
                "node_limit": 2,
            })
            self.assertEqual(result["node_limit"], 2)

    def test_trace_depth_zero_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _FakeWorkspace):
            service = CodeqService()
            result = service.handle({
                "command": "trace",
                "root": tmp,
                "target": "Foo.run",
                "direction": "in",
                "depth": 0,
                "node_limit": 20,
            })
            self.assertEqual(result["depth"], 0)

    def test_negative_trace_depth_is_rejected_by_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _FakeWorkspace):
            service = CodeqService()
            with self.assertRaisesRegex(ValueError, "must be >= 0"):
                service.handle({
                    "command": "trace",
                    "root": tmp,
                    "target": "Foo.run",
                    "direction": "in",
                    "depth": -1,
                })

    def test_review_merge_base_flag_reaches_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _FakeWorkspace):
            service = CodeqService()
            result = service.handle({"command": "review", "root": tmp, "base": "main", "merge_base": True})
            self.assertTrue(result["merge_base_seen"])

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
