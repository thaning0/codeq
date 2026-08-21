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
