from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import threading
from pathlib import Path
from typing import Any

from . import DAEMON_PROTOCOL_VERSION, __version__
from .service import CodeqService

_WORKSPACE_IDLE_SECONDS = float(os.environ.get("CODEQ_WORKSPACE_IDLE_SECONDS", "300"))
_DAEMON_IDLE_SECONDS = float(os.environ.get("CODEQ_DAEMON_IDLE_SECONDS", "900"))
_MAX_WORKSPACES = int(os.environ.get("CODEQ_MAX_WORKSPACES", "4"))
_MAINTENANCE_INTERVAL_SECONDS = 5.0


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path("/tmp") / f"codeq-{os.getuid()}"
    base.mkdir(parents=True, exist_ok=True)
    return base / "codeq.sock"


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
