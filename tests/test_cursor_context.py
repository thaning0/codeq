from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from codeq.workspace import Workspace


class _CursorSession:
    def __init__(self, api_path: Path, service_path: Path):
        self.api_path = api_path.resolve()
        self.service_path = service_path.resolve()

    def document_symbols(self, path: Path, timeout: float | None = None):
        resolved = path.resolve()
        if resolved == self.api_path:
            return [
                {
                    "name": "endpoint",
                    "kind": 12,
                    "range": {"start": {"line": 0, "character": 0}, "end": {"line": 3, "character": 20}},
                    "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 12}},
                    "children": [],
                }
            ]
        if resolved == self.service_path:
            return [
                {
                    "name": "service_call",
                    "kind": 12,
                    "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 12}},
                    "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 16}},
                    "children": [],
                }
            ]
        return []

    def definitions(self, path: Path, line: int, column: int):
        if path.resolve() == self.api_path and line == 2 and column == 12:
            return [
                {
                    "uri": self.service_path.as_uri(),
                    "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 16}},
                }
            ]
        return []

    def references(self, path: Path, line: int, column: int):
        return []

    def implementations(self, path: Path, line: int, column: int):
        return []

    def prepare_call_hierarchy(self, path: Path, line: int, column: int):
        return []

    def hover(self, path: Path, line: int, column: int):
        return None


