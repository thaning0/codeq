from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from codeq.textsearch import git_tracked_text_search


class TextSearchTests(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)

    def test_exact_text_search_is_tracked_ignore_aware_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._git(root, "init", "-q")
            self._git(root, "config", "user.email", "codeq@example.invalid")
            self._git(root, "config", "user.name", "codeq-test")
            (root / ".gitignore").write_text("ignored.env\n", encoding="utf-8")
            (root / "app.py").write_text(
                'KEY = "BACKTEST_TARGET"\nprint("BACKTEST_TARGET", "BACKTEST_TARGET")\n',
                encoding="utf-8",
            )
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_app.py").write_text('assert "BACKTEST_TARGET"\n', encoding="utf-8")
            (root / "ignored.env").write_text("BACKTEST_TARGET=1\n", encoding="utf-8")
            (root / "untracked.yaml").write_text("value: BACKTEST_TARGET\n", encoding="utf-8")
            self._git(root, "add", ".gitignore", "app.py", "tests/test_app.py")
            self._git(root, "commit", "-qm", "base")

            result = git_tracked_text_search(root, "BACKTEST_TARGET", limit=2)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["match_count"], 4)
            self.assertEqual(result["matching_line_count"], 3)
            self.assertEqual(result["returned_line_count"], 2)
            self.assertTrue(result["truncated"])
            self.assertEqual(result["test_line_count"], 1)
            returned_paths = {Path(item["path"]).name for item in result["results"]}
            self.assertNotIn("ignored.env", returned_paths)
            self.assertNotIn("untracked.yaml", returned_paths)
            self.assertTrue(all("text" in item for item in result["results"]))

    def test_empty_text_query_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = git_tracked_text_search(Path(tmp), "", limit=5)
            self.assertEqual(result["status"], "invalid_query")


if __name__ == "__main__":
    unittest.main()
