from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .daemon import default_socket_path
from .util import compact_location, git_root


def _connect(socket_path: Path, timeout: float) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(str(socket_path))
    return client


def _spawn_daemon(socket_path: Path) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = socket_path.parent / "daemon.log"
    log = open(log_path, "ab", buffering=0)
    subprocess.Popen(
        [sys.executable, "-m", "codeq.daemon", "--socket", str(socket_path)],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        close_fds=True,
    )


def _request(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    socket_path = default_socket_path()
    try:
        client = _connect(socket_path, min(timeout, 1.0))
    except OSError:
        _spawn_daemon(socket_path)
        deadline = time.monotonic() + min(max(timeout, 3.0), 10.0)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            time.sleep(0.05)
            try:
                client = _connect(socket_path, min(timeout, 1.0))
                break
            except OSError as exc:
                last_error = exc
        else:
            raise RuntimeError(f"codeq daemon failed to start: {last_error}")
    client.settimeout(timeout)
    with client:
        file = client.makefile("rwb")
        file.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        file.flush()
        line = file.readline()
    if not line:
        raise RuntimeError("codeq daemon closed connection without a response")
    response = json.loads(line)
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "unknown daemon error")
    return response["data"]


def _print_locations(title: str, items: list[dict[str, Any]], indent: str = "  ") -> None:
    print(f"{title} ({len(items)})")
    if not items:
        print(f"{indent}-")
        return
    for item in items:
        name = item.get("name")
        prefix = f"{name}  " if name else ""
        print(f"{indent}{prefix}{compact_location(item)}")


def _render_find(data: dict[str, Any]) -> None:
    for item in data.get("results", []):
        container = f"{item.get('container')}." if item.get("container") else ""
        print(
            f"{item.get('kind','?'):<12} {container}{item.get('name','')}  "
            f"{item['path']}:{item['line']}:{item.get('column',1)}"
            f"  [{item.get('source','?')}]"
        )
    if not data.get("results"):
        print("No matches.")
    meta = data.get("_meta", {})
    print(f"\n[{data.get('result_count',0)} results; {meta.get('duration_ms','?')} ms]", file=sys.stderr)


def _render_resolution(data: dict[str, Any]) -> bool:
    status = data.get("status")
    if status == "ok":
        return True
    if status == "ambiguous":
        print(f"Ambiguous target: {data.get('target')}", file=sys.stderr)
        for item in data.get("candidates", []):
            container = f"{item.get('container')}." if item.get("container") else ""
            print(f"  {item.get('kind')} {container}{item.get('name')}  {compact_location(item)}", file=sys.stderr)
        return False
    print(data.get("reason") or data.get("error") or f"Target not found: {data.get('target')}", file=sys.stderr)
    return False


def _print_dynamic_references(items: list[dict[str, Any]], indent: str = "  ") -> None:
    print(f"Possible dynamic references ({len(items)})")
    if not items:
        print(f"{indent}-")
        return
    for item in items:
        reason = item.get("reason") or "possible"
        text = str(item.get("text") or "").strip()
        suffix = f"  {text}" if text else ""
        print(f"{indent}[{reason}] {compact_location(item)}{suffix}")


def _render_context(data: dict[str, Any]) -> None:
    if not _render_resolution(data):
        return
    s = data["symbol"]
    container = f"{s.get('container')}." if s.get("container") else ""
    print(f"{s.get('kind','?')} {container}{s.get('name','')}")
    print(compact_location(s))
    if data.get("hover"):
        print("\nHover")
        print(data["hover"].strip())
    snippet = data.get("source", {}).get("text")
    if snippet:
        print("\nSource")
        print(snippet)
    print()
    _print_locations("Callers", data.get("callers", []))
    _print_locations("Callees", data.get("callees", []))
    _print_locations("Implementations", data.get("implementations", []))
    _print_locations("Tests", data.get("tests", []))
    _print_locations("References", data.get("references", []))
    _print_dynamic_references(data.get("possible_dynamic_references", []))
    meta = data.get("_meta", {})
    print(f"\n[{meta.get('duration_ms','?')} ms]", file=sys.stderr)


def _render_trace(data: dict[str, Any]) -> None:
    if not _render_resolution(data):
        return

    def visit(node: dict[str, Any], prefix: str = "", last: bool = True, root: bool = False) -> None:
        item = node["node"]
        connector = "" if root else ("└─ " if last else "├─ ")
        cycle = " [cycle]" if node.get("cycle") else ""
        print(f"{prefix}{connector}{item.get('name','?')}  {item['path']}:{item['line']}{cycle}")
        children = node.get("children", [])
        next_prefix = prefix + ("" if root else ("   " if last else "│  "))
        for index, child in enumerate(children):
            visit(child, next_prefix, index == len(children) - 1, False)

    visit(data["tree"], root=True)
    if data.get("note"):
        print(f"\nNote: {data['note']}", file=sys.stderr)
    meta = data.get("_meta", {})
    print(
        f"\n[{data.get('node_count',1)} nodes; depth={data.get('depth')}; {meta.get('duration_ms','?')} ms]",
        file=sys.stderr,
    )


