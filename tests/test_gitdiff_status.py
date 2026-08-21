from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

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
                review = workspace.review("HEAD")
            finally:
                workspace.close()
            self.assertEqual(review["changed_file_count"], 2)
            self.assertEqual(review["deleted_file_count"], 1)
            self.assertEqual(review["renamed_file_count"], 1)
            by_status = {item["status"]: item for item in review["file_changes"]}
            self.assertEqual(by_status["D"]["semantic_status"], "deleted_not_analyzed")
            self.assertEqual(by_status["R"]["semantic_status"], "rename_or_copy_without_content_changes")


if __name__ == "__main__":
    unittest.main()
