from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any, Callable

from codeq import __version__
from codeq.workspace import Workspace


def _rss_kb(pid: int) -> int:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return 0


def _workspace_rss_kb(workspace: Workspace) -> int:
    return sum(_rss_kb(int(item["pid"])) for item in workspace.session_stats() if item.get("alive"))


def _delta(after: dict[str, int], before: dict[str, int], key: str) -> int:
    return max(0, int(after.get(key, 0)) - int(before.get(key, 0)))


def _one(workspace: Workspace, action: Callable[[Workspace], dict[str, Any]]) -> dict[str, Any]:
    before = workspace.metrics_snapshot()
    started = time.perf_counter()
    result = action(workspace)
    duration_ms = (time.perf_counter() - started) * 1000.0
    after = workspace.metrics_snapshot()
    return {
        "status": result.get("status"),
        "duration_ms": round(duration_ms, 1),
        "lsp_requests": _delta(after, before, "lsp_request_count"),
        "sessions_started": _delta(after, before, "sessions_started"),
        "prewarm_files": _delta(after, before, "prewarm_files"),
        "prewarm_probes": _delta(after, before, "prewarm_probes"),
        "prewarm_early_stops": _delta(after, before, "prewarm_early_stops"),
        "document_symbols_hit": _delta(after, before, "document_symbols_hit"),
        "document_symbols_miss": _delta(after, before, "document_symbols_miss"),
        "rss_kb": _workspace_rss_kb(workspace),
    }


def _summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    durations = sorted(float(item["duration_ms"]) for item in samples)
    if not durations:
        return {}
    p50 = statistics.median(durations)
    p95_index = min(len(durations) - 1, max(0, int(round(0.95 * (len(durations) - 1)))))
    return {
        "runs": len(samples),
        "p50_ms": round(p50, 1),
        "p95_ms": round(durations[p95_index], 1),
        "max_ms": round(max(durations), 1),
        "max_rss_kb": max(int(item["rss_kb"]) for item in samples),
        "samples": samples,
    }


def benchmark(root: Path, reps: int) -> dict[str, Any]:
    actions: dict[str, Callable[[Workspace], dict[str, Any]]] = {
        "find_exact": lambda ws: ws.find("BacktestService", limit=8),
        "find_concept": lambda ws: ws.find("SSE backtest logs", limit=8),
        "context_symbol": lambda ws: ws.context("BacktestService.stream_backtest_logs", limit=10),
        "context_cursor": lambda ws: ws.context("backend/src/app/api/backtest.py:175:17", limit=10),
        "context_lexical": lambda ws: ws.context(
            "backend/src/app/api/backtest.py:175:17",
            limit=10,
            lexical_references=True,
            lexical_query="/logs/stream",
            lexical_paths=("frontend",),
            lexical_exclude_tests=True,
        ),
        "trace_in": lambda ws: ws.trace("BacktestService.stream_backtest_logs", "in", depth=2, limit=20),
        "text_env": lambda ws: ws.find("BACKTEST_QUESTDB_QUERY_TARGET_ROWS", limit=12, text=True),
        "review": lambda ws: ws.review("HEAD~1", limit=10),
    }

    cold: dict[str, Any] = {}
    for name, action in actions.items():
        samples: list[dict[str, Any]] = []
        for _ in range(reps):
            workspace = Workspace(root)
            try:
                samples.append(_one(workspace, action))
            finally:
                workspace.close()
        cold[name] = _summary(samples)

    warm: dict[str, Any] = {}
    workspace = Workspace(root)
    try:
        # One warmup pass establishes the representative language workspaces.
        for action in actions.values():
            _one(workspace, action)
        for name, action in actions.items():
            samples = [_one(workspace, action) for _ in range(reps)]
            warm[name] = _summary(samples)
    finally:
        workspace.close()

    return {
        "codeq_version": __version__,
        "root": str(root.resolve()),
        "reps": reps,
        "cold": cold,
        "warm": warm,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.home() / "Quant")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = benchmark(args.root.resolve(), max(1, args.reps))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
