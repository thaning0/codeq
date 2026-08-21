from __future__ import annotations

import unittest
from typing import Any

from benchmarks.readiness_gate import evaluate


class ReadinessGateTests(unittest.TestCase):
    def _performance(self) -> dict[str, Any]:
        sample = [{"duration_ms": 100.0}]
        return {
            "codeq_version": "0.5.1",
            "cold": {
                "context_symbol": {"p95_ms": 4000.0, "samples": sample},
                "trace_in": {"p95_ms": 4200.0, "samples": sample},
                "find_exact": {"p95_ms": 1000.0, "samples": sample},
                "find_concept": {"p95_ms": 1200.0, "samples": sample},
                "context_cursor": {"p95_ms": 4100.0, "samples": sample},
                "context_lexical": {"p95_ms": 4300.0, "samples": sample},
            },
            "warm": {
                "context_symbol": {"p95_ms": 200.0, "samples": sample},
                "trace_in": {"p95_ms": 400.0, "samples": sample},
                "find_exact": {"p95_ms": 300.0, "samples": sample},
                "find_concept": {"p95_ms": 900.0, "samples": sample},
                "context_cursor": {"p95_ms": 250.0, "samples": sample},
                "context_lexical": {"p95_ms": 500.0, "samples": sample},
            },
        }

    def _workflows(self) -> dict[str, Any]:
        navigation = [
            {"category": "navigation", "fallback_families": []}
            for _ in range(50)
        ]
        other = [
            {"category": "review", "fallback_families": ["architecture_rg_read"]}
            for _ in range(50)
        ]
        return {
            "codeq_version": "0.5.2",
            "sample": {
                "size": 100,
                "mapped_call_coverage_pct": 93.0,
                "workflows": navigation + other,
            },
            "current_validation": {
                "queries": 30,
                "statuses": {"ok": 30},
            },
        }

    def test_gate_passes_when_all_readiness_thresholds_hold(self) -> None:
        result = evaluate(self._performance(), self._workflows())
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(all(item["passed"] for item in result["checks"]))

    def test_gate_fails_on_navigation_fallback_or_slow_cold_context(self) -> None:
        performance = self._performance()
        performance["cold"]["context_symbol"]["p95_ms"] = 6000.0
        workflows = self._workflows()
        workflows["sample"]["workflows"][0]["fallback_families"] = ["architecture_rg_read"]
        result = evaluate(performance, workflows)
        self.assertEqual(result["status"], "FAIL")
        failed = {item["name"] for item in result["checks"] if not item["passed"]}
        self.assertIn("cold_context_p95", failed)
        self.assertIn("navigation_fallback_free", failed)


if __name__ == "__main__":
    unittest.main()
