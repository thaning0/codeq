from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import threading
from pathlib import Path
from typing import Any

from .service import CodeqService


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
            data = service.handle(request)
            response: dict[str, Any] = {"ok": True, "data": data}
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
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

    service = CodeqService()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen(32)
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
