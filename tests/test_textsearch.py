from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from codeq.textsearch import git_text_search


class TextSearchTests(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)

    def test_exact_text_search_includes_nonignored_untracked_and_counts_sources(self) -> None:
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

            result = git_text_search(root, "BACKTEST_TARGET", limit=3)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["match_count"], 5)
            self.assertEqual(result["matching_line_count"], 4)
            self.assertEqual(result["matching_file_count"], 3)
            self.assertEqual(result["tracked_line_count"], 3)
            self.assertEqual(result["untracked_line_count"], 1)
            self.assertEqual(result["test_line_count"], 1)
            self.assertEqual(result["returned_line_count"], 3)
            self.assertTrue(result["truncated"])

            all_result = git_text_search(root, "BACKTEST_TARGET", limit=20)
            by_name = {Path(item["path"]).name: item for item in all_result["results"]}
            self.assertNotIn("ignored.env", by_name)
            self.assertIn("untracked.yaml", by_name)
            self.assertFalse(by_name["untracked.yaml"]["tracked"])
            self.assertEqual(by_name["untracked.yaml"]["git_status"], "untracked")
            self.assertTrue(by_name["test_app.py"]["is_test"])

    def test_path_glob_and_test_filters_apply_to_counts_and_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._git(root, "init", "-q")
            self._git(root, "config", "user.email", "codeq@example.invalid")
            self._git(root, "config", "user.name", "codeq-test")
            (root / ".gitignore").write_text("generated/\n", encoding="utf-8")
            (root / "frontend/src").mkdir(parents=True)
            (root / "frontend/tests").mkdir(parents=True)
            (root / "quant-cli/src").mkdir(parents=True)
            (root / "infra").mkdir()
            (root / "generated").mkdir()
            (root / "frontend/src/api.ts").write_text('const route = "/logs/stream";\n', encoding="utf-8")
            (root / "frontend/tests/api.test.ts").write_text('expect("/logs/stream");\n', encoding="utf-8")
            (root / "quant-cli/src/api.ts").write_text('const route = "/logs/stream";\n', encoding="utf-8")
            (root / "infra/routes.yaml").write_text('route: "/logs/stream"\n', encoding="utf-8")
            (root / "generated/ignored.yaml").write_text('route: "/logs/stream"\n', encoding="utf-8")
            self._git(root, "add", ".gitignore", "frontend/src/api.ts", "frontend/tests/api.test.ts")
            self._git(root, "commit", "-qm", "base")

            result = git_text_search(
                root,
                "/logs/stream",
                limit=20,
                paths=("frontend", "quant-cli"),
                globs=("*.ts",),
                exclude_tests=True,
            )
            self.assertEqual(result["match_count"], 2)
            self.assertEqual(result["matching_line_count"], 2)
            self.assertEqual(result["test_line_count"], 0)
            self.assertEqual(result["tracked_line_count"], 1)
            self.assertEqual(result["untracked_line_count"], 1)
            relative_paths = {item["relative_path"] for item in result["results"]}
            self.assertEqual(relative_paths, {"frontend/src/api.ts", "quant-cli/src/api.ts"})

            yaml_only = git_text_search(root, "/logs/stream", globs=("*.yaml",), limit=20)
            self.assertEqual([item["relative_path"] for item in yaml_only["results"]], ["infra/routes.yaml"])
            self.assertEqual(yaml_only["untracked_line_count"], 1)

    def test_empty_text_query_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = git_text_search(Path(tmp), "", limit=5)
            self.assertEqual(result["status"], "invalid_query")


if __name__ == "__main__":
    unittest.main()
