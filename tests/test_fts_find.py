from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeq.ftssearch import FtsUnavailable
from codeq.workspace import Workspace


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)


def _track_all(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)


class FtsFindTests(unittest.TestCase):
    def test_multi_term_file_outranks_one_generic_term_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            target = root / "src/concept_search.py"
            generic = root / "benchmarks/helpers.py"
            target.parent.mkdir(parents=True)
            generic.parent.mkdir(parents=True)
            target.write_text(
                "# orchid ranking lantern implementation\nvalue = 1\n",
                encoding="utf-8",
            )
            generic.write_text(
                "# ranking ranking ranking ranking benchmark helper\n",
                encoding="utf-8",
            )
            _track_all(root)

            workspace = Workspace(root)
            try:
                result = workspace.find("orchid ranking lantern", limit=2)
                syntax = workspace.find('orchid OR "lantern', limit=2)
            finally:
                workspace.close()

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["mode"], "fts5")
            self.assertEqual(result["search_mode"], "concept")
            self.assertEqual(result["results"][0]["path"], str(target.resolve()))
            self.assertEqual(result["results"][0]["source"], "fts5")
            self.assertEqual(result["results"][0]["matched_terms"], ["orchid", "ranking", "lantern"])
            self.assertEqual(result["results"][0]["representative_lines"][0]["line"], 1)
            self.assertEqual(
                result["results"][0]["selection_command"],
                "codeq context src/concept_search.py:1:3",
            )
            self.assertEqual(result["ranking"]["engine"], "sqlite_fts5_bm25")
            self.assertEqual(syntax["status"], "ok")
            self.assertEqual(syntax["results"][0]["path"], str(target.resolve()))

    def test_concept_result_explains_distributed_terms_and_supports_explicit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            source = root / "src/evidence.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                "# orchid trial\nfailure = 'recovery'\n",
                encoding="utf-8",
            )
            _track_all(root)

            workspace = Workspace(root)
            try:
                result = workspace.find("orchid failure recovery")
                one_term = workspace.find("orchid", mode="concept")
            finally:
                workspace.close()

            item = result["results"][0]
            self.assertEqual(item["line"], 2)
            self.assertEqual(item["column"], 1)
            self.assertEqual(item["matched_terms"], ["orchid", "failure", "recovery"])
            self.assertEqual(
                [line["line"] for line in item["representative_lines"]],
                [2, 1],
            )
            self.assertEqual(
                item["selection_command"],
                "codeq context src/evidence.py:2:1",
            )
            self.assertEqual(one_term["status"], "ok")
            self.assertEqual(one_term["search_mode"], "concept")
            self.assertEqual(one_term["results"][0]["matched_terms"], ["orchid"])

    def test_git_visibility_and_scope_filters_apply_before_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            paths = {
                "production": root / "packages/core/src/catalog.py",
                "test": root / "packages/core/tests/test_catalog.py",
                "outside": root / "packages/other/src/catalog.py",
                "ignored": root / "ignored.py",
            }
            for path in paths.values():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# catalog refresh workflow\n", encoding="utf-8")
            (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
            _track_all(root)
            untracked = root / "packages/core/src/new_catalog.py"
            untracked.write_text("# catalog refresh workflow\n", encoding="utf-8")

            workspace = Workspace(root)
            try:
                unfiltered = workspace.find("catalog refresh", limit=10)
                scoped = workspace.find(
                    "catalog refresh",
                    paths=("packages/core",),
                    exclude_tests=True,
                    limit=1,
                )
                globbed = workspace.find(
                    "catalog refresh",
                    paths=("packages/core",),
                    globs=("*new*.py",),
                    limit=10,
                )
            finally:
                workspace.close()

            visible = {item["path"] for item in unfiltered["results"]}
            self.assertIn(str(untracked.resolve()), visible)
            self.assertNotIn(str(paths["ignored"].resolve()), visible)
            self.assertEqual(scoped["result_count"], 1)
            self.assertEqual(scoped["total_candidates"], 2)
            self.assertTrue(scoped["truncated"])
            self.assertEqual(globbed["total_candidates"], 1)
            self.assertEqual(globbed["results"][0]["path"], str(untracked.resolve()))

    def test_index_refreshes_changed_added_and_deleted_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_repo(root)
            source = root / "source.py"
            source.write_text("# copper meadow\n", encoding="utf-8")
            _track_all(root)
            workspace = Workspace(root)
            try:
                self.assertEqual(workspace.find("copper meadow")["total_candidates"], 1)
                source.write_text("# silver canyon\n", encoding="utf-8")
                changed = workspace.find("silver canyon")
                self.assertEqual(changed["total_candidates"], 1)
                self.assertTrue(changed["index"]["refreshed"])
                self.assertEqual(workspace.find("copper meadow")["total_candidates"], 0)

                added = root / "added.py"
                added.write_text("# amber forest\n", encoding="utf-8")
                self.assertEqual(workspace.find("amber forest")["total_candidates"], 1)
                (root / ".gitignore").write_text("added.py\n", encoding="utf-8")
                self.assertEqual(workspace.find("amber forest")["total_candidates"], 0)
                (root / ".gitignore").write_text("", encoding="utf-8")
                self.assertEqual(workspace.find("amber forest")["total_candidates"], 1)
                added.unlink()
                self.assertEqual(workspace.find("amber forest")["total_candidates"], 0)
            finally:
                workspace.close()

    def test_workspaces_are_isolated_and_missing_fts_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first_root = Path(first_tmp)
            second_root = Path(second_tmp)
            for root, name in ((first_root, "first.py"), (second_root, "second.py")):
                _init_repo(root)
                (root / name).write_text("# violet harbor\n", encoding="utf-8")
                _track_all(root)

            first = Workspace(first_root)
            second = Workspace(second_root)
            try:
                first_result = first.find("violet harbor")
                second_result = second.find("violet harbor")
                self.assertEqual(first_result["results"][0]["name"], "first.py")
                self.assertEqual(second_result["results"][0]["name"], "second.py")

                with patch(
                    "codeq.workspace.WorkspaceFtsIndex.search",
                    side_effect=FtsUnavailable("FTS5 unavailable for test"),
                ):
                    unavailable = first.find("another concept")
                self.assertEqual(unavailable["status"], "unsupported_capability")
                self.assertIn("FTS5 unavailable", unavailable["reason"])
                self.assertEqual(unavailable["results"], [])

                unsupported_kind = first.find("violet harbor", kind="function")
                self.assertEqual(unsupported_kind["status"], "unsupported_target")
                self.assertIn("--kind", unsupported_kind["reason"])
            finally:
                first.close()
                second.close()


if __name__ == "__main__":
    unittest.main()
