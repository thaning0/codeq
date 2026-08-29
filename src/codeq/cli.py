from __future__ import annotations

import argparse
import errno
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
from .daemon import SocketEndpoint, default_socket_endpoint
from .util import git_root


_ARGPARSE_PARAMS = inspect.signature(argparse.ArgumentParser).parameters
_PERMANENT_SOCKET_ERRNOS = {errno.EACCES, errno.EPERM}


class DaemonUnavailableError(RuntimeError):
    """The daemon transport is unavailable in the current execution sandbox."""


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


def _connect(endpoint: SocketEndpoint, timeout: float) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(endpoint.address)
        if endpoint.is_abstract and not _peer_is_trusted(client):
            raise PermissionError("codeq abstract daemon peer is not trusted")
    except OSError:
        client.close()
        raise
    return client


def _spawn_daemon(endpoint: SocketEndpoint) -> None:
    if endpoint.is_abstract:
        argv = [sys.executable, "-m", "codeq.daemon", "--abstract", endpoint.value]
    else:
        socket_path = endpoint.path
        if socket_path is None:
            raise RuntimeError("filesystem daemon endpoint is missing a path")
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, "-m", "codeq.daemon", "--socket", str(socket_path)]
    log_path_text = os.environ.get("CODEQ_DAEMON_LOG")
    with open(os.devnull, "r+b", buffering=0) as devnull:
        if log_path_text:
            log_path = Path(log_path_text).expanduser()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            output = open(log_path, "ab", buffering=0)
        else:
            output = devnull
        try:
            os.posix_spawn(
                sys.executable,
                argv,
                os.environ.copy(),
                file_actions=[
                    (os.POSIX_SPAWN_DUP2, devnull.fileno(), 0),
                    (os.POSIX_SPAWN_DUP2, output.fileno(), 1),
                    (os.POSIX_SPAWN_DUP2, output.fileno(), 2),
                ],
                setpgroup=0,
            )
        finally:
            if output is not devnull:
                output.close()


def _connect_or_spawn(endpoint: SocketEndpoint, timeout: float) -> socket.socket:
    try:
        return _connect(endpoint, min(timeout, 1.0))
    except OSError as exc:
        if exc.errno in _PERMANENT_SOCKET_ERRNOS:
            raise DaemonUnavailableError(f"codeq daemon socket is unavailable: {exc}") from exc
        try:
            _spawn_daemon(endpoint)
        except OSError as spawn_error:
            if spawn_error.errno in _PERMANENT_SOCKET_ERRNOS:
                raise DaemonUnavailableError(
                    f"codeq daemon cannot be started in this sandbox: {spawn_error}"
                ) from spawn_error
            raise
        deadline = time.monotonic() + min(max(timeout, 3.0), 10.0)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            time.sleep(0.05)
            try:
                return _connect(endpoint, min(timeout, 1.0))
            except OSError as exc:
                if exc.errno in _PERMANENT_SOCKET_ERRNOS:
                    raise DaemonUnavailableError(
                        f"codeq daemon socket is unavailable: {exc}"
                    ) from exc
                last_error = exc
        raise RuntimeError(f"codeq daemon failed to start: {last_error}")


def _peer_credentials(client: socket.socket) -> tuple[int, int, int] | None:
    peercred = getattr(socket, "SO_PEERCRED", None)
    if peercred is None:
        return None
    try:
        raw = client.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3i"))
        return struct.unpack("3i", raw)
    except (OSError, struct.error):
        return None


def _peer_is_trusted(client: socket.socket) -> bool:
    credentials = _peer_credentials(client)
    return credentials is not None and credentials[1] == os.getuid()


def _peer_pid(client: socket.socket) -> int | None:
    credentials = _peer_credentials(client)
    if credentials is None:
        return None
    pid, uid, _ = credentials
    if uid != os.getuid() or pid <= 1:
        return None
    return pid


