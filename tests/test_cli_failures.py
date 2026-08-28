from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from codeq.cli import main


class CliFailureContractTests(unittest.TestCase):
    def test_search_alias_sends_a_find_request(self) -> None:
        captured: dict[str, object] = {}

        def request(payload: dict[str, object], timeout: float) -> dict[str, object]:
            captured.update(payload)
            return {
                "status": "ok",
                "query": "Thing",
                "results": [],
                "result_count": 0,
                "total_candidates": 0,
                "errors": [],
            }

        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", side_effect=request),
            redirect_stdout(io.StringIO()),
        ):
            main(["search", "Thing", "--json"])
        self.assertEqual(captured["command"], "find")
        self.assertEqual(captured["query"], "Thing")

    def test_no_daemon_flag_uses_in_process_request(self) -> None:
        result = {
            "status": "ok",
            "query": "Thing",
            "results": [],
            "result_count": 0,
            "total_candidates": 0,
            "errors": [],
        }
        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request") as daemon_request,
            patch("codeq.cli._request_in_process", return_value=result) as in_process,
            redirect_stdout(io.StringIO()),
        ):
            main(["find", "Thing", "--no-daemon", "--json"])
        daemon_request.assert_not_called()
        in_process.assert_called_once()

    def test_plain_text_query_failure_exits_one(self) -> None:
        result = {
            "status": "not_found",
            "target": "scripts/missing.py",
            "path": "/repo/scripts/missing.py",
            "reason": "file not found: /repo/scripts/missing.py",
        }
        stderr = io.StringIO()
        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", return_value=result),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["context", "scripts/missing.py"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("file not found", stderr.getvalue())

    def test_json_query_failure_is_structured_and_exits_one(self) -> None:
        result = {
            "status": "not_found",
            "target": "scripts/missing.py:12",
            "path": "/repo/scripts/missing.py",
            "reason": "file not found: /repo/scripts/missing.py",
        }
        stdout = io.StringIO()
        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", return_value=result),
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["context", "scripts/missing.py:12", "--json"])
        self.assertEqual(raised.exception.code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "not_found")
        self.assertEqual(payload["target"], "scripts/missing.py:12")

    def test_trace_depth_zero_reaches_service_payload_unchanged(self) -> None:
        captured: dict[str, object] = {}

        def request(payload: dict[str, object], timeout: float) -> dict[str, object]:
            captured.update(payload)
            return {
                "status": "ok",
                "target": "Foo.run",
                "direction": "in",
                "depth": 0,
                "node_count": 1,
                "node_limit": 20,
                "truncated": False,
                "root": {"name": "run", "path": "/repo/foo.py", "line": 1, "column": 1},
                "tree": {"node": {"name": "run", "path": "/repo/foo.py", "line": 1, "column": 1}, "children": []},
            }

        stdout = io.StringIO()
        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", side_effect=request),
            redirect_stdout(stdout),
        ):
            main(["trace", "Foo.run", "--in", "--depth", "0", "--node-limit", "20", "--json"])
        self.assertEqual(captured["depth"], 0)
        self.assertEqual(json.loads(stdout.getvalue())["depth"], 0)

    def test_trace_limit_alias_bounds_emitted_nodes(self) -> None:
        captured: dict[str, object] = {}

        def request(payload: dict[str, object], timeout: float) -> dict[str, object]:
            captured.update(payload)
            return {
                "status": "ok",
                "target": "Foo.run",
                "direction": "out",
                "depth": 2,
                "node_count": 1,
                "node_limit": payload["node_limit"],
                "truncated": False,
                "root": {"name": "run", "path": "/repo/foo.py", "line": 1, "column": 1},
                "tree": {"node": {"name": "run", "path": "/repo/foo.py", "line": 1, "column": 1}, "children": []},
            }

        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", side_effect=request),
            redirect_stdout(io.StringIO()),
        ):
            main(["trace", "Foo.run", "--out", "--depth", "2", "--limit", "3", "--json"])
        self.assertEqual(captured["node_limit"], 3)

    def test_trace_keeps_default_node_limit_when_limit_is_omitted(self) -> None:
        captured: dict[str, object] = {}

        def request(payload: dict[str, object], timeout: float) -> dict[str, object]:
            captured.update(payload)
            return {"status": "not_found", "target": "Foo.run", "reason": "not found"}

        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", side_effect=request),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main(["trace", "Foo.run", "--out", "--json"])
        self.assertEqual(captured["node_limit"], 100)

    def test_trace_rejects_conflicting_limit_aliases(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["trace", "Foo.run", "--out", "--limit", "3", "--node-limit", "4"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("conflicting trace limits", stderr.getvalue())

    def test_trace_accepts_matching_limit_aliases(self) -> None:
        captured: dict[str, object] = {}

        def request(payload: dict[str, object], timeout: float) -> dict[str, object]:
            captured.update(payload)
            return {"status": "not_found", "target": "Foo.run", "reason": "not found"}

        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", side_effect=request),
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main(["trace", "Foo.run", "--out", "--limit", "3", "--node-limit", "3", "--json"])
        self.assertEqual(captured["node_limit"], 3)

    def test_negative_trace_depth_is_rejected(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["trace", "Foo.run", "--in", "--depth", "-1"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("must be >= 0", stderr.getvalue())

    def test_find_text_flag_reaches_request_payload(self) -> None:
        captured: dict[str, object] = {}

        def request(payload: dict[str, object], timeout: float) -> dict[str, object]:
            captured.update(payload)
            return {"status": "ok", "mode": "text", "query": "KEY", "results": [], "match_count": 0, "matching_line_count": 0, "returned_line_count": 0, "returned_match_count": 0, "test_line_count": 0, "truncated": False}

        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", side_effect=request),
            redirect_stdout(io.StringIO()),
        ):
            main(["find", "KEY", "--text", "--path", "frontend", "--glob", "*.ts", "--exclude-tests", "--json"])
        self.assertTrue(captured["text"])
        self.assertEqual(captured["paths"], ["frontend"])
        self.assertEqual(captured["globs"], ["*.ts"])
        self.assertTrue(captured["exclude_tests"])

    def test_context_lexical_override_reaches_request_payload(self) -> None:
        captured: dict[str, object] = {}

        def request(payload: dict[str, object], timeout: float) -> dict[str, object]:
            captured.update(payload)
            return {"status": "ok"}

        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", side_effect=request),
            redirect_stdout(io.StringIO()),
        ):
            main([
                "context",
                "Foo.run",
                "--lexical-references",
                "/logs/stream",
                "--path",
                "frontend",
                "--glob",
                "*.ts",
                "--exclude-tests",
                "--json",
            ])
        self.assertTrue(captured["lexical_references"])
        self.assertEqual(captured["lexical_query"], "/logs/stream")
        self.assertEqual(captured["lexical_paths"], ["frontend"])
        self.assertEqual(captured["semantic_paths"], [])
        self.assertEqual(captured["lexical_globs"], ["*.ts"])
        self.assertTrue(captured["lexical_exclude_tests"])

    def test_find_semantic_scope_reaches_request_payload(self) -> None:
        captured: dict[str, object] = {}

        def request(payload: dict[str, object], timeout: float) -> dict[str, object]:
            captured.update(payload)
            return {"status": "ok", "query": "KEY", "results": [], "result_count": 0, "total_candidates": 0, "errors": []}

        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", side_effect=request),
            redirect_stdout(io.StringIO()),
        ):
            main([
                "find",
                "architecture guard",
                "--path",
                "packages",
                "--glob",
                "*.py",
                "--exclude-tests",
                "--json",
            ])
        self.assertFalse(captured["text"])
        self.assertEqual(captured["paths"], ["packages"])
        self.assertEqual(captured["globs"], ["*.py"])
        self.assertTrue(captured["exclude_tests"])

    def test_find_semantic_path_keeps_empty_optional_filters(self) -> None:
        captured: dict[str, object] = {}

        def request(payload: dict[str, object], timeout: float) -> dict[str, object]:
            captured.update(payload)
            return {"status": "ok", "query": "KEY", "results": [], "result_count": 0, "total_candidates": 0, "errors": []}

        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", side_effect=request),
            redirect_stdout(io.StringIO()),
        ):
            main(["find", "KEY", "--path", "frontend", "--json"])
        self.assertEqual(captured["paths"], ["frontend"])
        self.assertEqual(captured["globs"], [])
        self.assertFalse(captured["exclude_tests"])

    def test_context_semantic_path_reaches_request_payload(self) -> None:
        captured: dict[str, object] = {}

        def request(payload: dict[str, object], timeout: float) -> dict[str, object]:
            captured.update(payload)
            return {"status": "ok"}

        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", side_effect=request),
            redirect_stdout(io.StringIO()),
        ):
            main(["context", "Foo.run", "--path", "backend", "--json"])
        self.assertEqual(captured["semantic_paths"], ["backend"])
        self.assertEqual(captured["lexical_paths"], [])

    def test_symbol_path_and_lexical_path_can_be_combined(self) -> None:
        captured: dict[str, object] = {}

        def request(payload: dict[str, object], timeout: float) -> dict[str, object]:
            captured.update(payload)
            return {"status": "ok"}

        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", side_effect=request),
            redirect_stdout(io.StringIO()),
        ):
            main([
                "context",
                "Foo.run",
                "--symbol-path",
                "backend",
                "--lexical-references",
                "KEY",
                "--path",
                "frontend",
                "--json",
            ])
        self.assertEqual(captured["semantic_paths"], ["backend"])
        self.assertEqual(captured["lexical_paths"], ["frontend"])

    def test_ambiguous_plain_output_contains_copyable_selection_commands(self) -> None:
        result = {
            "status": "ambiguous",
            "target": "Thing",
            "candidates": [
                {
                    "name": "Thing",
                    "kind": "Class",
                    "container": "",
                    "path": "/repo/src/model.py",
                    "line": 4,
                    "column": 7,
                    "selection_command": "codeq context src/model.py:4:7",
                }
            ],
        }
        stderr = io.StringIO()
        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", return_value=result),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["context", "Thing"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("try: codeq context src/model.py:4:7", stderr.getvalue())

    def test_ambiguous_find_output_contains_copyable_file_selection_commands(self) -> None:
        result = {
            "status": "ambiguous",
            "target": "model.py",
            "candidates": [
                {
                    "name": "model.py",
                    "kind": "File",
                    "container": "",
                    "path": "/repo/src/model.py",
                    "line": 1,
                    "column": 1,
                    "selection_command": "codeq context src/model.py",
                }
            ],
        }
        stderr = io.StringIO()
        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", return_value=result),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["find", "model.py"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("try: codeq context src/model.py", stderr.getvalue())

    def test_not_found_plain_output_includes_exact_name_recovery_candidates(self) -> None:
        result = {
            "status": "not_found",
            "target": "pkg.bridge.BridgeSession",
            "reason": "qualified target not found: pkg.bridge.BridgeSession",
            "candidates": [
                {
                    "name": "BridgeSession",
                    "kind": "Class",
                    "container": "",
                    "path": "/repo/pkg/bridge/session.py",
                    "line": 4,
                    "column": 7,
                    "selection_command": "codeq context pkg/bridge/session.py:4:7",
                }
            ],
        }
        stderr = io.StringIO()
        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", return_value=result),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["context", "pkg.bridge.BridgeSession"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Possible exact-name matches:", stderr.getvalue())
        self.assertIn("codeq context pkg/bridge/session.py:4:7", stderr.getvalue())

    def test_invalid_topology_plain_output_includes_whole_file_recovery(self) -> None:
        result = {
            "status": "invalid_query",
            "target": "Service.run",
            "reason": "--topology applies only to whole-file context; the target resolved to a symbol",
            "recovery_command": "codeq context src/service.py --topology",
        }
        stderr = io.StringIO()
        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", return_value=result),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["context", "Service.run", "--topology"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("whole-file context", stderr.getvalue())
        self.assertIn("try: codeq context src/service.py --topology", stderr.getvalue())

    def test_lexical_filters_require_lexical_reference_mode(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["context", "Foo.run", "--exclude-tests"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("require --lexical-references", stderr.getvalue())

    def test_success_still_exits_normally(self) -> None:
        result = {
            "status": "ok",
            "query": "Foo",
            "results": [],
            "result_count": 0,
            "total_candidates": 0,
            "errors": [],
            "_meta": {"duration_ms": 12.3},
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", return_value=result),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            main(["find", "Foo"])
        self.assertIn("No matches.", stdout.getvalue())
        self.assertIn("[0 results; 12.3 ms]", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_all_success_renderers_keep_stderr_empty(self) -> None:
        cases = [
            (
                ["find", "Foo"],
                {
                    "status": "ok",
                    "query": "Foo",
                    "results": [{"name": "Foo", "kind": "Class", "path": "/repo/foo.py", "line": 1, "column": 1, "source": "lsp"}],
                    "result_count": 1,
                    "_meta": {"duration_ms": 1.0},
                },
                "[1 results; 1.0 ms]",
            ),
            (
                ["context", "Foo.run"],
                {
                    "status": "ok",
                    "symbol": {"name": "run", "kind": "Method", "container": "Foo", "path": "/repo/foo.py", "line": 2, "column": 5},
                    "hover": "",
                    "source": {"text": ""},
                    "callers": [],
                    "callees": [],
                    "implementations": [],
                    "tests": [],
                    "references": [],
                    "possible_dynamic_references": [],
                    "_meta": {"duration_ms": 2.0},
                },
                "[2.0 ms]",
            ),
            (
                ["trace", "Foo.run", "--in", "--depth", "0"],
                {
                    "status": "ok",
                    "target": "Foo.run",
                    "direction": "in",
                    "depth": 0,
                    "node_count": 1,
                    "tree": {"node": {"name": "run", "path": "/repo/foo.py", "line": 2}, "children": []},
                    "_meta": {"duration_ms": 3.0},
                },
                "[1 nodes; depth=0; 3.0 ms]",
            ),
            (
                ["review", "--base", "HEAD~1"],
                {
                    "status": "ok",
                    "base": "HEAD~1",
                    "requested_base": "HEAD~1",
                    "base_mode": "direct",
                    "file_changes": [],
                    "changed_files": [],
                    "changed_file_count": 0,
                    "changed_symbols": [],
                    "changed_symbol_count": 0,
                    "impacted_files": [],
                    "impacted_file_count": 0,
                    "possible_dynamic_reference_count": 0,
                    "tests": [],
                    "test_count": 0,
                    "_meta": {"duration_ms": 4.0},
                },
                "[4.0 ms]",
            ),
        ]
        for argv, result, summary in cases:
            with self.subTest(argv=argv):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch("codeq.cli.git_root", return_value="/repo"),
                    patch("codeq.cli._request", return_value=result),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    main(argv)
                self.assertIn(summary, stdout.getvalue())
                self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
