from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from codeq.contracts import MAX_LINE_WINDOW_CHARS
from codeq.workspace import Workspace


class _FakeSession:
    def document_symbols(self, path: Path, timeout: float | None = None):
        return [
            {
                "name": "Service",
                "kind": 5,
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 12, "character": 0}},
                "selectionRange": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 13}},
                "children": [
                    {
                        "name": "run",
                        "kind": 6,
                        "range": {"start": {"line": 2, "character": 4}, "end": {"line": 6, "character": 0}},
                        "selectionRange": {"start": {"line": 2, "character": 8}, "end": {"line": 2, "character": 11}},
                        "children": [
                            {
                                "name": "local_value",
                                "kind": 13,
                                "range": {"start": {"line": 3, "character": 8}, "end": {"line": 3, "character": 19}},
                                "selectionRange": {"start": {"line": 3, "character": 8}, "end": {"line": 3, "character": 19}},
                            }
                        ],
                    },
                    {
                        "name": "stop",
                        "kind": 6,
                        "range": {"start": {"line": 8, "character": 4}, "end": {"line": 10, "character": 0}},
                        "selectionRange": {"start": {"line": 8, "character": 8}, "end": {"line": 8, "character": 12}},
                    },
                ],
            },
            {
                "name": "helper",
                "kind": 12,
                "range": {"start": {"line": 15, "character": 0}, "end": {"line": 18, "character": 0}},
                "selectionRange": {"start": {"line": 15, "character": 4}, "end": {"line": 15, "character": 10}},
            },
        ]

    def definitions(self, path: Path, line: int, column: int):
        return []

    def hover(self, path: Path, line: int, column: int):
        return None

    def close(self):
        return None


class FileContextDisclosureTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Workspace:
        workspace = Workspace(root)
        fake = _FakeSession()
        cast(Any, workspace)._session = lambda project: fake
        return workspace

    def test_default_file_context_discloses_top_level_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.py"
            source.write_text("class Service:\n    pass\n\ndef helper():\n    pass\n", encoding="utf-8")
            workspace = self._workspace(root)
            with (
                patch("codeq.workspace.extract_imports", return_value=[]),
                patch("codeq.workspace.importer_candidate_hits", side_effect=AssertionError("topology must stay lazy")),
            ):
                result = workspace.context(str(source), limit=20)
            self.assertEqual(result["symbol_count"], 5)
            self.assertEqual([item["name"] for item in result["outline"]], ["Service", "helper"])
            self.assertFalse(result["outline_truncated"])
            self.assertFalse(result["topology_loaded"])

    def test_lines_adds_a_source_window_to_a_file_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.py"
            source.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
            workspace = self._workspace(root)
            with (
                patch("codeq.workspace.extract_imports", return_value=[]),
                patch("codeq.workspace.importer_candidate_hits", return_value=[]),
            ):
                result = workspace.context(str(source), line_window_lines=3)
            self.assertEqual(result["line_window"]["start_line"], 1)
            self.assertEqual(result["line_window"]["end_line"], 3)
            self.assertEqual(result["line_window"]["returned_line_count"], 3)
            self.assertNotIn("four", result["line_window"]["text"])

    def test_line_window_character_cap_returns_a_copyable_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.py"
            source.write_text(("x" * 600 + "\n") * 250, encoding="utf-8")
            workspace = self._workspace(root)
            with (
                patch("codeq.workspace.extract_imports", return_value=[]),
                patch("codeq.workspace.importer_candidate_hits", return_value=[]),
            ):
                result = workspace.context(str(source), line_window_lines=250)
            window = result["line_window"]
            self.assertTrue(window["payload_truncated"])
            self.assertLessEqual(len(window["text"]), MAX_LINE_WINDOW_CHARS)
            self.assertEqual(window["next_line"], window["end_line"] + 1)
            remaining = 250 - window["returned_line_count"]
            self.assertEqual(
                window["recovery_command"],
                f"codeq context sample.py:{window['next_line']} --lines {remaining}",
            )

    def test_file_context_can_expand_container_or_filter_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.py"
            source.write_text("class Service:\n    pass\n\ndef helper():\n    pass\n", encoding="utf-8")
            workspace = self._workspace(root)
            with (
                patch("codeq.workspace.extract_imports", return_value=[]),
                patch("codeq.workspace.importer_candidate_hits", return_value=[]),
            ):
                container = workspace.context(str(source), limit=20, container="Service", outline_depth=1)
                methods = workspace.context(str(source), limit=1, outline_kind="method")
            self.assertEqual([item["name"] for item in container["outline"]], ["Service", "run", "stop"])
            self.assertEqual(methods["outline_matching_count"], 2)
            self.assertEqual(len(methods["outline"]), 1)
            self.assertTrue(methods["outline_truncated"])

    def test_topology_is_explicitly_disclosed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.py"
            source.write_text("class Service:\n    pass\n", encoding="utf-8")
            workspace = self._workspace(root)
            fake_import = {"specifier": "app.dep", "line": 1, "column": 1, "names": ["Dep"]}
            with (
                patch("codeq.workspace.extract_imports", return_value=[fake_import]),
                patch("codeq.workspace.resolve_import_specifier", return_value=[]),
                patch("codeq.workspace.importer_candidate_hits", return_value=[]),
            ):
                result = workspace.context(str(source), limit=20, include_topology=True)
            self.assertTrue(result["topology_loaded"])
            self.assertEqual(result["import_count"], 1)
            self.assertEqual(len(result["imports"]), 1)

    def test_topology_on_symbol_preserves_symbol_and_adds_containing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.py"
            source.write_text("class Service:\n    def run(self):\n        pass\n", encoding="utf-8")
            workspace = self._workspace(root)
            symbol = {
                "name": "run",
                "kind": "Method",
                "container": "Service",
                "path": str(source),
                "line": 2,
                "column": 9,
            }
            fake_import = {"specifier": "app.dep", "line": 1, "column": 1, "names": ["Dep"]}
            with (
                patch.object(workspace, "resolve", return_value={"status": "ok", "symbol": symbol}),
                patch("codeq.workspace.extract_imports", return_value=[fake_import]),
                patch("codeq.workspace.resolve_import_specifier", return_value=[]),
                patch("codeq.workspace.importer_candidate_hits", return_value=[]),
            ):
                result = workspace.context(
                    "Service.run",
                    include_topology=True,
                    selected_sections=("source",),
                )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["symbol"], symbol)
            self.assertEqual(result["file_topology"]["status"], "ok")
            self.assertEqual(result["file_topology"]["scope"], "containing_file")
            self.assertEqual(result["file_topology"]["path"], str(source))
            self.assertEqual(result["file_topology"]["import_count"], 1)
            self.assertEqual(len(result["file_topology"]["imports"]), 1)

    def test_dotted_module_keeps_file_topology_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.py"
            source.write_text("class Service:\n    pass\n", encoding="utf-8")
            workspace = self._workspace(root)
            with (
                patch.object(workspace, "_file_target", return_value=None),
                patch.object(workspace, "_dotted_module_candidates", return_value=[source]),
                patch("codeq.workspace.extract_imports", return_value=[]),
                patch("codeq.workspace.importer_candidate_hits", return_value=[]),
            ):
                result = workspace.context("pkg.sample", include_topology=True)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["topology_loaded"])


if __name__ == "__main__":
    unittest.main()