def _request_daemon_shutdown(endpoint: SocketEndpoint) -> bool:
    try:
        client = _connect(endpoint, 0.5)
    except OSError:
        return True
    try:
        client.settimeout(1.0)
        with client:
            with client.makefile("rwb") as file:
                file.write((json.dumps({"command": "_shutdown"}) + "\n").encode("utf-8"))
                file.flush()
                line = file.readline()
        if not line:
            return False
        response = json.loads(line)
        return bool(response.get("ok"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _restart_stale_daemon(pid: int | None, endpoint: SocketEndpoint) -> None:
    shutdown_sent = _request_daemon_shutdown(endpoint)
    if not shutdown_sent:
        if pid is None:
            raise RuntimeError("codeq daemon version mismatch; stale daemon cannot be stopped")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            probe = _connect(endpoint, 0.1)
        except OSError:
            path = endpoint.path
            if path is not None:
                path.unlink(missing_ok=True)
            return
        else:
            probe.close()
        time.sleep(0.05)
    raise RuntimeError("stale codeq daemon did not exit after version mismatch")


def _request_in_process(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Execute one request without a daemon when the sandbox forbids Unix sockets."""
    from .service import CodeqService

    service = CodeqService(max_workspaces=1)
    try:
        in_process_payload = dict(payload)
        in_process_payload.setdefault("timeout", timeout)
        data = service.handle(in_process_payload)
        meta = data.get("_meta")
        if isinstance(meta, dict):
            meta["transport"] = "in_process"
        return data
    finally:
        service.close()


def _request(payload: dict[str, Any], timeout: float, *, _allow_restart: bool = True) -> dict[str, Any]:
    endpoint = default_socket_endpoint()
    try:
        client = _connect_or_spawn(endpoint, timeout)
    except DaemonUnavailableError:
        return _request_in_process(payload, timeout)
    peer_pid = _peer_pid(client)
    wire_payload = {
        **payload,
        "_client_version": __version__,
        "_protocol_version": DAEMON_PROTOCOL_VERSION,
    }
    client.settimeout(timeout)
    with client:
        with client.makefile("rwb") as file:
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
        _restart_stale_daemon(peer_pid, endpoint)
        return _request(payload, timeout, _allow_restart=False)
    if version_mismatch:
        raise RuntimeError("codeq daemon version mismatch after restart")
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "unknown daemon error")
    return response["data"]


def _display_path(path: object, root: str | Path) -> str:
    """Render paths under the repository relative to its root for plain output."""
    text = str(path or "")
    candidate = Path(text)
    if not candidate.is_absolute():
        return text
    try:
        return candidate.relative_to(Path(root)).as_posix()
    except ValueError:
        return text


def _display_location(item: dict[str, Any], root: str | Path) -> str:
    return (
        f"{_display_path(item['path'], root)}:{item['line']}:{item.get('column', 1)}"
    )


def _display_message(message: object, root: str | Path) -> str:
    """Shorten repository path prefixes embedded in plain diagnostic messages."""
    text = str(message or "")
    prefix = str(Path(root)) + os.sep
    return text.replace(prefix, "")


def _print_locations(
    title: str,
    items: list[dict[str, Any]],
    root: str | Path,
    indent: str = "  ",
) -> None:
    print(f"{title} ({len(items)})")
    if not items:
        print(f"{indent}-")
        return
    for item in items:
        name = item.get("name")
        prefix = f"{name}  " if name else ""
        print(f"{indent}{prefix}{_display_location(item, root)}")


def _query_exit_code(data: dict[str, Any]) -> int:
    status = data.get("status")
    return 0 if status in (None, "ok") else 1


def _render_find(data: dict[str, Any], root: str | Path) -> None:
    if data.get("status") and data.get("status") != "ok":
        if data.get("status") == "ambiguous":
            _render_resolution(data, root)
            return
        print(
            _display_message(data.get("reason") or data.get("error") or "find failed", root),
            file=sys.stderr,
        )
        return
    if data.get("mode") == "text":
        for item in data.get("results", []):
            markers: list[str] = []
            if not item.get("tracked", True):
                markers.append("untracked")
            if item.get("is_test"):
                markers.append("test")
            marker = f" [{' '.join(markers)}]" if markers else ""
            occurrences = int(item.get("occurrences") or 1)
            repeat = f" x{occurrences}" if occurrences > 1 else ""
            print(
                f"{_display_location(item, root)}{marker}{repeat}  "
                f"{str(item.get('text') or '').strip()}"
            )
        if not data.get("results"):
            print("No matches.")
        if data.get("truncated"):
            print("... more matching lines available; increase --limit")
        meta = data.get("_meta", {})
        print(
            f"\n[{data.get('match_count',0)} exact matches across "
            f"{data.get('matching_line_count',0)} lines / {data.get('matching_file_count',0)} files; "
            f"tracked={data.get('tracked_line_count',0)} untracked={data.get('untracked_line_count',0)} "
            f"tests={data.get('test_line_count',0)}; showing {data.get('returned_line_count',0)} lines; "
            f"{meta.get('duration_ms','?')} ms]"
        )
        return
    for item in data.get("results", []):
        container = f"{item.get('container')}." if item.get("container") else ""
        print(
            f"{item.get('kind','?'):<12} {container}{item.get('name','')}  "
            f"{_display_location(item, root)}"
            f"  [{item.get('source','?')}]"
        )
    if not data.get("results"):
        print("No matches.")
    result_count = int(data.get("result_count") or 0)
    total_candidates = int(data.get("total_candidates", result_count) or 0)
    truncated = bool(data.get("truncated", result_count < total_candidates))
    if truncated:
        print("... more semantic candidates available; increase --limit")
    meta = data.get("_meta", {})
    print(
        f"\n[showing {result_count} of {total_candidates} candidates; "
        f"{meta.get('duration_ms','?')} ms]"
    )


def _render_resolution(data: dict[str, Any], root: str | Path) -> bool:
    status = data.get("status")
    if status == "ok":
        return True
    if status == "ambiguous":
        print(
            f"Ambiguous target: {_display_message(data.get('target'), root)}",
            file=sys.stderr,
        )
        for item in data.get("candidates", []):
            container = f"{item.get('container')}." if item.get("container") else ""
            print(
                f"  {item.get('kind')} {container}{item.get('name')}  "
                f"{_display_location(item, root)}",
                file=sys.stderr,
            )
            if item.get("selection_command"):
                print(f"    try: {item['selection_command']}", file=sys.stderr)
        return False
    print(
        _display_message(
            data.get("reason") or data.get("error") or f"Target not found: {data.get('target')}",
            root,
        ),
        file=sys.stderr,
    )
    if data.get("recovery_command"):
        print(f"  try: {data['recovery_command']}", file=sys.stderr)
    candidates = data.get("candidates") or []
    if candidates:
        print("Possible exact-name matches:", file=sys.stderr)
        for item in candidates:
            container = f"{item.get('container')}." if item.get("container") else ""
            print(
                f"  {item.get('kind')} {container}{item.get('name')}  "
                f"{_display_location(item, root)}",
                file=sys.stderr,
            )
            if item.get("selection_command"):
                print(f"    try: {item['selection_command']}", file=sys.stderr)
    return False


def _print_text_search(
    title: str,
    data: dict[str, Any] | None,
    root: str | Path,
    indent: str = "  ",
) -> None:
    if not data:
        return
    query = str(data.get("query") or "")
    print(
        f"{title} ({data.get('match_count', 0)} matches across "
        f"{data.get('matching_line_count', 0)} lines)  {query!r}"
    )
    results = data.get("results", [])
    if not results:
        print(f"{indent}-")
    for item in results:
        markers: list[str] = []
        if not item.get("tracked", True):
            markers.append("untracked")
        if item.get("is_test"):
            markers.append("test")
        marker = f" [{' '.join(markers)}]" if markers else ""
        occurrences = int(item.get("occurrences") or 1)
        repeat = f" x{occurrences}" if occurrences > 1 else ""
        text = str(item.get("text") or "").strip()
        print(
            f"{indent}{_display_location(item, root)}"
            f"{marker}{repeat}  {text}"
        )
    if data.get("truncated"):
        print(f"{indent}... more matching lines available; increase --limit")


def _print_dynamic_references(
    items: list[dict[str, Any]],
    root: str | Path,
    indent: str = "  ",
) -> None:
    print(f"Possible dynamic references ({len(items)})")
    if not items:
        print(f"{indent}-")
        return
    for item in items:
        reason = item.get("reason") or "possible"
        text = str(item.get("text") or "").strip()
        suffix = f"  {text}" if text else ""
        print(f"{indent}[{reason}] {_display_location(item, root)}{suffix}")


def _render_file_context(data: dict[str, Any], root: str | Path) -> None:
    file_info = data.get("file") or {}
    print(f"File {_display_path(file_info.get('path', data.get('target', '')), root)}")
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
        if data.get("lexical_references"):
            print()
            _print_text_search("Lexical references", data.get("lexical_references"), root)
        meta = data.get("_meta", {})
        print(f"\n[{meta.get('duration_ms','?')} ms]")
        return

    imports = data.get("imports", [])
    print(f"\nImports (showing {len(imports)} of {data.get('import_count', len(imports))})")
    if not imports:
        print("  -")
    for item in imports:
        names = ", ".join(str(name) for name in item.get("names", []))
        suffix = f" [{names}]" if names else ""
        resolved = item.get("resolved_paths", [])
        resolved_text = (
            f" -> {', '.join(_display_path(path, root) for path in resolved)}"
            if resolved
            else ""
        )
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
        print(f"  {_display_location(item, root)}{suffix}")

    if data.get("lexical_references"):
        print()
        _print_text_search("Lexical references", data.get("lexical_references"), root)

    meta = data.get("_meta", {})
    print(f"\n[{meta.get('duration_ms','?')} ms]")


def _render_context(data: dict[str, Any], root: str | Path) -> None:
    if not _render_resolution(data, root):
        return
    if data.get("kind") == "file":
        _render_file_context(data, root)
        return
    s = data["symbol"]
    container = f"{s.get('container')}." if s.get("container") else ""
    print(f"{s.get('kind','?')} {container}{s.get('name','')}")
    print(_display_location(s, root))
    requested = data.get("requested_location")
    if isinstance(requested, dict):
        mode = " -> cursor definition" if data.get("cursor_definition") else ""
        print(f"Requested at: {_display_location(requested, root)}{mode}")
    if data.get("definition_note"):
        print(f"Definition note: {_display_message(data['definition_note'], root)}")
    if data.get("hover"):
        print("\nHover")
        print(data["hover"].strip())
    request_snippet = data.get("request_source", {}).get("text")
    if request_snippet:
        print("\nRequest source")
        print(request_snippet)
    snippet = data.get("source", {}).get("text")
    if snippet:
        print("\nDefinition source" if request_snippet else "\nSource")
        print(snippet)
    print()
    _print_locations("Callers", data.get("callers", []), root)
    _print_locations("Callees", data.get("callees", []), root)
    _print_locations("Implementations", data.get("implementations", []), root)
    _print_locations("Tests", data.get("tests", []), root)
    _print_locations("References", data.get("references", []), root)
    _print_dynamic_references(data.get("possible_dynamic_references", []), root)
    if data.get("lexical_references"):
        print()
        _print_text_search("Lexical references", data.get("lexical_references"), root)
    meta = data.get("_meta", {})
    print(f"\n[{meta.get('duration_ms','?')} ms]")


def _render_trace(data: dict[str, Any], root: str | Path) -> None:
    if not _render_resolution(data, root):
        return

    def visit(
        node: dict[str, Any],
        prefix: str = "",
        last: bool = True,
        is_root: bool = False,
    ) -> None:
        item = node["node"]
        connector = "" if is_root else ("└─ " if last else "├─ ")
        cycle = " [cycle]" if node.get("cycle") else ""
        print(
            f"{prefix}{connector}{item.get('name','?')}  "
            f"{_display_path(item['path'], root)}:{item['line']}{cycle}"
        )
        children = node.get("children", [])
        next_prefix = prefix + ("" if is_root else ("   " if last else "│  "))
        for index, child in enumerate(children):
            visit(child, next_prefix, index == len(children) - 1, False)

    visit(data["tree"], is_root=True)
    if data.get("note"):
        print(f"\nNote: {_display_message(data['note'], root)}")
    meta = data.get("_meta", {})
    print(
        f"\n[{data.get('node_count',1)} nodes; depth={data.get('depth')}; {meta.get('duration_ms','?')} ms]"
    )


def _render_review(data: dict[str, Any], root: str | Path) -> None:
    print(f"Base: {data.get('requested_base', data.get('base'))}")
    print(f"Base mode: {data.get('base_mode', 'direct')}")
    if data.get("resolved_base"):
        print(f"Resolved base: {data['resolved_base']}")
    print(f"Changed files: {data.get('changed_file_count', 0)}")
    file_changes = data.get("file_changes", [])
    if file_changes:
        for change in file_changes:
            status = str(change.get("status") or "?")
            path = _display_path(change.get("path"), root)
            old_path = change.get("old_path")
            if status in {"R", "C"} and old_path:
                print(f"  {status} {_display_path(old_path, root)} -> {path}")
            else:
                print(f"  {status} {path}")
            if change.get("semantic_status") in {"deleted_base_analyzed", "deleted_base_unavailable"}:
                analysis = change.get("base_analysis") or {}
                print(
                    f"    base-side impact: {analysis.get('status', 'unavailable')} "
                    f"({analysis.get('base_symbol_count', 0)} symbols; lexical evidence)"
                )
                for item in analysis.get("base_symbols", [])[:5]:
                    symbol = item.get("symbol") or {}
                    print(
                        f"      {symbol.get('kind','?')} {symbol.get('name','?')}  "
                        f"residual={item.get('residual_match_count',0)} "
                        f"tests={len(item.get('tests', []))}"
                    )
            if change.get("semantic_status") == "rename_analyzed":
                analysis = change.get("rename_analysis") or {}
                print(
                    f"    rename impact: importers={analysis.get('importer_count', 0)} "
                    f"symbols={len(analysis.get('symbols', []))} (current semantic)"
                )
                for item in analysis.get("symbols", [])[:5]:
                    symbol = item.get("symbol") or {}
                    print(
                        f"      {symbol.get('kind','?')} {symbol.get('name','?')}  "
                        f"references={item.get('reference_count',0)} tests={len(item.get('tests', []))}"
                    )
    else:
        for path in data.get("changed_files", []):
            print(f"  {_display_path(path, root)}")
    print(f"\nChanged symbols: {data.get('changed_symbol_count', 0)}")
    for detail in data.get("changed_symbols", []):
        symbol = detail["symbol"]
        container = f"{symbol.get('container')}." if symbol.get("container") else ""
        print(
            f"  {symbol.get('kind')} {container}{symbol.get('name')}  "
            f"{_display_path(symbol['path'], root)}:{symbol['line']}"
        )
        for caller in detail.get("callers", [])[:5]:
            print(
                f"    <- {caller.get('name')}  "
                f"{_display_path(caller['path'], root)}:{caller['line']}"
            )
        for dynamic in detail.get("possible_dynamic_references", [])[:5]:
            print(
                f"    ? {dynamic.get('reason','possible')}  "
                f"{_display_path(dynamic['path'], root)}:{dynamic['line']}"
            )
        for test in detail.get("tests", [])[:5]:
            print(f"    test {_display_path(test['path'], root)}:{test['line']}")
    print(f"\nAffected files: {data.get('impacted_file_count', 0)}")
    for path in data.get("impacted_files", []):
        print(f"  {_display_path(path, root)}")
    if data.get("impacted_files_truncated"):
        print("  ... more affected files available; increase --limit")
    print(f"\nPossible dynamic references: {data.get('possible_dynamic_reference_count', 0)}")
    print(f"\nLikely tests: {data.get('test_count', 0)}")
    for test in data.get("tests", []):
        print(
            f"  {test.get('name','')}  {_display_path(test['path'], root)}:{test['line']}"
        )
    if data.get("tests_truncated"):
        print("  ... more likely tests available; increase --limit")
    if data.get("truncated"):
        print("\nResult truncated by --limit.")
    meta = data.get("_meta", {})
    print(f"\n[{meta.get('duration_ms','?')} ms]")


def build_parser() -> argparse.ArgumentParser:
    parser = PlainArgumentParser(
        prog="codeq",
        description="""\
Small, read-only code-intelligence CLI for coding agents.

Use codeq before broad manual exploration when you need to locate code, understand
one symbol, follow a call chain, or inspect the semantic impact of a Git diff.
Choose a command:
  find     You do not yet know the exact symbol/location (`search` is an alias).
  context  You know a symbol or file:line and need its local neighborhood.
  trace    You need a multi-hop caller/callee chain.
  review   You need changed symbols, impact, dynamic references, and likely tests.
""",
        epilog="""\
Common agent loop:
  codeq find 'backtest log streaming' --limit 8
  codeq context BacktestService.stream_backtest_logs
  codeq trace BacktestService.stream_backtest_logs --in --depth 2
  codeq review --base HEAD~1

Targets accepted by context/trace:
  Qualified symbol       BacktestService.stream_backtest_logs
  Bare symbol            fetchBars
  Python module (context) auto_research_core.domain.models
  Source location        backend/src/app/services/backtest_service.py:684
  Source location+column backend/src/app/services/backtest_service.py:684:9
  Source file (context)  backend/src/app/services/backtest_service.py
  Unique basename (context) backtest_service.py

Agent notes:
  * Prefer qualified symbols when known; qualified resolution is fail-closed.
  * Paths containing a separator are exact; missing files fail closed with status not_found.
  * A bare source basename resolves only when exactly one Git-visible file matches.
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
    parser.add_argument(
        "--no-daemon",
        action="store_true",
        help="Run this query in-process; also selected automatically when sandbox socket access is denied.",
    )
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=PlainArgumentParser,
        metavar="COMMAND",
    )

    find = sub.add_parser(
        "find",
        aliases=["search"],
        help="Find symbols or related source from a name or short description.",
        description="""\
Find likely code locations before you know an exact target.

By default, `QUERY` may be an exact symbol name, part of a qualified name, or a short
natural-language description. For concept searches, use vocabulary likely to occur
in the repository's source/comments (usually the source language); codeq does not
translate queries between natural languages.

`--path`, `--glob`, and `--exclude-tests` scope the candidate files in either mode.
Repeat path and glob filters for OR matching within each filter type.

With `--text`, QUERY is an exact literal searched across Git-visible working-tree
text: tracked files plus untracked files that are not ignored. Text mode is
intentionally non-semantic and supports optional path/glob/test filtering.
""",
        epilog="""\
Examples:
  codeq find BacktestService
  codeq find BacktestService --kind class
  codeq find Candidate --kind class --path packages/research-core
  codeq find 'architecture guard' --path packages --glob '*.py' --exclude-tests
  codeq find 'report summary freshness policy evidence' --limit 8
  codeq find --text 'BACKTEST_QUESTDB_QUERY_TARGET_ROWS' --limit 20
  codeq find --text '/logs/stream' --path frontend --exclude-tests
  codeq find --text 'DEPLOYMENTS' --glob '*.py' --glob '*.yaml'
  codeq find fetchBars --root ~/Quant --json

Typical next step:
  Take the best definition from `find`, then run `codeq context TARGET`.
""",
    )
    find.set_defaults(command="find")
    find.add_argument(
        "query",
        metavar="QUERY",
        help="Symbol name, qualified-name fragment, or short source-code description.",
    )
    find_mode = find.add_mutually_exclusive_group()
    find_mode.add_argument(
        "--kind",
        metavar="KIND",
        help="Optional semantic result filter, e.g. function, method, class, interface, test.",
    )
    find_mode.add_argument(
        "--text",
        action="store_true",
        help="Exact literal search across tracked + non-ignored untracked text files.",
    )
    find.add_argument(
        "--path",
        dest="paths",
        action="append",
        default=[],
        metavar="PREFIX",
        help="Repository-relative path prefix for semantic or text results; repeat for OR matching.",
    )
    find.add_argument(
        "--glob",
        dest="globs",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Shell-style path glob for semantic or text results; repeat for OR matching.",
    )
    find.add_argument(
        "--exclude-tests",
        dest="exclude_tests",
        action="store_true",
        help="Exclude test paths from semantic or text results and counts.",
    )

    context = sub.add_parser(
        "context",
        help="Get one target's definition, callers, callees, references, and tests.",
        description="""\
Return the local semantic neighborhood of one target in a single query.

For a symbol or source-position target, context returns:
  Definition/location and signature/hover
  Bounded definition source snippet
  Callers and callees
  Implementations and references
  Likely tests
  Possible dynamic callback/registry references when detected

For a source-file target, context uses progressive disclosure: top-level outline by
default. Expand only what you need with --outline-depth, --kind, or --container;
add --topology only when you need imports/importers.

PATH:LINE keeps the enclosing function/method/type. PATH:LINE:COLUMN first follows
the exact repository definition under the cursor when available and also returns a
small request-site snippet; it falls back to enclosing context when no definition is
available. Paths containing a separator are exact: missing files return not_found,
and unsupported languages return unsupported_language rather than falling back to
symbol search. A bare source basename or dotted Python module resolves as a file
only when its Git-visible match is unique; ambiguous matches return exact commands.

For a symbolic target, `--path` restricts semantic resolution to repository-relative
path prefixes. With `--lexical-references`, `--path` keeps its existing meaning and
scopes the attached exact-text search; use `--symbol-path` to scope the symbol too.
""",
        epilog="""\
Examples:
  codeq context BacktestService.stream_backtest_logs
  codeq context validate_discovery_plan --path packages/research-core/src
  codeq context auto_research_core.domain.models.Candidate
  codeq context auto_research_core.application.research_governance
  codeq context research_projection.py
  codeq context backend/src/app/services/backtest_service.py:684
  codeq context backend/src/app/services/backtest_service.py
  codeq context backend/src/app/services/backtest_service.py --container BacktestService
  codeq context backend/src/app/services/backtest_service.py --kind method --limit 20
  codeq context frontend/src/features/market/api.ts --topology --limit 20
  codeq context BacktestService.stream_backtest_logs --lexical-references
  codeq context BacktestService.stream_backtest_logs --lexical-references '/logs/stream'
  codeq context BacktestService.stream_backtest_logs --lexical-references '/logs/stream' --path frontend --exclude-tests
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
    context.add_argument(
        "--lexical-references",
        nargs="?",
        const="",
        default=None,
        metavar="TEXT",
        help=(
            "Also run exact tracked + non-ignored untracked text search. Without TEXT, "
            "search the resolved symbol/file name; with TEXT, search that exact contract string."
        ),
    )
    context.add_argument(
        "--path",
        dest="paths",
        action="append",
        default=[],
        metavar="PREFIX",
        help="Symbol path prefix, or lexical path prefix with --lexical-references; repeat for OR matching.",
    )
    context.add_argument(
        "--symbol-path",
        dest="semantic_paths",
        action="append",
        default=[],
        metavar="PREFIX",
        help="Always restrict symbolic target resolution; useful together with --lexical-references.",
    )
    context.add_argument(
        "--glob",
        dest="lexical_globs",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Lexical-reference mode only: shell-style path glob; repeat for OR matching.",
    )
    context.add_argument(
        "--exclude-tests",
        dest="lexical_exclude_tests",
        action="store_true",
        help="Lexical-reference mode only: exclude test paths from results and counts.",
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
  codeq trace fetchBars --in --depth 2 --limit 50 --json

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
        help="Backward-compatible trace-specific alias for --limit (default: 100).",
    )

    review = sub.add_parser(
        "review",
        help="Summarize semantic impact of changes relative to a Git base ref.",
        description="""\
Turn `git diff BASE --` into compact review context for an agent.

codeq reports Git-added, modified, deleted, and renamed files, then maps current
changed lines to enclosing semantic symbols and reports callers, references,
possible dynamic callback/registry references, affected source files, and likely
tests. Deleted files get conservative base-side declaration + exact-text residual
reference analysis; pure renames get current-path importer/reference analysis.

Untracked files are included from Git's working-tree view and marked `U`; ignored
files remain excluded according to Git ignore rules.
""",
        epilog="""\
Examples:
  codeq review --base HEAD~1
  codeq review --base origin/HEAD --merge-base
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
    flags = {"--json", "--version", "--no-daemon"}
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


def _option_was_supplied(argv: list[str], option: str) -> bool:
    """Return whether an option appeared explicitly, including --option=value."""
    return any(arg == option or arg.startswith(option + "=") for arg in argv)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_normalize_global_options(raw_argv))
    limit_was_supplied = _option_was_supplied(raw_argv, "--limit")
    node_limit_was_supplied = _option_was_supplied(raw_argv, "--node-limit")
    if args.command == "context":
        if (args.lexical_globs or args.lexical_exclude_tests) and args.lexical_references is None:
            parser.error("--glob/--exclude-tests require --lexical-references; --path also scopes symbol resolution")
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
        payload["text"] = args.text
        payload["paths"] = args.paths
        payload["globs"] = args.globs
        payload["exclude_tests"] = args.exclude_tests
    elif args.command == "context":
        lexical_mode = args.lexical_references is not None
        semantic_paths = [*args.semantic_paths, *([] if lexical_mode else args.paths)]
        payload.update(
            target=args.target,
            outline_depth=max(0, args.outline_depth),
            outline_kind=args.outline_kind,
            container=args.container,
            include_topology=args.topology,
            lexical_references=args.lexical_references is not None,
            lexical_query=args.lexical_references or None,
            lexical_paths=args.paths if lexical_mode else [],
            lexical_globs=args.lexical_globs,
            lexical_exclude_tests=args.lexical_exclude_tests,
            semantic_paths=semantic_paths,
        )
    elif args.command == "trace":
        if limit_was_supplied and node_limit_was_supplied and args.limit != args.node_limit:
            parser.error(
                "conflicting trace limits: --limit and --node-limit must have the same value when both are supplied"
            )
        trace_node_limit = (
            args.node_limit
            if node_limit_was_supplied
            else args.limit
            if limit_was_supplied
            else args.node_limit
        )
        payload.update(
            target=args.target,
            direction=args.direction,
            depth=args.depth,
            node_limit=max(1, trace_node_limit),
        )
    elif args.command == "review":
        payload["base"] = args.base
        payload["merge_base"] = args.merge_base

    try:
        # A semantic find may spend up to one LSP timeout queued behind the
        # workspace's cold-start owner, then need another timeout to execute.
        request_timeout = max(args.timeout * 2.0 + 5.0, 10.0)
        data = (
            _request_in_process(payload, timeout=request_timeout)
            if args.no_daemon
            else _request(payload, timeout=request_timeout)
        )
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
        _render_find(data, root)
    elif args.command == "context":
        _render_context(data, root)
    elif args.command == "trace":
        _render_trace(data, root)
    elif args.command == "review":
        _render_review(data, root)
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
