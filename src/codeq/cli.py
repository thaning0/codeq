from __future__ import annotations

import argparse
import inspect
import json
import os
import signal
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any

from . import DAEMON_PROTOCOL_VERSION, __version__
from .daemon import default_socket_path
from .util import compact_location, git_root


_ARGPARSE_PARAMS = inspect.signature(argparse.ArgumentParser).parameters


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


class PlainArgumentParser(argparse.ArgumentParser):
    """ArgumentParser with stable, plain-text help across Python versions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        # Python 3.14 enables colored argparse help by default. codeq output is
        # consumed primarily by agents, so keep help deterministic and ANSI-free.
        if "color" in _ARGPARSE_PARAMS:
            kwargs.setdefault("color", False)
        if "suggest_on_error" in _ARGPARSE_PARAMS:
            kwargs.setdefault("suggest_on_error", False)
        super().__init__(*args, **kwargs)


def _connect(socket_path: Path, timeout: float) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
    except OSError:
        client.close()
        raise
    return client


def _spawn_daemon(socket_path: Path) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = socket_path.parent / "daemon.log"
    argv = [sys.executable, "-m", "codeq.daemon", "--socket", str(socket_path)]
    with open(os.devnull, "rb") as devnull, open(log_path, "ab", buffering=0) as log:
        os.posix_spawn(
            sys.executable,
            argv,
            os.environ.copy(),
            file_actions=[
                (os.POSIX_SPAWN_DUP2, devnull.fileno(), 0),
                (os.POSIX_SPAWN_DUP2, log.fileno(), 1),
                (os.POSIX_SPAWN_DUP2, log.fileno(), 2),
            ],
            setpgroup=0,
        )


def _connect_or_spawn(socket_path: Path, timeout: float) -> socket.socket:
    try:
        return _connect(socket_path, min(timeout, 1.0))
    except OSError:
        _spawn_daemon(socket_path)
        deadline = time.monotonic() + min(max(timeout, 3.0), 10.0)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            time.sleep(0.05)
            try:
                return _connect(socket_path, min(timeout, 1.0))
            except OSError as exc:
                last_error = exc
        raise RuntimeError(f"codeq daemon failed to start: {last_error}")


def _peer_pid(client: socket.socket) -> int | None:
    peercred = getattr(socket, "SO_PEERCRED", None)
    if peercred is None:
        return None
    try:
        raw = client.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3i"))
        pid, uid, _ = struct.unpack("3i", raw)
    except (OSError, struct.error):
        return None
    if uid != os.getuid() or pid <= 1:
        return None
    return pid


def _restart_stale_daemon(pid: int | None, socket_path: Path) -> None:
    if pid is None:
        raise RuntimeError("codeq daemon version mismatch; unable to identify stale daemon process")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not socket_path.exists():
            return
        try:
            probe = _connect(socket_path, 0.1)
        except OSError:
            socket_path.unlink(missing_ok=True)
            return
        else:
            probe.close()
        time.sleep(0.05)
    raise RuntimeError("stale codeq daemon did not exit after version mismatch")


def _request(payload: dict[str, Any], timeout: float, *, _allow_restart: bool = True) -> dict[str, Any]:
    socket_path = default_socket_path()
    client = _connect_or_spawn(socket_path, timeout)
    peer_pid = _peer_pid(client)
    wire_payload = {
        **payload,
        "_client_version": __version__,
        "_protocol_version": DAEMON_PROTOCOL_VERSION,
    }
    client.settimeout(timeout)
    with client:
        file = client.makefile("rwb")
        file.write((json.dumps(wire_payload, ensure_ascii=False) + "\n").encode("utf-8"))
        file.flush()
        line = file.readline()
    if not line:
        raise RuntimeError("codeq daemon closed connection without a response")
    response = json.loads(line)
    version_mismatch = (
        response.get("error_code") == "version_mismatch"
        or response.get("server_version") != __version__
        or response.get("protocol_version") != DAEMON_PROTOCOL_VERSION
    )
    if version_mismatch and _allow_restart:
        _restart_stale_daemon(peer_pid, socket_path)
        return _request(payload, timeout, _allow_restart=False)
    if version_mismatch:
        raise RuntimeError("codeq daemon version mismatch after restart")
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


def _query_exit_code(data: dict[str, Any]) -> int:
    status = data.get("status")
    return 0 if status in (None, "ok") else 1


def _render_find(data: dict[str, Any]) -> None:
    if data.get("status") and data.get("status") != "ok":
        print(data.get("reason") or data.get("error") or "find failed", file=sys.stderr)
        return
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


def _render_file_context(data: dict[str, Any]) -> None:
    file_info = data.get("file") or {}
    print(f"File {file_info.get('path', data.get('target', ''))}")
    if file_info.get("language"):
        print(f"Language: {file_info['language']}")

    outline = data.get("outline", [])
    total_symbols = int(data.get("symbol_count", len(outline)))
    matching = int(data.get("outline_matching_count", len(outline)))
    print(f"\nOutline (showing {len(outline)} of {matching} matching; {total_symbols} total symbols)")
    if not outline:
        print("  -")
    for item in outline:
        container = f"{item.get('container')}." if item.get("container") else ""
        print(f"  {item.get('kind','?'):<12} {container}{item.get('name','')}  line {item.get('line',1)}")
    if data.get("outline_truncated"):
        print("  ... more matching symbols available; increase --limit")
    if not data.get("outline_kind") and not data.get("container"):
        print("  next: use --outline-depth 2, --kind KIND, or --container NAME to disclose more")

    if not data.get("topology_loaded"):
        print(f"\nTopology: hidden ({data.get('import_count', 0)} direct imports; use --topology to disclose imports/importers)")
        meta = data.get("_meta", {})
        print(f"\n[{meta.get('duration_ms','?')} ms]", file=sys.stderr)
        return

    imports = data.get("imports", [])
    print(f"\nImports (showing {len(imports)} of {data.get('import_count', len(imports))})")
    if not imports:
        print("  -")
    for item in imports:
        names = ", ".join(str(name) for name in item.get("names", []))
        suffix = f" [{names}]" if names else ""
        resolved = item.get("resolved_paths", [])
        resolved_text = f" -> {', '.join(resolved)}" if resolved else ""
        print(f"  {item.get('specifier','')}:{item.get('line',1)}{suffix}{resolved_text}")

    if data.get("imports_truncated"):
        print("  ... more imports available; increase --limit")

    importers = data.get("importers", [])
    importer_suffix = "+" if data.get("importers_truncated") else ""
    print(f"\nImported by (showing {len(importers)}{importer_suffix})")
    if not importers:
        print("  -")
    for item in importers:
        text = str(item.get("text") or "").strip()
        suffix = f"  {text}" if text else ""
        print(f"  {item['path']}:{item['line']}:{item.get('column',1)}{suffix}")

    meta = data.get("_meta", {})
    print(f"\n[{meta.get('duration_ms','?')} ms]", file=sys.stderr)


def _render_context(data: dict[str, Any]) -> None:
    if not _render_resolution(data):
        return
    if data.get("kind") == "file":
        _render_file_context(data)
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
    print(f"Base: {data.get('requested_base', data.get('base'))}")
    print(f"Base mode: {data.get('base_mode', 'direct')}")
    if data.get("resolved_base"):
        print(f"Resolved base: {data['resolved_base']}")
    print(f"Changed files: {data.get('changed_file_count', 0)}")
    file_changes = data.get("file_changes", [])
    if file_changes:
        for change in file_changes:
            status = str(change.get("status") or "?")
            path = str(change.get("path") or "")
            old_path = change.get("old_path")
            if status in {"R", "C"} and old_path:
                print(f"  {status} {old_path} -> {path}")
            else:
                print(f"  {status} {path}")
            if change.get("semantic_status") == "deleted_not_analyzed":
                print("    semantic impact: unavailable from current worktree")
    else:
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
    for path in data.get("impacted_files", []):
        print(f"  {path}")
    if data.get("impacted_files_truncated"):
        print("  ... more affected files available; increase --limit")
    print(f"\nPossible dynamic references: {data.get('possible_dynamic_reference_count', 0)}")
    print(f"\nLikely tests: {data.get('test_count', 0)}")
    for test in data.get("tests", []):
        print(f"  {test.get('name','')}  {test['path']}:{test['line']}")
    if data.get("tests_truncated"):
        print("  ... more likely tests available; increase --limit")
    if data.get("truncated"):
        print("\nResult truncated by --limit.", file=sys.stderr)
    meta = data.get("_meta", {})
    print(f"\n[{meta.get('duration_ms','?')} ms]", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = PlainArgumentParser(
        prog="codeq",
        description="""\
