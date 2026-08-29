"""Privacy-preserving replay of downstream tool use after codeq queries."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shlex
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Sequence


PARSER_VERSION = "1"

_QUERY_COMMANDS = {"find", "search", "context", "trace", "review"}
_CLASSIFICATIONS = {
    "codeq_refinement",
    "compensated_or_missed_result",
    "complemented_result",
    "new_result_not_consumed",
    "returned_path_consumed",
    "unclassified",
    "verification",
}
_UTILITY_SIGNALS = {
    "clean_codeq_only_events",
    "clean_events_editing_returned_path",
    "clean_events_exposing_paths",
    "clean_events_reading_returned_path",
    "same_target_search_events",
}
_SHELL_TOOL_NAMES = {"bash", "exec", "exec_command", "shell", "terminal"}
_SEARCH_COMMANDS = {"rg", "ripgrep", "grep", "egrep", "fgrep", "ag", "ack", "fd", "find"}
_READ_COMMANDS = {"bat", "cat", "head", "less", "more", "sed", "tail"}
_EDIT_COMMANDS = {"apply_patch", "cp", "install", "mkdir", "mv", "rm", "rmdir", "touch", "truncate"}
_CONTROL_WORDS = {"!", "do", "done", "elif", "else", "fi", "if", "then", "time", "until", "while"}
_SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cfg",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".md",
    ".mjs",
    ".php",
    ".proto",
    ".py",
    ".pyi",
    ".rb",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".svelte",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
_VALUE_OPTIONS = {
    "--base",
    "--container",
    "--depth",
    "--glob",
    "--head",
    "--kind",
    "--lexical-references",
    "--limit",
    "--max-lines",
    "--max-results",
    "--node-limit",
    "--outline-depth",
    "--path",
    "--root",
    "--section",
    "--symbol-path",
}
_PATH_KEY_RE = re.compile(r"[\"'](?:path|file|file_path)[\"']\s*:\s*[\"']([^\"'\r\n]+)[\"']", re.I)
_LOCATION_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?(?:[/\\]|\.?\.?[/\\])?"
    r"[A-Za-z0-9_.@+-]+(?:[/\\][A-Za-z0-9_.@+-]+)*\.[A-Za-z0-9]{1,8})"
    r"(?::\d+(?::\d+)?)?"
)
_HEREDOC_RE = re.compile(r"<<-?\s*(?:[rubfRUBF]*)(?P<quote>['\"]?)(?P<word>[A-Za-z_][A-Za-z0-9_]*)\1")
_VERSION_RE = re.compile(r"(?im)^\s*codeq(?:\s+version)?\s+v?([0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[A-Za-z0-9.-]+)?)\s*$")


@dataclass(frozen=True)
class CodeqQuery:
    command: str
    target_shape: str
    options: tuple[str, ...]
    target_key: str
    target_terms: frozenset[str]


@dataclass
class ShellInvocation:
    command: str
    workdir: str
    queries: list[CodeqQuery] = field(default_factory=list)
    families: Counter[str] = field(default_factory=Counter)
    read_paths: set[str] = field(default_factory=set)
    edit_paths: set[str] = field(default_factory=set)
    search_keys: set[str] = field(default_factory=set)
    search_terms: set[str] = field(default_factory=set)
    output: str = ""
    output_attribution: str = "unavailable"
    version_probe: bool = False


@dataclass
class ToolCall:
    call_id: str
    name: str
    raw_input: str
    output: str
    record_index: int
    turn: int
    timestamp: str
    session_cwd: str
    invocations: list[ShellInvocation] = field(default_factory=list)
    direct_families: Counter[str] = field(default_factory=Counter)
    read_paths: set[str] = field(default_factory=set)
    edit_paths: set[str] = field(default_factory=set)
    search_keys: set[str] = field(default_factory=set)
    search_terms: set[str] = field(default_factory=set)
    paired_output: bool = False

    @property
    def families(self) -> Counter[str]:
        result = self.direct_families.copy()
        for invocation in self.invocations:
            result.update(invocation.families)
        return result

    @property
    def queries(self) -> list[CodeqQuery]:
        return [query for invocation in self.invocations for query in invocation.queries]


@dataclass
class Session:
    key: str
    timestamps: list[str]
    working_directories: set[str]
    record_count: int
    calls: list[ToolCall]


def _json_records(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("payload")
    return value if isinstance(value, dict) else {}


def _text_envelope(value: Any) -> str:
    """Flatten tool-result envelopes without stringifying unrelated metadata."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _text_envelope(item)))
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    for key in ("text", "output", "content"):
        if key in value:
            part = _text_envelope(value[key])
            if part:
                parts.append(part)
    structured = value.get("structuredContent")
    if isinstance(structured, (dict, list)):
        parts.append(json.dumps(structured, ensure_ascii=False))
    return "\n".join(parts)


