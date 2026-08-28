from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

from codeq.lsp import LspProcess
from codeq.workspace import Project, Workspace


class _SymbolSession:
    def __init__(self) -> None:
        self.calls = 0

    def document_symbols(self, path: Path, timeout: float | None = None):
        self.calls += 1
        return [
            {
                "name": "value",
                "kind": 13,
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 9},
                },
                "selectionRange": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 5},
                },
            }
        ]


class _LiveSession:
    pid = 1234
    request_count = 0

    def alive(self) -> bool:
        return True

    def close(self) -> None:
        pass


class PerformanceHardeningTests(unittest.TestCase):
    def test_workspace_navigation_is_serialized_within_one_lsp_process(self) -> None:
        session = cast(Any, LspProcess.__new__(LspProcess))
        underlying = threading.Lock()
        guard = threading.Lock()
        contended = threading.Event()
        release = threading.Event()
        attempts = 0
        owners = 0
        max_owners = 0
        request_methods: list[str] = []

        class _ObservedLock:
            def __enter__(self):
                nonlocal attempts, owners, max_owners
                with guard:
                    attempts += 1
                    if attempts == 2:
                        contended.set()
                underlying.acquire()
                with guard:
                    owners += 1
                    max_owners = max(max_owners, owners)

            def __exit__(self, *args: Any):
                nonlocal owners
                with guard:
                    owners -= 1
                underlying.release()

        def request(method: str, params: dict[str, Any], timeout: float | None = None):
            request_methods.append(method)
            if method == "workspace/symbol":
                self.assertTrue(release.wait(timeout=1.0))
            return []

        session._navigation_lock = _ObservedLock()
        session.request = request
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(session.workspace_symbols, "Thing")
            second = pool.submit(session.incoming_calls, {})
            self.assertTrue(contended.wait(timeout=1.0))
            self.assertEqual(request_methods, ["workspace/symbol"])
            release.set()
            first.result(timeout=1.0)
            second.result(timeout=1.0)

        self.assertEqual(request_methods, ["workspace/symbol", "callHierarchy/incomingCalls"])
        self.assertEqual(max_owners, 1)

    def test_basedpyright_configuration_requests_receive_effective_settings(self) -> None:
        session = cast(Any, LspProcess.__new__(LspProcess))
        session.root = Path("/workspace")
        session._server_settings = LspProcess._settings_for_server("basedpyright-langserver")
        session._send = Mock()

        session._handle_server_request(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "workspace/configuration",
                "params": {
                    "items": [
                        {"section": "basedpyright.analysis"},
                        {"section": "basedpyright"},
                        {},
                        {"section": "python.analysis"},
                    ]
                },
            }
        )

        response = session._send.call_args.args[0]
        self.assertEqual(response["result"][0], {"diagnosticMode": "openFilesOnly"})
        self.assertEqual(response["result"][1], {"analysis": {"diagnosticMode": "openFilesOnly"}})
        self.assertEqual(response["result"][2], session._server_settings)
        self.assertIsNone(response["result"][3])

    def test_plain_pyright_keeps_python_configuration_namespace(self) -> None:
        self.assertEqual(
            LspProcess._settings_for_server("pyright-langserver"),
            {"python": {"analysis": {"diagnosticMode": "openFilesOnly"}}},
        )

    def test_ensure_open_does_not_reread_unchanged_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "service.py"
            path.write_text("value = 1\n", encoding="utf-8")
            session = cast(Any, LspProcess.__new__(LspProcess))
            session._open_docs = {}
            session.notify = Mock()
            with patch("pathlib.Path.read_text", autospec=True, return_value="value = 1\n") as read_text:
                session.ensure_open(path)
                session.ensure_open(path)
            self.assertEqual(read_text.call_count, 1)
            session.notify.assert_called_once()

    def test_different_projects_start_language_servers_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = [Project(root / name, "python") for name in ("one", "two")]
            for project in projects:
                project.root.mkdir()
            workspace = Workspace(root)
            barrier = threading.Barrier(2)

            def start(*args: Any, **kwargs: Any) -> _LiveSession:
                barrier.wait(timeout=1.0)
                return _LiveSession()

            try:
                with (
                    patch.object(workspace, "_server_command", return_value=(["fake-lsp"], "fake")),
                    patch("codeq.workspace.LspProcess", side_effect=start) as constructor,
                    ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    sessions = list(pool.map(workspace._session, projects))
                self.assertEqual(constructor.call_count, 2)
                self.assertEqual(len(sessions), 2)
            finally:
                workspace.close()

    def test_same_project_language_server_start_is_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Project(root, "python")
            workspace = Workspace(root)
            entered = threading.Event()
            release = threading.Event()

            def start(*args: Any, **kwargs: Any) -> _LiveSession:
                entered.set()
                self.assertTrue(release.wait(timeout=1.0))
                return _LiveSession()

            try:
                with (
                    patch.object(workspace, "_server_command", return_value=(["fake-lsp"], "fake")),
                    patch("codeq.workspace.LspProcess", side_effect=start) as constructor,
                    ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    futures = [pool.submit(workspace._session, project) for _ in range(2)]
                    self.assertTrue(entered.wait(timeout=1.0))
                    release.set()
                    sessions = [future.result(timeout=1.0) for future in futures]
                self.assertEqual(constructor.call_count, 1)
                self.assertIs(sessions[0], sessions[1])
            finally:
                workspace.close()

    def test_document_symbol_cache_hits_and_invalidates_on_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "module.py"
            path.write_text("value = 1\n", encoding="utf-8")
            project = Project(root, "python")
            session = _SymbolSession()
            workspace = Workspace(root)
            try:
                with patch.object(workspace, "_session", return_value=session):
                    first = workspace._document_symbols(path, project=project)
                    second = workspace._document_symbols(path, project=project)
                    self.assertEqual(first[0]["name"], "value")
                    self.assertEqual(second[0]["name"], "value")
                    self.assertEqual(session.calls, 1)
                    metrics = workspace.metrics_snapshot()
                    self.assertEqual(metrics["document_symbols_miss"], 1)
                    self.assertEqual(metrics["document_symbols_hit"], 1)

                    path.write_text("value = 1000\n", encoding="utf-8")
                    workspace._document_symbols(path, project=project)
                    self.assertEqual(session.calls, 2)
                    metrics = workspace.metrics_snapshot()
                    self.assertEqual(metrics["document_symbols_miss"], 2)
            finally:
                workspace.close()

    def test_concurrent_document_symbol_misses_are_single_flight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "module.py"
            path.write_text("value = 1\n", encoding="utf-8")
            project = Project(root, "python")
            session = _SymbolSession()
            entered = threading.Event()
            release = threading.Event()
            original = session.document_symbols

            def blocked(path: Path, timeout: float | None = None):
                entered.set()
                self.assertTrue(release.wait(timeout=1.0))
                return original(path, timeout)

            session.document_symbols = blocked  # type: ignore[method-assign]
            workspace = Workspace(root)
            callers = threading.Barrier(6)
            try:
                with patch.object(workspace, "_session", return_value=session), ThreadPoolExecutor(max_workers=6) as pool:
                    def load() -> list[dict[str, Any]]:
                        callers.wait(timeout=1.0)
                        return workspace._document_symbols(path, project=project)

                    futures = [pool.submit(load) for _ in range(6)]
                    self.assertTrue(entered.wait(timeout=1.0))
                    release.set()
                    results = [future.result(timeout=1.0) for future in futures]
                self.assertTrue(all(result[0]["name"] == "value" for result in results))
                self.assertEqual(session.calls, 1)
                self.assertGreaterEqual(workspace.metrics_snapshot()["document_symbols_waited"], 1)
            finally:
                release.set()
                workspace.close()

    def test_document_symbol_cache_is_bounded_lru(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = Project(root, "python")
            session = _SymbolSession()
            workspace = Workspace(root)
            try:
                with patch.object(workspace, "_session", return_value=session):
                    for index in range(260):
                        path = root / f"m{index}.py"
                        path.write_text(f"value = {index}\n", encoding="utf-8")
                        workspace._document_symbols(path, project=project)
                metrics = workspace.metrics_snapshot()
                self.assertEqual(metrics["document_symbol_cache_entries"], 256)
                self.assertEqual(metrics["document_symbols_evicted"], 4)
            finally:
                workspace.close()

    def test_adaptive_prewarm_stops_only_after_budget_is_satisfied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = []
            for index in range(6):
                path = root / f"f{index}.py"
                path.write_text("HotSymbol\n", encoding="utf-8")
                files.append(path)
            project = Project(root, "python")
            workspace = Workspace(root)
            opened: list[Path] = []
            probe_calls = 0

            def probe():
                nonlocal probe_calls
                probe_calls += 1
                size = 1 if probe_calls == 1 else 3
                return {(f"/r/{index}", 1, 1) for index in range(size)}

            lexical = [
                {"path": str(path), "line": 1, "column": 1, "text": "HotSymbol"}
                for path in files
            ]
            try:
                with (
                    patch("codeq.workspace.lexical_hits", return_value=lexical),
                    patch.object(workspace, "project_for_path", return_value=project),
                    patch.object(workspace, "_document_symbols", side_effect=lambda path, **kwargs: opened.append(path) or []),
                ):
                    workspace._prewarm_symbol(
                        project,
                        cast(Any, object()),
                        {"name": "HotSymbol", "source": "lsp"},
                        max_files=6,
                        desired_results=3,
                        probe=probe,
                    )
                self.assertEqual(len(opened), 4)
                metrics = workspace.metrics_snapshot()
                self.assertEqual(metrics["prewarm_files"], 4)
                self.assertEqual(metrics["prewarm_probes"], 2)
                self.assertEqual(metrics["prewarm_early_stops"], 1)
            finally:
                workspace.close()

    def test_prewarm_does_not_early_stop_below_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = []
            for index in range(5):
                path = root / f"f{index}.py"
                path.write_text("HotSymbol\n", encoding="utf-8")
                files.append(path)
            project = Project(root, "python")
            workspace = Workspace(root)
            opened: list[Path] = []
            lexical = [{"path": str(path), "line": 1, "column": 1} for path in files]
            try:
                with (
                    patch("codeq.workspace.lexical_hits", return_value=lexical),
                    patch.object(workspace, "project_for_path", return_value=project),
                    patch.object(workspace, "_document_symbols", side_effect=lambda path, **kwargs: opened.append(path) or []),
                ):
                    workspace._prewarm_symbol(
                        project,
                        cast(Any, object()),
                        {"name": "HotSymbol", "source": "lsp"},
                        max_files=5,
                        desired_results=10,
                        probe=lambda: {("/one", 1, 1)},
                    )
                self.assertEqual(len(opened), 5)
                self.assertEqual(workspace.metrics_snapshot()["prewarm_early_stops"], 0)
            finally:
                workspace.close()

    def test_concurrent_prewarm_for_same_symbol_is_single_flight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "module.py"
            path.write_text("HotSymbol\n", encoding="utf-8")
            project = Project(root, "python")
            workspace = Workspace(root)
            entered = threading.Event()
            release = threading.Event()
            lexical_calls = 0
            callers = threading.Barrier(2)

            def lexical(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
                nonlocal lexical_calls
                lexical_calls += 1
                entered.set()
                self.assertTrue(release.wait(timeout=1.0))
                return [{"path": str(path), "line": 1, "column": 1}]

            try:
                with (
                    patch("codeq.workspace.lexical_hits", side_effect=lexical),
                    patch.object(workspace, "project_for_path", return_value=project),
                    patch.object(workspace, "_document_symbols", return_value=[]),
                    ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    def prewarm() -> None:
                        callers.wait(timeout=1.0)
                        workspace._prewarm_symbol(
                            project,
                            cast(Any, object()),
                            {"name": "HotSymbol", "source": "lsp"},
                        )

                    futures = [pool.submit(prewarm) for _ in range(2)]
                    self.assertTrue(entered.wait(timeout=1.0))
                    release.set()
                    for future in futures:
                        future.result(timeout=1.0)
                self.assertEqual(lexical_calls, 1)
            finally:
                release.set()
                workspace.close()


if __name__ == "__main__":
    unittest.main()