Small, read-only code-intelligence CLI for coding agents.

Use codeq before broad manual exploration when you need to locate code, understand
one symbol, follow a call chain, or inspect the semantic impact of a Git diff.
Choose a command:
  find     You do not yet know the exact symbol/location.
  context  You know a symbol or file:line and need its local neighborhood.
  trace    You need a multi-hop caller/callee chain.
  review   You need changed symbols, impact, dynamic references, and likely tests.
""",
        epilog="""\
Common agent loop:
  codeq find 'backtest log streaming' --limit 8
  codeq context BacktestService.stream_backtest_logs
  codeq trace BacktestService.stream_backtest_logs --in --depth 2
  codeq review --base origin/main

Targets accepted by context/trace:
  Qualified symbol       BacktestService.stream_backtest_logs
  Bare symbol            fetchBars
  Source location        backend/src/app/services/backtest_service.py:684
  Source location+column backend/src/app/services/backtest_service.py:684:9
  Source file (context)  backend/src/app/services/backtest_service.py

Agent notes:
  * Prefer qualified symbols when known; qualified resolution is fail-closed.
  * Explicit path targets are exact; missing files fail closed with status not_found.
  * Existing unsupported source files return unsupported_language instead of fuzzy matches.
  * Query failures exit 1; runtime/tool failures exit 2.
  * Default output is compact plain text with no ANSI colors.
  * Use --json when another tool/script will consume the result.
  * Global options may appear before or after the subcommand.
  * Run `codeq COMMAND --help` for command-specific arguments and examples.
