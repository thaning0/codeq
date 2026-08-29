from __future__ import annotations

import io
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from codeq.cli import _render_context, build_parser, main
from codeq.contracts import (
    TEST_EVIDENCE_DIRECT_REFERENCE,
    TEST_EVIDENCE_EXACT_LEXICAL,
    TEST_EVIDENCE_MODULE_IMPORT,
    TEST_EVIDENCE_SEMANTIC_CALLER,
)
from codeq.workspace import Workspace


def _raw_location(path: Path, line: int, column: int = 1) -> dict[str, Any]:
    return {
        "uri": path.resolve().as_uri(),
        "range": {
            "start": {"line": line - 1, "character": column - 1},
            "end": {"line": line - 1, "character": column},
        },
    }


class _ContextSession:
    def __init__(self, references: list[dict[str, Any]]) -> None:
        self._references = references

    def references(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        return self._references

    def implementations(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        return []

    def hover(self, path: Path, line: int, column: int) -> None:
        return None


def _git_add(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)


def _symbol_context(
    root: Path,
    source: Path,
    symbol_name: str,
    *,
    references: list[dict[str, Any]] | None = None,
    callers: list[dict[str, Any]] | None = None,
    limit: int = 20,
    selected_sections: tuple[str, ...] = (),
    lexical_references: bool = False,
    lexical_query: str | None = None,
) -> dict[str, Any]:
    workspace = Workspace(root)
    project = workspace.project_for_path(source)
    if project is None:
        workspace.close()
        raise AssertionError(f"no project for {source}")
    symbol = {
        "name": symbol_name,
        "kind": "Function",
        "container": "",
        "path": str(source.resolve()),
        "line": 1,
        "column": 5,
    }
    session = _ContextSession(references or [])

    def neighbors(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return list(callers or []) if args[4] == "in" else []

    try:
        with (
            patch.object(workspace, "resolve", return_value={"status": "ok", "symbol": symbol}),
            patch.object(
                workspace,
                "_session_and_position",
                return_value=(session, project, source.resolve(), 1, 5),
            ),
            patch.object(workspace, "_prewarm_symbol", return_value=None),
            patch.object(workspace, "_call_neighbors", side_effect=neighbors),
        ):
            return workspace.context(
                symbol_name,
                limit=limit,
                selected_sections=selected_sections,
                lexical_references=lexical_references,
                lexical_query=lexical_query,
            )
    finally:
        workspace.close()


class ContextTestEvidenceTests(unittest.TestCase):
    def test_python_test_evidence_preserves_provenance_and_bounded_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                "[project]\nname='test-evidence'\nversion='0'\n",
                encoding="utf-8",
            )
            source = root / "src/target.py"
            direct = root / "tests/test_direct.py"
            caller_test = root / "tests/test_caller.py"
            import_only = root / "tests/test_import_only.py"
            lexical = root / "tests/test_registry.py"
            for path in (source, direct, caller_test, import_only, lexical):
                path.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("def dispatch_order():\n    return 1\n", encoding="utf-8")
            direct.write_text(
                "from src.target import dispatch_order\n\n"
                "def test_direct():\n    dispatch_order()\n",
                encoding="utf-8",
            )
            caller_test.write_text(
                "from src.target import dispatch_order\n\n"
                "def test_via_helper():\n    helper()\n",
                encoding="utf-8",
            )
            import_only.write_text(
                "from src.target import unrelated\n",
                encoding="utf-8",
            )
            lexical.write_text(
                "REGISTRY['order'] = dispatch_order\n",
                encoding="utf-8",
            )
            _git_add(root)
            caller = {
                "name": "test_via_helper",
                "kind": "Function",
                "path": str(caller_test.resolve()),
                "line": 3,
                "column": 5,
            }

            result = _symbol_context(
                root,
                source,
                "dispatch_order",
                references=[_raw_location(direct, 4, 5)],
                callers=[caller],
                selected_sections=("tests",),
            )
            evidence_types = {item["evidence_type"] for item in result["tests"]}
            self.assertEqual(
                evidence_types,
                {
                    TEST_EVIDENCE_DIRECT_REFERENCE,
                    TEST_EVIDENCE_SEMANTIC_CALLER,
                    TEST_EVIDENCE_MODULE_IMPORT,
                    TEST_EVIDENCE_EXACT_LEXICAL,
                },
            )
            self.assertEqual(result["tests"][0]["confidence"], "direct")
            self.assertTrue(all(isinstance(item["reason"], dict) for item in result["tests"]))
            self.assertTrue(result["section_metadata"]["tests"]["total_is_exact"])

            bounded = _symbol_context(
                root,
                source,
                "dispatch_order",
                references=[_raw_location(direct, 4, 5)],
                callers=[caller],
                selected_sections=("tests",),
                limit=2,
            )
            metadata = bounded["section_metadata"]["tests"]
            self.assertEqual(metadata["returned_count"], 2)
            self.assertIsNone(metadata["total_count"])
            self.assertGreater(metadata["total_lower_bound"], 2)
            self.assertFalse(metadata["total_is_exact"])
            self.assertTrue(metadata["truncated"])

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                _render_context(result, root)
            plain = stdout.getvalue()
            self.assertIn("[direct semantic reference]", plain)
            self.assertIn("[candidate: semantic caller]", plain)
            self.assertIn("[candidate: module import]", plain)
            self.assertIn("[candidate: exact lexical reference]", plain)

    def test_typescript_import_only_test_is_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
            source = root / "src/target.ts"
            test = root / "tests/target.test.ts"
            source.parent.mkdir()
            test.parent.mkdir()
            source.write_text("export function dispatchOrder() {}\n", encoding="utf-8")
            test.write_text(
                "import { unrelated } from '../src/target';\n",
                encoding="utf-8",
            )
            _git_add(root)

            result = _symbol_context(
                root,
                source,
                "dispatchOrder",
                selected_sections=("tests",),
            )
            self.assertEqual(len(result["tests"]), 1)
            self.assertEqual(result["tests"][0]["evidence_type"], TEST_EVIDENCE_MODULE_IMPORT)

    def test_common_short_name_does_not_create_unrelated_lexical_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                "[project]\nname='short-name'\nversion='0'\n",
                encoding="utf-8",
            )
            source = root / "src/short.py"
            unrelated = root / "tests/test_unrelated.py"
            source.parent.mkdir()
            unrelated.parent.mkdir()
            source.write_text("def run():\n    pass\n", encoding="utf-8")
            unrelated.write_text("def run():\n    pass\n", encoding="utf-8")
            _git_add(root)

            result = _symbol_context(
                root,
                source,
                "run",
                selected_sections=("tests",),
            )
            self.assertEqual(result["tests"], [])


class ContextSectionSelectionTests(unittest.TestCase):
    def test_cli_preserves_repeated_section_selection(self) -> None:
        captured: dict[str, Any] = {}

        def request(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
            captured.update(payload)
            return {"status": "ok", "symbol": {"name": "run", "path": "/repo/a.py"}}

        with (
            patch("codeq.cli.git_root", return_value="/repo"),
            patch("codeq.cli._request", side_effect=request),
            redirect_stdout(io.StringIO()),
        ):
            main(["context", "Service.run", "--section", "callers", "--section", "tests", "--json"])
        self.assertEqual(captured["selected_sections"], ["callers", "tests"])

    def test_focused_context_returns_only_selected_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                "[project]\nname='section-selection'\nversion='0'\n",
                encoding="utf-8",
            )
            source = root / "src/target.py"
            caller_path = root / "src/caller.py"
            source.parent.mkdir()
            source.write_text("def target():\n    pass\n", encoding="utf-8")
            caller_path.write_text("def caller():\n    target()\n", encoding="utf-8")
            caller = {
                "name": "caller",
                "kind": "Function",
                "path": str(caller_path.resolve()),
                "line": 1,
                "column": 5,
            }

            result = _symbol_context(
                root,
                source,
                "target",
                callers=[caller],
                selected_sections=("callers", "references"),
            )
            self.assertEqual(result["section_selection"], {
                "mode": "focused",
                "selected": ["callers", "references"],
            })
            self.assertEqual(result["callers"], [caller])
            self.assertEqual(result["references"], [])
            for omitted in (
                "source",
                "callees",
                "implementations",
                "tests",
                "possible_dynamic_references",
            ):
                self.assertNotIn(omitted, result)
            self.assertEqual(result["evidence"], "semantic")
            self.assertIn("symbol", result)

    def test_lexical_mode_composes_with_focused_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                "[project]\nname='lexical-selection'\nversion='0'\n",
                encoding="utf-8",
            )
            source = root / "src/target.py"
            config = root / "config/settings.toml"
            source.parent.mkdir()
            config.parent.mkdir()
            source.write_text("def target():\n    pass\n", encoding="utf-8")
            config.write_text("key = 'RUNTIME_KEY'\n", encoding="utf-8")
            _git_add(root)

            result = _symbol_context(
                root,
                source,
                "target",
                selected_sections=("references",),
                lexical_references=True,
                lexical_query="RUNTIME_KEY",
            )
            self.assertEqual(
                result["section_selection"]["selected"],
                ["references", "lexical-references"],
            )
            self.assertIn("references", result)
            self.assertIn("lexical_references", result)
            self.assertNotIn("callers", result)
            self.assertEqual(result["lexical_references"]["matching_line_count"], 1)

    def test_invalid_and_file_sections_return_copyable_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                "[project]\nname='invalid-selection'\nversion='0'\n",
                encoding="utf-8",
            )
            source = root / "src/target.py"
            source.parent.mkdir()
            source.write_text("def target():\n    pass\n", encoding="utf-8")
            workspace = Workspace(root)
            try:
                invalid = workspace.context("target", selected_sections=("unknown",))
                lexical_missing = workspace.context(
                    "target",
                    selected_sections=("lexical-references",),
                )
                file_result = workspace.context(str(source), selected_sections=("callers",))
            finally:
                workspace.close()

            self.assertEqual(invalid["status"], "invalid_query")
            self.assertIn("allowed values", invalid["reason"])
            self.assertEqual(lexical_missing["status"], "invalid_query")
            self.assertIn("requires --lexical-references", lexical_missing["reason"])
            self.assertEqual(file_result["status"], "invalid_query")
            self.assertIn("symbol context", file_result["reason"])
            for result in (invalid, lexical_missing, file_result):
                argv = shlex.split(result["recovery_command"])
                self.assertEqual(argv[0], "codeq")
                build_parser().parse_args(argv[1:])


if __name__ == "__main__":
    unittest.main()
