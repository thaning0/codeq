from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

from codeq.workspace import Project, Workspace


class _ReferenceSession:
    def __init__(self, consumer: Path, test_file: Path):
        self.consumer = consumer.resolve()
        self.test_file = test_file.resolve()

    def references(self, path: Path, line: int, column: int):
        return [
            {
                "uri": self.consumer.as_uri(),
                "range": {"start": {"line": 0, "character": 5}, "end": {"line": 0, "character": 16}},
            },
            {
                "uri": self.test_file.as_uri(),
                "range": {"start": {"line": 0, "character": 5}, "end": {"line": 0, "character": 16}},
            },
        ]


class ReviewEdgeAnalysisTests(unittest.TestCase):
    def test_pure_rename_analysis_prewarms_and_separates_test_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            renamed = root / "new.py"
            consumer = root / "consumer.py"
            tests = root / "tests"
            tests.mkdir()
            test_file = tests / "test_consumer.py"
            for path in (renamed, consumer, test_file):
                path.write_text("# placeholder\n", encoding="utf-8")

            workspace = Workspace(root)
            project = Project(root.resolve(), "python")
            symbol = {
                "name": "renamed_api",
                "kind": "Function",
                "container": "",
                "path": str(renamed.resolve()),
                "line": 1,
                "column": 1,
            }
            topology = {
                "status": "ok",
                "outline": [symbol],
                "outline_matching_count": 1,
                "importers": [
                    {"path": str(consumer.resolve()), "line": 1, "column": 1, "text": "from new import renamed_api"},
                    {"path": str(test_file.resolve()), "line": 1, "column": 1, "text": "from new import renamed_api"},
                ],
                "importer_count": 2,
                "importers_truncated": False,
            }
            session = _ReferenceSession(consumer, test_file)
            prewarm = Mock()
            with (
                patch.object(workspace, "_file_context", return_value=topology),
                patch.object(workspace, "_session_and_position", return_value=(session, project, renamed, 1, 1)),
                patch.object(workspace, "_prewarm_symbol", prewarm),
            ):
                result = workspace._pure_rename_analysis(renamed, limit=10)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["importer_count"], 2)
            self.assertEqual(len(result["symbols"]), 1)
            summary = result["symbols"][0]
            self.assertEqual(summary["reference_count"], 2)
            self.assertEqual(len(summary["references"]), 1)
            self.assertEqual(len(summary["tests"]), 1)
            self.assertTrue(summary["tests"][0]["path"].endswith("tests/test_consumer.py"))
            prewarm.assert_called_once()


if __name__ == "__main__":
    unittest.main()