def _raw_input(payload: dict[str, Any]) -> str:
    value = payload.get("input") if payload.get("input") is not None else payload.get("arguments")
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _tool_name(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower().replace("-", "_")


def _is_call_payload(payload: dict[str, Any]) -> bool:
    return payload.get("type") in {"custom_tool_call", "function_call", "tool_call"}


def _is_output_payload(payload: dict[str, Any]) -> bool:
    return payload.get("type") in {"custom_tool_call_output", "function_call_output", "tool_call_output"}


def _has_event_user_boundaries(records: Sequence[dict[str, Any]]) -> bool:
    return any(
        record.get("type") == "event_msg" and _payload(record).get("type") == "user_message"
        for record in records
    )


def _session_from_path(path: Path) -> Session:
    records = list(_json_records(path))
    event_boundaries = _has_event_user_boundaries(records)
    output_by_id: dict[str, str] = {}
    calls: list[ToolCall] = []
    timestamps: list[str] = []
    cwd = ""
    session_id = ""
    working_directories: set[str] = set()
    turn = 0

    for index, record in enumerate(records):
        timestamp = str(record.get("timestamp") or "")
        if timestamp:
            timestamps.append(timestamp)
        payload = _payload(record)
        if record.get("type") == "session_meta":
            session_id = str(payload.get("id") or payload.get("session_id") or session_id)
            next_cwd = str(payload.get("cwd") or "")
            if next_cwd:
                cwd = next_cwd
                working_directories.add(next_cwd)
        is_event_boundary = record.get("type") == "event_msg" and payload.get("type") == "user_message"
        is_response_boundary = (
            not event_boundaries
            and record.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        )
        if is_event_boundary or is_response_boundary:
            turn += 1
            continue
        if record.get("type") != "response_item":
            continue
        if _is_output_payload(payload):
            call_id = str(payload.get("call_id") or "")
            if call_id:
                part = _text_envelope(payload.get("output"))
                if part:
                    output_by_id[call_id] = "\n".join(filter(None, (output_by_id.get(call_id), part)))
            continue
        if not _is_call_payload(payload):
            continue
        call_id = str(payload.get("call_id") or payload.get("id") or f"record-{index}")
        calls.append(
            ToolCall(
                call_id=call_id,
                name=str(payload.get("name") or ""),
                raw_input=_raw_input(payload),
                output="",
                record_index=index,
                turn=turn,
                timestamp=timestamp,
                session_cwd=cwd,
            )
        )

    for call in calls:
        if call.call_id in output_by_id:
            call.output = output_by_id[call.call_id]
            call.paired_output = True
        _populate_call(call, working_directories)
    key = hashlib.sha256((session_id or str(path)).encode()).hexdigest()[:16]
    return Session(key, timestamps, working_directories, len(records), calls)


def _decode_quoted(source: str, start: int) -> tuple[str, int] | None:
    quote = source[start]
    if quote not in {'"', "'", "`"}:
        return None
    escaped = False
    for index in range(start + 1, len(source)):
        char = source[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char != quote:
            continue
        literal = source[start : index + 1]
        if quote == '"':
            try:
                return str(json.loads(literal)), index + 1
            except json.JSONDecodeError:
                pass
        if quote == "'":
            try:
                return str(ast.literal_eval(literal)), index + 1
            except (SyntaxError, ValueError):
                pass
        raw = literal[1:-1]
        raw = raw.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")
        raw = raw.replace(f"\\{quote}", quote).replace("\\\\", "\\")
        return raw, index + 1
    return None


def _balanced_call_body(source: str, open_paren: int) -> tuple[str, int] | None:
    depth = 0
    quote = ""
    escaped = False
    for index in range(open_paren, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[open_paren + 1 : index], index + 1
    return None


def _object_string_field(body: str, field_name: str) -> str:
    match = re.search(rf"(?:[\"']{re.escape(field_name)}[\"']|\b{re.escape(field_name)})\s*:\s*", body)
    if not match:
        return ""
    index = match.end()
    while index < len(body) and body[index].isspace():
        index += 1
    decoded = _decode_quoted(body, index) if index < len(body) else None
    return decoded[0] if decoded else ""


def _nested_tool_bodies(source: str, marker: str) -> list[str]:
    result: list[str] = []
    cursor = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    while cursor < len(source):
        char = source[cursor]
        following = source[cursor : cursor + 2]
        if line_comment:
            line_comment = char != "\n"
            cursor += 1
            continue
        if block_comment:
            if following == "*/":
                block_comment = False
                cursor += 2
            else:
                cursor += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            cursor += 1
            continue
        if following == "//":
            line_comment = True
            cursor += 2
            continue
        if following == "/*":
            block_comment = True
            cursor += 2
            continue
        if char in {'"', "'", "`"}:
            quote = char
            cursor += 1
            continue
        if not source.startswith(marker, cursor):
            cursor += 1
            continue
        open_paren = cursor + len(marker)
        while open_paren < len(source) and source[open_paren].isspace():
            open_paren += 1
        if open_paren >= len(source) or source[open_paren] != "(":
            cursor += len(marker)
            continue
        parsed = _balanced_call_body(source, open_paren)
        if not parsed:
            break
        body, cursor = parsed
        result.append(body)
    return result


def _nested_exec_invocations(source: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for body in _nested_tool_bodies(source, "tools.exec_command"):
        command = _object_string_field(body, "cmd") or _object_string_field(body, "command")
        if command:
            result.append((command, _object_string_field(body, "workdir")))
    return result


def _javascript_string_literals(source: str) -> Iterator[str]:
    cursor = 0
    while cursor < len(source):
        if source[cursor] not in {'"', "'", "`"}:
            cursor += 1
            continue
        decoded = _decode_quoted(source, cursor)
        if not decoded:
            cursor += 1
            continue
        value, cursor = decoded
        yield value


def _direct_shell_invocation(raw: str) -> tuple[str, str] | None:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        command = value.get("cmd") if value.get("cmd") is not None else value.get("command")
        workdir = value.get("workdir") or value.get("cwd") or ""
        if isinstance(command, str):
            return command, str(workdir)
    return (raw, "") if raw.strip() else None


def _strip_heredoc_bodies(command: str) -> str:
    lines = command.splitlines()
    kept: list[str] = []
    delimiter = ""
    allow_tabs = False
    for line in lines:
        if delimiter:
            candidate = line.lstrip("\t") if allow_tabs else line
            if candidate.strip() == delimiter:
                delimiter = ""
                allow_tabs = False
            continue
        kept.append(line)
        match = _HEREDOC_RE.search(line)
        if match:
            delimiter = match.group("word")
            allow_tabs = "<<-" in match.group(0)
    return "\n".join(kept)


def _shell_segments(command: str) -> Iterator[list[str]]:
    cleaned = _strip_heredoc_bodies(command).replace("\\\n", " ")
    for line in cleaned.splitlines():
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
            lexer.whitespace_split = True
            lexer.commenters = "#"
            tokens = list(lexer)
        except ValueError:
            continue
        segment: list[str] = []
        for token in tokens:
            if token and all(char in ";&|()" for char in token):
                if segment:
                    yield segment
                    segment = []
            else:
                segment.append(token)
        if segment:
            yield segment


def _strip_command_prefix(tokens: Sequence[str]) -> list[str]:
    result = list(tokens)
    while result and (result[0] in _CONTROL_WORDS or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", result[0])):
        result.pop(0)
    if result and result[0] == "$":
        result.pop(0)
    return result


def _basename(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def _codeq_argv(tokens: Sequence[str]) -> list[str] | None:
    values = _strip_command_prefix(tokens)
    if not values:
        return None
    if _basename(values[0]) == "codeq":
        return values
    if values[0] in {"command", "env", "sudo", "timeout"}:
        for index, token in enumerate(values[1:], 1):
            if _basename(token) == "codeq":
                return values[index:]
    if _basename(values[0]) == "uv" and len(values) > 1 and values[1] == "run":
        for index, token in enumerate(values[2:], 2):
            if _basename(token) == "codeq":
                return values[index:]
    if _basename(values[0]) in {"python", "python3"} and len(values) > 2 and values[1:3] == ["-m", "codeq"]:
        return ["codeq", *values[3:]]
    return None


def _options(argv: Sequence[str]) -> tuple[str, ...]:
    values = {token.split("=", 1)[0] for token in argv[1:] if token.startswith("-")}
    return tuple(sorted(values))


def _positional_after_command(argv: Sequence[str], command_index: int) -> str:
    index = command_index + 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[index + 1] if index + 1 < len(argv) else ""
        option = token.split("=", 1)[0]
        if token.startswith("-"):
            if "=" not in token and option in _VALUE_OPTIONS:
                index += 2
            else:
                index += 1
            continue
        return token
    return ""


def _target_shape(command: str, target: str, options: Sequence[str]) -> str:
    if command == "review":
        return "worktree"
    if not target:
        return "none"
    if command == "find" and "--text" in options:
        return "exact_text"
    if re.search(r":\d+(?::\d+)?$", target):
        return "location"
    normalized = target.replace("\\", "/")
    if "/" in normalized or PurePosixPath(normalized).suffix.lower() in _SOURCE_SUFFIXES:
        return "path"
    if "::" in target or "." in target:
        return "qualified_symbol"
    if any(char.isspace() for char in target):
        return "natural_language"
    return "symbol"


def _searchable_terms(value: str) -> frozenset[str]:
    normalized = re.sub(r":\d+(?::\d+)?$", "", value.strip().lower().replace("\\", "/"))
    tokens = {
        token
        for token in re.findall(r"[a-z_][a-z0-9_-]*", normalized)
        if len(token) >= 3 and token not in {"src", "test", "tests", "file", "path"}
    }
    basename = normalized.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0]
    for part in re.split(r"[.:/]+", stem):
        if len(part) >= 3:
            tokens.add(part)
    return frozenset(tokens)


def _query_from_argv(argv: Sequence[str]) -> CodeqQuery | None:
    if any(token in {"--help", "-h"} for token in argv[1:]) or "--version" in argv[1:]:
        return None
    command_index = next((index for index, token in enumerate(argv[1:], 1) if token in _QUERY_COMMANDS), -1)
    if command_index < 0:
        return None
    command = "find" if argv[command_index] == "search" else argv[command_index]
    options = _options(argv)
    target = "" if command == "review" else _positional_after_command(argv, command_index)
    target_key = hashlib.sha256(target.strip().lower().encode()).hexdigest()[:16] if target else ""
    return CodeqQuery(command, _target_shape(command, target, options), options, target_key, _searchable_terms(target))


def _plain_paths(text: str, roots: Sequence[str]) -> set[str]:
    candidates = list(_PATH_KEY_RE.findall(text))
    for line in text.splitlines():
        match = _LOCATION_RE.search(line)
        if not match:
            continue
        prefix = line[: match.start()].strip()
        if prefix and (any(char.isdigit() for char in prefix) or len(prefix.split()) > 3):
            continue
        candidates.append(match.group("path"))
    return {path for raw in candidates if (path := _normalize_path(raw, roots))}


def _normalize_path(raw: str, roots: Sequence[str]) -> str:
    value = raw.strip().strip("`'\"[](){}<>,;").replace("\\", "/")
    value = re.sub(r":\d+(?::\d+)?$", "", value)
    value = re.sub(r"^\./", "", value)
    if not value or "\n" in value or "\x00" in value:
        return ""
    for root in roots:
        normalized_root = root.strip().replace("\\", "/").rstrip("/")
        if normalized_root and value.startswith(normalized_root + "/"):
            value = value[len(normalized_root) + 1 :]
            break
    parts: list[str] = []
    for part in value.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    if not parts or PurePosixPath(parts[-1]).suffix.lower() not in _SOURCE_SUFFIXES:
        return ""
    return "/".join(parts)


def _path_matches(left: str, right: str) -> bool:
    left_parts = tuple(part.lower() for part in left.split("/") if part)
    right_parts = tuple(part.lower() for part in right.split("/") if part)
    if not left_parts or not right_parts:
        return False
    size = min(len(left_parts), len(right_parts))
    return left_parts[-size:] == right_parts[-size:]


def _file_args(tokens: Sequence[str], roots: Sequence[str], command: str) -> set[str]:
    result: set[str] = set()
    skip_next = False
    for index, token in enumerate(tokens[1:], 1):
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            if command in {"head", "tail", "sed"} and token in {"-n", "-e", "-f"}:
                skip_next = True
            continue
        if command == "sed" and index == 1 and not PurePosixPath(token).suffix:
            continue
        path = _normalize_path(token, roots)
        if path:
            result.add(path)
    return result


def _search_patterns(tokens: Sequence[str]) -> list[str]:
    values = _strip_command_prefix(tokens)
    if not values:
        return []
    executable = _basename(values[0]).lower()
    offset = 1
    if executable == "git" and len(values) > 1 and values[1] == "grep":
        offset = 2
    elif executable not in _SEARCH_COMMANDS - {"find", "fd"}:
        return []
    patterns: list[str] = []
    index = offset
    value_options = {
        "-A",
        "-B",
        "-C",
        "-e",
        "-f",
        "-g",
        "-j",
        "-m",
        "-t",
        "--after-context",
        "--before-context",
        "--context",
        "--encoding",
        "--engine",
        "--file",
        "--glob",
        "--iglob",
        "--max-count",
        "--max-depth",
        "--regexp",
        "--type",
        "--type-add",
        "--type-not",
    }
    while index < len(values):
        token = values[index]
        option = token.split("=", 1)[0]
        if option in {"-e", "--regexp"} and "=" not in token and index + 1 < len(values):
            patterns.append(values[index + 1])
            index += 2
            continue
        if token.startswith("-"):
            index += 2 if "=" not in token and option in value_options else 1
            continue
        patterns.append(token)
        break
    return patterns


def _families_and_paths(
    command: str, workdir: str
) -> tuple[Counter[str], set[str], set[str], set[str], set[str]]:
    families: Counter[str] = Counter()
    reads: set[str] = set()
    edits: set[str] = set()
    search_keys: set[str] = set()
    search_terms: set[str] = set()
    roots = [workdir] if workdir else []
    for tokens in _shell_segments(command):
        values = _strip_command_prefix(tokens)
        if not values:
            continue
        executable = _basename(values[0]).lower()
        if executable == "git":
            families["git"] += 1
            if len(values) > 1 and values[1] == "grep":
                families["search"] += 1
        elif executable in _SEARCH_COMMANDS:
            families["search"] += 1
        if executable in _SEARCH_COMMANDS or (executable == "git" and len(values) > 1 and values[1] == "grep"):
            for pattern in _search_patterns(values):
                normalized = pattern.strip().lower()
                if normalized:
                    search_keys.add(hashlib.sha256(normalized.encode()).hexdigest()[:16])
                    search_terms.update(_searchable_terms(pattern))
        if executable in _READ_COMMANDS:
            file_paths = _file_args(values, roots, executable)
            if file_paths:
                families["read"] += 1
                reads.update(file_paths)
        if executable in _EDIT_COMMANDS:
            families["edit"] += 1
            edits.update(_file_args(values, roots, executable))
        in_place = any("i" in token.lstrip("-") for token in values[1:] if token.startswith("-"))
        if executable in {"perl", "sed"} and in_place:
            families["edit"] += 1
            edits.update(_file_args(values, roots, executable))
    return families, reads, edits, search_keys, search_terms


def _split_indexed_output(text: str, count: int) -> dict[int, str]:
    marker = re.compile(r"(?im)^\s*(?:---[A-Za-z]*|result\s+)(\d+)(?:---)?\s*$")
    matches = list(marker.finditer(text))
    if not matches:
        return {}
    parts: dict[int, str] = {}
    for index, match in enumerate(matches):
        number = int(match.group(1)) - 1
        if number < 0 or number >= count or number in parts:
            return {}
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        parts[number] = text[match.end() : end]
    return parts if len(parts) == count else {}


def _populate_invocation(invocation: ShellInvocation) -> None:
    for tokens in _shell_segments(invocation.command):
        argv = _codeq_argv(tokens)
        if argv:
            invocation.version_probe = invocation.version_probe or "--version" in argv[1:]
            query = _query_from_argv(argv)
            if query:
                invocation.queries.append(query)
    families, reads, edits, search_keys, search_terms = _families_and_paths(invocation.command, invocation.workdir)
    invocation.families = families
    invocation.read_paths = reads
    invocation.edit_paths = edits
    invocation.search_keys = search_keys
    invocation.search_terms = search_terms


def _direct_tool_paths(call: ToolCall, roots: Sequence[str]) -> None:
    name = _tool_name(call.name)
    if name in {"read", "read_file"}:
        call.direct_families["read"] += 1
    if name in {"grep", "search", "search_files"}:
        call.direct_families["search"] += 1
    if name in {"apply_patch", "edit", "edit_file", "write_file"}:
        call.direct_families["edit"] += 1
    try:
        value = json.loads(call.raw_input)
    except json.JSONDecodeError:
        value = {}
    if isinstance(value, dict):
        for key in ("query", "pattern", "search_term"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                normalized = raw.strip().lower()
                call.search_keys.add(hashlib.sha256(normalized.encode()).hexdigest()[:16])
                call.search_terms.update(_searchable_terms(raw))
        for key in ("path", "file", "file_path"):
            raw = value.get(key)
            if isinstance(raw, str) and (path := _normalize_path(raw, roots)):
                if call.direct_families["edit"]:
                    call.edit_paths.add(path)
                elif call.direct_families["read"]:
                    call.read_paths.add(path)
    if name == "apply_patch":
        for match in re.finditer(r"(?m)^\*\*\* (?:Add|Delete|Update) File:\s*(.+?)\s*$", call.raw_input):
            if path := _normalize_path(match.group(1), roots):
                call.edit_paths.add(path)


def _populate_call(call: ToolCall, working_directories: set[str]) -> None:
    name = _tool_name(call.name)
    if name == "exec" and _nested_tool_bodies(call.raw_input, "tools.apply_patch"):
        call.direct_families["edit"] += 1
        roots = [call.session_cwd] if call.session_cwd else []
        for literal in _javascript_string_literals(call.raw_input):
            if "*** Begin Patch" not in literal:
                continue
            for match in re.finditer(r"(?m)^\*\*\* (?:Add|Delete|Update) File:\s*(.+?)\s*$", literal):
                if path := _normalize_path(match.group(1), roots):
                    call.edit_paths.add(path)
    nested = _nested_exec_invocations(call.raw_input) if name == "exec" else []
    if nested:
        for command, workdir in nested:
            actual_workdir = workdir or call.session_cwd
            if actual_workdir:
                working_directories.add(actual_workdir)
            invocation = ShellInvocation(command, actual_workdir)
            _populate_invocation(invocation)
            call.invocations.append(invocation)
    elif name in _SHELL_TOOL_NAMES:
        direct = _direct_shell_invocation(call.raw_input)
        if direct:
            command, workdir = direct
            actual_workdir = workdir or call.session_cwd
            if actual_workdir:
                working_directories.add(actual_workdir)
            invocation = ShellInvocation(command, actual_workdir)
            _populate_invocation(invocation)
            call.invocations.append(invocation)
    else:
        _direct_tool_paths(call, [call.session_cwd] if call.session_cwd else [])

    if not call.paired_output or not call.invocations:
        return
    if len(call.invocations) == 1:
        call.invocations[0].output = call.output
        call.invocations[0].output_attribution = "invocation"
        return
    indexed = _split_indexed_output(call.output, len(call.invocations))
    if indexed:
        for index, invocation in enumerate(call.invocations):
            invocation.output = indexed[index]
            invocation.output_attribution = "invocation"
    elif all(invocation.queries and not invocation.families for invocation in call.invocations):
        for invocation in call.invocations:
            invocation.output = call.output
            invocation.output_attribution = "event"


def _call_paths(call: ToolCall, kind: str) -> set[str]:
    paths = set(call.read_paths if kind == "read" else call.edit_paths)
    for invocation in call.invocations:
        paths.update(invocation.read_paths if kind == "read" else invocation.edit_paths)
    return paths


def _search_output_paths(call: ToolCall) -> set[str]:
    result: set[str] = set()
    for invocation in call.invocations:
        if not invocation.families["search"] or not invocation.output:
            continue
        result.update(_plain_paths(invocation.output, [invocation.workdir] if invocation.workdir else []))
    if call.direct_families["search"] and call.output:
        result.update(_plain_paths(call.output, [call.session_cwd] if call.session_cwd else []))
    return result


def _search_matches_queries(call: ToolCall, queries: Sequence[CodeqQuery]) -> bool:
    keys = set(call.search_keys)
    terms = set(call.search_terms)
    for invocation in call.invocations:
        keys.update(invocation.search_keys)
        terms.update(invocation.search_terms)
    for query in queries:
        if query.target_key and query.target_key in keys:
            return True
        overlap = query.target_terms & terms
        if not overlap:
            continue
        if query.target_shape == "natural_language":
            required = max(2, (len(query.target_terms) + 1) // 2)
            if len(overlap) >= required:
                return True
        else:
            return True
    return False


def _returned_paths(call: ToolCall) -> tuple[set[str], str]:
    result: set[str] = set()
    attributions: set[str] = set()
    for invocation in call.invocations:
        if not invocation.queries or not invocation.output:
            continue
        if invocation.families:
            continue
        paths = _plain_paths(invocation.output, [invocation.workdir] if invocation.workdir else [])
        result.update(paths)
        attributions.add(invocation.output_attribution)
    if not result:
        return set(), "unavailable"
    if "event" in attributions:
        return result, "event"
    if len(call.queries) > 1:
        return result, "invocation"
    return result, "query"


def _any_path_matches(candidates: Iterable[str], paths: Iterable[str]) -> bool:
    right = tuple(paths)
    return any(_path_matches(candidate, path) for candidate in candidates for path in right)


def _matching_paths(candidates: Iterable[str], paths: Iterable[str]) -> set[str]:
    right = tuple(paths)
    return {candidate for candidate in candidates if any(_path_matches(candidate, path) for path in right)}


def _call_consumes(call: ToolCall, paths: Iterable[str]) -> tuple[bool, bool]:
    values = tuple(paths)
    read = _any_path_matches(_call_paths(call, "read"), values)
    edit = _any_path_matches(_call_paths(call, "edit"), values)
    return read, edit


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def _event_id(session_key: str, call_id: str) -> str:
    return hashlib.sha256(f"{session_key}:{call_id}".encode()).hexdigest()[:16]


def _classify_event(session: Session, index: int, call: ToolCall) -> dict[str, Any]:
    queries = call.queries
    returned, path_attribution = _returned_paths(call)
    later = [candidate for candidate in session.calls[index + 1 :] if candidate.turn == call.turn]
    coissued = call.families
    later_families: Counter[str] = Counter()
    for candidate in later:
        later_families.update(candidate.families)

    returned_read = False
    returned_edit = False
    for candidate in later:
        read, edit = _call_consumes(candidate, returned)
        returned_read = returned_read or read
        returned_edit = returned_edit or edit

    search_paths: set[str] = set()
    repeated: set[str] = set()
    new_paths: set[str] = set()
    new_consumed: set[str] = set()
    search_seen = False
    for later_index, candidate in enumerate(later):
        if not candidate.families["search"] or not _search_matches_queries(candidate, queries):
            continue
        search_seen = True
        found = _search_output_paths(candidate)
        search_paths.update(found)
        repeated.update(_matching_paths(found, returned))
        discovered = {path for path in found if not _any_path_matches([path], returned)}
        new_paths.update(discovered)
        for following in later[later_index + 1 :]:
            read, edit = _call_consumes(following, discovered)
            if read or edit:
                new_consumed.update(
                    path
                    for path in discovered
                    if _any_path_matches(_call_paths(following, "read") | _call_paths(following, "edit"), [path])
                )

    target_keys = {query.target_key for query in queries if query.target_key}
    refinement = any(
        candidate.queries
        and (
            not target_keys
            or any(query.target_key in target_keys for query in candidate.queries if query.target_key)
        )
        for candidate in later
    )
    if search_seen:
        if repeated and new_consumed:
            classification = "complemented_result"
        elif new_consumed and not repeated:
            classification = "compensated_or_missed_result"
        elif repeated and not new_paths:
            classification = "verification"
        elif new_paths and not new_consumed:
            classification = "new_result_not_consumed"
        else:
            classification = "unclassified"
    elif returned_read or returned_edit:
        classification = "returned_path_consumed"
    elif refinement:
        classification = "codeq_refinement"
    else:
        classification = "unclassified"

    command_counts = Counter(query.command for query in queries)
    shape_counts = Counter(query.target_shape for query in queries)
    option_counts = Counter(option for query in queries for option in query.options)
    return {
        "id": _event_id(session.key, call.call_id),
        "query_count": len(queries),
        "commands": _counter_dict(command_counts),
        "target_shapes": _counter_dict(shape_counts),
        "options": _counter_dict(option_counts),
        "output_paired_by_call_id": call.paired_output,
        "path_attribution": path_attribution,
        "returned_path_count": len(returned),
        "coissued_families": _counter_dict(coissued),
        "later_families": _counter_dict(later_families),
        "signals": {
            "returned_path_read": returned_read,
            "returned_path_edited": returned_edit,
            "codeq_refinement": refinement,
            "later_search": search_seen,
            "search_repeated_returned_path": bool(repeated),
            "new_search_path_count": len(new_paths),
            "new_search_path_consumed_count": len(new_consumed),
        },
        "classification": classification,
    }


def load_sessions(root: Path, since: str = "", until: str = "") -> list[Session]:
    result: list[Session] = []
    for path in sorted(root.rglob("*.jsonl")):
        session = _session_from_path(path)
        dates = [timestamp[:10] for timestamp in session.timestamps if re.match(r"\d{4}-\d{2}-\d{2}", timestamp)]
        if since and dates and max(dates) < since:
            continue
        if until and dates and min(dates) > until:
            continue
        if any(call.queries for call in session.calls):
            result.append(session)
    return result


def summarize(sessions: Sequence[Session], since: str = "", until: str = "") -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    commands: Counter[str] = Counter()
    shapes: Counter[str] = Counter()
    options: Counter[str] = Counter()
    coissued: Counter[str] = Counter()
    later: Counter[str] = Counter()
    classifications: Counter[str] = Counter({key: 0 for key in _CLASSIFICATIONS})
    signals: Counter[str] = Counter({key: 0 for key in _UTILITY_SIGNALS})
    observed_versions: set[str] = set()
    dates: list[str] = []
    working_directories: set[str] = set()
    outer_tool_calls = 0

    for session in sessions:
        dates.extend(timestamp[:10] for timestamp in session.timestamps if re.match(r"\d{4}-\d{2}-\d{2}", timestamp))
        working_directories.update(
            hashlib.sha256(path.encode()).hexdigest()[:16] for path in session.working_directories if path
        )
        outer_tool_calls += len(session.calls)
        for call in session.calls:
            for invocation in call.invocations:
                if invocation.version_probe and invocation.output:
                    observed_versions.update(_VERSION_RE.findall(invocation.output))
        for index, call in enumerate(session.calls):
            if not call.queries:
                continue
            event = _classify_event(session, index, call)
            events.append(event)
            commands.update(event["commands"])
            shapes.update(event["target_shapes"])
            options.update(event["options"])
            coissued.update(event["coissued_families"].keys())
            later.update(event["later_families"].keys())
            classifications[event["classification"]] += 1
            if not event["coissued_families"]:
                signals["clean_codeq_only_events"] += 1
                if event["returned_path_count"]:
                    signals["clean_events_exposing_paths"] += 1
                if event["signals"]["returned_path_read"]:
                    signals["clean_events_reading_returned_path"] += 1
                if event["signals"]["returned_path_edited"]:
                    signals["clean_events_editing_returned_path"] += 1
            if event["signals"]["later_search"]:
                signals["same_target_search_events"] += 1

    events.sort(key=lambda item: item["id"])
    total_queries = sum(int(event["query_count"]) for event in events)
    paired = sum(1 for event in events if event["output_paired_by_call_id"])
    return {
        "schema_version": 1,
        "parser_version": PARSER_VERSION,
        "codeq_versions_observed": sorted(observed_versions),
        "corpus": {
            "requested_window": {"since": since or None, "until": until or None},
            "observed_window": {"start": min(dates) if dates else None, "end": max(dates) if dates else None},
            "sessions_with_codeq": len(sessions),
            "working_directories": len(working_directories),
            "records": sum(session.record_count for session in sessions),
            "outer_tool_calls": outer_tool_calls,
            "codeq_call_events": len(events),
            "codeq_queries": total_queries,
            "paired_output_events": paired,
        },
        "aggregates": {
            "commands": _counter_dict(commands),
            "target_shapes": _counter_dict(shapes),
            "options": _counter_dict(options),
            "coissued_follow_up_family_events": _counter_dict(coissued),
            "later_follow_up_family_events": _counter_dict(later),
            "utility_signals": _counter_dict(signals),
            "classifications": _counter_dict(classifications),
        },
        "events": events,
        "attribution_limitations": [
            "Signals are observational weak labels; they do not establish that codeq caused task success or failure.",
            "A later read, edit, search, or Git call may serve a different purpose even within the same user turn.",
            "Parallel or chained codeq queries share event-level path evidence unless indexed output "
            "markers pair nested invocations.",
            "Mixed shell invocations containing codeq and another tool do not attribute shared output paths to codeq.",
            "Path matching uses normalized repository-relative suffixes and can be ambiguous when only "
            "a basename is available.",
            "Same-target search attribution uses normalized target terms; natural-language and regex "
            "reformulations may be missed or overmatched.",
            "Truncated, missing, or unfamiliar tool output remains unclassified rather than being treated as a miss.",
        ],
        "claim_boundary": (
            "This replay measures observed tool-use sequences, not a causal A/B improvement in task success."
        ),
    }


def render_markdown(data: dict[str, Any]) -> str:
    corpus = data["corpus"]
    aggregates = data["aggregates"]
    window = corpus["observed_window"]
    versions = data.get("codeq_versions_observed") or ["not observable"]
    rows = [
        ("Sessions", corpus["sessions_with_codeq"]),
        ("Working directories", corpus["working_directories"]),
        ("Outer codeq call events", corpus["codeq_call_events"]),
        ("Non-help codeq queries", corpus["codeq_queries"]),
        ("Outputs paired by call ID", corpus["paired_output_events"]),
    ]
    lines = [
        "# codeq downstream agent-utility replay",
        "",
        "This privacy-preserving replay reports observational tool-use signals. It does **not** claim "
        "that codeq caused task success, failure, or an A/B improvement.",
        "",
        f"Corpus window: `{window['start'] or 'unknown'}` through `{window['end'] or 'unknown'}`. "
        f"Parser version: `{data['parser_version']}`. Observable codeq versions: `{', '.join(versions)}`.",
        "",
        "| Measure | Count |",
        "| --- | ---: |",
        *(f"| {name} | {value} |" for name, value in rows),
        "",
        "## Aggregates",
        "",
        f"- Commands: `{json.dumps(aggregates['commands'], sort_keys=True)}`",
        f"- Target shapes: `{json.dumps(aggregates['target_shapes'], sort_keys=True)}`",
        f"- Options: `{json.dumps(aggregates['options'], sort_keys=True)}`",
        "- Co-issued follow-up family events: "
        f"`{json.dumps(aggregates['coissued_follow_up_family_events'], sort_keys=True)}`",
        f"- Later follow-up family events: `{json.dumps(aggregates['later_follow_up_family_events'], sort_keys=True)}`",
        f"- Utility signals: `{json.dumps(aggregates['utility_signals'], sort_keys=True)}`",
        f"- Downstream classifications: `{json.dumps(aggregates['classifications'], sort_keys=True)}`",
        "",
        "Classifications are event-level when one outer tool call launches multiple codeq queries. "
        "Searches that only repeat returned paths are verification; a new path must be consumed by a "
        "later read/edit to count as complemented or compensated/missed evidence.",
        "",
        "## Known attribution limitations",
        "",
        *(f"- {item}" for item in data["attribution_limitations"]),
        "",
        "Privacy: this artifact contains no prompts, query text, source text, raw repository paths, raw "
        "working directories, or session paths.",
        "",
        "Re-run the benchmark with:",
        "",
        "```bash",
        "uv run python benchmarks/agent_utility_benchmark.py "
        "--output benchmarks/results/agent-utility.json --markdown benchmarks/agent-utility.md",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay downstream use of codeq results from Codex JSONL sessions.")
    parser.add_argument("--codex-root", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument("--since", default="", help="Include sessions on or after YYYY-MM-DD.")
    parser.add_argument("--until", default="", help="Include sessions on or before YYYY-MM-DD.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args()
    sessions = load_sessions(args.codex_root, args.since, args.until)
    data = summarize(sessions, args.since, args.until)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render_markdown(data), encoding="utf-8")
    print(json.dumps({"corpus": data["corpus"], "aggregates": data["aggregates"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
