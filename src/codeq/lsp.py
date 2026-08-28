from __future__ import annotations

import json
import os
import queue
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .util import language_for, path_to_uri


class LspError(RuntimeError):
    pass


def _lsp_environment() -> dict[str, str]:
    env = os.environ.copy()
    runtime_text = env.get("CODEQ_EFFECTIVE_RUNTIME_DIR") or env.get("CODEQ_RUNTIME_DIR")
    runtime = Path(runtime_text).expanduser() if runtime_text else Path("/tmp") / f"codeq-{os.getuid()}"
    temp_dir = runtime / "lsp-tmp"
    temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = temp_dir.stat()
    if info.st_uid != os.getuid():
        raise LspError(f"LSP temp directory is not owned by current user: {temp_dir}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        temp_dir.chmod(0o700)
    temp_text = str(temp_dir.resolve())
    env["TMPDIR"] = temp_text
    env["TEMP"] = temp_text
    env["TMP"] = temp_text
    return env


class LspProcess:
    """Minimal JSON-RPC/LSP stdio client with a persistent language-server process."""

    def __init__(self, command: list[str], root: Path, name: str, timeout: float = 15.0):
        self.command = command
        self.root = root.resolve()
        self.name = name
        self.timeout = timeout
        self._server_settings = self._settings_for_server(name)
        self._proc = subprocess.Popen(
            command,
            cwd=str(self.root),
            env=_lsp_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        if self._proc.stdin is None or self._proc.stdout is None or self._proc.stderr is None:
            raise LspError(f"failed to create stdio pipes for {name}")
        self._write_lock = threading.Lock()
        self._next_id = 1
        self.request_count = 0
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._open_docs: dict[str, tuple[int, int]] = {}
        self._stderr_tail: list[str] = []
        self._reader = threading.Thread(target=self._reader_loop, name=f"codeq-{name}-reader", daemon=True)
        self._stderr_reader = threading.Thread(target=self._stderr_loop, name=f"codeq-{name}-stderr", daemon=True)
        self._reader.start()
        self._stderr_reader.start()
        self._initialize()

    @property
    def pid(self) -> int:
        return self._proc.pid

    def alive(self) -> bool:
        return self._proc.poll() is None

    def close(self) -> None:
        if self.alive():
            try:
                self.notify("exit", {})
            except Exception:
                pass
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def _stderr_loop(self) -> None:
        assert self._proc.stderr is not None
        for raw in iter(self._proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._stderr_tail.append(line)
                if len(self._stderr_tail) > 50:
                    del self._stderr_tail[:10]

    def _reader_loop(self) -> None:
        assert self._proc.stdout is not None
        try:
            while True:
                headers: dict[str, str] = {}
                while True:
                    line = self._proc.stdout.readline()
                    if not line:
                        raise EOFError
                    if line in (b"\r\n", b"\n"):
                        break
                    decoded = line.decode("ascii", errors="replace").strip()
                    if ":" in decoded:
                        key, value = decoded.split(":", 1)
                        headers[key.lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                if length <= 0:
                    continue
                chunks: list[bytes] = []
                remaining = length
                while remaining:
                    chunk = self._proc.stdout.read(remaining)
                    if not chunk:
                        raise EOFError
                    chunks.append(chunk)
                    remaining -= len(chunk)
                payload = b"".join(chunks)
                message = json.loads(payload.decode("utf-8"))
                self._dispatch(message)
        except (EOFError, OSError, ValueError, json.JSONDecodeError):
            with self._pending_lock:
                for waiter in self._pending.values():
                    waiter.put({"error": {"message": f"{self.name} language server exited"}})
                self._pending.clear()

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message) and "method" not in message:
            request_id = message.get("id")
            if not isinstance(request_id, int):
                return
            with self._pending_lock:
                waiter = self._pending.pop(request_id, None)
            if waiter:
                waiter.put(message)
            return
        if "id" in message and "method" in message:
            self._handle_server_request(message)

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        method = message.get("method", "")
        params = message.get("params") or {}
        if method == "workspace/configuration":
            result = [
                self._configuration_value(item.get("section")) if isinstance(item, dict) else None
                for item in params.get("items", [])
            ]
        elif method == "workspace/workspaceFolders":
            result = [{"uri": path_to_uri(self.root), "name": self.root.name}]
        elif method in {
            "client/registerCapability",
            "client/unregisterCapability",
            "window/workDoneProgress/create",
        }:
            result = None
        elif method == "workspace/applyEdit":
            result = {"applied": False, "failureReason": "codeq is read-only"}
        elif method == "window/showMessageRequest":
            result = None
        else:
            result = None
        self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    @staticmethod
    def _settings_for_server(name: str) -> dict[str, Any]:
        analysis = {"diagnosticMode": "openFilesOnly"}
        lowered = name.lower()
        if "basedpyright" in lowered:
            return {"basedpyright": {"analysis": analysis}}
        if "pyright" in lowered:
            return {"python": {"analysis": analysis}}
        return {}

    def _configuration_value(self, section: Any) -> Any:
        if not isinstance(section, str) or not section:
            return self._server_settings
        value: Any = self._server_settings
        for part in section.split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value

    def _send(self, message: dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        packet = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        with self._write_lock:
            if not self.alive():
                raise LspError(f"{self.name} language server is not running")
            self._proc.stdin.write(packet)
            self._proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        self.request_count += 1
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        try:
            message = waiter.get(timeout=timeout or self.timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise LspError(f"{self.name} timed out on {method}") from exc
        if "error" in message:
            err = message["error"]
            raise LspError(f"{self.name} {method}: {err.get('message', err)}")
        return message.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _initialize(self) -> None:
        capabilities = {
            "workspace": {
                "workspaceFolders": True,
                "configuration": True,
                "symbol": {"dynamicRegistration": False},
            },
            "textDocument": {
                "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                "definition": {"dynamicRegistration": False},
                "references": {"dynamicRegistration": False},
                "implementation": {"dynamicRegistration": False},
                "hover": {"contentFormat": ["plaintext", "markdown"]},
                "callHierarchy": {"dynamicRegistration": False},
            },
        }
        result = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "clientInfo": {"name": "codeq", "version": __version__},
                "rootUri": path_to_uri(self.root),
                "rootPath": str(self.root),
                "workspaceFolders": [{"uri": path_to_uri(self.root), "name": self.root.name}],
                "capabilities": capabilities,
            },
            timeout=max(self.timeout, 20),
        )
        self.server_capabilities = (result or {}).get("capabilities", {})
        self.notify("initialized", {})
        # Pyright/BasedPyright requests the effective settings after this
        # notification. Keep the payload consistent with our configuration
        # responses instead of advertising settings that the server cannot read.
        if self._server_settings:
            self.notify(
                "workspace/didChangeConfiguration",
                {"settings": self._server_settings},
            )

    def ensure_open(self, path: Path) -> None:
        path = path.resolve()
        language = language_for(path)
        if language is None:
            return
        try:
            stat = path.stat()
            marker = (stat.st_mtime_ns, stat.st_size)
        except OSError as exc:
            raise LspError(f"cannot open {path}: {exc}") from exc
        uri = path_to_uri(path)
        old = self._open_docs.get(uri)
        if old == marker:
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LspError(f"cannot open {path}: {exc}") from exc
        if old is None:
            self.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": uri, "languageId": language, "version": 1, "text": text}},
            )
            self._open_docs[uri] = marker
        elif old != marker:
            version = int(time.time_ns() % 2_000_000_000)
            self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
            self._open_docs[uri] = marker

    def position_params(self, path: Path, line: int, column: int) -> dict[str, Any]:
        self.ensure_open(path)
        return {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": max(0, line - 1), "character": max(0, column - 1)},
        }

    def workspace_symbols(self, query: str, timeout: float | None = None) -> list[dict[str, Any]]:
        return self.request("workspace/symbol", {"query": query}, timeout=timeout) or []

    def document_symbols(self, path: Path, timeout: float | None = None) -> list[dict[str, Any]]:
        self.ensure_open(path)
        return self.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": path_to_uri(path)}},
            timeout=timeout,
        ) or []

    def hover(self, path: Path, line: int, column: int) -> Any:
        return self.request("textDocument/hover", self.position_params(path, line, column))

    def definitions(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        try:
            result = self.request("textDocument/definition", self.position_params(path, line, column))
        except LspError:
            return []
        if not result:
            return []
        if isinstance(result, dict):
            return [result]
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return []

    def references(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        params = self.position_params(path, line, column)
        params["context"] = {"includeDeclaration": False}
        return self.request("textDocument/references", params) or []

    def implementations(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        try:
            return self.request("textDocument/implementation", self.position_params(path, line, column)) or []
        except LspError:
            return []

    def prepare_call_hierarchy(self, path: Path, line: int, column: int) -> list[dict[str, Any]]:
        try:
            return self.request("textDocument/prepareCallHierarchy", self.position_params(path, line, column)) or []
        except LspError:
            return []

    def incoming_calls(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return self.request("callHierarchy/incomingCalls", {"item": item}) or []
        except LspError:
            return []

    def outgoing_calls(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return self.request("callHierarchy/outgoingCalls", {"item": item}) or []
        except LspError:
            return []