def _render_review(data: dict[str, Any]) -> None:
    print(f"Base: {data.get('base')}")
    print(f"Changed files: {data.get('changed_file_count', 0)}")
    for path in data.get("changed_files", []):
        print(f"  {path}")
    print(f"\nChanged symbols: {data.get('changed_symbol_count', 0)}")
    for detail in data.get("changed_symbols", []):
        symbol = detail["symbol"]
        container = f"{symbol.get('container')}." if symbol.get("container") else ""
        print(f"  {symbol.get('kind')} {container}{symbol.get('name')}  {symbol['path']}:{symbol['line']}")
        for caller in detail.get("callers", [])[:5]:
            print(f"    <- {caller.get('name')}  {caller['path']}:{caller['line']}")
        for dynamic in detail.get("possible_dynamic_references", [])[:5]:
            print(
                f"    ? {dynamic.get('reason','possible')}  "
                f"{dynamic['path']}:{dynamic['line']}"
            )
        for test in detail.get("tests", [])[:5]:
            print(f"    test {test['path']}:{test['line']}")
    print(f"\nAffected files: {data.get('impacted_file_count', 0)}")
    for path in data.get("impacted_files", [])[:30]:
        print(f"  {path}")
    print(f"\nPossible dynamic references: {data.get('possible_dynamic_reference_count', 0)}")
    print(f"\nLikely tests: {data.get('test_count', 0)}")
    for test in data.get("tests", [])[:30]:
        print(f"  {test.get('name','')}  {test['path']}:{test['line']}")
    if data.get("truncated"):
        print("\nResult truncated by --limit.", file=sys.stderr)
    meta = data.get("_meta", {})
    print(f"\n[{meta.get('duration_ms','?')} ms]", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codeq",
        description="Small CLI-first semantic code queries for coding agents.",
    )
    parser.add_argument("--root", default=".", help="Repository path (default: cwd).")
    parser.add_argument("--json", action="store_true", help="Emit one machine-readable JSON document.")
    parser.add_argument("--limit", type=int, default=20, help="Bound returned results/symbols.")
    parser.add_argument("--timeout", type=float, default=20.0, help="Per request timeout in seconds.")
    sub = parser.add_subparsers(dest="command", required=True)

    find = sub.add_parser("find", help="Find symbols or related source.")
    find.add_argument("query")
    find.add_argument(
        "--kind",
        help="Optional semantic kind filter, e.g. function, class, test, method.",
    )

    context = sub.add_parser("context", help="Get one symbol's local semantic neighborhood.")
    context.add_argument("target")

    trace = sub.add_parser("trace", help="Trace incoming or outgoing call hierarchy.")
    trace.add_argument("target")
    direction = trace.add_mutually_exclusive_group(required=True)
    direction.add_argument("--in", dest="direction", action="store_const", const="in", help="Trace callers.")
    direction.add_argument("--out", dest="direction", action="store_const", const="out", help="Trace callees.")
    trace.add_argument("--depth", type=int, default=3)
    trace.add_argument("--node-limit", type=int, default=100)

    review = sub.add_parser("review", help="Summarize semantic impact of git changes.")
    review.add_argument("--base", default="HEAD~1", help="Git base ref (default: HEAD~1).")
    return parser


def _normalize_global_options(argv: list[str]) -> list[str]:
    """Allow global options before or after the subcommand.

    Coding agents naturally emit both `codeq --json find Foo` and
    `codeq find Foo --json`; argparse normally accepts only the former.
    """
    flags = {"--json"}
    valued = {"--root", "--limit", "--timeout"}
    front: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in flags:
            front.append(arg)
            index += 1
            continue
        if arg in valued:
            if index + 1 >= len(argv):
                rest.append(arg)
                index += 1
                continue
            front.extend([arg, argv[index + 1]])
            index += 2
            continue
        if any(arg.startswith(option + "=") for option in valued):
            front.append(arg)
            index += 1
            continue
        rest.append(arg)
        index += 1
    return front + rest


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_normalize_global_options(raw_argv))
    root = git_root(args.root)
    payload: dict[str, Any] = {
        "command": args.command,
        "root": str(root),
        "limit": max(1, args.limit),
        "timeout": max(1.0, args.timeout),
    }
    if args.command == "find":
        payload["query"] = args.query
        payload["kind"] = args.kind
    elif args.command == "context":
        payload["target"] = args.target
    elif args.command == "trace":
        payload.update(
            target=args.target,
            direction=args.direction,
            depth=max(0, args.depth),
            node_limit=max(1, args.node_limit),
        )
    elif args.command == "review":
        payload["base"] = args.base

    try:
        data = _request(payload, timeout=max(args.timeout + 5.0, 10.0))
    except Exception as exc:
        print(f"codeq: {exc}", file=sys.stderr)
        raise SystemExit(2)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if args.command == "find":
        _render_find(data)
    elif args.command == "context":
        _render_context(data)
    elif args.command == "trace":
        _render_trace(data)
    elif args.command == "review":
        _render_review(data)


if __name__ == "__main__":
    main()
