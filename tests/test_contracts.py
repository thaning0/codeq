from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeq.contracts import (
    EVIDENCE_BASE_SIDE_LEXICAL,
    EVIDENCE_CURRENT_SEMANTIC,
    EVIDENCE_LEXICAL,
    EVIDENCE_POSSIBLE_DYNAMIC,
    EVIDENCE_SEMANTIC,
    EVIDENCE_VALUES,
    QueryBudget,
    SCHEMA_VERSION,
    STATUS_VALUES,
    bounded_text,
)
from codeq.service import CodeqService
from codeq.textsearch import git_text_search
from codeq.util import source_snippet


class _ContractWorkspace:
    def __init__(self, root: Path, timeout: float = 15.0) -> None:
        self.root = root
        self.timeout = timeout

    def close(self) -> None:
        pass

    def session_stats(self):
        return []

    def metrics_snapshot(self):
        return {
            "sessions_started": 0,
            "lsp_request_count": 0,
            "document_symbols_hit": 0,
            "document_symbols_miss": 0,
            "document_symbol_cache_entries": 0,
            "prewarm_files": 0,
            "prewarm_probes": 0,
            "prewarm_early_stops": 0,
        }

    def find(self, query: str, limit: int = 20, kind: str | None = None, **kwargs):
        return {"status": "ok", "query": query, "results": []}


class ContractTests(unittest.TestCase):
    def test_schema_version_is_attached_to_service_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("codeq.service.Workspace", _ContractWorkspace):
            service = CodeqService()
            result = service.handle({"command": "find", "root": tmp, "query": "Foo"})
            self.assertEqual(result["schema_version"], SCHEMA_VERSION)
            self.assertIn(result["status"], STATUS_VALUES)

    def test_evidence_values_are_machine_stable(self) -> None:
        self.assertEqual(
            EVIDENCE_VALUES,
            {
                EVIDENCE_SEMANTIC,
                EVIDENCE_LEXICAL,
                EVIDENCE_POSSIBLE_DYNAMIC,
                EVIDENCE_BASE_SIDE_LEXICAL,
                EVIDENCE_CURRENT_SEMANTIC,
            },
        )
        for value in EVIDENCE_VALUES:
            self.assertRegex(value, r"^[a-z][a-z0-9_]*$")

    def test_query_budget_is_monotone_and_nested_is_bounded(self) -> None:
        previous = 0
        for limit in (1, 2, 5, 10, 20, 100):
            budget = QueryBudget.from_limit(limit)
            self.assertGreaterEqual(budget.items, previous)
            self.assertLessEqual(budget.nested_items, budget.items)
            self.assertLessEqual(budget.nested_items, 5)
            previous = budget.items

    def test_bounded_text_reports_truncation(self) -> None:
        short, truncated = bounded_text("abc", 10)
        self.assertEqual(short, "abc")
        self.assertFalse(truncated)
        long, truncated = bounded_text("abcdefghij", 5)
        self.assertEqual(len(long), 5)
        self.assertTrue(truncated)

    def test_source_snippet_has_hard_character_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.py"
            path.write_text("x = '" + ("a" * 5000) + "'\n", encoding="utf-8")
            snippet = source_snippet(path, 1, max_chars=300, max_line_chars=200)
            self.assertLessEqual(len(snippet["text"]), 300)
            self.assertTrue(snippet["truncated"])

    def test_text_limit_preserves_complete_counts_and_bounds_line_payload(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "codeq@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "codeq-test"], check=True)
            text = ("x" * 1000) + " NEEDLE " + ("y" * 1000)
            for index in range(8):
                (root / f"f{index}.txt").write_text(text + "\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)

            small = git_text_search(root, "NEEDLE", limit=3)
            large = git_text_search(root, "NEEDLE", limit=6)
            self.assertEqual(small["match_count"], large["match_count"])
            self.assertEqual(small["matching_line_count"], 8)
            self.assertEqual(len(small["results"]), 3)
            self.assertEqual(len(large["results"]), 6)
            self.assertTrue(small["truncated"])
            self.assertTrue(all(len(item["text"]) <= QueryBudget.from_limit(3).text_line_chars for item in small["results"]))
            self.assertTrue(all(item["text_truncated"] for item in small["results"]))


if __name__ == "__main__":
    unittest.main()
