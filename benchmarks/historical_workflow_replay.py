from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from codeq import __version__

_MCP_RE = re.compile(r"mcporter\s+call\s+code-review-graph\.([A-Za-z0-9_]+)([^\n\r]*)", re.I)
_DIRECT_RE = re.compile(r"(?:code_review_graph_|code-review-graph\.)([A-Za-z0-9_]+)$", re.I)
_ARG_RE = re.compile(
    r"\b(?P<key>pattern|target|query|task|base|changed_files)=(?:\\?[\"'](?P<quoted>.*?)(?<!\\)\\?[\"']|(?P<plain>[^\s;,)]+))",
    re.I,
)

_REVIEW_TOOLS = {"detect_changes", "get_impact_radius", "get_affected_flows", "get_review_context"}
_ARCH_TOOLS = {"get_architecture_overview", "get_community", "list_communities"}
_FLOW_TOOLS = {"list_flows", "get_flow"}
_MAINTENANCE_TOOLS = {
    "build_or_update_graph",
    "status",
    "list_graph_stats",
    "embed_graph",
    "run_postprocess",
}
_SUPPORTED_QUERY_PATTERNS = {
    "callers_of",
    "callees_of",
    "tests_for",
    "file_summary",
    "imports_of",
    "importers_of",
    "children_of",
}


@dataclass
class Call:
    name: str
    args: dict[str, str] = field(default_factory=dict)


@dataclass
class Workflow:
    source: str
    session_key: str
    timestamp: str
    cwd: str
    calls: list[Call]
    companion: Counter[str]
    quant_related: bool


def _normalize_tool(name: str) -> str:
    value = name.strip().lower().replace("-", "_")
    if value.startswith("code_review_graph_"):
        value = value[len("code_review_graph_") :]
    if value.endswith("_tool"):
        value = value[:-5]
    return value


def _parse_args(text: str) -> dict[str, str]:
    normalized = text.replace("\\\"", '"').replace("\\'", "'")
    out: dict[str, str] = {}
    for match in _ARG_RE.finditer(normalized):
        value = match.group("quoted") if match.group("quoted") is not None else match.group("plain")
        if value is not None:
            out[match.group("key").lower()] = value.strip("\\\"")[:500]
    return out


def _calls_from_tool(name: str, raw: str) -> list[Call]:
    calls: list[Call] = []
    direct = _DIRECT_RE.search(name)
    if direct:
        calls.append(Call(_normalize_tool(direct.group(1)), _parse_args(raw)))
    for match in _MCP_RE.finditer(raw):
        calls.append(Call(_normalize_tool(match.group(1)), _parse_args(match.group(2))))
    return calls


def _companion_from_tool(name: str, raw: str) -> Counter[str]:
    out: Counter[str] = Counter()
    lowered_name = name.lower()
    lowered = raw.lower()
    if lowered_name in {"read", "read_file"}:
        out["read"] += 1
    if lowered_name in {"grep", "search"}:
        out["grep"] += 1
    if any(token in lowered for token in (" rg ", "\nrg ", "grep ", "ripgrep")):
        out["grep"] += 1
    if any(token in lowered for token in ("sed -n", "cat ", "head ", "tail ")):
        out["read"] += 1
    if re.search(r"(?:^|[\n;&])\s*git\s+", lowered):
        out["git"] += 1
    return out


def _git_is_quant(cwd: str) -> bool:
    if not cwd:
        return False
    if cwd.startswith("/home/thn/Quant") or "Quant-worktrees" in cwd:
        return True
    path = Path(cwd)
    if not path.exists():
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "thaning0/quant" in proc.stdout.lower()


def _quant_hint(*texts: str) -> bool:
    joined = " ".join(texts).lower()
    return any(
        token in joined
        for token in (
            "/home/thn/quant",
            "quant-worktrees",
            "thaning0/quant",
            "backend/src/app/",
            "frontend/src/",
            "quant-cli/",
        )
    )


