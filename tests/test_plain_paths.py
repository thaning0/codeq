from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest.mock import patch

from codeq.cli import main


class PlainPathRenderingTests(unittest.TestCase):
    def _render(self, argv: list[str], result: dict[str, Any]) -> tuple[str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", return_value=result),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            main(argv)
        return stdout.getvalue(), stderr.getvalue()

    def test_find_plain_paths_are_repository_relative(self) -> None:
        cases = [
            (
                ["find", "Thing"],
                {
                    "status": "ok",
                    "results": [
                        {
                            "name": "Thing",
                            "kind": "Class",
                            "path": "/repo/src/model.py",
                            "line": 4,
                            "column": 7,
                            "source": "lsp",
                        }
                    ],
                    "result_count": 1,
                },
            ),
            (
                ["find", "needle", "--text"],
                {
                    "status": "ok",
                    "mode": "text",
                    "results": [
                        {
                            "path": "/repo/src/model.py",
                            "line": 8,
                            "column": 2,
                            "text": "needle",
                        }
                    ],
                    "match_count": 1,
                    "matching_line_count": 1,
                    "matching_file_count": 1,
                    "returned_line_count": 1,
                },
            ),
        ]
        for argv, result in cases:
            with self.subTest(argv=argv):
                stdout, _ = self._render(argv, result)
                self.assertIn("src/model.py", stdout)
                self.assertNotIn("/repo/", stdout)

    def test_symbol_context_shortens_every_location_family(self) -> None:
        locations = [
            {"name": "neighbor", "path": "/repo/src/neighbor.py", "line": 3, "column": 2}
        ]
        result: dict[str, object] = {
            "status": "ok",
            "symbol": {
                "name": "run",
                "kind": "Method",
                "container": "Thing",
                "path": "/repo/src/model.py",
                "line": 4,
                "column": 7,
            },
            "requested_location": {"path": "/repo/src/api.py", "line": 9, "column": 5},
            "definition_note": "definition resolved from /repo/src/api.py",
            "callers": locations,
            "callees": locations,
            "implementations": locations,
            "tests": locations,
            "references": locations,
            "possible_dynamic_references": [
                {"path": "/repo/src/registry.py", "line": 6, "column": 1, "reason": "string"}
            ],
            "lexical_references": {
                "query": "run",
                "match_count": 1,
                "matching_line_count": 1,
                "results": [
                    {"path": "/repo/docs/usage.md", "line": 2, "column": 1, "text": "run"}
                ],
            },
        }
        stdout, _ = self._render(["context", "Thing.run"], result)
        for expected in (
            "src/model.py:4:7",
            "src/api.py:9:5",
            "src/neighbor.py:3:2",
            "src/registry.py:6:1",
            "docs/usage.md:2:1",
        ):
            self.assertIn(expected, stdout)
        self.assertNotIn("/repo/", stdout)

    def test_file_context_shortens_topology_paths(self) -> None:
        result = {
            "status": "ok",
            "kind": "file",
            "file": {"path": "/repo/src/model.py", "language": "python"},
            "outline": [],
            "topology_loaded": True,
            "imports": [
                {
                    "specifier": ".helper",
                    "line": 1,
                    "resolved_paths": ["/repo/src/helper.py", "/opt/shared.py"],
                }
            ],
            "importers": [
                {"path": "/repo/src/api.py", "line": 2, "column": 1, "text": "import model"}
            ],
        }
        stdout, _ = self._render(["context", "src/model.py", "--topology"], result)
        self.assertIn("File src/model.py", stdout)
        self.assertIn("src/helper.py, /opt/shared.py", stdout)
        self.assertIn("src/api.py:2:1", stdout)
        self.assertNotIn("/repo/", stdout)

    def test_symbol_context_renders_containing_file_topology(self) -> None:
        result = {
            "status": "ok",
            "symbol": {
                "name": "run",
                "kind": "Method",
                "container": "Thing",
                "path": "/repo/src/model.py",
                "line": 4,
                "column": 7,
            },
            "section_metadata": {},
            "file_topology": {
                "status": "ok",
                "scope": "containing_file",
                "path": "/repo/src/model.py",
                "imports": [
                    {
                        "specifier": ".helper",
                        "line": 1,
                        "resolved_paths": ["/repo/src/helper.py"],
                    }
                ],
                "import_count": 1,
                "imports_truncated": False,
                "importers": [
                    {"path": "/repo/src/api.py", "line": 2, "column": 1, "text": "import model"}
                ],
                "importer_count": 1,
                "importers_truncated": False,
            },
        }
        stdout, _ = self._render(["context", "Thing.run", "--topology"], result)
        self.assertIn("Containing file topology (src/model.py)", stdout)
        self.assertIn("src/helper.py", stdout)
        self.assertIn("src/api.py:2:1", stdout)
        self.assertNotIn("/repo/", stdout)

    def test_trace_and_review_plain_paths_are_repository_relative(self) -> None:
        trace = {
            "status": "ok",
            "target": "Thing.run",
            "direction": "out",
            "depth": 1,
            "node_count": 2,
            "node_limit": 100,
            "truncated": False,
            "tree": {
                "node": {"name": "run", "path": "/repo/src/model.py", "line": 4},
                "children": [
                    {
                        "node": {"name": "helper", "path": "/repo/src/helper.py", "line": 2},
                        "children": [],
                    }
                ],
            },
        }
        stdout, _ = self._render(["trace", "Thing.run", "--out"], trace)
        self.assertIn("src/model.py:4", stdout)
        self.assertIn("src/helper.py:2", stdout)
        self.assertNotIn("/repo/", stdout)

        review = {
            "status": "ok",
            "base": "HEAD~1",
            "file_changes": [
                {"status": "R", "old_path": "/repo/src/old.py", "path": "/repo/src/new.py"}
            ],
            "changed_file_count": 1,
            "changed_symbols": [
                {
                    "symbol": {"kind": "Function", "name": "run", "path": "/repo/src/new.py", "line": 1},
                    "callers": [{"name": "main", "path": "/repo/src/main.py", "line": 2}],
                    "possible_dynamic_references": [
                        {"reason": "string", "path": "/repo/src/routes.py", "line": 3}
                    ],
                    "tests": [{"path": "/repo/tests/test_new.py", "line": 4}],
                }
            ],
            "changed_symbol_count": 1,
            "impacted_files": ["/repo/src/main.py"],
            "impacted_file_count": 1,
            "tests": [{"name": "test_run", "path": "/repo/tests/test_new.py", "line": 4}],
            "test_count": 1,
        }
        stdout, _ = self._render(["review"], review)
        for expected in (
            "src/old.py -> src/new.py",
            "src/main.py:2",
            "src/routes.py:3",
            "tests/test_new.py:4",
        ):
            self.assertIn(expected, stdout)
        self.assertNotIn("/repo/", stdout)

    def test_json_keeps_absolute_paths_and_outside_paths_stay_absolute(self) -> None:
        result = {
            "status": "ok",
            "results": [
                {
                    "name": "Thing",
                    "kind": "Class",
                    "path": "/repo/src/model.py",
                    "line": 4,
                    "column": 7,
                    "source": "lsp",
                },
                {
                    "name": "External",
                    "kind": "Class",
                    "path": "/opt/external.py",
                    "line": 1,
                    "column": 1,
                    "source": "lsp",
                },
            ],
            "result_count": 2,
        }
        stdout, _ = self._render(["find", "Thing"], result)
        self.assertIn("src/model.py:4:7", stdout)
        self.assertIn("/opt/external.py:1:1", stdout)

        stdout, _ = self._render(["find", "Thing", "--json"], result)
        payload = json.loads(stdout)
        self.assertEqual(payload["results"][0]["path"], "/repo/src/model.py")

    def test_plain_failure_shortens_embedded_repository_path(self) -> None:
        result = {
            "status": "not_found",
            "target": "missing.py",
            "reason": "file not found: /repo/src/missing.py",
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", return_value=result),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["context", "missing.py"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("file not found: src/missing.py", stderr.getvalue())
        self.assertNotIn("/repo/", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