""",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show codeq version and exit.",
    )
    parser.add_argument(
        "--root",
        default=".",
        metavar="PATH",
        help="Repository/worktree path; codeq resolves its Git root (default: current directory).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable JSON document instead of plain text.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="Bound returned matches/symbols where applicable (default: 20).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        metavar="SEC",
        help="Language-server request timeout in seconds (default: 20).",
    )
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=PlainArgumentParser,
        metavar="COMMAND",
    )

    find = sub.add_parser(
        "find",
        help="Find symbols or related source from a name or short description.",
        description="""\
Find likely code locations before you know an exact target.

`QUERY` may be an exact symbol name, part of a qualified name, or a short
natural-language description. For concept searches, use vocabulary likely to occur
in the repository's source/comments (usually the source language); codeq does not
translate queries between natural languages.
""",
        epilog="""\
Examples:
  codeq find BacktestService
  codeq find BacktestService --kind class
  codeq find 'report summary freshness policy evidence' --limit 8
  codeq find fetchBars --root ~/Quant --json

Typical next step:
  Take the best definition from `find`, then run `codeq context TARGET`.
""",
    )
    find.add_argument(
        "query",
        metavar="QUERY",
        help="Symbol name, qualified-name fragment, or short source-code description.",
    )
    find.add_argument(
        "--kind",
        metavar="KIND",
        help="Optional result filter, e.g. function, method, class, interface, test.",
    )

    context = sub.add_parser(
        "context",
        help="Get one target's definition, callers, callees, references, and tests.",
        description="""\
Return the local semantic neighborhood of one target in a single query.

For a symbol or file:line target, context returns:
  Definition/location and signature/hover
  Bounded source snippet
  Callers and callees
  Implementations and references
  Likely tests
  Possible dynamic callback/registry references when detected

For a source-file target, context uses progressive disclosure: top-level outline by
default. Expand only what you need with --outline-depth, --kind, or --container;
add --topology only when you need imports/importers.

A file:line[:column] target is promoted to its enclosing function/method/type when
possible. Prefer a qualified symbol when you already know it. Explicit paths are
exact: missing files return not_found, and unsupported languages return
unsupported_language rather than falling back to symbol search.
""",
        epilog="""\
Examples:
  codeq context BacktestService.stream_backtest_logs
  codeq context backend/src/app/services/backtest_service.py:684
  codeq context backend/src/app/services/backtest_service.py
  codeq context backend/src/app/services/backtest_service.py --container BacktestService
  codeq context backend/src/app/services/backtest_service.py --kind method --limit 20
  codeq context frontend/src/features/market/api.ts --topology --limit 20
  codeq context fetchBars --json

Use `context` instead of several separate grep/read/caller/test lookups. If you need
more than the direct callers/callees, continue with `codeq trace`.
""",
    )
    context.add_argument(
        "target",
        metavar="TARGET",
        help="Qualified/bare symbol, source file, or location as PATH:LINE[:COLUMN].",
    )
    context.add_argument(
        "--outline-depth",
        type=int,
        default=1,
        metavar="N",
        help="File targets only: outline nesting depth; 1=top-level (default: 1).",
    )
    context.add_argument(
        "--kind",
        dest="outline_kind",
        metavar="KIND",
        help="File targets only: show matching symbols of one kind across the file.",
    )
    context.add_argument(
        "--container",
        metavar="NAME",
        help="File targets only: show a class/container and its children.",
    )
    context.add_argument(
        "--topology",
        action="store_true",
        help="File targets only: additionally disclose bounded imports and importers.",
    )

    trace = sub.add_parser(
        "trace",
        help="Trace a bounded incoming or outgoing call hierarchy.",
        description="""\