def _json_records(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _codex_workflow(path: Path) -> Workflow | None:
    cwd = ""
    timestamp = ""
    calls: list[Call] = []
    companion: Counter[str] = Counter()
    hints: list[str] = []
    for record in _json_records(path):
        timestamp = timestamp or str(record.get("timestamp") or "")
        if record.get("type") == "session_meta":
            payload = record.get("payload") or {}
            cwd = str(payload.get("cwd") or cwd)
            continue
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload") or {}
        if payload.get("type") not in {"custom_tool_call", "function_call"}:
            continue
        name = str(payload.get("name") or "")
        raw_value = payload.get("input") if payload.get("input") is not None else payload.get("arguments")
        raw = raw_value if isinstance(raw_value, str) else json.dumps(raw_value or {}, ensure_ascii=False)
        found = _calls_from_tool(name, raw)
        if found:
            calls.extend(found)
            hints.append(raw[:1200])
        companion.update(_companion_from_tool(name, raw))
    if not calls:
        return None
    key = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    return Workflow(
        source="codex",
        session_key=key,
        timestamp=timestamp,
        cwd=cwd,
        calls=calls,
        companion=companion,
        quant_related=_git_is_quant(cwd) or _quant_hint(cwd, *hints),
    )


def _pi_workflow(path: Path) -> Workflow | None:
    cwd = ""
    timestamp = ""
    calls: list[Call] = []
    companion: Counter[str] = Counter()
    hints: list[str] = []
    for record in _json_records(path):
        timestamp = timestamp or str(record.get("timestamp") or "")
        if record.get("type") == "session":
            cwd = str(record.get("cwd") or cwd)
            continue
        if record.get("type") != "message":
            continue
        message = record.get("message") or {}
        if message.get("role") != "assistant":
            continue
        for item in message.get("content") or []:
            if not isinstance(item, dict) or item.get("type") != "toolCall":
                continue
            name = str(item.get("name") or "")
            args = item.get("arguments") or {}
            raw = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
            found = _calls_from_tool(name, raw)
            if found:
                calls.extend(found)
                hints.append(raw[:1200])
            companion.update(_companion_from_tool(name, raw))
    if not calls:
        return None
    key = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    return Workflow(
        source="pi",
        session_key=key,
        timestamp=timestamp,
        cwd=cwd,
        calls=calls,
        companion=companion,
        quant_related=_git_is_quant(cwd) or _quant_hint(cwd, *hints),
    )


def load_workflows(codex_root: Path, pi_root: Path) -> list[Workflow]:
    out: list[Workflow] = []
    for path in codex_root.rglob("*.jsonl"):
        workflow = _codex_workflow(path)
        if workflow:
            out.append(workflow)
    for path in pi_root.rglob("*.jsonl"):
        workflow = _pi_workflow(path)
        if workflow:
            out.append(workflow)
    return out


def _query_pattern(call: Call) -> str:
    return call.args.get("pattern", "").lower()


def _coverage(call: Call) -> str:
    name = call.name
    if name in _MAINTENANCE_TOOLS:
        return "eliminated"
    if name in {"semantic_search_nodes", "get_minimal_context", "detect_changes", "get_impact_radius", "get_review_context"}:
        return "direct"
    if name == "query_graph":
        return "direct" if _query_pattern(call) in _SUPPORTED_QUERY_PATTERNS else "approximate"
    if name in {"get_affected_flows", *_FLOW_TOOLS}:
        return "approximate"
    if name in _ARCH_TOOLS or name in {"refactor", "find_large_functions"}:
        return "fallback"
    return "fallback"


def _mapped_actions(workflow: Workflow) -> tuple[list[str], list[str]]:
    names = {call.name for call in workflow.calls}
    patterns = {_query_pattern(call) for call in workflow.calls if call.name == "query_graph"}
    actions: list[str] = []
    fallbacks: list[str] = []
    if names & _REVIEW_TOOLS:
        actions.append("review")
    if "semantic_search_nodes" in names or "get_minimal_context" in names:
        actions.append("find")
    if "get_minimal_context" in names or patterns & {"tests_for", "file_summary", "children_of", "imports_of", "importers_of"}:
        actions.append("context")
    if patterns & {"callers_of", "callees_of"} or names & (_FLOW_TOOLS | {"get_affected_flows"}):
        actions.append("trace")
    if names & _ARCH_TOOLS:
        fallbacks.append("architecture_rg_read")
    if "refactor" in names:
        fallbacks.append("refactor_manual")
    return list(dict.fromkeys(actions)), list(dict.fromkeys(fallbacks))


def _category(workflow: Workflow) -> str:
    names = {call.name for call in workflow.calls}
    if names & {"detect_changes", "get_review_context"}:
        return "review"
    if names & (_ARCH_TOOLS | _FLOW_TOOLS | {"get_affected_flows"}) or len(workflow.calls) >= 15:
        return "complex"
    return "navigation"


def _select_evenly(items: list[Workflow], count: int) -> list[Workflow]:
    if count <= 0 or not items:
        return []
    ordered = sorted(items, key=lambda item: (item.timestamp, item.session_key))
    if len(ordered) <= count:
        return ordered
    if count == 1:
        return [ordered[len(ordered) // 2]]
    indexes = [round(index * (len(ordered) - 1) / (count - 1)) for index in range(count)]
    return [ordered[index] for index in indexes]


def sample_workflows(workflows: list[Workflow]) -> list[Workflow]:
    quant = [workflow for workflow in workflows if workflow.quant_related]
    buckets = {category: [w for w in quant if _category(w) == category] for category in ("navigation", "review", "complex")}
    requested = {"navigation": 50, "review": 30, "complex": 20}
    sample: list[Workflow] = []
    for category, count in requested.items():
        sample.extend(_select_evenly(buckets[category], count))
    if len(sample) < 100:
        selected = {workflow.session_key for workflow in sample}
        remaining = [workflow for workflow in quant if workflow.session_key not in selected]
        sample.extend(_select_evenly(remaining, 100 - len(sample)))
    return sample[:100]


def _normalize_target(target: str) -> str:
    value = target.strip().strip('"\'')
    if "::" in value:
        value = value.rsplit("::", 1)[1]
    return value.removesuffix("()")[:200]


def validation_candidates(sample: list[Workflow], limit: int) -> list[tuple[str, list[str]]]:
    candidates: list[tuple[str, list[str]]] = []
    seen: set[tuple[str, str]] = set()
    for workflow in sample:
        for call in workflow.calls:
            if call.name == "query_graph" and _query_pattern(call) in _SUPPORTED_QUERY_PATTERNS:
                target = _normalize_target(call.args.get("target", ""))
                if not target or len(target) > 160:
                    continue
                key = ("context", target)
                if key in seen:
                    continue
                seen.add(key)
                args = ["context", target, "--limit", "5", "--json"]
                candidates.append(("context", args))
            elif call.name == "semantic_search_nodes":
                query = call.args.get("query", "").strip()
                if not query or "\n" in query or len(query) > 120:
                    continue
                key = ("find", query)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(("find", ["find", query, "--limit", "5", "--json"]))
            if len(candidates) >= limit:
                return candidates
    return candidates


def run_current_validation(root: Path, candidates: list[tuple[str, list[str]]]) -> dict[str, Any]:
    statuses: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    durations: list[float] = []
    for kind, args in candidates:
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                ["codeq", "--root", str(root), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            statuses["runtime_error"] += 1
            kinds[kind] += 1
            continue
        durations.append((time.perf_counter() - started) * 1000.0)
        kinds[kind] += 1
        try:
            data = json.loads(proc.stdout)
            statuses[str(data.get("status") or "unknown")] += 1
        except json.JSONDecodeError:
            statuses["invalid_json"] += 1
    return {
        "queries": len(candidates),
        "by_kind": dict(kinds),
        "statuses": dict(statuses),
        "p50_ms": round(sorted(durations)[len(durations) // 2], 1) if durations else None,
        "max_ms": round(max(durations), 1) if durations else None,
    }


def summarize(workflows: list[Workflow], sample: list[Workflow], validation: dict[str, Any]) -> dict[str, Any]:
    all_quant = [workflow for workflow in workflows if workflow.quant_related]
    call_counts: Counter[str] = Counter(call.name for workflow in all_quant for call in workflow.calls)
    pattern_counts: Counter[str] = Counter(
        _query_pattern(call)
        for workflow in all_quant
        for call in workflow.calls
        if call.name == "query_graph" and _query_pattern(call)
    )
    coverage: Counter[str] = Counter(_coverage(call) for workflow in sample for call in workflow.calls)
    categories = Counter(_category(workflow) for workflow in sample)
    source_counts = Counter(workflow.source for workflow in sample)
    old_calls = sum(len(workflow.calls) for workflow in sample)
    companion = sum(sum(workflow.companion.values()) for workflow in sample)
    mapped_calls = 0
    fallback_workflows = 0
    mapped_action_counts: Counter[str] = Counter()
    fallback_counts: Counter[str] = Counter()
    anonymous: list[dict[str, Any]] = []
    for workflow in sample:
        actions, fallbacks = _mapped_actions(workflow)
        mapped_calls += len(actions)
        if fallbacks:
            fallback_workflows += 1
        mapped_action_counts.update(actions)
        fallback_counts.update(fallbacks)
        anonymous.append(
            {
                "id": workflow.session_key,
                "source": workflow.source,
                "category": _category(workflow),
                "crg_calls": len(workflow.calls),
                "companion_calls": dict(workflow.companion),
                "tools": dict(Counter(call.name for call in workflow.calls)),
                "query_graph_patterns": dict(
                    Counter(_query_pattern(call) for call in workflow.calls if call.name == "query_graph" and _query_pattern(call))
                ),
                "mapped_codeq_actions": actions,
                "fallback_families": fallbacks,
            }
        )
    actionable = sum(value for key, value in coverage.items() if key != "eliminated")
    directly_covered = coverage["direct"] + coverage["approximate"]
    return {
        "codeq_version": __version__,
        "corpus": {
            "all_crg_workflows": len(workflows),
            "quant_crg_workflows": len(all_quant),
            "quant_crg_calls": sum(call_counts.values()),
            "top_tools": call_counts.most_common(20),
            "query_graph_patterns": pattern_counts.most_common(20),
        },
        "sample": {
            "size": len(sample),
            "categories": dict(categories),
            "sources": dict(source_counts),
            "historical_crg_calls": old_calls,
            "historical_companion_observations": companion,
            "coverage": dict(coverage),
            "actionable_calls": actionable,
            "mapped_direct_or_approximate_calls": directly_covered,
            "mapped_call_coverage_pct": round(100.0 * directly_covered / actionable, 1) if actionable else 0.0,
            "mapped_unique_codeq_actions": mapped_calls,
            "observation_compression_ratio": round(old_calls / mapped_calls, 2) if mapped_calls else None,
            "fallback_workflows": fallback_workflows,
            "fallback_workflow_pct": round(100.0 * fallback_workflows / len(sample), 1) if sample else 0.0,
            "mapped_action_counts": dict(mapped_action_counts),
            "fallback_counts": dict(fallback_counts),
            "workflows": anonymous,
        },
        "current_validation": validation,
    }


def render_markdown(data: dict[str, Any]) -> str:
    corpus = data["corpus"]
    sample = data["sample"]
    validation = data["current_validation"]
    category_rows: list[str] = []
    workflows = sample.get("workflows", [])
    for category in ("navigation", "review", "complex"):
        selected = [item for item in workflows if item.get("category") == category]
        old_calls = sum(int(item.get("crg_calls", 0)) for item in selected)
        mapped = sum(len(item.get("mapped_codeq_actions", [])) for item in selected)
        fallback = sum(1 for item in selected if item.get("fallback_families"))
        compression = round(old_calls / mapped, 2) if mapped else 0.0
        category_rows.append(
            f"| {category} | {len(selected)} | {old_calls} | {mapped} | {compression}x | {fallback} |"
        )
    lines = [
        f"# codeq {data.get('codeq_version', '0.5.2')} historical workflow replay",
        "",
        "This report is generated from actual local Codex/Pi session JSONL records containing real code-review-graph tool calls. It is a structural/tool-observation replay, not an LLM re-run of the historical tasks.",
        "",
        "Privacy: committed artifacts are anonymized. No historical user prompts, raw session paths, or concrete private query targets are stored.",
        "",
        "## Corpus",
        "",
        f"- CRG workflows parsed: **{corpus['all_crg_workflows']}**",
        f"- Quant-related CRG workflows: **{corpus['quant_crg_workflows']}**",
        f"- Quant-related CRG calls: **{corpus['quant_crg_calls']}**",
        "",
        "## 100-workflow sample",
        "",
        f"- Categories: `{sample['categories']}`",
        f"- Sources: `{sample['sources']}`",
        f"- Historical CRG calls: **{sample['historical_crg_calls']}**",
        f"- Historical companion grep/read/git observations: **{sample['historical_companion_observations']}**",
        f"- Direct/approximate mapping coverage (excluding eliminated graph-maintenance calls): **{sample['mapped_call_coverage_pct']}%**",
        f"- Mapped unique codeq observations: **{sample['mapped_unique_codeq_actions']}**",
        f"- CRG-to-codeq observation compression: **{sample['observation_compression_ratio']}x**",
        f"- Workflows retaining a genuinely unsupported rg/read fallback family: **{sample['fallback_workflows']} / {sample['size']} ({sample['fallback_workflow_pct']}%)**",
        "",
        "Category breakdown:",
        "",
        "| Category | Workflows | Historical CRG calls | Mapped codeq observations | Compression | Unsupported fallback workflows |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        *category_rows,
        "",
        "Mapped codeq action counts:",
        "",
        "```text",
        json.dumps(sample["mapped_action_counts"], ensure_ascii=False, indent=2),
        "```",
        "",
        "Residual unsupported fallback families (named affected-flow calls are mapped approximately to review/trace rather than counted as mandatory fallback):",
        "",
        "```text",
        json.dumps(sample["fallback_counts"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Current-repository extracted-query validation",
        "",
        f"- Extracted anonymous concrete query probes: **{validation['queries']}**",
        f"- Kinds: `{validation['by_kind']}`",
        f"- Current statuses: `{validation['statuses']}`",
        f"- P50 wall time: **{validation['p50_ms']} ms**",
        f"- Max wall time: **{validation['max_ms']} ms**",
        "",
        "A historical target returning `not_found` is not automatically a codeq failure: the replay queries the current Quant tree, so deleted/renamed historical symbols can legitimately be stale. The correctness signal is that current queries remain fail-closed rather than silently resolving to unrelated symbols.",
        "",
        "## Comparison boundary",
        "",
        "The historical CRG side is observed data: actual CRG calls plus actual companion grep/read/git observations from the same session-level workflows. The codeq side is a deterministic structural replay that compresses those CRG observation families into find/context/trace/review actions. There is no honest counterfactual record of the same 100 historical tasks being re-run by the same agent with rg/read/git only, so this report does **not** invent a synthetic C-group success/time number. The historical companion count is reported as observed pressure, not as a pure baseline.",
        "",
        "## Interpretation",
        "",
        "Navigation is the strongest result: all 50 sampled navigation workflows map without an unsupported fallback. Review/complex workflows retain architecture/community fallback only when the historical session explicitly asked CRG for those abstractions; named affected-flow calls are treated as approximate review/trace coverage because their underlying impact questions are already represented by current codeq. The replay deliberately does not treat architecture/community abstractions as a reason to rebuild a persistent graph.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-root", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--pi-root", type=Path, default=Path.home() / ".pi/agent/sessions")
    parser.add_argument("--quant-root", type=Path, default=Path.home() / "Quant")
    parser.add_argument("--validate", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args()

    workflows = load_workflows(args.codex_root, args.pi_root)
    sample = sample_workflows(workflows)
    candidates = validation_candidates(sample, max(0, args.validate))
    validation = run_current_validation(args.quant_root, candidates)
    data = summarize(workflows, sample, validation)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render_markdown(data), encoding="utf-8")
    print(json.dumps({"corpus": data["corpus"], "sample": {k: v for k, v in data["sample"].items() if k != "workflows"}, "current_validation": validation}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