class CursorContextTests(unittest.TestCase):
    def test_path_line_column_prefers_cursor_definition_and_keeps_request_snippet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='cursor-test'\nversion='0'\n", encoding="utf-8")
            api = root / "api.py"
            service = root / "service.py"
            api.write_text("def endpoint():\n    return service_call()\n    # tail\n", encoding="utf-8")
            service.write_text("def service_call():\n    return 1\n", encoding="utf-8")
            workspace = Workspace(root)
            fake = _CursorSession(api, service)
            cast(Any, workspace)._session = lambda project: fake
            cast(Any, workspace)._prewarm_symbol = lambda *args, **kwargs: None

            result = workspace.context("api.py:2:12")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["symbol"]["name"], "service_call")
            self.assertEqual(Path(result["symbol"]["path"]), service.resolve())
            self.assertTrue(result["cursor_definition"])
            self.assertEqual(result["requested_location"]["line"], 2)
            self.assertIn("return service_call()", result["request_source"]["text"])
            self.assertIn("def service_call", result["source"]["text"])

    def test_path_line_without_column_keeps_enclosing_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='cursor-test'\nversion='0'\n", encoding="utf-8")
            api = root / "api.py"
            service = root / "service.py"
            api.write_text("def endpoint():\n    return service_call()\n", encoding="utf-8")
            service.write_text("def service_call():\n    return 1\n", encoding="utf-8")
            workspace = Workspace(root)
            fake = _CursorSession(api, service)
            cast(Any, workspace)._session = lambda project: fake
            cast(Any, workspace)._prewarm_symbol = lambda *args, **kwargs: None

            result = workspace.context("api.py:2")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["symbol"]["name"], "endpoint")
            self.assertFalse(result["cursor_definition"])

    def test_lines_adds_a_source_window_starting_at_the_requested_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                "[project]\nname='cursor-test'\nversion='0'\n",
                encoding="utf-8",
            )
            api = root / "api.py"
            service = root / "service.py"
            api.write_text(
                "def endpoint():\n" + "".join(f"    value_{line} = {line}\n" for line in range(2, 151)),
                encoding="utf-8",
            )
            service.write_text("def service_call():\n    return 1\n", encoding="utf-8")
            workspace = Workspace(root)
            fake = _CursorSession(api, service)
            cast(Any, workspace)._session = lambda project: fake
            cast(Any, workspace)._prewarm_symbol = lambda *args, **kwargs: None

            result = workspace.context("api.py:2", line_window_lines=120)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["line_window"]["start_line"], 2)
            self.assertEqual(result["line_window"]["end_line"], 121)
            self.assertEqual(result["line_window"]["requested_line_count"], 120)
            self.assertEqual(result["line_window"]["returned_line_count"], 120)
            self.assertIn("value_121", result["line_window"]["text"])
            self.assertNotIn("value_122", result["line_window"]["text"])

    def test_trace_without_call_hierarchy_keeps_limit_disclosure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                "[project]\nname='cursor-test'\nversion='0'\n",
                encoding="utf-8",
            )
            api = root / "api.py"
            service = root / "service.py"
            api.write_text("def endpoint():\n    return 1\n", encoding="utf-8")
            service.write_text("def service_call():\n    return 1\n", encoding="utf-8")
            workspace = Workspace(root)
            fake = _CursorSession(api, service)
            cast(Any, workspace)._session = lambda project: fake
            try:
                result = workspace.trace("api.py:1", "out", depth=2, limit=7)
            finally:
                workspace.close()

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["node_count"], 1)
            self.assertEqual(result["node_limit"], 7)
            self.assertFalse(result["truncated"])
            self.assertEqual(
                result["note"],
                "language server returned no call hierarchy for this position",
            )

    def test_trace_both_prepares_once_and_returns_incoming_and_outgoing_trees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                "[project]\nname='cursor-test'\nversion='0'\n",
                encoding="utf-8",
            )
            api = root / "api.py"
            service = root / "service.py"
            api.write_text("def endpoint():\n    return service_call()\n", encoding="utf-8")
            service.write_text("def service_call():\n    return 1\n", encoding="utf-8")

            class _CallHierarchySession(_CursorSession):
                def __init__(self, api_path: Path, service_path: Path):
                    super().__init__(api_path, service_path)
                    self.prepare_count = 0

                @staticmethod
                def _item(name: str, path: Path) -> dict[str, object]:
                    return {
                        "name": name,
                        "kind": 12,
                        "uri": path.resolve().as_uri(),
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": len(name)},
                        },
                        "selectionRange": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": len(name)},
                        },
                    }

                def prepare_call_hierarchy(self, path: Path, line: int, column: int):
                    self.prepare_count += 1
                    return [self._item("endpoint", self.api_path)]

                def incoming_calls(self, item: dict[str, object]):
                    return [{"from": self._item("caller", self.service_path)}]

                def outgoing_calls(self, item: dict[str, object]):
                    return [{"to": self._item("callee", self.service_path)}]

            workspace = Workspace(root)
            fake = _CallHierarchySession(api, service)
            cast(Any, workspace)._session = lambda project: fake
            cast(Any, workspace)._prewarm_symbol = lambda *args, **kwargs: None
            try:
                result = workspace.trace("api.py:1", "both", depth=1, limit=7)
            finally:
                workspace.close()

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["direction"], "both")
            self.assertEqual(fake.prepare_count, 1)
            self.assertEqual(
                result["traces"]["in"]["tree"]["children"][0]["node"]["name"],
                "caller",
            )
            self.assertEqual(
                result["traces"]["out"]["tree"]["children"][0]["node"]["name"],
                "callee",
            )
            self.assertEqual(result["traces"]["in"]["node_limit"], 7)
            self.assertEqual(result["traces"]["out"]["node_limit"], 7)
            self.assertIn("--in", result["hint"])
            self.assertIn("--out", result["hint"])

    def test_multiple_local_definitions_collapse_to_shared_enclosing_function(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='cursor-test'\nversion='0'\n", encoding="utf-8")
            source = root / "choose.py"
            source.write_text(
                "def choose(flag):\n"
                "    if flag:\n"
                "        value = 1\n"
                "    else:\n"
                "        value = 2\n"
                "    return value\n",
                encoding="utf-8",
            )

            class _MultipleDefinitionSession:
                def document_symbols(self, path: Path, timeout: float | None = None):
                    return [
                        {
                            "name": "choose",
                            "kind": 12,
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 5, "character": 16},
                            },
                            "selectionRange": {
                                "start": {"line": 0, "character": 4},
                                "end": {"line": 0, "character": 10},
                            },
                            "children": [
                                {
                                    "name": "value",
                                    "kind": 13,
                                    "range": {
                                        "start": {"line": 2, "character": 8},
                                        "end": {"line": 2, "character": 17},
                                    },
                                    "selectionRange": {
                                        "start": {"line": 2, "character": 8},
                                        "end": {"line": 2, "character": 13},
                                    },
                                },
                                {
                                    "name": "value",
                                    "kind": 13,
                                    "range": {
                                        "start": {"line": 4, "character": 8},
                                        "end": {"line": 4, "character": 17},
                                    },
                                    "selectionRange": {
                                        "start": {"line": 4, "character": 8},
                                        "end": {"line": 4, "character": 13},
                                    },
                                },
                            ],
                        }
                    ]

                def definitions(self, path: Path, line: int, column: int):
                    return [
                        {
                            "uri": source.as_uri(),
                            "range": {
                                "start": {"line": 2, "character": 8},
                                "end": {"line": 2, "character": 13},
                            },
                        },
                        {
                            "uri": source.as_uri(),
                            "range": {
                                "start": {"line": 4, "character": 8},
                                "end": {"line": 4, "character": 13},
                            },
                        },
                    ]

            workspace = Workspace(root)
            fake = _MultipleDefinitionSession()
            cast(Any, workspace)._session = lambda project: fake
            try:
                result = workspace.resolve("choose.py:6:12")
            finally:
                workspace.close()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["symbol"]["name"], "choose")
            self.assertEqual(result["cursor_definition_count"], 2)
            self.assertIn("same enclosing function", result["definition_note"])


if __name__ == "__main__":
    unittest.main()
