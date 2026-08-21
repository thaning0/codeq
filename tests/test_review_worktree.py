from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeq.gitdiff import git_changed_files, git_merge_base, git_untracked_files, whole_file_range
from codeq.workspace import Workspace


class ReviewWorktreeTests(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()

    def _init(self, root: Path) -> None:
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "codeq@example.invalid")
        self._git(root, "config", "user.name", "codeq-test")

    def test_untracked_files_respect_git_ignore_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init(root)
            (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
            (root / "tracked.py").write_text("x = 1\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-qm", "base")

            visible = root / "new_feature.py"
            visible.write_text("def new_feature():\n    return 1\n", encoding="utf-8")
            (root / "ignored.py").write_text("ignored = True\n", encoding="utf-8")

            untracked = git_untracked_files(root)
            names = {Path(item["path"]).name for item in untracked}
            self.assertEqual(names, {"new_feature.py"})
            self.assertEqual(whole_file_range(visible), {
                "path": str(visible.resolve()),
                "start": 1,
                "end": 2,
            })

    def test_merge_base_excludes_base_only_commits_and_keeps_feature_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init(root)
            (root / "base.txt").write_text("base\n", encoding="utf-8")
            self._git(root, "add", ".")
            self._git(root, "commit", "-qm", "A")
            branch = self._git(root, "branch", "--show-current")

            self._git(root, "checkout", "-qb", "feature")
            (root / "feature.py").write_text("def feature():\n    return 1\n", encoding="utf-8")
            self._git(root, "add", "feature.py")
            self._git(root, "commit", "-qm", "feature")

            self._git(root, "checkout", branch)
            (root / "base_only.py").write_text("BASE_ONLY = True\n", encoding="utf-8")
            self._git(root, "add", "base_only.py")
            self._git(root, "commit", "-qm", "base advanced")

            self._git(root, "checkout", "feature")
            (root / "feature.py").write_text("def feature():\n    return 2\n", encoding="utf-8")

            direct = {Path(item["path"]).name for item in git_changed_files(root, branch)}
            merge_base = git_merge_base(root, branch)
            pr = {Path(item["path"]).name for item in git_changed_files(root, merge_base)}

            self.assertIn("base_only.py", direct)
            self.assertNotIn("base_only.py", pr)
            self.assertEqual(pr, {"feature.py"})
            self.assertEqual(merge_base, self._git(root, "rev-parse", "feature~1"))

            workspace = Workspace(root)
            try:
                with patch.object(workspace, "_changed_symbols_for_file", return_value=[]):
                    review = workspace.review(branch, merge_base=True)
            finally:
                workspace.close()
            self.assertEqual(review["requested_base"], branch)
            self.assertEqual(review["base_mode"], "merge-base")
            self.assertEqual(review["resolved_base"], merge_base)
            review_names = {Path(item["path"]).name for item in review["file_changes"]}
            self.assertEqual(review_names, {"feature.py"})


if __name__ == "__main__":
    unittest.main()
