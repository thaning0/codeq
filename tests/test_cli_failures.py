from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from codeq.cli import main


class CliFailureContractTests(unittest.TestCase):
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
            main(["find", "KEY", "--text", "--json"])
        self.assertTrue(captured["text"])

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
            main(["context", "Foo.run", "--lexical-references", "/logs/stream", "--json"])
        self.assertTrue(captured["lexical_references"])
        self.assertEqual(captured["lexical_query"], "/logs/stream")

    def test_success_still_exits_normally(self) -> None:
        result = {
            "status": "ok",
            "query": "Foo",
            "results": [],
            "result_count": 0,
            "total_candidates": 0,
            "errors": [],
        }
        stdout = io.StringIO()
        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", return_value=result),
            redirect_stdout(stdout),
        ):
            main(["find", "Foo"])
        self.assertIn("No matches.", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
