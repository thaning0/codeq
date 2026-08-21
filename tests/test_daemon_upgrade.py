from __future__ import annotations

import json
import multiprocessing
import os
import signal
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codeq.cli import _connect, _peer_pid, _request, _restart_stale_daemon
from codeq.daemon import SocketEndpoint


def _serve_stale_daemon(socket_path_text: str) -> None:
    socket_path = Path(socket_path_text)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    def stop(*_: object) -> None:
        server.close()
        socket_path.unlink(missing_ok=True)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    server.bind(str(socket_path))
    server.listen(1)
    conn, _ = server.accept()
    with conn:
        file = conn.makefile("rwb")
        if file.readline():
            file.write(
                (
                    json.dumps(
                        {
                            "ok": False,
                            "error_code": "version_mismatch",
                            "error": "stale daemon",
                            "server_version": "0.1.0",
                            "protocol_version": 1,
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
            file.flush()
    while True:
        signal.pause()


class DaemonUpgradeTests(unittest.TestCase):
    def test_client_restarts_stale_daemon_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "codeq.sock"
            endpoint = SocketEndpoint.filesystem(socket_path)
            stale = multiprocessing.Process(target=_serve_stale_daemon, args=(str(socket_path),))
            stale.start()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not socket_path.exists():
                time.sleep(0.02)
            self.assertTrue(socket_path.exists())

            try:
                with patch("codeq.cli.default_socket_endpoint", return_value=endpoint):
                    data = _request({"command": "_status"}, timeout=5.0)
                self.assertEqual(data["status"], "ok")
                stale.join(timeout=2.0)
                self.assertFalse(stale.is_alive())

                client = _connect(endpoint, 1.0)
                try:
                    current_pid = _peer_pid(client)
                finally:
                    client.close()
                self.assertIsNotNone(current_pid)
                self.assertNotEqual(current_pid, os.getpid())
                _restart_stale_daemon(current_pid, endpoint)
            finally:
                if stale.is_alive():
                    stale.terminate()
                    stale.join(timeout=2.0)
                if socket_path.exists():
                    try:
                        client = _connect(endpoint, 0.5)
                    except OSError:
                        socket_path.unlink(missing_ok=True)
                    else:
                        try:
                            pid = _peer_pid(client)
                        finally:
                            client.close()
                        if pid is not None:
                            try:
                                os.kill(pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass


if __name__ == "__main__":
    unittest.main()
