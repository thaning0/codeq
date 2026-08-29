from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.agent_utility_benchmark import (
    _codeq_argv,
    _nested_exec_invocations,
    _query_from_argv,
    _returned_paths,
    _session_from_path,
    _shell_segments,
    load_sessions,
    render_markdown,
    summarize,
)


FIXTURES = Path(__file__).parent / "fixtures" / "agent_utility"


class AgentUtilityBenchmarkTests(unittest.TestCase):
    def test_direct_calls_pair_content_blocks_and_ignore_prose_examples(self) -> None:
        session = _session_from_path(FIXTURES / "direct_and_boundaries.jsonl")
        codeq_calls = [call for call in session.calls if call.queries]
        self.assertEqual(
            [call.call_id for call in codeq_calls],
            ["direct-codeq", "boundary-codeq", "edit-codeq", "nested-edit-codeq"],
        )
        self.assertTrue(codeq_calls[0].paired_output)
        self.assertEqual(codeq_calls[0].queries[0].command, "context")
        self.assertEqual(codeq_calls[0].queries[0].options, ("--limit",))
        self.assertEqual(_returned_paths(codeq_calls[0]), ({"src/widget.py"}, "query"))

    def test_heredocs_and_quoted_examples_are_not_shell_invocations(self) -> None:
        examples = [
            "printf 'codeq context QuotedExample'",
            "gh issue create --body-file - <<'EOF'\ncodeq context HeredocExample\nEOF",
        ]
        for command in examples:
            queries = []
            for segment in _shell_segments(command):
                argv = _codeq_argv(segment)
                if argv and (query := _query_from_argv(argv)):
                    queries.append(query)
            self.assertEqual(queries, [])

    def test_nested_exec_parser_ignores_javascript_strings_and_comments(self) -> None:
        source = """
const prose = "tools.exec_command({cmd: 'codeq context NotCalled'})";
// tools.exec_command({cmd: "codeq context Commented"})
const r = await tools.exec_command({cmd: "codeq context Called", workdir: "/fixture/repo"});
"""
        self.assertEqual(_nested_exec_invocations(source), [("codeq context Called", "/fixture/repo")])

    def test_parallel_queries_keep_collective_event_level_paths(self) -> None:
        session = _session_from_path(FIXTURES / "nested_parallel.jsonl")
        call = next(call for call in session.calls if call.call_id == "parallel-codeq")
        self.assertEqual(len(call.queries), 2)
        self.assertEqual({query.command for query in call.queries}, {"find", "context"})
        paths, attribution = _returned_paths(call)
        self.assertEqual(paths, {"src/widget.py", "src/factory.py"})
        self.assertEqual(attribution, "event")

    def test_user_turn_boundary_stops_downstream_consumption(self) -> None:
        sessions = [_session_from_path(FIXTURES / "direct_and_boundaries.jsonl")]
        data = summarize(sessions)
        edited_find = next(event for event in data["events"] if event["commands"] == {"find": 1})
        boundary = next(
            event
            for event in data["events"]
            if event["returned_path_count"] == 1
            and event["commands"] == {"context": 1}
            and event["classification"] == "unclassified"
        )
        self.assertFalse(boundary["signals"]["returned_path_read"])
        self.assertEqual(edited_find["classification"], "returned_path_consumed")
        self.assertTrue(edited_find["signals"]["returned_path_edited"])
        nested_edit = next(
            event
            for event in data["events"]
            if event["commands"] == {"context": 1} and event["signals"]["returned_path_edited"]
        )
        self.assertEqual(nested_edit["classification"], "returned_path_consumed")

    def test_verification_is_distinct_from_new_consumed_path(self) -> None:
        sessions = load_sessions(FIXTURES)
        data = summarize(sessions)
        classes = data["aggregates"]["classifications"]
        self.assertEqual(classes["verification"], 1)
        self.assertEqual(classes["complemented_result"], 1)
        self.assertEqual(classes["compensated_or_missed_result"], 1)
        self.assertEqual(classes["new_result_not_consumed"], 1)
        complemented = next(event for event in data["events"] if event["classification"] == "complemented_result")
        self.assertTrue(complemented["signals"]["search_repeated_returned_path"])
        self.assertEqual(complemented["signals"]["new_search_path_consumed_count"], 1)
        compensated = next(
            event for event in data["events"] if event["classification"] == "compensated_or_missed_result"
        )
        self.assertFalse(compensated["signals"]["search_repeated_returned_path"])
        self.assertEqual(compensated["signals"]["new_search_path_consumed_count"], 1)

    def test_coissued_families_and_refinement_are_reported_separately(self) -> None:
        data = summarize(load_sessions(FIXTURES))
        coissued = next(event for event in data["events"] if event["coissued_families"].get("git") == 1)
        self.assertEqual(coissued["coissued_families"], {"git": 1, "search": 1})
        self.assertNotIn("git", coissued["later_families"])
        self.assertEqual(coissued["classification"], "returned_path_consumed")
        self.assertEqual(data["aggregates"]["classifications"]["codeq_refinement"], 1)

    def test_output_is_deterministic_and_contains_no_raw_inputs(self) -> None:
        sessions = load_sessions(FIXTURES)
        first = summarize(sessions, "2026-08-01", "2026-08-02")
        second = summarize(load_sessions(FIXTURES), "2026-08-01", "2026-08-02")
        first_json = json.dumps(first, ensure_ascii=False, sort_keys=True)
        self.assertEqual(first_json, json.dumps(second, ensure_ascii=False, sort_keys=True))
        markdown = render_markdown(first)
        for secret in ("Widget", "RefineMe", "/fixture/repo", "src/widget.py", "direct-codeq"):
            self.assertNotIn(secret, first_json)
            self.assertNotIn(secret, markdown)
        self.assertIn("does **not** claim", markdown)

    def test_cli_artifacts_are_byte_deterministic(self) -> None:
        data = summarize(load_sessions(FIXTURES))
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.json"
            second = Path(tmp) / "second.json"
            first.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            second.write_text(
                json.dumps(summarize(load_sessions(FIXTURES)), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
