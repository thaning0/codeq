from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from codeq import DAEMON_PROTOCOL_VERSION
from codeq.cli import _peer_is_trusted, _restart_stale_daemon, _spawn_daemon
from codeq.daemon import SocketEndpoint, _serve_connection, _trusted_peer, default_socket_endpoint
from codeq.lsp import _lsp_environment


class RuntimeStateTests(unittest.TestCase):
    def test_linux_default_uses_abstract_socket_without_touching_runtime_dirs(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"CODEQ_RUNTIME_DIR": "", "XDG_RUNTIME_DIR": "/sandbox/read-only"},
                clear=False,
            ),
            patch("codeq.daemon._supports_abstract_socket", return_value=True),
            patch("codeq.daemon._prepare_runtime_dir") as prepare,
        ):
            endpoint = default_socket_endpoint()
        self.assertTrue(endpoint.is_abstract)
        self.assertEqual(endpoint.value, f"codeq-{os.getuid()}-p{DAEMON_PROTOCOL_VERSION}")
        self.assertEqual(endpoint.address, f"\x00codeq-{os.getuid()}-p{DAEMON_PROTOCOL_VERSION}")
        prepare.assert_not_called()

    def test_explicit_runtime_dir_forces_filesystem_socket_and_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            with patch.dict(os.environ, {"CODEQ_RUNTIME_DIR": str(runtime)}, clear=False):
                endpoint = default_socket_endpoint()
            self.assertFalse(endpoint.is_abstract)
            self.assertEqual(endpoint.path, runtime / "codeq.sock")
            self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)
            self.assertEqual(list(runtime.glob(".socket-probe-*")), [])

    def test_non_linux_fallback_uses_private_xdg_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xdg = Path(tmp) / "xdg"
            with (
                patch.dict(
                    os.environ,
                    {"XDG_RUNTIME_DIR": str(xdg), "CODEQ_RUNTIME_DIR": ""},
                    clear=False,
                ),
                patch("codeq.daemon._supports_abstract_socket", return_value=False),
            ):
                endpoint = default_socket_endpoint()
            self.assertEqual(endpoint.path, xdg / "codeq" / "codeq.sock")
            self.assertEqual(stat.S_IMODE((xdg / "codeq").stat().st_mode), 0o700)

    def test_non_linux_unusable_xdg_runtime_falls_back_to_tmp(self) -> None:
        xdg = Path("/unusable-xdg")
        uid = os.getuid()

        def prepare(path: Path) -> Path:
            if path == xdg / "codeq":
                raise PermissionError("read-only sandbox")
            self.assertEqual(path, Path("/tmp") / f"codeq-{uid}")
            return path

        with (
            patch.dict(
                os.environ,
                {"XDG_RUNTIME_DIR": str(xdg), "CODEQ_RUNTIME_DIR": ""},
                clear=False,
            ),
            patch("codeq.daemon._supports_abstract_socket", return_value=False),
            patch("codeq.daemon._prepare_runtime_dir", side_effect=prepare),
        ):
            endpoint = default_socket_endpoint()
        self.assertEqual(endpoint.path, Path("/tmp") / f"codeq-{uid}" / "codeq.sock")

    def test_explicit_runtime_dir_failure_is_not_silently_ignored(self) -> None:
        with (
            patch.dict(os.environ, {"CODEQ_RUNTIME_DIR": "/explicit/runtime"}, clear=False),
            patch("codeq.daemon._prepare_runtime_dir", side_effect=PermissionError("read-only")),
        ):
            with self.assertRaisesRegex(RuntimeError, "CODEQ_RUNTIME_DIR is not usable"):
                default_socket_endpoint()

    def test_abstract_socket_name_respects_linux_sun_path_limit(self) -> None:
        self.assertEqual(len(SocketEndpoint.abstract("x" * 107).address.encode("utf-8")), 108)
        with self.assertRaisesRegex(ValueError, "too long"):
            SocketEndpoint.abstract("x" * 108)

    def test_abstract_daemon_spawn_uses_argv_safe_name_and_no_log_by_default(self) -> None:
        endpoint = SocketEndpoint.abstract("codeq-test")
        with (
            patch.dict(os.environ, {"CODEQ_DAEMON_LOG": ""}, clear=False),
            patch("codeq.cli.os.posix_spawn", return_value=1234) as spawn,
        ):
            _spawn_daemon(endpoint)
        argv = spawn.call_args.args[1]
        self.assertEqual(argv[-2:], ["--abstract", "codeq-test"])
        self.assertNotIn("\x00", "".join(argv))

    def test_filesystem_daemon_spawn_does_not_create_log_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "runtime" / "codeq.sock"
            endpoint = SocketEndpoint.filesystem(socket_path)
            with (
                patch.dict(os.environ, {"CODEQ_DAEMON_LOG": ""}, clear=False),
                patch("codeq.cli.os.posix_spawn", return_value=1234) as spawn,
            ):
                _spawn_daemon(endpoint)
            self.assertTrue(spawn.called)
            self.assertFalse((socket_path.parent / "daemon.log").exists())
            self.assertEqual(list(socket_path.parent.iterdir()), [])

    def test_daemon_log_is_created_only_when_explicitly_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            endpoint = SocketEndpoint.abstract("codeq-log-test")
            log_path = Path(tmp) / "logs" / "daemon.log"
            with (
                patch.dict(os.environ, {"CODEQ_DAEMON_LOG": str(log_path)}, clear=False),
                patch("codeq.cli.os.posix_spawn", return_value=1234),
            ):
                _spawn_daemon(endpoint)
            self.assertTrue(log_path.exists())
            self.assertEqual(log_path.read_bytes(), b"")

    def test_abstract_server_accepts_same_uid_live_peer(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.assertTrue(_trusted_peer(left, require_credentials=True))
        finally:
            left.close()
            right.close()

    def test_abstract_client_accepts_same_uid_server_when_pid_is_hidden_by_namespace(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with patch("codeq.cli._peer_credentials", return_value=(0, os.getuid(), os.getgid())):
                self.assertTrue(_peer_is_trusted(left))
        finally:
            left.close()
            right.close()

    def test_abstract_server_accepts_same_uid_peer_when_pid_is_hidden_by_namespace(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with patch("codeq.daemon._peer_credentials", return_value=(0, os.getuid(), os.getgid())):
                self.assertTrue(_trusted_peer(left, require_credentials=True))
        finally:
            left.close()
            right.close()

    def test_abstract_server_rejects_wrong_uid_peer(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with patch("codeq.daemon._peer_credentials", return_value=(os.getpid(), os.getuid() + 1, os.getgid())):
                self.assertFalse(_trusted_peer(left, require_credentials=True))
        finally:
            left.close()
            right.close()

    def test_internal_shutdown_bypasses_version_match_and_requests_stop(self) -> None:
        server_side, client_side = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        stopped = threading.Event()

        class _Service:
            def handle(self, request: dict[str, object]) -> dict[str, object]:
                raise AssertionError(f"shutdown must not reach service: {request}")

        thread = threading.Thread(
            target=_serve_connection,
            args=(server_side, _Service(), stopped.set),
            daemon=True,
        )
        thread.start()
        with client_side:
            file = client_side.makefile("rwb")
            file.write(
                (json.dumps({"command": "_shutdown", "_client_version": "stale"}) + "\n").encode("utf-8")
            )
            file.flush()
            response = json.loads(file.readline())
        thread.join(timeout=1.0)
        self.assertTrue(response["ok"])
        self.assertTrue(stopped.is_set())

    def test_stale_daemon_restart_does_not_require_visible_peer_pid_when_shutdown_works(self) -> None:
        endpoint = SocketEndpoint.abstract("codeq-shutdown-test")
        with (
            patch("codeq.cli._request_daemon_shutdown", return_value=True),
            patch("codeq.cli._connect", side_effect=ConnectionRefusedError),
        ):
            _restart_stale_daemon(None, endpoint)

    def test_lsp_temp_environment_uses_effective_codeq_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            bad_windows_temp = "/mnt/c/Users/example/AppData/Local/Temp"
            with patch.dict(
                os.environ,
                {
                    "CODEQ_EFFECTIVE_RUNTIME_DIR": str(runtime),
                    "CODEQ_RUNTIME_DIR": "",
                    "TMPDIR": bad_windows_temp,
                    "TEMP": bad_windows_temp,
                    "TMP": bad_windows_temp,
                },
                clear=False,
            ):
                env = _lsp_environment()
            expected = str((runtime / "lsp-tmp").resolve())
            self.assertEqual(env["TMPDIR"], expected)
            self.assertEqual(env["TEMP"], expected)
            self.assertEqual(env["TMP"], expected)
            self.assertEqual(stat.S_IMODE((runtime / "lsp-tmp").stat().st_mode), 0o700)

    def test_lsp_temp_environment_ignores_host_temp_without_effective_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "runtime"
            with patch.dict(
                os.environ,
                {
                    "CODEQ_EFFECTIVE_RUNTIME_DIR": "",
                    "CODEQ_RUNTIME_DIR": str(explicit),
                    "TMPDIR": "/host/read-only/tmpdir",
                    "TEMP": "/host/read-only/temp",
                    "TMP": "/host/read-only/tmp",
                },
                clear=False,
            ):
                env = _lsp_environment()
            expected = str((explicit / "lsp-tmp").resolve())
            self.assertEqual({env["TMPDIR"], env["TEMP"], env["TMP"]}, {expected})


if __name__ == "__main__":
    unittest.main()
