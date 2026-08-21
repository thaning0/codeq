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


if __name__ == "__main__":
    unittest.main()
