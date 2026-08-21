from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeq.contracts import EVIDENCE_BASE_SIDE_LEXICAL, EVIDENCE_CURRENT_SEMANTIC
from codeq.gitdiff import git_changed_files
from codeq.workspace import Workspace


class GitChangedFilesTests(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)

    def test_reports_added_modified_deleted_and_renamed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._git(root, "init", "-q")
            self._git(root, "config", "user.email", "codeq@example.invalid")
            self._git(root, "config", "user.name", "codeq-test")
            (root / "modified.py").write_text("x = 1\n", encoding="utf-8")
            (root / "deleted.py").write_text("deleted_value = 17\n", encoding="utf-8")
            (root / "old.py").write_text("renamed_value = 23\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-qm", "base")

            (root / "modified.py").write_text("x = 2\n", encoding="utf-8")
            (root / "deleted.py").unlink()
            self._git(root, "mv", "old.py", "new.py")
            (root / "added.py").write_text("added_value = 99\n", encoding="utf-8")
            self._git(root, "add", "-A")

            changes = git_changed_files(root, "HEAD")
            by_status = {item["status"]: item for item in changes}
            self.assertEqual(set(by_status), {"A", "M", "D", "R"})
            self.assertEqual(Path(by_status["A"]["path"]).name, "added.py")
            self.assertEqual(Path(by_status["M"]["path"]).name, "modified.py")
            self.assertEqual(Path(by_status["D"]["path"]).name, "deleted.py")
            self.assertEqual(Path(by_status["R"]["old_path"]).name, "old.py")
            self.assertEqual(Path(by_status["R"]["path"]).name, "new.py")

    def test_review_keeps_deleted_and_pure_rename_without_current_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._git(root, "init", "-q")
            self._git(root, "config", "user.email", "codeq@example.invalid")
            self._git(root, "config", "user.name", "codeq-test")
            (root / "deleted.py").write_text("deleted_value = 17\n", encoding="utf-8")
            (root / "old.py").write_text("renamed_value = 23\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-qm", "base")

            (root / "deleted.py").unlink()
            self._git(root, "mv", "old.py", "new.py")
            self._git(root, "add", "-A")

            workspace = Workspace(root)
            try:
                with patch.object(
                    workspace,
                    "_pure_rename_analysis",
                    return_value={"status": "ok", "evidence": EVIDENCE_CURRENT_SEMANTIC, "importers": [], "symbols": []},
                ):
                    review = workspace.review("HEAD")
            finally:
                workspace.close()
            self.assertEqual(review["changed_file_count"], 2)
            self.assertEqual(review["deleted_file_count"], 1)
            self.assertEqual(review["renamed_file_count"], 1)
            by_status = {item["status"]: item for item in review["file_changes"]}
            self.assertEqual(by_status["D"]["semantic_status"], "deleted_base_analyzed")
            self.assertEqual(by_status["D"]["base_analysis"]["evidence"], EVIDENCE_BASE_SIDE_LEXICAL)
            self.assertEqual(by_status["R"]["semantic_status"], "rename_analyzed")
            self.assertEqual(by_status["R"]["rename_analysis"]["evidence"], EVIDENCE_CURRENT_SEMANTIC)

    def test_deleted_file_reports_residual_references_and_tests_from_base_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._git(root, "init", "-q")
            self._git(root, "config", "user.email", "codeq@example.invalid")
            self._git(root, "config", "user.name", "codeq-test")
            (root / "deleted.py").write_text("def old_api():\n    return 1\n", encoding="utf-8")
            (root / "consumer.py").write_text("from deleted import old_api\nvalue = old_api()\n", encoding="utf-8")
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_consumer.py").write_text("from deleted import old_api\nassert old_api() == 1\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-qm", "base")
            (root / "deleted.py").unlink()

            workspace = Workspace(root)
            try:
                review = workspace.review("HEAD", limit=10)
            finally:
                workspace.close()

            deleted = next(item for item in review["file_changes"] if item["status"] == "D")
            analysis = deleted["base_analysis"]
            self.assertEqual(analysis["status"], "ok")
            old_api = next(item for item in analysis["base_symbols"] if item["symbol"]["name"] == "old_api")
            self.assertGreaterEqual(old_api["residual_match_count"], 4)
            self.assertTrue(old_api["residual_references"])
            self.assertTrue(old_api["tests"])
            self.assertTrue(all(item["is_test"] for item in old_api["tests"]))


if __name__ == "__main__":
    unittest.main()
