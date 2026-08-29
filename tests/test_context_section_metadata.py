from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from codeq.cli import _render_context
from codeq.workspace import Project, Workspace, _bounded_section_disclosure


def _raw_location(path: Path, line: int, column: int = 1) -> dict[str, Any]:
    return {
        "uri": path.resolve().as_uri(),
        "range": {
            "start": {"line": line - 1, "character": column - 1},
            "end": {"line": line - 1, "character": column},
        },
    }


def _location(path: Path, line: int, name: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {
        "path": str(path.resolve()),
        "line": line,
        "column": 1,
    }
    if name:
        item["name"] = name
    return item


class _SectionSession:
    def __init__(
        self,
        references: list[dict[str, Any]],
        implementations: list[dict[str, Any]],
    ) -> None:
        self._references = references
        self._implementations = implementations
        self.references_calls = 0
        self.implementations_calls = 0
        self.hover_calls = 0

    def references(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        self.references_calls += 1
        return self._references

    def implementations(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        self.implementations_calls += 1
        return self._implementations

    def hover(self, path: Path, line: int, column: int) -> None:
        self.hover_calls += 1
        return None


class ContextSectionMetadataTests(unittest.TestCase):
    def test_bounded_probe_reports_an_exact_total_when_it_completes(self) -> None:
        item = {"path": "/repo/source.py", "line": 1, "column": 1}
        returned, metadata = _bounded_section_disclosure([item], limit=2)
        self.assertEqual(returned, [item])
        self.assertEqual(
            metadata,
            {
                "returned_count": 1,
                "total_count": 1,
                "total_lower_bound": 1,
                "total_is_exact": True,
                "truncated": False,
            },
        )

    def test_symbol_context_reports_exact_and_lower_bound_section_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                "[project]\nname='section-test'\nversion='0'\n",
                encoding="utf-8",
            )
            source = root / "src/target.py"
            source.parent.mkdir()
            source.write_text("def target():\n    return 1\n", encoding="utf-8")

            source_paths = [root / f"src/ref_{index}.py" for index in range(3)]
            test_paths = [root / f"tests/test_{index}.py" for index in range(3)]
            implementation_paths = [root / f"src/impl_{index}.py" for index in range(3)]
            caller_paths = [root / f"src/caller_{index}.py" for index in range(3)]
            callee = root / "src/callee.py"
            for path in [*source_paths, *test_paths, *implementation_paths, *caller_paths, callee]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("pass\n", encoding="utf-8")

            raw_references = [
                *[_raw_location(path, 1) for path in source_paths],
                _raw_location(source_paths[0], 1),
                *[_raw_location(path, 1) for path in test_paths],
                _raw_location(test_paths[0], 1),
            ]
            raw_implementations = [
                _raw_location(source, 1, 5),
                *[_raw_location(path, 1) for path in implementation_paths],
                _raw_location(implementation_paths[0], 1),
            ]
            session = _SectionSession(raw_references, raw_implementations)
            callers = [
                *[_location(path, 1, f"caller_{index}") for index, path in enumerate(caller_paths)],
                _location(caller_paths[0], 1, "caller_0"),
            ]
            callees = [_location(callee, 1, "callee")]
            dynamic_probe = [
                {
                    **_location(source_paths[index], 1),
                    "reason": "callback_argument",
                    "text": "target",
                }
                for index in range(3)
            ]
            symbol = {
                "name": "target",
                "kind": "Function",
                "container": "",
                "path": str(source.resolve()),
                "line": 1,
                "column": 5,
            }
            workspace = Workspace(root)
            project = Project(root.resolve(), "python")
            neighbor_calls = {"in": 0, "out": 0}

            def neighbors(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
                direction = str(args[4])
                neighbor_calls[direction] += 1
                return callers if direction == "in" else callees

            def symbol_at(location: dict[str, Any]) -> dict[str, Any]:
                return {
                    **location,
                    "name": Path(str(location["path"])).stem,
                    "kind": "Function",
                    "container": "",
                }

            def dynamic(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
                self.assertEqual(kwargs["limit"], 3)
                return dynamic_probe

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
                    patch.object(workspace, "_symbol_at_location", side_effect=symbol_at),
                    patch("codeq.workspace.classify_dynamic_references", side_effect=dynamic),
                ):
                    result = workspace.context("target", limit=2)
            finally:
                workspace.close()

            self.assertEqual(session.references_calls, 1)
            self.assertEqual(session.implementations_calls, 1)
            self.assertEqual(session.hover_calls, 1)
            self.assertEqual(neighbor_calls, {"in": 1, "out": 1})
            sections = result["section_metadata"]

            for name in ("callers", "implementations", "references", "tests"):
                with self.subTest(section=name):
                    self.assertEqual(sections[name]["returned_count"], 2)
                    self.assertEqual(sections[name]["total_count"], 3)
                    self.assertEqual(sections[name]["total_lower_bound"], 3)
                    self.assertTrue(sections[name]["total_is_exact"])
                    self.assertTrue(sections[name]["truncated"])
                    self.assertEqual(len(result[name]), 2)

            self.assertEqual(
                sections["callees"],
                {
                    "returned_count": 1,
                    "total_count": 1,
                    "total_lower_bound": 1,
                    "total_is_exact": True,
                    "truncated": False,
                },
            )
            dynamic_meta = sections["possible_dynamic_references"]
            self.assertEqual(dynamic_meta["returned_count"], 2)
            self.assertIsNone(dynamic_meta["total_count"])
            self.assertEqual(dynamic_meta["total_lower_bound"], 3)
            self.assertFalse(dynamic_meta["total_is_exact"])
            self.assertTrue(dynamic_meta["truncated"])
            self.assertEqual(len(result["possible_dynamic_references"]), 2)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                _render_context(result, root)
            rendered = stdout.getvalue()
            for heading in (
                "Callers (showing 2 of 3)",
                "Implementations (showing 2 of 3)",
                "Tests (showing 2 of 3)",
                "References (showing 2 of 3)",
                "Possible dynamic references (showing 2+)",
                "Callees (1)",
            ):
                self.assertIn(heading, rendered)
            self.assertEqual(rendered.count("increase --limit"), 5)


if __name__ == "__main__":
    unittest.main()
