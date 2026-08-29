from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from codeq.cli import PlainArgumentParser, build_parser


class HelpTests(unittest.TestCase):
    def _subparser(self, name: str) -> argparse.ArgumentParser:
        parser = build_parser()
        action = next(
            item
            for item in parser._actions
            if isinstance(item, argparse._SubParsersAction)
        )
        return action.choices[name]

    def test_feature_surface_is_four_commands_plus_search_alias(self):
        parser = build_parser()
        action = next(
            item
            for item in parser._actions
            if isinstance(item, argparse._SubParsersAction)
        )
        self.assertEqual(set(action.choices), {"find", "search", "context", "trace", "review"})
        self.assertIs(action.choices["search"], action.choices["find"])

    def test_top_level_help_is_agent_self_describing(self):
        help_text = build_parser().format_help()
        self.assertIn("Choose a command:", help_text)
        self.assertIn("codeq find 'backtest log streaming'", help_text)
        self.assertIn("codeq COMMAND --help", help_text)
        self.assertIn("--no-daemon", help_text)
        self.assertNotIn("\x1b[", help_text)

    def test_agent_opt_in_makes_command_help_conditional(self):
        readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
        agent_setup = readme.split("## Agent setup", 1)[1].split("## Supported analysis", 1)[0]
        self.assertIn(
            "if a command is unfamiliar, run `codeq COMMAND --help`",
            agent_setup,
        )
        self.assertNotIn("run `codeq --help` for usage", agent_setup)

    def test_subcommand_help_explains_arguments_and_examples(self):
        expected = {
            "find": ["QUERY", "--kind KIND", "--text", "untracked files", "--path PREFIX", "--glob PATTERN", "--exclude-tests", "Examples:", "Typical next step:"],
            "context": ["TARGET", "PATH:LINE[:COLUMN]", "progressive disclosure", "--outline-depth N", "--topology", "containing file", "--section SECTION", "prefer it to increasing", "--lexical-references", "--path PREFIX", "--symbol-path PREFIX", "--glob PATTERN", "--exclude-tests", "Callers", "Examples:"],
            "trace": ["--in", "--out", "--depth N", "--node-limit N", "Examples:"],
            "review": ["--base REF", "--merge-base", "Untracked files", "merge-base", "Examples:"],
        }
        for name, phrases in expected.items():
            with self.subTest(command=name):
                help_text = self._subparser(name).format_help()
                for phrase in phrases:
                    self.assertIn(phrase, help_text)
                self.assertNotIn("\x1b[", help_text)

    def test_plain_parser_disables_python_314_color_when_supported(self):
        parser = PlainArgumentParser(prog="codeq-test")
        if hasattr(parser, "color"):
            self.assertFalse(parser.color)

    def test_version_is_a_real_top_level_flag(self):
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(stdout):
            build_parser().parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertRegex(stdout.getvalue().strip(), r"^codeq \d+\.\d+\.\d+(?:rc\d+)?$")


if __name__ == "__main__":
    unittest.main()
