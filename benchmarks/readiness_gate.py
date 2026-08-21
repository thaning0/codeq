from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from codeq import __version__


def _check(name: str, passed: bool, actual: Any, requirement: str) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "actual": actual,
        "requirement": requirement,
    }


def evaluate(performance: dict[str, Any], workflows: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    cold = performance.get("cold") or {}
    warm = performance.get("warm") or {}

    def p95(group: dict[str, Any], name: str) -> float:
        return float((group.get(name) or {}).get("p95_ms") or 0.0)

    checks.extend(
        [
            _check("warm_context_p95", p95(warm, "context_symbol") <= 3000.0, p95(warm, "context_symbol"), "<= 3000 ms"),
            _check("warm_trace_p95", p95(warm, "trace_in") <= 3000.0, p95(warm, "trace_in"), "<= 3000 ms"),
            _check("cold_context_p95", p95(cold, "context_symbol") <= 5000.0, p95(cold, "context_symbol"), "<= 5000 ms"),
            _check("cold_trace_p95", p95(cold, "trace_in") <= 5000.0, p95(cold, "trace_in"), "<= 5000 ms"),
        ]
    )

    semantic_names = {"find_exact", "find_concept", "context_symbol", "context_cursor", "context_lexical", "trace_in"}
    semantic_samples: list[float] = []
    for phase in (cold, warm):
        for name, result in phase.items():
            if name not in semantic_names or not isinstance(result, dict):
                continue
            semantic_samples.extend(float(item.get("duration_ms", 0.0)) for item in result.get("samples", []))
    semantic_max = max(semantic_samples, default=0.0)
    checks.append(_check("no_10s_semantic_outlier", semantic_max < 10000.0, round(semantic_max, 1), "< 10000 ms"))

    sample = workflows.get("sample") or {}
    validation = workflows.get("current_validation") or {}
    sample_size = int(sample.get("size") or 0)
    coverage = float(sample.get("mapped_call_coverage_pct") or 0.0)
    validation_queries = int(validation.get("queries") or 0)
    statuses = validation.get("statuses") or {}
    validation_ok = int(statuses.get("ok") or 0)
    ok_rate = (100.0 * validation_ok / validation_queries) if validation_queries else 0.0
    navigation = [item for item in sample.get("workflows", []) if item.get("category") == "navigation"]
    navigation_fallback = sum(1 for item in navigation if item.get("fallback_families"))

    checks.extend(
        [
            _check("historical_sample_size", sample_size >= 100, sample_size, ">= 100 workflows"),
            _check("historical_mapping_coverage", coverage >= 90.0, coverage, ">= 90%"),
            _check("navigation_fallback_free", len(navigation) >= 50 and navigation_fallback == 0, {"workflows": len(navigation), "fallback": navigation_fallback}, ">= 50 navigation workflows and 0 unsupported fallback"),
            _check("extracted_query_validation", validation_queries >= 30 and ok_rate >= 95.0, {"queries": validation_queries, "ok_rate_pct": round(ok_rate, 1)}, ">= 30 queries and >= 95% ok"),
        ]
    )

    passed = all(item["passed"] for item in checks)
    return {
        "status": "PASS" if passed else "FAIL",
        "gate_version": __version__,
        "checks": checks,
        "performance_version": performance.get("codeq_version"),
        "workflow_version": workflows.get("codeq_version"),
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# codeq 1.0 readiness gate",
        "",
        f"Overall: **{result['status']}**",
        "",
        "| Gate | Result | Actual | Requirement |",
        "| --- | --- | --- | --- |",
    ]
    for item in result["checks"]:
        lines.append(
            f"| {item['name']} | {'PASS' if item['passed'] else 'FAIL'} | `{json.dumps(item['actual'], ensure_ascii=False)}` | {item['requirement']} |"
        )
    lines.extend(
        [
            "",
            f"Readiness gate version: `{result.get('gate_version')}`",
            f"Performance artifact version: `{result.get('performance_version')}`",
            f"Historical replay artifact version: `{result.get('workflow_version')}`",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--performance", type=Path, default=Path("benchmarks/results/0.5.1-quant.json"))
    parser.add_argument("--workflows", type=Path, default=Path("benchmarks/results/0.5.2-workflows.json"))
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results/0.5.3-readiness.json"))
    parser.add_argument("--markdown", type=Path, default=Path("benchmarks/0.5.3-readiness.md"))
    args = parser.parse_args()

    performance = json.loads(args.performance.read_text(encoding="utf-8"))
    workflows = json.loads(args.workflows.read_text(encoding="utf-8"))
    result = evaluate(performance, workflows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
