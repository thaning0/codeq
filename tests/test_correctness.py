from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeq.util import exact_definition_hits, path_target_intent
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

    def test_missing_source_paths_fail_closed_without_fuzzy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace(root)
            try:
                with patch.object(workspace, "find", side_effect=AssertionError("fuzzy fallback must not run")):
                    for target in (
                        "scripts/migrate_catalog.py",
                        "frontend/src/missing.ts",
                        "frontend/src/missing.js",
                        "scripts/migrate_catalog.py:12",
                        "scripts/migrate_catalog.py:12:4",
                    ):
                        with self.subTest(target=target):
                            result = workspace.context(target)
                            self.assertEqual(result["status"], "not_found")
                            self.assertIn("file not found", result["reason"])
            finally:
                workspace.close()

    def test_path_intent_does_not_capture_qualified_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(path_target_intent("BacktestService.stream_backtest_logs", root))
            self.assertIsNotNone(path_target_intent("scripts/missing.py", root))
            self.assertIsNotNone(path_target_intent("missing.ts:12", root))

    def test_unique_source_basename_resolves_to_repo_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "packages/core/src/research_projection.py"
            source.parent.mkdir(parents=True)
            source.write_text("value = 1\n", encoding="utf-8")
            workspace = Workspace(root)
            try:
                self.assertEqual(workspace._file_target("research_projection.py"), source.resolve())
            finally:
                workspace.close()

    def test_ambiguous_source_basename_fails_closed_with_exact_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for package in ("one", "two"):
                source = root / f"packages/{package}/src/model.py"
                source.parent.mkdir(parents=True)
                source.write_text("value = 1\n", encoding="utf-8")
            workspace = Workspace(root)
            try:
                result = workspace.resolve("model.py")
            finally:
                workspace.close()
            self.assertEqual(result["status"], "ambiguous")
            self.assertEqual(len(result["candidates"]), 2)
            self.assertTrue(all(item["selection_command"].startswith("codeq context packages/") for item in result["candidates"]))

    def test_dotted_python_module_resolves_to_file_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "packages/core/src/auto_research_core/application/research_governance.py"
            source.parent.mkdir(parents=True)
            source.write_text("value = 1\n", encoding="utf-8")
            workspace = Workspace(root)
            expected = {"status": "ok", "kind": "file", "file": {"path": str(source.resolve())}}
            try:
                with patch.object(workspace, "_file_context", return_value=expected) as file_context:
                    result = workspace.context("auto_research_core.application.research_governance")
            finally:
                workspace.close()
            self.assertEqual(result, expected)
            self.assertEqual(file_context.call_args.args[0], source.resolve())

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

    def test_module_qualified_top_level_symbol_resolves_by_file_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "packages/research-core/src/auto_research_core/domain/models.py"
            path.parent.mkdir(parents=True)
            path.write_text("class Candidate:\n    pass\n", encoding="utf-8")
            candidate = {
                "name": "Candidate",
                "kind": "Class",
                "container": "",
                "path": str(path.resolve()),
                "line": 1,
                "column": 7,
                "source": "lsp",
                "origin": "document",
            }
            workspace = Workspace(root)
            try:
                with patch.object(
                    workspace,
                    "_exact_document_candidates",
                    side_effect=lambda name, **_: [candidate] if name == "Candidate" else [],
                ):
                    result = workspace.resolve("auto_research_core.domain.models.Candidate")
            finally:
                workspace.close()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["symbol"]["path"], str(path.resolve()))

    def test_module_qualified_member_resolves_by_semantic_and_file_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "src/pkg/service.py"
            path.parent.mkdir(parents=True)
            path.write_text("class Service:\n    def run(self):\n        pass\n", encoding="utf-8")
            member = {
                "name": "run",
                "kind": "Method",
                "container": "Service",
                "path": str(path.resolve()),
                "line": 2,
                "column": 9,
                "source": "lsp",
                "origin": "document",
            }
            workspace = Workspace(root)
            try:
                with patch.object(
                    workspace,
                    "_exact_document_candidates",
                    side_effect=lambda name, **_: [member] if name == "run" else [],
                ):
                    result = workspace.resolve("pkg.service.Service.run")
                    wrong = workspace.resolve("pkg.other.Service.run")
            finally:
                workspace.close()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(wrong["status"], "not_found")

    def test_qualified_miss_offers_exact_leaf_name_locations_without_resolving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "src/pkg/bridge/session.py"
            path.parent.mkdir(parents=True)
            path.write_text("class BridgeSession:\n    pass\n", encoding="utf-8")
            candidate = {
                "name": "BridgeSession",
                "kind": "Class",
                "container": "",
                "path": str(path.resolve()),
                "line": 1,
                "column": 7,
                "source": "lsp",
                "origin": "document",
            }
            workspace = Workspace(root)
            try:
                with patch.object(
                    workspace,
                    "_exact_document_candidates",
                    side_effect=lambda name, **_: [candidate] if name == "BridgeSession" else [],
                ):
                    result = workspace.resolve("pkg.bridge.BridgeSession")
            finally:
                workspace.close()
            self.assertEqual(result["status"], "not_found")
            self.assertEqual(result["candidates"][0]["path"], str(path.resolve()))
            self.assertEqual(
                result["candidates"][0]["selection_command"],
                "codeq context src/pkg/bridge/session.py:1:7",
            )

    def test_semantic_path_constraint_filters_find_and_disambiguates_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "packages/one/src/model.py"
            second = root / "packages/two/src/model.py"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text("class Thing:\n    pass\n", encoding="utf-8")
            second.write_text("class Thing:\n    pass\n", encoding="utf-8")

            def candidate(path: Path) -> dict[str, object]:
                return {
                    "name": "Thing",
                    "kind": "Class",
                    "container": "",
                    "path": str(path.resolve()),
                    "line": 1,
                    "column": 7,
                    "source": "lsp",
                    "origin": "document",
                }

            workspace = Workspace(root)
            try:
                with (
                    patch("codeq.workspace.lexical_hits", return_value=[]),
                    patch.object(workspace, "_exact_document_candidates", return_value=[candidate(first), candidate(second)]),
                ):
                    unscoped = workspace.resolve("Thing")
                    root_scoped = workspace.find("Thing", paths=(".",))
                    scoped_find = workspace.find("Thing", paths=("packages/two",))
                    scoped = workspace.resolve("Thing", semantic_paths=("packages/two",))
            finally:
                workspace.close()
            self.assertEqual(unscoped["status"], "ambiguous")
            self.assertTrue(all(item.get("selection_command") for item in unscoped["candidates"]))
            self.assertEqual(root_scoped["result_count"], 2)
            self.assertEqual(scoped_find["result_count"], 1)
            self.assertEqual(scoped_find["paths"], ["packages/two"])
            self.assertEqual(scoped["status"], "ok")
            self.assertEqual(scoped["symbol"]["path"], str(second.resolve()))

    def test_semantic_find_applies_path_glob_and_test_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "packages/core/src/guard.py"
            test = root / "packages/core/tests/test_guard.py"
            typescript = root / "packages/core/src/guard.ts"
            outside = root / "packages/other/src/guard.py"
            for path in (source, test, typescript, outside):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("class Guard:\n    pass\n", encoding="utf-8")

            def candidate(path: Path) -> dict[str, object]:
                return {
                    "name": "Guard",
                    "kind": "Class",
                    "container": "",
                    "path": str(path.resolve()),
                    "line": 1,
                    "column": 7,
                    "source": "lsp",
                    "origin": "document",
                }

            workspace = Workspace(root)
            try:
                with (
                    patch("codeq.workspace.lexical_hits", return_value=[]),
                    patch.object(
                        workspace,
                        "_exact_document_candidates",
                        return_value=[candidate(source), candidate(test), candidate(typescript), candidate(outside)],
                    ),
                ):
                    result = workspace.find(
                        "Guard",
                        paths=("packages/core",),
                        globs=("*.py",),
                        exclude_tests=True,
                    )
            finally:
                workspace.close()

            self.assertEqual(result["result_count"], 1)
            self.assertEqual(result["total_candidates"], 1)
            self.assertEqual(result["results"][0]["path"], str(source.resolve()))
            self.assertEqual(result["filters"], {
                "paths": ["packages/core"],
                "globs": ["*.py"],
                "exclude_tests": True,
            })

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
