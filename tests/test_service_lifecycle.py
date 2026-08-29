from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from codeq.service import CodeqService


class _FakeWorkspace:
    def __init__(self, root: Path, timeout: float = 15.0):
        self.root = root
        self.timeout = timeout
        self.closed = False
        self.lsp_active = False

    def close(self) -> None:
        self.closed = True

    def session_stats(self):
        return []

    def has_active_lsp(self) -> bool:
        return self.lsp_active

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

    def context(
        self,
        target: str,
        limit: int = 20,
        *,
        outline_depth: int = 1,
        outline_kind: str | None = None,
        container: str | None = None,
        include_topology: bool = False,
        lexical_references: bool = False,
        lexical_query: str | None = None,
        lexical_paths: tuple[str, ...] = (),
        lexical_globs: tuple[str, ...] = (),
        lexical_exclude_tests: bool = False,
        semantic_paths: tuple[str, ...] = (),
        selected_sections: tuple[str, ...] = (),
    ):
        return {
            "status": "ok",
            "target": target,
            "_phase_ms": {
                "resolution": 1.24,
                "prewarm": 2.26,
                "semantic_neighborhood": 3.28,
            },
        }

    def review(self, base: str, limit: int = 20, *, merge_base: bool = False):
        return {
            "status": "ok",
            "base": base,
            "limit_seen": limit,
            "merge_base_seen": merge_base,
            "_phase_ms": {"change_discovery": 4.44, "review_analysis": 5.55},
        }


class ServiceLifecycleTests(unittest.TestCase):
    def test_same_workspace_semantic_finds_are_queued(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        class _BlockingWorkspace(_FakeWorkspace):
            def find(self, *args, **kwargs):
                nonlocal calls
                with calls_lock:
                    calls += 1
                    call_number = calls
                if call_number == 1:
                    entered.set()
                    self.assert_release()
                return super().find(*args, **kwargs)

            @staticmethod
            def assert_release() -> None:
                if not release.wait(timeout=1.0):
                    raise AssertionError("semantic find was not released")

        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _BlockingWorkspace):
            service = CodeqService()
            second_acquired = threading.Event()
            original_acquire = service._acquire_workspace
            acquisitions = 0

            def acquire(root: str, timeout: float):
                nonlocal acquisitions
                result = original_acquire(root, timeout)
                acquisitions += 1
                if acquisitions == 2:
                    second_acquired.set()
                return result

            try:
                with patch.object(service, "_acquire_workspace", side_effect=acquire), ThreadPoolExecutor(max_workers=2) as pool:
                    request = {"command": "find", "root": tmp, "query": "Thing", "timeout": 1.0}
                    first = pool.submit(service.handle, request)
                    self.assertTrue(entered.wait(timeout=1.0))
                    second = pool.submit(service.handle, request)
                    self.assertTrue(second_acquired.wait(timeout=1.0))
                    with calls_lock:
                        self.assertEqual(calls, 1)
                    release.set()
                    first_result = first.result(timeout=1.0)
                    second_result = second.result(timeout=1.0)
                self.assertEqual(calls, 2)
                self.assertIn("queue_ms", first_result["_meta"])
                self.assertIn("execution_ms", second_result["_meta"])
            finally:
                release.set()
                service.close()

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

    def test_idle_workspace_with_live_lsp_uses_longer_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _FakeWorkspace):
            service = CodeqService()
            service.handle({"command": "find", "root": tmp, "query": "Foo"})
            entry = next(iter(service._workspaces.values()))
            setattr(entry.workspace, "lsp_active", True)
            entry.last_used = time.monotonic() - 1.0

            self.assertEqual(service.evict_idle(0.01, lsp_idle_seconds=3600), [])
            self.assertFalse(bool(getattr(entry.workspace, "closed", False)))
            self.assertEqual(service.evict_idle(0.01, lsp_idle_seconds=0.01), [str(Path(tmp).resolve())])
            self.assertTrue(bool(getattr(entry.workspace, "closed", False)))

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

    def test_workspace_phase_timings_are_promoted_to_json_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _FakeWorkspace):
            service = CodeqService()
            context = service.handle({"command": "context", "root": tmp, "target": "Service.run"})
            review = service.handle({"command": "review", "root": tmp, "base": "HEAD~4"})
            self.assertEqual(
                context["_meta"]["phase_ms"],
                {"resolution": 1.2, "prewarm": 2.3, "semantic_neighborhood": 3.3},
            )
            self.assertEqual(
                review["_meta"]["phase_ms"],
                {"change_discovery": 4.4, "review_analysis": 5.5},
            )
            self.assertNotIn("_phase_ms", context)
            self.assertNotIn("_phase_ms", review)

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
