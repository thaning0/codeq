from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeq.cli import _normalize_global_options
from codeq.gitdiff import merge_ranges
from codeq.util import fuzzy_score, identifier_tokens, is_test_path, parse_target


class UtilTests(unittest.TestCase):
    def test_identifier_tokens_longest_unique(self):
        self.assertEqual(
            identifier_tokens("report summary report_summary evidence"),
            ["report_summary", "evidence", "summary", "report"],
        )

    def test_fuzzy_score_prefers_exact(self):
        exact = fuzzy_score("Foo.bar", "bar", "Foo", "/x/foo.py")
        partial = fuzzy_score("Foo.bar", "bar_helper", "Foo", "/x/foo.py")
        self.assertGreater(exact, partial)

    def test_test_path_classification(self):
        self.assertTrue(is_test_path("/repo/tests/test_service.py"))
        self.assertTrue(is_test_path("/repo/src/foo.spec.ts"))
        self.assertFalse(is_test_path("/repo/src/service.py"))

    def test_parse_location_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            parsed = parse_target("src/a.py:12:4", root)
            self.assertEqual(parsed["kind"], "location")
            self.assertEqual(parsed["line"], 12)
            self.assertEqual(parsed["column"], 4)
            self.assertEqual(parsed["path"], str((root / "src/a.py").resolve()))

    def test_parse_name_target(self):
        self.assertEqual(parse_target("Foo.bar", "/tmp"), {"kind": "name", "name": "Foo.bar"})

    def test_global_options_can_follow_subcommand(self):
        self.assertEqual(
            _normalize_global_options(["find", "Foo", "--json", "--root", "/repo", "--limit=5", "--kind", "function"]),
            ["--json", "--root", "/repo", "--limit=5", "find", "Foo", "--kind", "function"],
        )


class GitDiffTests(unittest.TestCase):
    def test_merge_ranges(self):
        merged = merge_ranges([
            {"path": "/a.py", "start": 3, "end": 5},
            {"path": "/a.py", "start": 6, "end": 8},
            {"path": "/a.py", "start": 20, "end": 20},
            {"path": "/b.py", "start": 1, "end": 1},
        ])
        self.assertEqual(merged[0]["ranges"], [{"start": 3, "end": 8}, {"start": 20, "end": 20}])
        self.assertEqual(merged[1]["ranges"], [{"start": 1, "end": 1}])


if __name__ == "__main__":
    unittest.main()
