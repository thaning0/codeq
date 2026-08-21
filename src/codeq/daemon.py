from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import stat
import threading
from pathlib import Path
from typing import Any

from . import DAEMON_PROTOCOL_VERSION, __version__
from .service import CodeqService

_WORKSPACE_IDLE_SECONDS = float(os.environ.get("CODEQ_WORKSPACE_IDLE_SECONDS", "300"))
_DAEMON_IDLE_SECONDS = float(os.environ.get("CODEQ_DAEMON_IDLE_SECONDS", "900"))
_MAX_WORKSPACES = int(os.environ.get("CODEQ_MAX_WORKSPACES", "4"))
_MAINTENANCE_INTERVAL_SECONDS = 5.0


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


def default_socket_path() -> Path:
    explicit = os.environ.get("CODEQ_RUNTIME_DIR")
    if explicit:
        try:
            return _prepare_runtime_dir(Path(explicit)) / "codeq.sock"
        except OSError as exc:
            raise RuntimeError(f"CODEQ_RUNTIME_DIR is not usable: {explicit}: {exc}") from exc

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        try:
            return _prepare_runtime_dir(Path(runtime) / "codeq") / "codeq.sock"
        except OSError:
            pass

    fallback = Path("/tmp") / f"codeq-{os.getuid()}"
    try:
        return _prepare_runtime_dir(fallback) / "codeq.sock"
    except OSError as exc:
        raise RuntimeError(f"no usable codeq runtime directory: {fallback}: {exc}") from exc


def _serve_connection(conn: socket.socket, service: CodeqService) -> None:
    with conn:
        file = conn.makefile("rwb")
        line = file.readline()
        if not line:
            return
        try:
            request = json.loads(line)
            client_version = request.get("_client_version")
            client_protocol = request.get("_protocol_version")
            if client_version != __version__ or client_protocol != DAEMON_PROTOCOL_VERSION:
                response: dict[str, Any] = {
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


def run(socket_path: Path) -> int:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            probe.connect(str(socket_path))
            probe.close()
            return 0
        except OSError:
            socket_path.unlink(missing_ok=True)

    service = CodeqService(max_workspaces=_MAX_WORKSPACES)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
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
                service.evict_idle(_WORKSPACE_IDLE_SECONDS)
                if service.workspace_count() == 0 and service.idle_seconds() >= _DAEMON_IDLE_SECONDS:
                    break
                continue
            except OSError:
                break
            threading.Thread(target=_serve_connection, args=(conn, service), daemon=True).start()
    finally:
        service.close()
        server.close()
        socket_path.unlink(missing_ok=True)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default=str(default_socket_path()))
    args = parser.parse_args()
    raise SystemExit(run(Path(args.socket)))


if __name__ == "__main__":
    main()