Follow a semantic call chain from one target.

Direction is explicit:
  --in   Walk incoming calls: who calls this symbol? Useful for impact radius.
  --out  Walk outgoing calls: what does this symbol call? Useful for execution flow.

Depth counts call edges: depth 0 returns only the root, depth 1 adds direct
neighbors, depth 2 adds one more hop. Traversal is cycle-protected and repository-
source bounded; external library nodes are omitted.
""",
        epilog="""\
Examples:
  codeq trace BacktestService.stream_backtest_logs --in --depth 2
  codeq trace fetchBars --out --depth 3
  codeq trace fetchBars --in --depth 2 --node-limit 50 --json

Use --in when asking "what can this change affect?" and --out when asking "what
happens after this entry point?".
""",
    )
    trace.add_argument(
        "target",
        metavar="TARGET",
        help="Qualified/bare symbol or source location as PATH:LINE[:COLUMN].",
    )
    direction = trace.add_mutually_exclusive_group(required=True)
    direction.add_argument(
        "--in",
        dest="direction",
        action="store_const",
        const="in",
        help="Trace callers/incoming calls toward higher-level entry points.",
    )
    direction.add_argument(
        "--out",
        dest="direction",
        action="store_const",
        const="out",
        help="Trace callees/outgoing calls toward lower-level implementation.",
    )
    trace.add_argument(
        "--depth",
        type=_nonnegative_int,
        default=3,
        metavar="N",
        help="Maximum call-edge depth; 0=root only, 1=direct neighbors (default: 3; must be >= 0).",
    )
    trace.add_argument(
        "--node-limit",
        type=int,
        default=100,
        metavar="N",
        help="Hard cap on emitted call-tree nodes to bound agent context (default: 100).",
    )

    review = sub.add_parser(
        "review",
        help="Summarize semantic impact of changes relative to a Git base ref.",
        description="""\
Turn `git diff BASE --` into compact review context for an agent.

codeq reports Git-added, modified, deleted, and renamed files, then maps current
changed lines to enclosing semantic symbols and reports callers, references,
possible dynamic callback/registry references, affected source files, and likely
tests. Deleted files remain visible in the file list but cannot be semantically
analyzed against the current worktree.

Untracked files are included from Git's working-tree view and marked `U`; ignored
files remain excluded according to Git ignore rules.
""",
        epilog="""\
Examples:
  codeq review --base HEAD~1
  codeq review --base origin/main --merge-base
  codeq review --base master --merge-base --limit 15 --json

Use --merge-base for PR/feature-branch review: codeq resolves `git merge-base BASE
HEAD` and diffs that commit against the current worktree, preserving staged,
unstaged, and untracked worktree changes. Without --merge-base, BASE is compared
directly. `--limit` bounds detailed changed symbols, affected files, and likely
tests while file status/counts remain complete.
""",
    )
    review.add_argument(
        "--base",
        default="HEAD~1",
        metavar="REF",
        help="Requested Git base ref (default: HEAD~1).",
    )
    review.add_argument(
        "--merge-base",
        action="store_true",
        help="Resolve merge-base(BASE, HEAD) before diffing; use for PR/feature-branch review.",
    )
    return parser


def _normalize_global_options(argv: list[str]) -> list[str]:
    """Allow global options before or after the subcommand.

    Coding agents naturally emit both `codeq --json find Foo` and
    `codeq find Foo --json`; argparse normally accepts only the former.
    """
    flags = {"--json", "--version"}
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
        payload.update(
            target=args.target,
            outline_depth=max(0, args.outline_depth),
            outline_kind=args.outline_kind,
            container=args.container,
            include_topology=args.topology,
        )
    elif args.command == "trace":
        payload.update(
            target=args.target,
            direction=args.direction,
            depth=args.depth,
            node_limit=max(1, args.node_limit),
        )
    elif args.command == "review":
        payload["base"] = args.base
        payload["merge_base"] = args.merge_base

    try:
        data = _request(payload, timeout=max(args.timeout + 5.0, 10.0))
    except Exception as exc:
        print(f"codeq: {exc}", file=sys.stderr)
        raise SystemExit(2)

    exit_code = _query_exit_code(data)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if exit_code:
            raise SystemExit(exit_code)
        return
    if args.command == "find":
        _render_find(data)
    elif args.command == "context":
        _render_context(data)
    elif args.command == "trace":
        _render_trace(data)
    elif args.command == "review":
        _render_review(data)
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
