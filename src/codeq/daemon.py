from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import signal
import socket
import stat
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import DAEMON_PROTOCOL_VERSION, __version__
from .service import CodeqService

_WORKSPACE_IDLE_SECONDS = float(os.environ.get("CODEQ_WORKSPACE_IDLE_SECONDS", "300"))
_LSP_IDLE_SECONDS = float(os.environ.get("CODEQ_LSP_IDLE_SECONDS", "1800"))
_DAEMON_IDLE_SECONDS = float(os.environ.get("CODEQ_DAEMON_IDLE_SECONDS", "900"))
_MAX_WORKSPACES = int(os.environ.get("CODEQ_MAX_WORKSPACES", "4"))
_MAINTENANCE_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class SocketEndpoint:
    kind: str
    value: str

    @classmethod
    def abstract(cls, name: str) -> "SocketEndpoint":
        if not name or "\x00" in name:
            raise ValueError("abstract socket name must be non-empty and contain no NUL")
        if len(name.encode("utf-8")) + 1 > 108:
            raise ValueError("abstract socket name is too long")
        return cls("abstract", name)

    @classmethod
    def filesystem(cls, path: Path | str) -> "SocketEndpoint":
        return cls("filesystem", str(Path(path).expanduser()))

    @property
    def address(self) -> str:
        if self.kind == "abstract":
            return f"\x00{self.value}"
        return self.value

    @property
    def path(self) -> Path | None:
        return Path(self.value) if self.kind == "filesystem" else None

    @property
    def is_abstract(self) -> bool:
        return self.kind == "abstract"


def _supports_abstract_socket() -> bool:
    return sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED")


def _prepare_runtime_dir(path: Path) -> Path:
    path = path.expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if info.st_uid != os.getuid():
        raise PermissionError(f"runtime directory is not owned by current user: {path}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)

    socket_path = path / "codeq.sock"
    if socket_path.exists():
        return path

    probe_path = path / f".socket-probe-{os.getpid()}-{secrets.token_hex(4)}"
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.bind(str(probe_path))
    finally:
        probe.close()
        probe_path.unlink(missing_ok=True)
    return path


def default_runtime_dir() -> Path:
    explicit = os.environ.get("CODEQ_RUNTIME_DIR")
    if explicit:
        try:
            return _prepare_runtime_dir(Path(explicit))
        except OSError as exc:
            raise RuntimeError(f"CODEQ_RUNTIME_DIR is not usable: {explicit}: {exc}") from exc

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        try:
            return _prepare_runtime_dir(Path(runtime) / "codeq")
        except OSError:
            pass

    fallback = Path("/tmp") / f"codeq-{os.getuid()}"
    try:
        return _prepare_runtime_dir(fallback)
    except OSError as exc:
        raise RuntimeError(f"no usable codeq runtime directory: {fallback}: {exc}") from exc


def default_socket_endpoint() -> SocketEndpoint:
    explicit = os.environ.get("CODEQ_RUNTIME_DIR")
    if explicit:
        return SocketEndpoint.filesystem(default_runtime_dir() / "codeq.sock")
    if _supports_abstract_socket():
        return SocketEndpoint.abstract(f"codeq-{os.getuid()}-p{DAEMON_PROTOCOL_VERSION}")
    return SocketEndpoint.filesystem(default_runtime_dir() / "codeq.sock")


def _peer_credentials(conn: socket.socket) -> tuple[int, int, int] | None:
    peercred = getattr(socket, "SO_PEERCRED", None)
    if peercred is None:
        return None
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
    except (OSError, struct.error):
        return None
    return pid, uid, gid


def _pid_is_live(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        text = stat_path.read_text(encoding="utf-8")
    except OSError:
        return True
    close = text.rfind(")")
    if close >= 0:
        fields = text[close + 1 :].strip().split()
        if fields and fields[0] == "Z":
            return False
    return True


def _trusted_peer(conn: socket.socket, *, require_credentials: bool) -> bool:
    credentials = _peer_credentials(conn)
    if credentials is None:
        return not require_credentials
    pid, uid, _ = credentials
    if uid != os.getuid():
        return False
    if pid <= 0:
        return True
    return _pid_is_live(pid)


def _serve_connection(
    conn: socket.socket,
    service: CodeqService,
    request_stop: Callable[[], None],
) -> None:
    with conn:
        file = conn.makefile("rwb")
        line = file.readline()
        if not line:
            return
        shutdown_requested = False
        try:
            request = json.loads(line)
            if request.get("command") == "_shutdown":
                shutdown_requested = True
                response: dict[str, Any] = {"ok": True, "data": {"status": "ok"}}
            else:
                client_version = request.get("_client_version")
                client_protocol = request.get("_protocol_version")
                if client_version != __version__ or client_protocol != DAEMON_PROTOCOL_VERSION:
                    response = {
                        "ok": False,
                        "error_code": "version_mismatch",
                        "error": "codeq client/daemon version mismatch",
                    }
                else:
                    data = service.handle(request)
                    response = {"ok": True, "data": data}
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        response["server_version"] = __version__
        response["protocol_version"] = DAEMON_PROTOCOL_VERSION
        file.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        file.flush()
        if shutdown_requested:
            request_stop()


def run(endpoint: SocketEndpoint) -> int:
    if endpoint.is_abstract:
        runtime_dir = default_runtime_dir()
    else:
        path = endpoint.path
        if path is None:
            raise ValueError("filesystem endpoint requires a path")
        runtime_dir = _prepare_runtime_dir(path.parent)
    os.environ["CODEQ_EFFECTIVE_RUNTIME_DIR"] = str(runtime_dir.resolve())

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if endpoint.is_abstract:
        try:
            server.bind(endpoint.address)
        except OSError as exc:
            server.close()
            if exc.errno == errno.EADDRINUSE:
                return 0
            raise
    else:
        path = endpoint.path
        assert path is not None
        if path.exists():
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.2)
                probe.connect(str(path))
                probe.close()
                server.close()
                return 0
            except OSError:
                path.unlink(missing_ok=True)
        server.bind(str(path))
        os.chmod(path, 0o600)

    service = CodeqService(max_workspaces=_MAX_WORKSPACES)
    server.listen(32)
    server.settimeout(_MAINTENANCE_INTERVAL_SECONDS)
    stopping = threading.Event()

    def stop(*_: Any) -> None:
        stopping.set()
        try:
            server.close()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                service.evict_idle(_WORKSPACE_IDLE_SECONDS, lsp_idle_seconds=_LSP_IDLE_SECONDS)
                if service.workspace_count() == 0 and service.idle_seconds() >= _DAEMON_IDLE_SECONDS:
                    break
                continue
            except OSError:
                break
            if not _trusted_peer(conn, require_credentials=endpoint.is_abstract):
                conn.close()
                continue
            threading.Thread(target=_serve_connection, args=(conn, service, stop), daemon=True).start()
    finally:
        service.close()
        server.close()
        path = endpoint.path
        if path is not None:
            path.unlink(missing_ok=True)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--abstract")
    group.add_argument("--socket")
    args = parser.parse_args()
    if args.abstract:
        endpoint = SocketEndpoint.abstract(args.abstract)
    elif args.socket:
        endpoint = SocketEndpoint.filesystem(args.socket)
    else:
        endpoint = default_socket_endpoint()
    raise SystemExit(run(endpoint))


if __name__ == "__main__":
    main()
