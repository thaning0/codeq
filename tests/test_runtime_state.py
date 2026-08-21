from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codeq.cli import _spawn_daemon
from codeq.daemon import default_socket_path


class RuntimeStateTests(unittest.TestCase):
    def test_explicit_runtime_dir_is_private_and_probe_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            with patch.dict(os.environ, {"CODEQ_RUNTIME_DIR": str(runtime)}, clear=False):
                path = default_socket_path()
            self.assertEqual(path, runtime / "codeq.sock")
            self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)
            self.assertEqual(list(runtime.glob(".socket-probe-*")), [])

    def test_xdg_runtime_uses_private_codeq_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xdg = Path(tmp) / "xdg"
            with patch.dict(
                os.environ,
                {"XDG_RUNTIME_DIR": str(xdg), "CODEQ_RUNTIME_DIR": ""},
                clear=False,
            ):
                path = default_socket_path()
            self.assertEqual(path, xdg / "codeq" / "codeq.sock")
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_unusable_xdg_runtime_falls_back_to_tmp(self) -> None:
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
            patch("codeq.daemon._prepare_runtime_dir", side_effect=prepare),
        ):
            path = default_socket_path()
        self.assertEqual(path, Path("/tmp") / f"codeq-{uid}" / "codeq.sock")

    def test_explicit_runtime_dir_failure_is_not_silently_ignored(self) -> None:
        with (
            patch.dict(os.environ, {"CODEQ_RUNTIME_DIR": "/explicit/runtime"}, clear=False),
            patch("codeq.daemon._prepare_runtime_dir", side_effect=PermissionError("read-only")),
        ):
            with self.assertRaisesRegex(RuntimeError, "CODEQ_RUNTIME_DIR is not usable"):
                default_socket_path()

    def test_daemon_spawn_does_not_create_log_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "runtime" / "codeq.sock"
            with (
                patch.dict(os.environ, {"CODEQ_DAEMON_LOG": ""}, clear=False),
                patch("codeq.cli.os.posix_spawn", return_value=1234) as spawn,
            ):
                _spawn_daemon(socket_path)
            self.assertTrue(spawn.called)
            self.assertFalse((socket_path.parent / "daemon.log").exists())
            self.assertEqual(list(socket_path.parent.iterdir()), [])

    def test_daemon_log_is_created_only_when_explicitly_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            socket_path = Path(tmp) / "runtime" / "codeq.sock"
            log_path = Path(tmp) / "logs" / "daemon.log"
            with (
                patch.dict(os.environ, {"CODEQ_DAEMON_LOG": str(log_path)}, clear=False),
                patch("codeq.cli.os.posix_spawn", return_value=1234),
            ):
                _spawn_daemon(socket_path)
            self.assertTrue(log_path.exists())
            self.assertEqual(log_path.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
