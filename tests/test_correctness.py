from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeq.util import exact_definition_hits
from codeq.workspace import Workspace


class CorrectnessGuardTests(unittest.TestCase):
    def test_existing_unsupported_files_never_fall_back_to_fuzzy_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shell = root / "deploy.sh"
            sql = root / "query.sql"
            shell.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            sql.write_text("select 1;\n", encoding="utf-8")
            workspace = Workspace(root)
            try:
                self.assertEqual(workspace.context(str(shell))["status"], "unsupported_language")
                self.assertEqual(workspace.resolve(f"{sql}:1")["status"], "unsupported_language")
                self.assertEqual(workspace.find(str(shell))["status"], "unsupported_language")
            finally:
                workspace.close()

    def test_qualified_target_never_falls_back_to_global_fuzzy_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp))
            exact_failure = {
                "status": "not_found",
                "target": "Foo.run",
                "reason": "qualified member not found in Foo: run",
                "candidates": [],
            }
            try:
                with (
                    patch.object(workspace, "_resolve_qualified", return_value=exact_failure),
                    patch.object(workspace, "find", side_effect=AssertionError("fuzzy fallback must not run")),
                ):
                    result = workspace.resolve("Foo.run")
            finally:
                workspace.close()
            self.assertEqual(result, exact_failure)

    def test_exact_definition_scan_is_not_displaced_by_many_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "service.py"
            source.write_text("class HotService:\n    pass\n", encoding="utf-8")
            tests = root / "tests"
            tests.mkdir()
            for index in range(40):
                (tests / f"test_{index}.py").write_text(
                    "from service import HotService\n" + "\n".join("HotService" for _ in range(20)) + "\n",
                    encoding="utf-8",
                )
            hits = exact_definition_hits(root, "HotService")
            self.assertTrue(hits)
            self.assertEqual(Path(hits[0]["path"]), source.resolve())
            self.assertEqual(hits[0]["line"], 1)


if __name__ == "__main__":
    unittest.main()
