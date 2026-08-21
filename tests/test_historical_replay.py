from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from benchmarks.historical_workflow_replay import (
    Call,
    Workflow,
    _codex_workflow,
    _coverage,
    _mapped_actions,
    _pi_workflow,
    summarize,
)


class HistoricalReplayTests(unittest.TestCase):
    def test_codex_parser_counts_only_actual_tool_calls_not_injected_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            records = [
                {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta", "payload": {"cwd": "/home/thn/Quant"}},
                {
                    "timestamp": "2026-01-01T00:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "example: mcporter call code-review-graph.get_minimal_context_tool task=x"}],
                    },
                },
                {
                    "timestamp": "2026-01-01T00:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "input": "mcporter call code-review-graph.semantic_search_nodes_tool query=fetchBars\nmcporter call code-review-graph.query_graph_tool pattern=callers_of target=frontend/src/api.ts::fetchBars",
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            workflow = _codex_workflow(path)
            assert workflow is not None
            self.assertEqual([call.name for call in workflow.calls], ["semantic_search_nodes", "query_graph"])
            self.assertEqual(workflow.calls[1].args["pattern"], "callers_of")
            self.assertTrue(workflow.quant_related)

    def test_pi_parser_supports_direct_and_mcporter_wrapped_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            records = [
                {"type": "session", "timestamp": "2026-01-01", "cwd": "/home/thn/Quant"},
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "toolCall", "name": "code_review_graph_get_minimal_context_tool", "arguments": {"task": "review"}},
                            {
                                "type": "toolCall",
                                "name": "bash",
                                "arguments": {"command": "mcporter call code-review-graph.detect_changes_tool base=HEAD~1"},
                            },
                        ],
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            workflow = _pi_workflow(path)
            assert workflow is not None
            self.assertEqual([call.name for call in workflow.calls], ["get_minimal_context", "detect_changes"])

    def test_standard_review_flow_maps_to_review_trace_without_named_flow_fallback(self) -> None:
        workflow = Workflow(
            source="codex",
            session_key="abc",
            timestamp="",
            cwd="/home/thn/Quant",
            calls=[Call("detect_changes"), Call("get_impact_radius"), Call("get_affected_flows")],
            companion=Counter(),
            quant_related=True,
        )
        actions, fallbacks = _mapped_actions(workflow)
        self.assertIn("review", actions)
        self.assertIn("trace", actions)
        self.assertEqual(fallbacks, [])
        self.assertEqual(_coverage(Call("get_affected_flows")), "approximate")

    def test_committed_summary_is_anonymized(self) -> None:
        secret = "PRIVATE_SYMBOL_XYZ"
        workflow = Workflow(
            source="codex",
            session_key="hash-only",
            timestamp="2026-01-01",
            cwd="/home/thn/Quant/private/worktree",
            calls=[Call("query_graph", {"pattern": "callers_of", "target": secret})],
            companion=Counter({"read": 1}),
            quant_related=True,
        )
        data = summarize([workflow], [workflow], {"queries": 0, "by_kind": {}, "statuses": {}, "p50_ms": None, "max_ms": None})
        rendered = json.dumps(data, ensure_ascii=False)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("/home/thn/Quant/private/worktree", rendered)
        self.assertIn("hash-only", rendered)


if __name__ == "__main__":
    unittest.main()
