from __future__ import annotations

import fnmatch
import os
import re
import shlex
import shutil
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .baseanalysis import extract_base_declarations
from .contracts import (
    CONTEXT_SECTION_KEYS,
    CONTEXT_SECTION_VALUES,
    EVIDENCE_BASE_SIDE_LEXICAL,
    EVIDENCE_CURRENT_SEMANTIC,
    EVIDENCE_LEXICAL,
    EVIDENCE_SEMANTIC,
    QueryBudget,
    TEST_EVIDENCE_DIRECT_REFERENCE,
    TEST_EVIDENCE_EXACT_LEXICAL,
    TEST_EVIDENCE_MODULE_IMPORT,
    TEST_EVIDENCE_SEMANTIC_CALLER,
    bounded_text,
)
from .dynamic import (
    classify_dynamic_references,
    classify_python_call_reference,
    is_python_property_definition,
)
from .gitdiff import (
    git_changed_files,
    git_changed_ranges,
    git_merge_base,
    git_resolve_commit,
    git_show_file,
    git_untracked_files,
    merge_ranges,
    whole_file_range,
)
from .ftssearch import FtsUnavailable, WorkspaceFtsIndex, lexical_terms
from .lsp import LspError, LspProcess
from .textsearch import git_text_search
from .topology import extract_imports, importer_candidate_hits, resolve_import_specifier
from .util import (
    exact_definition_hits,
    fuzzy_score,
    git_visible_files,
    guess_symbol_column,
    identifier_tokens,
    is_test_path,
    language_for,
    lexical_hits,
    lsp_location,
    parse_target,
    path_to_uri,
    path_target_intent,
    source_snippet,
    symbol_kind,
    uri_to_path,
)


@dataclass(frozen=True)
class Project:
    root: Path
    family: str


def _skip_dir(name: str) -> bool:
    return name in {
        ".git", ".hg", ".svn", ".venv", "venv", "node_modules", ".next",
        "dist", "build", "coverage", "__pycache__", ".mypy_cache", ".pytest_cache",
        "Quant-worktrees", "worktrees",
    }


def discover_projects(root: Path) -> list[Project]:
    root = root.resolve()
    projects: set[Project] = set()
    max_depth = 4
    for dirpath, dirnames, filenames in os.walk(root):
        path = Path(dirpath)
        try:
            depth = len(path.relative_to(root).parts)
        except ValueError:
            continue
        dirnames[:] = [
            d for d in dirnames
            if not _skip_dir(d) and not (d.startswith(".") and d not in {".github"})
        ]
        if depth >= max_depth:
            dirnames[:] = []
        if path != root and (path / ".git").is_file():
            dirnames[:] = []
            continue
        names = set(filenames)
        if "pyproject.toml" in names:
            try:
                text = (path / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if "[tool.basedpyright]" in text or "[tool.pyright]" in text or "[project]" in text:
                projects.add(Project(path.resolve(), "python"))
        if "tsconfig.json" in names:
            projects.add(Project(path.resolve(), "typescript"))
    return sorted(projects, key=lambda p: (p.family, len(p.root.parts), str(p.root)))


def _flatten_document_symbols(raw: list[dict[str, Any]], path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def visit(item: dict[str, Any], container: str = "") -> None:
        if "location" in item:
            location = lsp_location(item.get("location"))
            if location:
                out.append({
                    "name": item.get("name", ""),
                    "kind": symbol_kind(item.get("kind")),
                    "container": item.get("containerName") or container,
                    **location,
                    "range": item.get("location", {}).get("range", {}),
                    "source": "lsp",
                    "origin": "document",
                })
            return
        rng = item.get("selectionRange") or item.get("range") or {}
        start = rng.get("start", {})
        full = item.get("range") or rng
        entry = {
            "name": item.get("name", ""),
            "kind": symbol_kind(item.get("kind")),
            "container": container,
            "path": str(path.resolve()),
            "line": int(start.get("line", 0)) + 1,
            "column": int(start.get("character", 0)) + 1,
            "range": full,
            "source": "lsp",
            "origin": "document",
        }
        out.append(entry)
        child_container = ".".join(x for x in (container, item.get("name", "")) if x)
        for child in item.get("children") or []:
            visit(child, child_container)

    for item in raw:
        visit(item)
    return out


def _workspace_symbol_entry(item: dict[str, Any]) -> dict[str, Any] | None:
    location = item.get("location")
    if not isinstance(location, dict) or not location.get("range"):
        return None
    loc = lsp_location(location)
    if not loc:
        return None
    return {
        "name": item.get("name", ""),
        "kind": symbol_kind(item.get("kind")),
        "container": item.get("containerName") or "",
        **loc,
        "source": "lsp",
        "origin": "workspace",
    }


def _definition_priority(item: dict[str, Any]) -> int:
    kind = str(item.get("kind") or "")
    if kind in {"Function", "Method", "Class", "Interface", "Enum", "Constructor", "Struct", "TypeParameter"}:
        base = 30
    elif kind in {"Constant", "Property", "Field"}:
        base = 20
    elif kind == "Variable":
        base = 10
    else:
        base = 0
    if item.get("origin") == "document":
        base += 2
    return base


def _query_seeks_tests(query: str, kind: str | None) -> bool:
    if (kind or "").strip().lower() == "test":
        return True
    lowered = query.lower()
    return any(
        cue in lowered
        for cue in ("test", "tests", "pytest", "fixture", "mock", "spec", "测试")
    )


def _agent_ranking_adjustment(query: str, kind: str | None, item: dict[str, Any]) -> int:
    """Prefer production definitions unless the query explicitly asks for tests."""
    path = str(item.get("path") or "")
    adjustment = 0
    if is_test_path(path) and not _query_seeks_tests(query, kind):
        adjustment -= 2500
    lowered = path.lower()
    if any(segment in lowered for segment in ("/generated/", "/fixtures/", "/snapshots/")):
        adjustment -= 700
    if "/examples/" in lowered:
        adjustment -= 500
    if item.get("origin") == "document":
        adjustment += 150
    return adjustment


def _call_item_entry(item: dict[str, Any]) -> dict[str, Any]:
    path = uri_to_path(item.get("uri", ""))
    selection = item.get("selectionRange") or item.get("range") or {}
    start = selection.get("start", {})
    return {
        "name": item.get("name", ""),
        "kind": symbol_kind(item.get("kind")),
        "path": str(path),
        "line": int(start.get("line", 0)) + 1,
        "column": int(start.get("character", 0)) + 1,
        "detail": item.get("detail") or "",
    }


def _deduplicate_locations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve first-seen evidence while collapsing duplicate locations."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, str]] = set()
    for item in items:
        key = (
            str(item.get("path") or ""),
            int(item.get("line") or 0),
            int(item.get("column") or 1),
            str(item.get("name") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _known_section_disclosure(
    items: list[dict[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    returned = items[:limit]
    total = len(items)
    return returned, {
        "returned_count": len(returned),
        "total_count": total,
        "total_lower_bound": total,
        "total_is_exact": True,
        "truncated": len(returned) < total,
    }


def _bounded_section_disclosure(
    items: list[dict[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Describe a limit+1 probe whose full total is unknown when it overflows."""
    returned = items[:limit]
    overflow = len(items) > len(returned)
    total = None if overflow else len(items)
    return returned, {
        "returned_count": len(returned),
        "total_count": total,
        "total_lower_bound": len(items),
        "total_is_exact": not overflow,
        "truncated": overflow,
    }


_CALL_HIERARCHY_KIND_CODES = {
    "Class": 5,
    "Method": 6,
    "Constructor": 9,
    "Function": 12,
}

_COMMON_SHORT_TEST_TOKENS = {
    "call",
    "data",
    "get",
    "item",
    "load",
    "main",
    "name",
    "run",
    "save",
    "set",
    "test",
    "value",
}


def _lexical_test_candidate_allowed(symbol_name: str) -> bool:
    normalized = symbol_name.strip()
    return bool(
        len(normalized) >= 5
        and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", normalized)
        and normalized.lower() not in _COMMON_SHORT_TEST_TOKENS
    )


class Workspace:
    def __init__(self, root: Path, timeout: float = 15.0):
        self.root = root.resolve()
        self.timeout = timeout
        self.projects = discover_projects(self.root)
        self._sessions: dict[Project, LspProcess] = {}
        self._session_locks: dict[Project, threading.Lock] = {}
        self._closed = False
        self._prewarmed: set[tuple[str, str, int]] = set()
        self._prewarm_flights: dict[tuple[str, str, int], threading.Event] = {}
        self._document_symbol_cache: OrderedDict[Path, tuple[tuple[int, int], list[dict[str, Any]]]] = OrderedDict()
        self._document_symbol_flights: dict[tuple[Path, tuple[int, int]], threading.Event] = {}
        self._fts_index = WorkspaceFtsIndex(self.root)
        self._metrics: dict[str, int] = {
            "sessions_started": 0,
            "document_symbols_hit": 0,
            "document_symbols_miss": 0,
            "document_symbols_waited": 0,
            "document_symbols_evicted": 0,
            "prewarm_files": 0,
            "prewarm_probes": 0,
            "prewarm_early_stops": 0,
        }
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._document_symbol_cache.clear()
            flights = [*self._document_symbol_flights.values(), *self._prewarm_flights.values()]
            self._document_symbol_flights.clear()
            self._prewarm_flights.clear()
        for flight in flights:
            flight.set()
        self._fts_index.close()
        for session in sessions:
            session.close()

    def session_stats(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "family": project.family,
                    "root": str(project.root),
                    "pid": session.pid,
                    "alive": session.alive(),
                    "server": session.name,
                    "request_count": session.request_count,
                }
                for project, session in self._sessions.items()
            ]

    def has_active_lsp(self) -> bool:
        with self._lock:
            return any(session.alive() for session in self._sessions.values())

    def metrics_snapshot(self) -> dict[str, int]:
        with self._lock:
            metrics = dict(self._metrics)
            metrics["lsp_request_count"] = sum(session.request_count for session in self._sessions.values())
            metrics["document_symbol_cache_entries"] = len(self._document_symbol_cache)
            return metrics

    @staticmethod
    def _file_marker(path: Path) -> tuple[int, int]:
        try:
            stat = path.stat()
        except OSError:
            return (0, 0)
        return (int(stat.st_mtime_ns), int(stat.st_size))

    def _document_symbols(
        self,
        path: Path,
        *,
        project: Project | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        resolved = path.resolve()
        wait_timeout = max(1.0, timeout if timeout is not None else self.timeout)
        while True:
            marker = self._file_marker(resolved)
            flight_key = (resolved, marker)
            with self._lock:
                cached = self._document_symbol_cache.get(resolved)
                if cached is not None and cached[0] == marker:
                    self._document_symbol_cache.move_to_end(resolved)
                    self._metrics["document_symbols_hit"] += 1
                    return [dict(item) for item in cached[1]]
                flight = self._document_symbol_flights.get(flight_key)
                if flight is None:
                    flight = threading.Event()
                    self._document_symbol_flights[flight_key] = flight
                    self._metrics["document_symbols_miss"] += 1
                    break
                self._metrics["document_symbols_waited"] += 1
            if not flight.wait(timeout=wait_timeout):
                raise LspError(f"timed out waiting for document symbols: {resolved}")

        try:
            selected = project or self.project_for_path(resolved)
            if selected is None:
                return []
            raw = self._session(selected).document_symbols(resolved, timeout=timeout)
            flattened = _flatten_document_symbols(raw, resolved)
            with self._lock:
                if self._closed:
                    raise LspError(f"workspace is closed: {self.root}")
                self._document_symbol_cache[resolved] = (marker, flattened)
                self._document_symbol_cache.move_to_end(resolved)
                while len(self._document_symbol_cache) > 256:
                    self._document_symbol_cache.popitem(last=False)
                    self._metrics["document_symbols_evicted"] += 1
            return [dict(item) for item in flattened]
        finally:
            with self._lock:
                completed = self._document_symbol_flights.pop(flight_key, None)
                if completed is not None:
                    completed.set()

    def _server_command(self, project: Project) -> tuple[list[str], str] | None:
        if project.family == "python":
            for exe in ("basedpyright-langserver", "pyright-langserver"):
                found = shutil.which(exe)
                if found:
                    return [found, "--stdio"], exe
            return None
        global_tls = shutil.which("typescript-language-server")
        candidates = [
            Path(global_tls) if global_tls else None,
            project.root / "node_modules/.bin/typescript-language-server",
            Path(__file__).resolve().parents[2] / ".vendor/node_modules/.bin/typescript-language-server",
        ]
        for candidate in candidates:
            if candidate and candidate.exists():
                return [str(candidate), "--stdio"], "typescript-language-server"
        return None

    def _session(self, project: Project) -> LspProcess:
        with self._lock:
            existing = self._sessions.get(project)
            if existing and existing.alive():
                return existing
            if self._closed:
                raise LspError(f"workspace is closed: {self.root}")
            start_lock = self._session_locks.setdefault(project, threading.Lock())

        # Language-server initialization can take several seconds. Serialize only
        # the same project so a multi-package find may start independent projects
        # concurrently instead of paying every cold start in sequence.
        with start_lock:
            with self._lock:
                existing = self._sessions.get(project)
                if existing and existing.alive():
                    return existing
                if self._closed:
                    raise LspError(f"workspace is closed: {self.root}")
            server = self._server_command(project)
            if server is None:
                raise LspError(f"no {project.family} language server available for {project.root}")
            command, name = server
            session = LspProcess(command, project.root, name=name, timeout=self.timeout)
            with self._lock:
                if self._closed:
                    session.close()
                    raise LspError(f"workspace is closed: {self.root}")
                self._sessions[project] = session
                self._metrics["sessions_started"] += 1
                return session

    def project_for_path(self, path: Path) -> Project | None:
        path = path.resolve()
        family = "python" if path.suffix in {".py", ".pyi"} else "typescript" if path.suffix.lower() in {
            ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"
        } else None
        if not family:
            return None
        matches: list[Project] = []
        for project in self.projects:
            if project.family != family:
                continue
            try:
                path.relative_to(project.root)
            except ValueError:
                continue
            matches.append(project)
        if matches:
            return max(matches, key=lambda p: len(p.root.parts))
        if any(project.family == family for project in self.projects):
            return None
        return Project(self.root, family)

    def _is_repo_path(self, path: str | Path) -> bool:
        try:
            relative = Path(path).resolve().relative_to(self.root)
        except ValueError:
            return False
        return not any(
            part in {"node_modules", ".venv", "venv", ".next", "dist", "build"}
            for part in relative.parts
        )

    def _matches_path_prefixes(self, path: str | Path, prefixes: tuple[str, ...]) -> bool:
        if not prefixes:
            return True
        try:
            relative = Path(path).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return False
        for raw_prefix in prefixes:
            prefix_path = Path(raw_prefix).expanduser()
            try:
                if prefix_path.is_absolute():
                    prefix = prefix_path.resolve().relative_to(self.root).as_posix()
                else:
                    prefix = (self.root / prefix_path).resolve().relative_to(self.root).as_posix()
            except ValueError:
                continue
            prefix = prefix.rstrip("/")
            if prefix in {"", "."}:
                return True
            if relative == prefix or relative.startswith(prefix + "/"):
                return True
        return False

    def _matches_find_scope(
        self,
        path: str | Path,
        *,
        paths: tuple[str, ...],
        globs: tuple[str, ...],
        exclude_tests: bool,
    ) -> bool:
        candidate = Path(path).resolve()
        if exclude_tests and is_test_path(candidate):
            return False
        if not self._matches_path_prefixes(candidate, paths):
            return False
        if not globs:
            return True
        try:
            relative = candidate.relative_to(self.root).as_posix()
        except ValueError:
            return False
        return any(
            fnmatch.fnmatchcase(relative, pattern)
            or fnmatch.fnmatchcase(candidate.name, pattern)
            for pattern in globs
        )

    def _selection_command(self, item: dict[str, Any]) -> str:
        path = Path(str(item.get("path") or "")).resolve()
        try:
            rendered_path = path.relative_to(self.root).as_posix()
        except ValueError:
            rendered_path = str(path)
        location = f"{rendered_path}:{int(item.get('line') or 1)}:{int(item.get('column') or 1)}"
        return shlex.join(["codeq", "context", location])

    def _file_context_command(self, path: str | Path, *options: str) -> str:
        resolved = Path(path).resolve()
        try:
            rendered_path = resolved.relative_to(self.root).as_posix()
        except ValueError:
            rendered_path = str(resolved)
        return shlex.join(["codeq", "context", rendered_path, *options])

    def _with_selection_commands(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**item, "selection_command": self._selection_command(item)}
            for item in items
        ]

    def _module_qualifier_matches(self, path: str | Path, qualifier: list[str]) -> bool:
        if not qualifier:
            return True
        try:
            relative = Path(path).resolve().relative_to(self.root).with_suffix("")
        except ValueError:
            return False
        path_parts = list(relative.parts)
        if path_parts and path_parts[-1] == "__init__":
            path_parts.pop()
        return len(path_parts) >= len(qualifier) and path_parts[-len(qualifier):] == qualifier

    def _candidate_projects(self, hits: list[dict[str, Any]]) -> list[Project]:
        selected: set[Project] = set()
        for hit in hits:
            project = self.project_for_path(Path(hit["path"]))
            if project:
                selected.add(project)
        return sorted(selected or set(self.projects), key=lambda p: (p.family, str(p.root)))

    def _exact_document_candidates(
        self,
        name: str,
        *,
        limit: int = 80,
        path_filter: Callable[[Path], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Map exact declaration-looking hits back to LSP document symbols.

        This bypasses workspace/symbol indexing for cold-start correctness while
        still requiring the language server to confirm the semantic symbol.
        """
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, int, str]] = set()
        for hit in exact_definition_hits(self.root, name, limit=limit, path_filter=path_filter):
            path = Path(hit["path"])
            project = self.project_for_path(path)
            if project is None:
                continue
            try:
                symbols = self._document_symbols(path, project=project)
            except (LspError, OSError):
                continue
            exact = [symbol for symbol in symbols if symbol.get("name") == name]
            if not exact:
                continue
            hit_line = int(hit["line"])
            exact.sort(
                key=lambda symbol: (
                    abs(int(symbol["line"]) - hit_line),
                    -_definition_priority(symbol),
                    int(symbol["line"]),
                )
            )
            for symbol in exact:
                key = (symbol["path"], int(symbol["line"]), str(symbol.get("name") or ""))
                if key in seen:
                    continue
                seen.add(key)
                out.append({**symbol, "exact_definition": True})
        return out

    def find(
        self,
        query: str,
        limit: int = 20,
        kind: str | None = None,
        *,
        text: bool = False,
        paths: tuple[str, ...] = (),
        globs: tuple[str, ...] = (),
        exclude_tests: bool = False,
    ) -> dict[str, Any]:
        path_filters = tuple(value for value in paths if value.strip())
        glob_filters = tuple(value for value in globs if value.strip())
        if text:
            return git_text_search(
                self.root,
                query,
                limit=limit,
                paths=path_filters,
                globs=glob_filters,
                exclude_tests=exclude_tests,
            )

        def in_scope(path: str | Path) -> bool:
            return self._matches_find_scope(
                path,
                paths=path_filters,
                globs=glob_filters,
                exclude_tests=exclude_tests,
            )

        reference = self._path_reference(query)
        if reference is not None:
            path = Path(reference["path"])
            common = {
                "query": query,
                "path": str(path),
                "results": [],
                "result_count": 0,
                "total_candidates": 0,
                "truncated": False,
                "errors": [],
            }
            ambiguous_paths = [Path(value) for value in reference.get("ambiguous_paths", [])]
            if ambiguous_paths:
                ambiguous = self._ambiguous_file_result(query, ambiguous_paths)
                return {
                    **common,
                    **ambiguous,
                }
            if not reference["inside_repo"]:
                return {
                    **common,
                    "status": "not_found",
                    "reason": f"path is outside repository root: {path}",
                }
            if not reference["exists"]:
                return {
                    **common,
                    "status": "not_found",
                    "reason": f"file not found: {path}",
                }
            if reference["line"] is not None:
                return {
                    **common,
                    "status": "unsupported_target",
                    "reason": "use `codeq context PATH:LINE[:COLUMN]` for a source location",
                }
            if language_for(path) is None:
                return {
                    **common,
                    "status": "unsupported_language",
                    "reason": f"unsupported source language: {path.suffix or '<no extension>'}",
                }
            return {
                **common,
                "status": "unsupported_target",
                "reason": "use `codeq context FILE` for a source-file target",
            }

        terms = lexical_terms(query)
        if len(terms) >= 2:
            filters = {
                "paths": list(path_filters),
                "globs": list(glob_filters),
                "exclude_tests": exclude_tests,
            }
            common = {
                "mode": "fts5",
                "query": query,
                "kind": kind,
                "paths": list(path_filters),
                "filters": filters,
                "results": [],
                "result_count": 0,
                "total_candidates": 0,
                "truncated": False,
                "errors": [],
            }
            if kind:
                return {
                    **common,
                    "status": "unsupported_target",
                    "reason": (
                        "--kind is unavailable for multi-token file discovery; "
                        "use an exact identifier for symbol filtering"
                    ),
                }
            try:
                search = self._fts_index.search(query)
            except FtsUnavailable as exc:
                return {
                    **common,
                    "status": "unsupported_capability",
                    "reason": str(exc),
                }
            matching = [hit for hit in search.hits if in_scope(hit.path)]
            returned = [
                {
                    "name": hit.relative_path,
                    "kind": "File",
                    "container": "",
                    "path": str(hit.path),
                    "relative_path": hit.relative_path,
                    "line": 1,
                    "column": 1,
                    "source": "fts5",
                    "evidence": EVIDENCE_LEXICAL,
                    "is_test": is_test_path(hit.path),
                    "bm25": hit.bm25,
                    "selection_command": self._file_context_command(hit.path),
                }
                for hit in matching[: max(1, limit)]
            ]
            return {
                **common,
                "status": "ok",
                "results": returned,
                "result_count": len(returned),
                "total_candidates": len(matching),
                "truncated": len(returned) < len(matching),
                "ranking": {
                    "engine": "sqlite_fts5_bm25",
                    "terms": search.terms,
                    "match_expression": search.match_expression,
                    "tie_breaker": "relative_path",
                },
                "index": {
                    "storage": "memory_contentless",
                    "file_count": search.file_count,
                    "source_bytes": search.source_bytes,
                    "index_bytes": search.index_bytes,
                    "refreshed": search.refreshed,
                    "build_ms": round(search.build_ms, 3),
                    "query_ms": round(search.query_ms, 3),
                },
            }

        hits = lexical_hits(
            self.root,
            query,
            limit=max(80, limit * 8),
            path_filter=in_scope,
        )
        projects = self._candidate_projects(hits)
        tokens = identifier_tokens(query)
        search_terms = tokens[:3] or [query]
        results: list[dict[str, Any]] = []
        errors: list[str] = []

        # Exact identifiers get a cold-start-safe definition path that does not
        # depend on workspace/symbol having finished background indexing.
        if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", query.strip()):
            results.extend(
                item
                for item in self._exact_document_candidates(
                    query.strip(),
                    limit=max(40, limit * 4),
                    path_filter=in_scope,
                )
                if in_scope(item["path"])
            )

        def search_project(project: Project) -> tuple[list[dict[str, Any]], str | None]:
            try:
                session = self._session(project)
                # typescript-language-server can transiently answer workspace/symbol
                # with "No Project" until at least one relevant document is open.
                # Prime only bounded lexical-hit files from this project.
                if project.family == "typescript":
                    primed = 0
                    for hit in hits:
                        path = Path(hit["path"])
                        if self.project_for_path(path) != project:
                            continue
                        try:
                            self._document_symbols(path, project=project, timeout=min(self.timeout, 5.0))
                        except (LspError, OSError):
                            continue
                        primed += 1
                        if primed >= 4:
                            break
                found: list[dict[str, Any]] = []
                seen: set[tuple[str, str, int]] = set()
                for term in search_terms:
                    try:
                        items = session.workspace_symbols(term, timeout=self.timeout)
                    except LspError as exc:
                        if "No Project" in str(exc) and project.family == "typescript":
                            # Document-symbol fallback below remains authoritative for
                            # files already discovered lexically; do not expose a
                            # transient project-initialization error to the agent.
                            continue
                        raise
                    for item in items:
                        entry = _workspace_symbol_entry(item)
                        if not entry or not in_scope(entry["path"]):
                            continue
                        key = (entry["path"], entry["name"], entry["line"])
                        if key in seen:
                            continue
                        seen.add(key)
                        found.append(entry)
                return found, None
            except (LspError, OSError) as exc:
                return [], str(exc)

        if projects:
            with ThreadPoolExecutor(max_workers=min(4, len(projects))) as pool:
                futures = [pool.submit(search_project, p) for p in projects]
                for future in as_completed(futures):
                    found, error = future.result()
                    results.extend(found)
                    if error:
                        errors.append(error)

        # Initial LSP workspace indexing can lag. Use rg hits to inspect only the
        # relevant documents and map hit lines back to semantic document symbols.
        for hit in hits[: min(16, len(hits))]:
            path = Path(hit["path"])
            project = self.project_for_path(path)
            if not project:
                continue
            try:
                symbols = self._document_symbols(path, project=project)
            except (LspError, OSError):
                continue
            hit_line = hit["line"]
            mapped_hit = False
            for symbol in symbols:
                rng = symbol.get("range") or {}
                start = int((rng.get("start") or {}).get("line", symbol["line"] - 1)) + 1
                end = int((rng.get("end") or {}).get("line", start - 1)) + 1
                score = fuzzy_score(query, symbol["name"], symbol.get("container", ""), symbol["path"])
                if start <= hit_line <= end:
                    mapped_hit = True
                    results.append(
                        {
                            **symbol,
                            "lexical_match_score": max(
                                int(symbol.get("lexical_match_score", 0)),
                                int(hit.get("match_score", 1)),
                            ),
                            "match_text": hit.get("text", ""),
                        }
                    )
                elif score > 0:
                    results.append(symbol)
            if not mapped_hit and symbols:
                # Documentation normally precedes its declaration. Prefer the
                # closest following semantic symbol before falling back to a
                # symmetric neighborhood; this maps comments like "SSE streaming"
                # to streamBacktestLogs rather than to the previous function.
                following = [
                    symbol
                    for symbol in symbols
                    if 0 <= int(symbol["line"]) - int(hit_line) <= 12
                ]
                if following:
                    nearest = min(
                        following,
                        key=lambda symbol: (
                            int(symbol["line"]) - int(hit_line),
                            -_definition_priority(symbol),
                        ),
                    )
                    max_distance = 12
                else:
                    nearest = min(
                        symbols,
                        key=lambda symbol: (
                            abs(int(symbol["line"]) - int(hit_line)),
                            -_definition_priority(symbol),
                        ),
                    )
                    max_distance = 8
                if abs(int(nearest["line"]) - int(hit_line)) <= max_distance:
                    results.append(
                        {
                            **nearest,
                            "lexical_match_score": max(
                                int(nearest.get("lexical_match_score", 0)),
                                int(hit.get("match_score", 1)),
                            ),
                            "match_text": hit.get("text", ""),
                        }
                    )

        if not results:
            for hit in hits:
                results.append({
                    "name": (hit.get("text") or "").strip()[:160],
                    "kind": "Text",
                    "container": "",
                    "path": hit["path"],
                    "line": hit["line"],
                    "column": 1,
                    "source": "rg",
                })

        dedup: dict[tuple[str, int, str], dict[str, Any]] = {}
        for item in results:
            key = (item["path"], int(item["line"]), item.get("name", ""))
            semantic_score = fuzzy_score(query, item.get("name", ""), item.get("container", ""), item["path"])
            lexical_boost = min(3500, int(item.get("lexical_match_score", 0)) * 700)
            score = semantic_score + lexical_boost + _agent_ranking_adjustment(query, kind, item)
            enriched = {**item, "score": score}
            current = dedup.get(key)
            if (
                current is None
                or score > int(current.get("score", 0))
                or (
                    score == int(current.get("score", 0))
                    and _definition_priority(enriched) > _definition_priority(current)
                )
            ):
                dedup[key] = enriched
        ordered = sorted(
            dedup.values(),
            key=lambda item: (
                -int(item.get("score", 0)),
                -_definition_priority(item),
                item["path"],
                int(item["line"]),
            ),
        )
        ordered = [item for item in ordered if self._is_repo_path(item["path"])]
        ordered = [item for item in ordered if in_scope(item["path"])]
        if kind:
            requested = kind.strip().lower()
            if requested == "function":
                allowed = {"function", "method", "constructor"}
                ordered = [item for item in ordered if str(item.get("kind", "")).lower() in allowed]
            elif requested == "class":
                allowed = {"class", "interface", "struct", "enum"}
                ordered = [item for item in ordered if str(item.get("kind", "")).lower() in allowed]
            elif requested == "test":
                ordered = [item for item in ordered if is_test_path(item["path"])]
            else:
                ordered = [item for item in ordered if str(item.get("kind", "")).lower() == requested]
        returned = self._with_selection_commands(ordered[:limit])
        return {
            "status": "ok",
            "query": query,
            "kind": kind,
            "paths": list(path_filters),
            "filters": {
                "paths": list(path_filters),
                "globs": list(glob_filters),
                "exclude_tests": exclude_tests,
            },
            "results": returned,
            "result_count": len(returned),
            "total_candidates": len(ordered),
            "truncated": len(returned) < len(ordered),
            "errors": errors[:4],
        }

    def _resolve_qualified(
        self,
        target: str,
        *,
        semantic_paths: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        parts = [part for part in target.split(".") if part]
        if len(parts) < 2:
            return None
        container_name = parts[-2]
        member_name = parts[-1]
        matches: list[dict[str, Any]] = []
        exact_leaf_candidates = [
            symbol
            for symbol in self._exact_document_candidates(member_name, limit=80)
            if self._matches_path_prefixes(symbol["path"], semantic_paths)
        ]

        # A fully qualified target may end in a top-level class/function rather
        # than a container/member pair, for example package.domain.models.Candidate.
        # Accept only LSP-confirmed exact declarations whose semantic suffix and
        # module/file suffix both match the requested target.
        for symbol in exact_leaf_candidates:
            semantic_parts = [
                *[part for part in str(symbol.get("container") or "").split(".") if part],
                member_name,
            ]
            if len(parts) < len(semantic_parts) or parts[-len(semantic_parts):] != semantic_parts:
                continue
            module_parts = parts[:-len(semantic_parts)]
            if self._module_qualifier_matches(symbol["path"], module_parts):
                matches.append({**symbol, "score": 10000})

        containers = [
            item
            for item in self._exact_document_candidates(container_name, limit=80)
            if item.get("name") == container_name
            and item.get("kind") in {"Class", "Interface", "Struct", "Enum", "Namespace", "Module"}
            and self._matches_path_prefixes(item["path"], semantic_paths)
        ]
        seen_files: set[str] = set()
        for container in containers:
            path = Path(container["path"])
            if str(path) in seen_files:
                continue
            seen_files.add(str(path))
            project = self.project_for_path(path)
            if project is None:
                continue
            try:
                symbols = self._document_symbols(path, project=project)
            except (LspError, OSError):
                continue
            for symbol in symbols:
                if symbol.get("name") != member_name:
                    continue
                combined = ".".join(
                    part for part in (str(symbol.get("container") or ""), member_name) if part
                )
                semantic_parts = [part for part in combined.split(".") if part]
                if not semantic_parts or len(parts) < len(semantic_parts):
                    continue
                module_parts = parts[:-len(semantic_parts)]
                if parts[-len(semantic_parts):] == semantic_parts and self._module_qualifier_matches(
                    symbol["path"], module_parts
                ):
                    matches.append({**symbol, "score": 10000})
        if not matches:
            reason = (
                f"qualified member not found in {container_name}: {member_name}"
                if containers
                else f"qualified target not found: {target}"
            )
            return {
                "status": "not_found",
                "target": target,
                "reason": reason,
                # Stay fail-closed, but retain exact-name locations so a missing
                # module segment or wrong owner is recoverable without broad grep.
                "candidates": self._with_selection_commands(
                    sorted(
                        exact_leaf_candidates,
                        key=lambda item: (
                            str(item.get("container") or "").split(".")[-1:] != [container_name],
                            -_definition_priority(item),
                            item["path"],
                            int(item["line"]),
                        ),
                    )[:4]
                ),
            }
        deduplicated = {
            (str(item["path"]), int(item["line"]), str(item.get("name") or "")): item
            for item in matches
        }
        matches = list(deduplicated.values())
        matches.sort(key=lambda item: (-_definition_priority(item), item["path"], item["line"]))
        best_priority = _definition_priority(matches[0])
        top = [item for item in matches if _definition_priority(item) == best_priority]
        unique = {(item["path"], item["line"]) for item in top}
        if len(unique) > 1:
            return {
                "status": "ambiguous",
                "target": target,
                "candidates": self._with_selection_commands(top[:8]),
            }
        chosen = top[0]
        return {
            "status": "ok",
            "target": target,
            "symbol": chosen,
            "candidates": self._with_selection_commands(matches[1:5]),
        }

    def resolve(
        self,
        target: str,
        *,
        semantic_paths: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        reference = self._path_reference(target)
        if reference is not None:
            path = Path(reference["path"])
            ambiguous_paths = [Path(value) for value in reference.get("ambiguous_paths", [])]
            if ambiguous_paths:
                return self._ambiguous_file_result(target, ambiguous_paths)
            if not reference["inside_repo"]:
                return {
                    "status": "not_found",
                    "target": target,
                    "path": str(path),
                    "reason": f"path is outside repository root: {path}",
                }
            if not reference["exists"]:
                return {
                    "status": "not_found",
                    "target": target,
                    "path": str(path),
                    "reason": f"file not found: {path}",
                }
            language = language_for(path)
            if language is None:
                return {
                    "status": "unsupported_language",
                    "target": target,
                    "path": str(path),
                    "reason": f"unsupported source language: {path.suffix or '<no extension>'}",
                }
            if reference["line"] is None:
                return {
                    "status": "unsupported_target",
                    "target": target,
                    "path": str(path),
                    "reason": "source-file targets are supported by `codeq context`, not symbol tracing",
                }
            parsed = {
                "kind": "location",
                "path": str(path),
                "line": int(reference["line"]),
                "column": int(reference["column"] or 1),
            }
        else:
            parsed = parse_target(target, self.root)
        if parsed["kind"] == "location":
            path = Path(str(parsed["path"]))
            parsed_line = int(parsed["line"])
            parsed_column = int(parsed["column"])
            if not path.exists():
                return {"status": "not_found", "target": target, "reason": f"file not found: {path}"}
            project = self.project_for_path(path)
            if project is not None:
                try:
                    symbols = self._document_symbols(path, project=project)
                except (LspError, OSError):
                    symbols = []
                line = parsed_line
                column = parsed_column
                explicit_column = bool(re.search(r":\d+:\d+$", target))

                def contains(symbol: dict[str, Any]) -> bool:
                    rng = symbol.get("range") or {}
                    start = int((rng.get("start") or {}).get("line", symbol["line"] - 1)) + 1
                    end = int((rng.get("end") or {}).get("line", start - 1)) + 1
                    return start <= line <= end

                def contains_point(symbol: dict[str, Any]) -> bool:
                    if not contains(symbol):
                        return False
                    rng = symbol.get("range") or {}
                    start = rng.get("start") or {}
                    end = rng.get("end") or {}
                    start_line = int(start.get("line", symbol["line"] - 1)) + 1
                    end_line = int(end.get("line", start_line - 1)) + 1
                    column0 = max(0, column - 1)
                    if line == start_line and column0 < int(start.get("character", 0)):
                        return False
                    if line == end_line and column0 > int(end.get("character", column0)):
                        return False
                    return True

                def span_size(symbol: dict[str, Any]) -> tuple[int, int]:
                    rng = symbol.get("range") or {}
                    start = rng.get("start") or {}
                    end = rng.get("end") or {}
                    line_span = int(end.get("line", symbol["line"] - 1)) - int(start.get("line", symbol["line"] - 1))
                    char_span = int(end.get("character", 0)) - int(start.get("character", 0)) if line_span == 0 else 0
                    return max(0, line_span), max(0, char_span)

                requested_location = {
                    "path": str(path),
                    "line": line,
                    "column": column,
                    "source": "explicit",
                }
                containing = [symbol for symbol in symbols if contains(symbol)]
                if explicit_column:
                    definition_candidates: list[dict[str, Any]] = []
                    definition_enclosures: list[dict[str, Any] | None] = []
                    seen_raw_definitions: set[tuple[str, int, int]] = set()
                    seen_definitions: set[tuple[str, int, int, str]] = set()
                    try:
                        definitions = self._session(project).definitions(path, line, column)
                    except (LspError, OSError):
                        definitions = []
                    for raw_definition in definitions:
                        location = lsp_location(raw_definition)
                        if not location or not self._is_repo_path(location["path"]):
                            continue
                        raw_key = (
                            str(location.get("path") or ""),
                            int(location.get("line") or 1),
                            int(location.get("column") or 1),
                        )
                        if raw_key in seen_raw_definitions:
                            continue
                        seen_raw_definitions.add(raw_key)
                        definition_enclosures.append(self._semantic_symbol_at_location(location))
                        mapped = self._symbol_at_location(location)
                        key = (
                            str(mapped.get("path") or ""),
                            int(mapped.get("line") or 1),
                            int(mapped.get("column") or 1),
                            str(mapped.get("name") or ""),
                        )
                        if key in seen_definitions:
                            continue
                        seen_definitions.add(key)
                        definition_candidates.append(mapped)
                    if len(seen_raw_definitions) > 1:
                        enclosing = [item for item in definition_enclosures if item is not None]
                        enclosing_keys = {
                            (
                                str(item.get("path") or ""),
                                int(item.get("line") or 1),
                                str(item.get("name") or ""),
                                str(item.get("kind") or ""),
                            )
                            for item in enclosing
                        }
                        if len(enclosing) == len(definition_enclosures) and len(enclosing_keys) == 1:
                            return {
                                "status": "ok",
                                "target": target,
                                "symbol": enclosing[0],
                                "candidates": [],
                                "requested_location": requested_location,
                                "cursor_definition": True,
                                "cursor_definition_count": len(seen_raw_definitions),
                                "definition_note": (
                                    f"{len(seen_raw_definitions)} local definitions resolve inside "
                                    f"the same enclosing {str(enclosing[0].get('kind') or 'symbol').lower()}"
                                ),
                            }
                    if len(definition_candidates) == 1:
                        return {
                            "status": "ok",
                            "target": target,
                            "symbol": definition_candidates[0],
                            "candidates": [],
                            "requested_location": requested_location,
                            "cursor_definition": True,
                        }
                    if len(definition_candidates) > 1:
                        actionable = [
                            item
                            for item in definition_candidates
                            if not (
                                Path(str(item.get("path") or "")).resolve() == path.resolve()
                                and int(item.get("line") or 1) == line
                                and int(item.get("column") or 1) == column
                            )
                        ]
                        return {
                            "status": "ambiguous",
                            "target": target,
                            "candidates": self._with_selection_commands((actionable or definition_candidates)[:8]),
                            "requested_location": requested_location,
                            "reason": "multiple definitions found at requested cursor position",
                        }
                    point_matches = [symbol for symbol in containing if contains_point(symbol)]
                    if point_matches:
                        chosen = min(
                            point_matches,
                            key=lambda symbol: (span_size(symbol), -_definition_priority(symbol)),
                        )
                        return {
                            "status": "ok",
                            "target": target,
                            "symbol": {**chosen, "source": "lsp", "origin": "document"},
                            "candidates": [],
                            "requested_location": requested_location,
                            "cursor_definition": False,
                        }
                semantic = [
                    symbol
                    for symbol in containing
                    if symbol.get("kind") in {"Function", "Method", "Constructor", "Class", "Interface", "Struct", "Enum"}
                ]
                if semantic:
                    chosen = min(semantic, key=lambda symbol: (span_size(symbol), -_definition_priority(symbol)))
                    return {
                        "status": "ok",
                        "target": target,
                        "symbol": {**chosen, "source": "lsp", "origin": "document"},
                        "candidates": [],
                        "requested_location": {
                            "path": str(path),
                            "line": line,
                            "column": column,
                            "source": "explicit",
                        },
                        "cursor_definition": False,
                    }
            col = parsed_column
            if col <= 1:
                col = guess_symbol_column(path, parsed_line) + 1
            return {
                "status": "ok",
                "target": target,
                "symbol": {
                    "name": path.name,
                    "kind": "Location",
                    "container": "",
                    "path": str(path),
                    "line": parsed_line,
                    "column": col,
                    "source": "explicit",
                },
                "requested_location": {
                    "path": str(path),
                    "line": parsed_line,
                    "column": parsed_column,
                    "source": "explicit",
                },
                "cursor_definition": False,
            }

        qualified_target = bool(
            re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+", target)
        )
        if qualified_target:
            qualified = self._resolve_qualified(target, semantic_paths=semantic_paths)
            if qualified is not None:
                return qualified
            return {
                "status": "not_found",
                "target": target,
                "reason": "qualified target could not be resolved exactly",
                "candidates": [],
            }

        found = self.find(target, limit=12, paths=semantic_paths)
        candidates = [r for r in found["results"] if r.get("source") == "lsp"] or found["results"]
        if not candidates:
            return {"status": "not_found", "target": target, "candidates": []}
        top_score = candidates[0].get("score", 0)
        top = [c for c in candidates if c.get("score", 0) == top_score]
        if len(top) > 1:
            best_priority = max(_definition_priority(c) for c in top)
            top = [c for c in top if _definition_priority(c) == best_priority]
        if len(top) > 1 and top_score >= 7000:
            unique_paths = {(c["path"], c["line"]) for c in top}
            if len(unique_paths) > 1:
                return {
                    "status": "ambiguous",
                    "target": target,
                    "candidates": self._with_selection_commands(top[:8]),
                }
        chosen = top[0]
        others = [c for c in candidates if c is not chosen]
        return {
            "status": "ok",
            "target": target,
            "symbol": chosen,
            "candidates": self._with_selection_commands(others[:4]),
        }

    def _prewarm_symbol(
        self,
        project: Project,
        session: LspProcess,
        symbol: dict[str, Any],
        max_files: int = 12,
        *,
        desired_results: int = 5,
        probe: Callable[[], set[tuple[str, int, int]]] | None = None,
    ) -> None:
        name = str(symbol.get("name") or "").strip()
        if not name or symbol.get("source") == "explicit":
            return
        desired = max(1, desired_results)
        key = (str(project.root), name, desired)
        with self._lock:
            if key in self._prewarmed:
                return
            flight = self._prewarm_flights.get(key)
            if flight is None:
                flight = threading.Event()
                self._prewarm_flights[key] = flight
                owns_flight = True
            else:
                owns_flight = False
        if not owns_flight:
            # Prewarming is optional. Do not launch duplicate lexical/LSP work if
            # another request is already filling the same symbol budget.
            flight.wait(timeout=max(1.0, self.timeout))
            return

        completed = False
        try:
            hits = lexical_hits(project.root, name, limit=max(40, max_files * 3))
            files: list[Path] = []
            seen: set[Path] = set()
            for hit in hits:
                path = Path(hit["path"]).resolve()
                if path in seen or self.project_for_path(path) != project:
                    continue
                seen.add(path)
                files.append(path)
                if len(files) >= max_files:
                    break

            for index, path in enumerate(files, start=1):
                try:
                    self._document_symbols(path, project=project, timeout=min(self.timeout, 6.0))
                    with self._lock:
                        self._metrics["prewarm_files"] += 1
                except (LspError, OSError):
                    continue
                if probe is None or index % 2 != 0:
                    continue
                with self._lock:
                    self._metrics["prewarm_probes"] += 1
                current = probe()
                if len(current) >= desired:
                    with self._lock:
                        self._metrics["prewarm_early_stops"] += 1
                    break
            completed = True
        finally:
            with self._lock:
                if completed:
                    self._prewarmed.add(key)
                finished = self._prewarm_flights.pop(key, None)
                if finished is not None:
                    finished.set()

    def _session_and_position(self, symbol: dict[str, Any]) -> tuple[LspProcess, Project, Path, int, int]:
        path = Path(symbol["path"]).resolve()
        project = self.project_for_path(path)
        if project is None:
            raise LspError(f"unsupported source file: {path}")
        session = self._session(project)
        line = int(symbol["line"])
        column = int(symbol.get("column") or 1)
        if column <= 1:
            column = guess_symbol_column(path, line, symbol.get("name")) + 1
        return session, project, path, line, column

    def _reference_probe(self, session: LspProcess, path: Path, line: int, column: int) -> set[tuple[str, int, int]]:
        try:
            refs = session.references(path, line, column)
        except LspError:
            return set()
        out: set[tuple[str, int, int]] = set()
        for raw in refs:
            location = lsp_location(raw)
            if not location or not self._is_repo_path(location["path"]):
                continue
            out.add((str(location["path"]), int(location["line"]), int(location.get("column") or 1)))
        return out

    def _call_probe(
        self,
        session: LspProcess,
        path: Path,
        line: int,
        column: int,
        direction: str,
    ) -> set[tuple[str, int, int]]:
        return {
            (str(item["path"]), int(item["line"]), int(item.get("column") or 1))
            for item in self._call_neighbors(session, path, line, column, direction)
        }

    @staticmethod
    def _call_hierarchy_item(symbol: dict[str, Any]) -> dict[str, Any] | None:
        path = Path(str(symbol.get("path") or ""))
        if path.suffix not in {".py", ".pyi"}:
            return None
        name = str(symbol.get("name") or "")
        kind = _CALL_HIERARCHY_KIND_CODES.get(str(symbol.get("kind") or ""))
        line = int(symbol.get("line") or 0)
        column = int(symbol.get("column") or 0)
        if not name or kind is None or line <= 0 or column <= 0:
            return None
        start = {"line": line - 1, "character": column - 1}
        # LSP columns count UTF-16 code units, not Python code points.
        name_units = len(name.encode("utf-16-le")) // 2
        selection = {
            "start": start,
            "end": {"line": line - 1, "character": column - 1 + name_units},
        }
        return {
            "name": name,
            "kind": kind,
            "uri": path_to_uri(path.resolve()),
            "range": selection,
            "selectionRange": selection,
        }

    def _call_neighbors(
        self,
        session: LspProcess,
        path: Path,
        line: int,
        column: int,
        direction: str,
        *,
        root_item: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if root_item is None:
            roots = session.prepare_call_hierarchy(path, line, column)
            if not roots:
                return []
            root_item = roots[0]
        raw = session.incoming_calls(root_item) if direction == "in" else session.outgoing_calls(root_item)
        key = "from" if direction == "in" else "to"
        out: list[dict[str, Any]] = []
        for edge in raw:
            item = edge.get(key)
            if not isinstance(item, dict):
                continue
            entry = _call_item_entry(item)
            if self._is_repo_path(entry["path"]):
                out.append(entry)
        return out

    @staticmethod
    def _can_derive_python_callers(symbol: dict[str, Any]) -> bool:
        path = Path(str(symbol.get("path") or ""))
        line = int(symbol.get("line") or 0)
        name = str(symbol.get("name") or "")
        return (
            path.suffix in {".py", ".pyi"}
            and str(symbol.get("kind") or "") in _CALL_HIERARCHY_KIND_CODES
            and bool(name)
            and not is_python_property_definition(path, line, name)
        )

    def _python_callers_from_references(
        self,
        references: list[dict[str, Any]],
        symbol: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        if not self._can_derive_python_callers(symbol):
            return None
        name = str(symbol["name"])
        callers: list[dict[str, Any]] = []
        seen: set[tuple[str, int, str]] = set()
        for reference in references:
            classification = classify_python_call_reference(reference, name)
            if classification is None:
                return None
            if not classification:
                continue
            caller = self._semantic_symbol_at_location(reference)
            if caller is None:
                return None
            key = (str(caller["path"]), int(caller["line"]), str(caller["name"]))
            if key in seen:
                continue
            seen.add(key)
            callers.append(
                {
                    "name": caller.get("name", ""),
                    # BasedPyright represents Python def/async def callers as
                    # Function even when documentSymbol nests them as Method.
                    "kind": (
                        "Function"
                        if caller.get("kind") in {"Function", "Method", "Constructor"}
                        else caller.get("kind", "Unknown")
                    ),
                    "path": str(caller["path"]),
                    "line": int(caller["line"]),
                    "column": int(caller.get("column") or 1),
                    "detail": "",
                }
            )
        return callers

    def _path_reference(self, target: str) -> dict[str, Any] | None:
        """Resolve explicit path intent, preserving missing-path information."""
        intent = path_target_intent(target, self.root)
        if intent is None:
            return None
        resolved = Path(intent["path"])
        alternatives: list[Path] = []
        if not resolved.is_file():
            alternatives = self._basename_source_candidates(target)
            if len(alternatives) == 1:
                resolved = alternatives[0]
        return {
            "path": resolved,
            "line": intent["line"],
            "column": intent["column"],
            "exists": resolved.is_file(),
            "inside_repo": self._is_repo_path(resolved),
            "ambiguous_paths": [str(path) for path in alternatives] if len(alternatives) > 1 else [],
        }

    def _basename_source_candidates(self, target: str) -> list[Path]:
        if Path(target).name != target or "/" in target or "\\" in target or ":" in target:
            return []
        if language_for(Path(target)) is None:
            return []
        return [path for path in git_visible_files(self.root) if path.name == target]

    def _dotted_module_candidates(
        self,
        target: str,
        *,
        semantic_paths: tuple[str, ...] = (),
    ) -> list[Path]:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", target):
            return []
        requested = target.split(".")
        matches: list[Path] = []
        for path in git_visible_files(self.root):
            if path.suffix not in {".py", ".pyi"} or not self._matches_path_prefixes(path, semantic_paths):
                continue
            try:
                parts = list(path.relative_to(self.root).with_suffix("").parts)
            except ValueError:
                continue
            if parts and parts[-1] == "__init__":
                parts.pop()
            if len(parts) >= len(requested) and parts[-len(requested):] == requested:
                matches.append(path)
        return matches

    def _ambiguous_file_result(self, target: str, paths: list[Path]) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        for path in paths[:8]:
            try:
                rendered = path.relative_to(self.root).as_posix()
            except ValueError:
                rendered = str(path)
            candidates.append({
                "name": path.name,
                "kind": "File",
                "container": "",
                "path": str(path),
                "line": 1,
                "column": 1,
                "selection_command": shlex.join(["codeq", "context", rendered]),
            })
        return {
            "status": "ambiguous",
            "target": target,
            "reason": f"multiple source files match: {target}",
            "candidates": candidates,
        }

    def _file_target(self, target: str) -> Path | None:
        reference = self._path_reference(target)
        if (
            reference is None
            or reference["line"] is not None
            or reference.get("ambiguous_paths")
            or not reference["exists"]
            or not reference["inside_repo"]
        ):
            return None
        return Path(reference["path"])

    def _symbol_at_location(self, location: dict[str, Any]) -> dict[str, Any]:
        path = Path(location["path"]).resolve()
        project = self.project_for_path(path)
        if project is None:
            return location
        try:
            symbols = self._document_symbols(path, project=project)
        except (LspError, OSError):
            return location
        line = int(location.get("line") or 1)
        column0 = max(0, int(location.get("column") or 1) - 1)

        def contains(symbol: dict[str, Any]) -> bool:
            rng = symbol.get("range") or {}
            start = rng.get("start") or {}
            end = rng.get("end") or {}
            start_line = int(start.get("line", symbol["line"] - 1)) + 1
            end_line = int(end.get("line", start_line - 1)) + 1
            if not (start_line <= line <= end_line):
                return False
            if line == start_line and column0 < int(start.get("character", 0)):
                return False
            if line == end_line and column0 > int(end.get("character", column0)):
                return False
            return True

        candidates = [symbol for symbol in symbols if contains(symbol)]
        if not candidates:
            candidates = [symbol for symbol in symbols if int(symbol["line"]) == line]
        if not candidates:
            return location

        def span(symbol: dict[str, Any]) -> tuple[int, int]:
            rng = symbol.get("range") or {}
            start = rng.get("start") or {}
            end = rng.get("end") or {}
            line_span = int(end.get("line", symbol["line"] - 1)) - int(start.get("line", symbol["line"] - 1))
            char_span = int(end.get("character", 0)) - int(start.get("character", 0)) if line_span == 0 else 0
            return max(0, line_span), max(0, char_span)

        chosen = min(candidates, key=lambda symbol: (span(symbol), -_definition_priority(symbol)))
        return dict(chosen)

    def _semantic_symbol_at_location(self, location: dict[str, Any]) -> dict[str, Any] | None:
        """Return the smallest enclosing callable/type for a raw LSP location."""
        path = Path(location["path"]).resolve()
        project = self.project_for_path(path)
        if project is None:
            return None
        try:
            symbols = self._document_symbols(path, project=project)
        except (LspError, OSError):
            return None
        line = int(location.get("line") or 1)
        semantic_kinds = {
            "Function", "Method", "Constructor", "Class", "Interface", "Struct", "Enum"
        }

        def bounds(symbol: dict[str, Any]) -> tuple[int, int]:
            rng = symbol.get("range") or {}
            start = int((rng.get("start") or {}).get("line", symbol["line"] - 1)) + 1
            end = int((rng.get("end") or {}).get("line", start - 1)) + 1
            return start, end

        candidates = [
            symbol
            for symbol in symbols
            if symbol.get("kind") in semantic_kinds
            and bounds(symbol)[0] <= line <= bounds(symbol)[1]
        ]
        if not candidates:
            return None
        return dict(
            min(
                candidates,
                key=lambda symbol: (
                    bounds(symbol)[1] - bounds(symbol)[0],
                    -_definition_priority(symbol),
                ),
            )
        )

    def _definition_paths(
        self,
        session: LspProcess,
        path: Path,
        line: int,
        column: int,
    ) -> list[Path]:
        out: list[Path] = []
        seen: set[Path] = set()
        for raw in session.definitions(path, line, column):
            location = lsp_location(raw)
            if not location:
                continue
            candidate = Path(location["path"]).resolve()
            if candidate in seen or not self._is_repo_path(candidate):
                continue
            seen.add(candidate)
            out.append(candidate)
        return out

    def _verified_importers(
        self,
        target: Path,
        *,
        limit: int,
        only_tests: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        target = target.resolve()
        project = self.project_for_path(target)
        if project is None:
            return [], False
        scan_limit = max(400, limit * 20)
        hits = importer_candidate_hits(project.root, target, limit=scan_limit + 1)
        discovery_truncated = len(hits) > scan_limit
        importers: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for hit in hits[:scan_limit]:
            importer_path = Path(hit["path"]).resolve()
            if only_tests and not is_test_path(importer_path):
                continue
            importer_project = self.project_for_path(importer_path)
            if importer_project is None:
                continue
            matched = False
            resolved_any_local = False
            matched_import: dict[str, Any] | None = None
            for imported in extract_imports(importer_path):
                if int(imported.get("line") or 0) != int(hit["line"]):
                    continue
                resolved_paths = resolve_import_specifier(
                    importer_path,
                    str(imported.get("specifier") or ""),
                    importer_project.root,
                )
                resolved_any_local = resolved_any_local or bool(resolved_paths)
                if target in resolved_paths:
                    matched = True
                    matched_import = imported
                    break
            if not matched and not resolved_any_local:
                try:
                    importer_session = self._session(importer_project)
                    matched = target in self._definition_paths(
                        importer_session,
                        importer_path,
                        int(hit["line"]),
                        int(hit.get("column") or 1),
                    )
                except (LspError, OSError):
                    matched = False
            if not matched:
                continue
            key = (str(importer_path), int(hit["line"]))
            if key in seen:
                continue
            seen.add(key)
            importers.append(
                {
                    "path": str(importer_path),
                    "line": int(hit["line"]),
                    "column": int(hit.get("column") or 1),
                    "text": str(hit.get("text") or "").strip(),
                    "specifier": str((matched_import or {}).get("specifier") or ""),
                }
            )
            if len(importers) > limit:
                return importers[:limit], True
        return importers, discovery_truncated

    def _test_evidence(
        self,
        *,
        symbol: dict[str, Any],
        path: Path,
        references: list[dict[str, Any]],
        callers: list[dict[str, Any]],
        limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        symbol_name = str(symbol.get("name") or "")
        discovered: list[dict[str, Any]] = []

        for reference in references:
            if not is_test_path(reference["path"]):
                continue
            discovered.append(
                {
                    **reference,
                    "evidence": EVIDENCE_SEMANTIC,
                    "evidence_type": TEST_EVIDENCE_DIRECT_REFERENCE,
                    "confidence": "direct",
                    "reason": {
                        "relationship": "references_symbol",
                        "source": "language_server",
                        "symbol": symbol_name,
                    },
                }
            )

        for caller in callers:
            if not is_test_path(caller["path"]):
                continue
            discovered.append(
                {
                    **caller,
                    "evidence": EVIDENCE_SEMANTIC,
                    "evidence_type": TEST_EVIDENCE_SEMANTIC_CALLER,
                    "confidence": "candidate",
                    "reason": {
                        "relationship": "contains_incoming_caller",
                        "source": "language_server",
                        "symbol": symbol_name,
                        "caller": str(caller.get("name") or ""),
                    },
                }
            )

        importers, import_discovery_truncated = self._verified_importers(
            path,
            limit=limit + 1,
            only_tests=True,
        )
        for importer in importers:
            discovered.append(
                {
                    **importer,
                    "evidence": EVIDENCE_LEXICAL,
                    "evidence_type": TEST_EVIDENCE_MODULE_IMPORT,
                    "confidence": "candidate",
                    "reason": {
                        "relationship": "imports_defining_module",
                        "source": "resolved_import",
                        "symbol": symbol_name,
                        "specifier": str(importer.get("specifier") or ""),
                    },
                }
            )

        lexical_discovery_truncated = False
        if _lexical_test_candidate_allowed(symbol_name):
            lexical: dict[str, Any]
            try:
                lexical = git_text_search(
                    self.root,
                    symbol_name,
                    limit=limit + 1,
                    only_tests=True,
                )
            except (OSError, RuntimeError):
                lexical = {"results": [], "truncated": False}
            lexical_discovery_truncated = bool(lexical.get("truncated"))
            for hit in lexical.get("results", []):
                if (
                    str(hit.get("path") or "") == str(path)
                    and int(hit.get("line") or 0) == int(symbol.get("line") or 0)
                ):
                    continue
                discovered.append(
                    {
                        **hit,
                        "evidence": EVIDENCE_LEXICAL,
                        "evidence_type": TEST_EVIDENCE_EXACT_LEXICAL,
                        "confidence": "candidate",
                        "reason": {
                            "relationship": "contains_exact_symbol_text",
                            "source": str(hit.get("source") or "text_search"),
                            "symbol": symbol_name,
                            "query": symbol_name,
                        },
                    }
                )

        deduplicated: list[dict[str, Any]] = []
        seen: set[tuple[str, int, int]] = set()
        for item in discovered:
            key = (
                str(item.get("path") or ""),
                int(item.get("line") or 0),
                int(item.get("column") or 1),
            )
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(item)

        discovery_truncated = import_discovery_truncated or lexical_discovery_truncated
        if discovery_truncated:
            returned, metadata = _bounded_section_disclosure(deduplicated, limit)
            metadata.update(
                total_count=None,
                total_is_exact=False,
                truncated=True,
            )
        else:
            returned, metadata = _known_section_disclosure(deduplicated, limit)
        metadata["discovery_truncated"] = discovery_truncated
        metadata["returned_evidence_counts"] = {
            evidence_type: sum(
                1 for item in returned if item.get("evidence_type") == evidence_type
            )
            for evidence_type in (
                TEST_EVIDENCE_DIRECT_REFERENCE,
                TEST_EVIDENCE_SEMANTIC_CALLER,
                TEST_EVIDENCE_MODULE_IMPORT,
                TEST_EVIDENCE_EXACT_LEXICAL,
            )
        }
        return returned, metadata

    def _file_context(
        self,
        path: Path,
        limit: int,
        *,
        outline_depth: int = 1,
        outline_kind: str | None = None,
        container: str | None = None,
        include_topology: bool = False,
    ) -> dict[str, Any]:
        language = language_for(path)
        if language is None:
            return {
                "status": "unsupported_language",
                "target": str(path),
                "path": str(path),
                "reason": f"unsupported source language: {path.suffix or '<no extension>'}",
            }
        project = self.project_for_path(path)
        if project is None:
            return {
                "status": "error",
                "target": str(path),
                "error": f"no language project found for {path}",
            }
        try:
            session = self._session(project)
            symbols = self._document_symbols(path, project=project)
        except (LspError, OSError) as exc:
            return {"status": "error", "target": str(path), "error": str(exc)}

        imports = extract_imports(path)
        resolved_imports: list[dict[str, Any]] = []
        importers: list[dict[str, Any]] = []
        importers_truncated = False
        if include_topology:
            for item in imports:
                definitions = resolve_import_specifier(path, str(item.get("specifier") or ""), project.root)
                if not definitions:
                    definitions = self._definition_paths(
                        session,
                        path,
                        int(item.get("line") or 1),
                        int(item.get("column") or 1),
                    )
                resolved_imports.append(
                    {
                        **item,
                        "resolved_paths": [str(candidate) for candidate in definitions if candidate != path],
                    }
                )

            importers, importers_truncated = self._verified_importers(path, limit=limit)

        def outline_depth_of(symbol: dict[str, Any]) -> int:
            parent = str(symbol.get("container") or "")
            return 1 if not parent else len([part for part in parent.split(".") if part]) + 1

        def kind_matches(symbol: dict[str, Any]) -> bool:
            if not outline_kind:
                return True
            requested = outline_kind.strip().lower()
            actual = str(symbol.get("kind") or "").lower()
            if requested == "function":
                return actual in {"function", "method", "constructor"}
            if requested == "class":
                return actual in {"class", "interface", "struct", "enum"}
            return actual == requested

        selected_symbols: list[dict[str, Any]] = []
        normalized_container = (container or "").strip()
        for symbol in symbols:
            symbol_container = str(symbol.get("container") or "")
            if normalized_container:
                in_container = (
                    symbol.get("name") == normalized_container
                    or symbol_container == normalized_container
                    or symbol_container.startswith(normalized_container + ".")
                )
                if not in_container:
                    continue
                if symbol.get("name") == normalized_container and not symbol_container:
                    relative_depth = 0
                elif symbol_container == normalized_container:
                    relative_depth = 1
                else:
                    suffix = symbol_container.removeprefix(normalized_container + ".")
                    relative_depth = len([part for part in suffix.split(".") if part]) + 1
                if relative_depth > max(0, outline_depth):
                    continue
            elif not outline_kind and outline_depth_of(symbol) > max(1, outline_depth):
                continue
            if not kind_matches(symbol):
                continue
            selected_symbols.append(symbol)

        outline_total_matching = len(selected_symbols)
        outline = [
            {k: symbol[k] for k in ("name", "kind", "container", "path", "line", "column")}
            for symbol in selected_symbols[:limit]
        ]
        returned_importers = importers[:limit]
        returned_imports = resolved_imports[:limit]
        return {
            "status": "ok",
            "target": str(path),
            "kind": "file",
            "file": {
                "path": str(path),
                "language": language_for(path),
                "project_root": str(project.root),
            },
            "outline": outline,
            "symbol_count": len(symbols),
            "outline_count": len(outline),
            "outline_matching_count": outline_total_matching,
            "outline_truncated": outline_total_matching > len(outline),
            "outline_depth": outline_depth,
            "outline_kind": outline_kind,
            "container": container,
            "topology_loaded": include_topology,
            "imports": returned_imports,
            "import_count": len(imports),
            "imports_truncated": include_topology and len(resolved_imports) > len(returned_imports),
            "importers": returned_importers,
            "importer_count": len(returned_importers),
            "importers_truncated": importers_truncated,
        }

    def context(
        self,
        target: str,
        limit: int = 20,
        *,
        outline_depth: int = 1,
        outline_kind: str | None = None,
        container: str | None = None,
        include_topology: bool = False,
        lexical_references: bool = False,
        lexical_query: str | None = None,
        lexical_paths: tuple[str, ...] = (),
        lexical_globs: tuple[str, ...] = (),
        lexical_exclude_tests: bool = False,
        semantic_paths: tuple[str, ...] = (),
        selected_sections: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        context_started = time.perf_counter()

        def with_phase_timing(
            data: dict[str, Any],
            *,
            resolution_finished: float,
            prewarm_ms: float = 0.0,
            neighborhood_started: float | None = None,
        ) -> dict[str, Any]:
            finished = time.perf_counter()
            data["_phase_ms"] = {
                "resolution": round(max(0.0, resolution_finished - context_started) * 1000, 1),
                "prewarm": round(max(0.0, prewarm_ms), 1),
                "semantic_neighborhood": round(
                    max(0.0, finished - neighborhood_started) * 1000,
                    1,
                )
                if neighborhood_started is not None
                else 0.0,
            }
            return data

        requested_sections = tuple(
            dict.fromkeys(value.strip() for value in selected_sections if value.strip())
        )
        invalid_sections = [
            value for value in requested_sections if value not in CONTEXT_SECTION_KEYS
        ]
        if invalid_sections:
            resolution_finished = time.perf_counter()
            allowed = ", ".join(CONTEXT_SECTION_VALUES)
            return with_phase_timing(
                {
                    "status": "invalid_query",
                    "target": target,
                    "reason": (
                        f"unknown context section(s): {', '.join(invalid_sections)}; "
                        f"allowed values: {allowed}"
                    ),
                    "allowed_sections": list(CONTEXT_SECTION_VALUES),
                    "recovery_command": shlex.join(["codeq", "context", target]),
                },
                resolution_finished=resolution_finished,
            )
        focused = bool(requested_sections)
        selected_keys = (
            {CONTEXT_SECTION_KEYS[value] for value in requested_sections}
            if focused
            else {
                "source",
                "callers",
                "callees",
                "implementations",
                "tests",
                "references",
                "possible_dynamic_references",
            }
        )
        if "lexical_references" in selected_keys and not lexical_references:
            resolution_finished = time.perf_counter()
            return with_phase_timing(
                {
                    "status": "invalid_query",
                    "target": target,
                    "reason": "section lexical-references requires --lexical-references",
                    "allowed_sections": list(CONTEXT_SECTION_VALUES),
                    "recovery_command": shlex.join(
                        [
                            "codeq",
                            "context",
                            target,
                            "--section",
                            "lexical-references",
                            "--lexical-references",
                        ]
                    ),
                },
                resolution_finished=resolution_finished,
            )
        if lexical_references:
            selected_keys.add("lexical_references")

        file_target = self._file_target(target)
        if file_target is not None:
            resolution_finished = time.perf_counter()
            if focused:
                return with_phase_timing(
                    {
                        "status": "invalid_query",
                        "target": target,
                        "reason": "--section applies only to symbol context; the target resolved to a file",
                        "recovery_command": self._file_context_command(file_target),
                    },
                    resolution_finished=resolution_finished,
                )
            data = self._file_context(
                file_target,
                limit,
                outline_depth=outline_depth,
                outline_kind=outline_kind,
                container=container,
                include_topology=include_topology,
            )
            if data.get("status") == "ok" and lexical_references:
                data["lexical_references"] = git_text_search(
                    self.root,
                    lexical_query or file_target.name,
                    limit=limit,
                    paths=lexical_paths,
                    globs=lexical_globs,
                    exclude_tests=lexical_exclude_tests,
                )
            return with_phase_timing(
                data,
                resolution_finished=resolution_finished,
                neighborhood_started=resolution_finished,
            )

        module_candidates = self._dotted_module_candidates(
            target,
            semantic_paths=semantic_paths,
        )
        if len(module_candidates) == 1:
            resolution_finished = time.perf_counter()
            if focused:
                return with_phase_timing(
                    {
                        "status": "invalid_query",
                        "target": target,
                        "reason": "--section applies only to symbol context; the target resolved to a file",
                        "recovery_command": self._file_context_command(module_candidates[0]),
                    },
                    resolution_finished=resolution_finished,
                )
            data = self._file_context(
                module_candidates[0],
                limit,
                outline_depth=outline_depth,
                outline_kind=outline_kind,
                container=container,
                include_topology=include_topology,
            )
            if data.get("status") == "ok" and lexical_references:
                data["lexical_references"] = git_text_search(
                    self.root,
                    lexical_query or module_candidates[0].name,
                    limit=limit,
                    paths=lexical_paths,
                    globs=lexical_globs,
                    exclude_tests=lexical_exclude_tests,
                )
            return with_phase_timing(
                data,
                resolution_finished=resolution_finished,
                neighborhood_started=resolution_finished,
            )
        if len(module_candidates) > 1:
            resolution_finished = time.perf_counter()
            return with_phase_timing(
                self._ambiguous_file_result(target, module_candidates),
                resolution_finished=resolution_finished,
            )

        resolved = self.resolve(target, semantic_paths=semantic_paths)
        resolution_finished = time.perf_counter()
        if resolved["status"] != "ok":
            return with_phase_timing(resolved, resolution_finished=resolution_finished)
        symbol = resolved["symbol"]
        if include_topology:
            return with_phase_timing(
                {
                    "status": "invalid_query",
                    "target": target,
                    "reason": "--topology applies only to whole-file context; the target resolved to a symbol",
                    "symbol": symbol,
                    "recovery_command": self._file_context_command(symbol["path"], "--topology"),
                },
                resolution_finished=resolution_finished,
            )
        budget = QueryBudget.from_limit(limit)
        prewarm_started = time.perf_counter()
        try:
            session, project, path, line, column = self._session_and_position(symbol)
            if selected_keys & {
                "callers",
                "callees",
                "implementations",
                "tests",
                "references",
                "possible_dynamic_references",
            }:
                self._prewarm_symbol(
                    project,
                    session,
                    symbol,
                    desired_results=budget.items,
                    probe=lambda: self._reference_probe(session, path, line, column),
                )
        except LspError as exc:
            prewarm_finished = time.perf_counter()
            return with_phase_timing(
                {"status": "error", "target": target, "error": str(exc), "symbol": symbol},
                resolution_finished=resolution_finished,
                prewarm_ms=(prewarm_finished - prewarm_started) * 1000,
            )
        prewarm_finished = time.perf_counter()

        hover: Any = None
        if "source" in selected_keys:
            try:
                hover = session.hover(path, line, column)
            except LspError:
                pass
        refs: list[dict[str, Any]] = []
        if selected_keys & {"references", "tests", "possible_dynamic_references"}:
            try:
                refs = _deduplicate_locations([
                    x
                    for x in (lsp_location(r) for r in session.references(path, line, column))
                    if x and self._is_repo_path(x["path"])
                ])
            except LspError:
                refs = []
        impls: list[dict[str, Any]] = []
        if "implementations" in selected_keys:
            try:
                impl_locations = [
                    x
                    for x in (lsp_location(r) for r in session.implementations(path, line, column))
                    if x and self._is_repo_path(x["path"])
                ]
                seen_impls: set[tuple[str, int, int]] = set()
                for implementation in impl_locations:
                    key = (
                        str(implementation["path"]),
                        int(implementation["line"]),
                        int(implementation.get("column") or 1),
                    )
                    if key == (str(path), int(line), int(column)) or key in seen_impls:
                        continue
                    seen_impls.add(key)
                    impls.append(self._symbol_at_location(implementation))
            except LspError:
                impls = []
            impls = _deduplicate_locations(impls)
        callers = (
            _deduplicate_locations(self._call_neighbors(session, path, line, column, "in"))
            if selected_keys & {"callers", "tests"}
            else []
        )
        callees = (
            _deduplicate_locations(self._call_neighbors(session, path, line, column, "out"))
            if "callees" in selected_keys
            else []
        )

        source_refs = [r for r in refs if not is_test_path(r["path"])]
        hover_text = ""
        if isinstance(hover, dict):
            contents = hover.get("contents")
            if isinstance(contents, str):
                hover_text = contents
            elif isinstance(contents, dict):
                hover_text = str(contents.get("value", ""))
            elif isinstance(contents, list):
                parts = [item if isinstance(item, str) else str(item.get("value", "")) for item in contents]
                hover_text = "\n".join(p for p in parts if p)

        requested_location = resolved.get("requested_location")
        request_source: dict[str, Any] | None = None
        if (
            "source" in selected_keys
            and isinstance(requested_location, dict)
            and requested_location.get("path")
            and requested_location.get("line")
        ):
            request_source = source_snippet(
                requested_location["path"],
                int(requested_location["line"]),
                before=2,
                after=4,
                max_chars=budget.snippet_chars,
                max_line_chars=budget.text_line_chars,
            )
        lexical_data: dict[str, Any] | None = None
        if "lexical_references" in selected_keys:
            lexical_data = git_text_search(
                self.root,
                lexical_query or str(symbol.get("name") or ""),
                limit=limit,
                paths=lexical_paths,
                globs=lexical_globs,
                exclude_tests=lexical_exclude_tests,
            )

        data: dict[str, Any] = {
            "status": "ok",
            "evidence": EVIDENCE_SEMANTIC,
            "target": target,
            "symbol": symbol,
            "section_selection": {
                "mode": "focused" if focused else "default",
                "selected": [
                    value
                    for value in CONTEXT_SECTION_VALUES
                    if CONTEXT_SECTION_KEYS[value] in selected_keys
                ],
            },
            "section_metadata": {},
        }
        section_metadata = data["section_metadata"]
        if "source" in selected_keys:
            bounded_hover, hover_truncated = bounded_text(hover_text, budget.hover_chars)
            data["hover"] = bounded_hover
            data["hover_truncated"] = hover_truncated
            data["source"] = source_snippet(
                path,
                line,
                before=2,
                after=12,
                max_chars=budget.snippet_chars,
                max_line_chars=budget.text_line_chars,
            )
        if "callers" in selected_keys:
            data["callers"], section_metadata["callers"] = _known_section_disclosure(
                callers, budget.items
            )
        if "callees" in selected_keys:
            data["callees"], section_metadata["callees"] = _known_section_disclosure(
                callees, budget.items
            )
        if "implementations" in selected_keys:
            data["implementations"], section_metadata["implementations"] = (
                _known_section_disclosure(impls, budget.items)
            )
        if "references" in selected_keys:
            data["references"], section_metadata["references"] = _known_section_disclosure(
                source_refs, budget.items
            )
        if "tests" in selected_keys:
            data["tests"], section_metadata["tests"] = self._test_evidence(
                symbol=symbol,
                path=path,
                references=refs,
                callers=callers,
                limit=budget.items,
            )
        if "possible_dynamic_references" in selected_keys:
            possible_dynamic_probe = classify_dynamic_references(
                source_refs,
                str(symbol.get("name") or ""),
                limit=budget.items + 1,
            )
            (
                data["possible_dynamic_references"],
                section_metadata["possible_dynamic_references"],
            ) = _bounded_section_disclosure(possible_dynamic_probe, budget.items)
        if requested_location is not None:
            data["requested_location"] = requested_location
            data["cursor_definition"] = bool(resolved.get("cursor_definition"))
            if resolved.get("definition_note"):
                data["definition_note"] = resolved["definition_note"]
            if resolved.get("cursor_definition_count"):
                data["cursor_definition_count"] = int(resolved["cursor_definition_count"])
        if request_source is not None:
            data["request_source"] = request_source
        if lexical_data is not None:
            data["lexical_references"] = lexical_data
        return with_phase_timing(
            data,
            resolution_finished=resolution_finished,
            prewarm_ms=(prewarm_finished - prewarm_started) * 1000,
            neighborhood_started=prewarm_finished,
        )

    def trace(self, target: str, direction: str, depth: int = 3, limit: int = 100) -> dict[str, Any]:
        resolved = self.resolve(target)
        if resolved["status"] != "ok":
            return resolved
        symbol = resolved["symbol"]
        try:
            session, project, path, line, column = self._session_and_position(symbol)
            if direction == "in":
                self._prewarm_symbol(
                    project,
                    session,
                    symbol,
                    desired_results=max(1, limit),
                    probe=lambda: self._call_probe(session, path, line, column, "in"),
                )
        except LspError as exc:
            return {"status": "error", "target": target, "error": str(exc), "symbol": symbol}
        limit = max(1, limit)
        roots = session.prepare_call_hierarchy(path, line, column)
        if not roots:
            return {
                "status": "ok", "evidence": EVIDENCE_SEMANTIC, "target": target, "direction": direction, "depth": depth,
                "root": symbol, "tree": {"node": symbol, "children": []}, "node_count": 1,
                "node_limit": limit, "truncated": False,
                "note": "language server returned no call hierarchy for this position",
            }
        root_item = roots[0]
        seen: set[tuple[str, int, str]] = set()
        count = 0
        truncated = False
        def walk(item: dict[str, Any], remaining: int) -> dict[str, Any] | None:
            nonlocal count, truncated
            if count >= limit:
                truncated = True
                return None
            entry = _call_item_entry(item)
            key = (entry["path"], entry["line"], entry["name"])
            cycle = key in seen
            count += 1
            node: dict[str, Any] = {"node": entry, "children": []}
            if cycle:
                node["cycle"] = True
                return node
            seen.add(key)
            if remaining <= 0:
                return node
            raw = session.incoming_calls(item) if direction == "in" else session.outgoing_calls(item)
            edge_key = "from" if direction == "in" else "to"
            for edge in raw:
                child = edge.get(edge_key)
                if not isinstance(child, dict):
                    continue
                child_entry = _call_item_entry(child)
                if not self._is_repo_path(child_entry["path"]):
                    continue
                if count >= limit:
                    truncated = True
                    break
                child_node = walk(child, remaining - 1)
                if child_node is not None:
                    node["children"].append(child_node)
            return node

        tree = walk(root_item, max(0, depth))
        assert tree is not None
        return {
            "status": "ok",
            "evidence": EVIDENCE_SEMANTIC,
            "target": target,
            "direction": direction,
            "depth": depth,
            "node_count": count,
            "node_limit": limit,
            "truncated": truncated,
            "root": _call_item_entry(root_item),
            "tree": tree,
        }

    def _changed_symbols_for_file(self, path: Path, ranges: list[dict[str, int]]) -> list[dict[str, Any]]:
        project = self.project_for_path(path)
        if project is None or not path.exists():
            return []
        try:
            symbols = self._document_symbols(path, project=project)
        except (LspError, OSError):
            return []
        def span(symbol: dict[str, Any]) -> tuple[int, int]:
            rng = symbol.get("range") or {}
            start = int((rng.get("start") or {}).get("line", symbol["line"] - 1)) + 1
            end = int((rng.get("end") or {}).get("line", start - 1)) + 1
            return start, end

        callable_kinds = {"Function", "Method", "Constructor"}
        type_kinds = {"Class", "Interface", "Enum", "Struct"}
        selected: list[dict[str, Any]] = []
        for changed in ranges:
            intersecting = [
                symbol
                for symbol in symbols
                if not (span(symbol)[1] < changed["start"] or span(symbol)[0] > changed["end"])
            ]
            if not intersecting:
                continue
            callables = [s for s in intersecting if s.get("kind") in callable_kinds]
            structural = [s for s in intersecting if s.get("kind") in type_kinds]
            pool = callables or structural
            if pool:
                # Keep leaf semantic owners. A method changed inside a class should
                # report the method, not both the method and its containing class.
                leaves: list[dict[str, Any]] = []
                for candidate in pool:
                    c_start, c_end = span(candidate)
                    is_ancestor = False
                    for other in pool:
                        if other is candidate:
                            continue
                        o_start, o_end = span(other)
                        if c_start <= o_start and o_end <= c_end and (c_start, c_end) != (o_start, o_end):
                            is_ancestor = True
                            break
                    if not is_ancestor:
                        leaves.append(candidate)
                selected.extend(leaves)
            else:
                selected.append(min(intersecting, key=lambda s: (span(s)[1] - span(s)[0], -_definition_priority(s))))

        selected.sort(key=lambda x: (x["path"], x["line"], x["name"]))
        dedup: dict[tuple[str, int], dict[str, Any]] = {}
        for item in selected:
            dedup[(item["name"], item["line"])] = item
        return list(dedup.values())

    def _deleted_base_analysis(self, path: Path, resolved_base: str, limit: int) -> dict[str, Any]:
        text = git_show_file(self.root, resolved_base, path)
        language = language_for(path)
        if text is None or language is None:
            return {
                "status": "unavailable",
                "evidence": EVIDENCE_BASE_SIDE_LEXICAL,
                "base_symbol_count": 0,
                "base_symbols": [],
                "truncated": False,
            }
        declarations = extract_base_declarations(text, language)
        budget = QueryBudget.from_limit(limit)
        analyzed: list[dict[str, Any]] = []
        for declaration in declarations[: budget.items]:
            search = git_text_search(self.root, str(declaration["name"]), limit=budget.nested_items)
            results = list(search.get("results", []))
            analyzed.append(
                {
                    "symbol": {
                        "name": declaration["name"],
                        "kind": declaration["kind"],
                        "path": str(path),
                        "line": int(declaration["line"]),
                    },
                    "evidence": EVIDENCE_LEXICAL,
                    "residual_match_count": int(search.get("match_count", 0)),
                    "residual_matching_line_count": int(search.get("matching_line_count", 0)),
                    "residual_references": [item for item in results if not item.get("is_test")],
                    "tests": [item for item in results if item.get("is_test")],
                    "truncated": bool(search.get("truncated")),
                }
            )
        return {
            "status": "ok",
            "evidence": EVIDENCE_BASE_SIDE_LEXICAL,
            "base_symbol_count": len(declarations),
            "base_symbols": analyzed,
            "truncated": len(declarations) > len(analyzed),
        }

    def _pure_rename_analysis(self, path: Path, limit: int) -> dict[str, Any]:
        budget = QueryBudget.from_limit(limit)
        topology = self._file_context(
            path,
            limit=budget.items,
            outline_depth=1,
            include_topology=True,
        )
        if topology.get("status") != "ok":
            return {
                "status": "unavailable",
                "evidence": EVIDENCE_CURRENT_SEMANTIC,
                "reason": topology.get("error") or topology.get("reason") or "file context unavailable",
            }

        symbol_summaries: list[dict[str, Any]] = []
        semantic_kinds = {"Function", "Method", "Constructor", "Class", "Interface", "Enum", "Struct", "Constant"}
        for symbol in topology.get("outline", []):
            if symbol.get("kind") not in semantic_kinds:
                continue
            try:
                session, project, symbol_path, line, column = self._session_and_position(symbol)
                self._prewarm_symbol(project, session, symbol, max_files=12)
                refs = [
                    item
                    for item in (lsp_location(raw) for raw in session.references(symbol_path, line, column))
                    if item and self._is_repo_path(item["path"])
                ]
            except (LspError, OSError):
                refs = []
            tests = [item for item in refs if is_test_path(item["path"])]
            source_refs = [item for item in refs if not is_test_path(item["path"])]
            symbol_summaries.append(
                {
                    "symbol": {k: symbol.get(k) for k in ("name", "kind", "container", "path", "line", "column")},
                    "reference_count": len(refs),
                    "references": source_refs[:budget.nested_items],
                    "tests": tests[:budget.nested_items],
                }
            )
            if len(symbol_summaries) >= budget.items:
                break

        return {
            "status": "ok",
            "evidence": EVIDENCE_CURRENT_SEMANTIC,
            "importers": topology.get("importers", []),
            "importer_count": int(topology.get("importer_count", 0)),
            "importers_truncated": bool(topology.get("importers_truncated")),
            "symbols": symbol_summaries,
            "symbols_truncated": bool(topology.get("outline_truncated")),
        }

    def review(self, base: str, limit: int = 20, *, merge_base: bool = False) -> dict[str, Any]:
        review_started = time.perf_counter()
        budget = QueryBudget.from_limit(limit)
        resolved_base = git_merge_base(self.root, base) if merge_base else git_resolve_commit(self.root, base)
        file_changes = git_changed_files(self.root, resolved_base)
        untracked_changes = git_untracked_files(self.root)
        tracked_paths = {str(item["path"]) for item in file_changes}
        file_changes.extend(item for item in untracked_changes if str(item["path"]) not in tracked_paths)

        merged = merge_ranges(git_changed_ranges(self.root, resolved_base))
        ranges_by_path = {item["path"]: item["ranges"] for item in merged}
        for change in untracked_changes:
            path = Path(change["path"])
            if path.is_file():
                whole = whole_file_range(path)
                ranges_by_path[str(path)] = [{"start": whole["start"], "end": whole["end"]}]
        change_discovery_finished = time.perf_counter()

        changed_symbols: list[dict[str, Any]] = []
        unsupported: list[str] = []
        analyzed_changes: list[dict[str, Any]] = []
        for change in file_changes:
            path = Path(change["path"])
            annotated = dict(change)
            status = str(change.get("status") or "")
            if status == "D":
                base_analysis = self._deleted_base_analysis(path, resolved_base, limit)
                annotated["base_analysis"] = base_analysis
                annotated["semantic_status"] = (
                    "deleted_base_analyzed" if base_analysis.get("status") == "ok" else "deleted_base_unavailable"
                )
                analyzed_changes.append(annotated)
                continue
            if language_for(path) is None:
                annotated["semantic_status"] = "unsupported_language"
                unsupported.append(str(path))
                analyzed_changes.append(annotated)
                continue
            if not path.exists():
                annotated["semantic_status"] = "missing_from_worktree"
                analyzed_changes.append(annotated)
                continue
            ranges = ranges_by_path.get(str(path), [])
            if not ranges:
                if status == "R":
                    annotated["rename_analysis"] = self._pure_rename_analysis(path, limit)
                    annotated["semantic_status"] = "rename_analyzed"
                else:
                    annotated["semantic_status"] = "rename_or_copy_without_content_changes"
                analyzed_changes.append(annotated)
                continue
            symbols = self._changed_symbols_for_file(path, ranges)
            changed_symbols.extend(symbols)
            annotated["semantic_status"] = "analyzed" if symbols else "no_enclosing_symbol"
            analyzed_changes.append(annotated)

        seen_symbols: set[tuple[str, int, str]] = set()
        distinct: list[dict[str, Any]] = []
        for symbol in changed_symbols:
            key = (symbol["path"], symbol["line"], symbol["name"])
            if key not in seen_symbols:
                seen_symbols.add(key)
                distinct.append(symbol)
        distinct = distinct[:budget.items]

        details: list[dict[str, Any]] = []
        impacted_files: set[str] = set()
        tests: dict[tuple[str, int], dict[str, Any]] = {}
        for symbol in distinct:
            try:
                session, project, path, line, column = self._session_and_position(symbol)
                self._prewarm_symbol(project, session, symbol, max_files=8)
            except LspError:
                continue
            can_derive_callers = self._can_derive_python_callers(symbol)
            use_warm_reference_path = can_derive_callers and bool(
                getattr(session, "semantic_navigation_warmed", False)
            )
            callers: list[dict[str, Any]] | None = None
            if not use_warm_reference_path:
                callers = self._call_neighbors(
                    session,
                    path,
                    line,
                    column,
                    "in",
                    root_item=self._call_hierarchy_item(symbol),
                )
            try:
                refs = [
                    x
                    for x in (lsp_location(r) for r in session.references(path, line, column))
                    if x and self._is_repo_path(x["path"])
                ]
            except LspError:
                refs = []
            if callers is None:
                callers = self._python_callers_from_references(refs, symbol)
                if callers is None:
                    callers = self._call_neighbors(
                        session,
                        path,
                        line,
                        column,
                        "in",
                        root_item=self._call_hierarchy_item(symbol),
                    )
            direct_tests = [r for r in refs if is_test_path(r["path"])]
            possible_dynamic = classify_dynamic_references(
                [r for r in refs if not is_test_path(r["path"])],
                str(symbol.get("name") or ""),
                limit=budget.nested_items,
            )
            for caller in callers:
                impacted_files.add(caller["path"])
                if is_test_path(caller["path"]):
                    tests[(caller["path"], caller["line"])] = caller
            for ref in refs:
                impacted_files.add(ref["path"])
            for test in direct_tests:
                tests[(test["path"], test["line"])] = test
            details.append({
                "symbol": {k: symbol.get(k) for k in ("name", "kind", "container", "path", "line", "column")},
                "callers": callers[:budget.nested_items],
                "possible_dynamic_references": possible_dynamic,
                "tests": direct_tests[:budget.nested_items],
                "reference_count": len(refs),
            })

        changed_files = [item["path"] for item in analyzed_changes]
        impacted_files.difference_update(changed_files)
        dynamic_reference_count = sum(
            len(detail.get("possible_dynamic_references", [])) for detail in details
        )
        deleted_count = sum(1 for item in analyzed_changes if item.get("status") == "D")
        renamed_count = sum(1 for item in analyzed_changes if item.get("status") == "R")
        data: dict[str, Any] = {
            "status": "ok",
            "base": base,
            "requested_base": base,
            "base_mode": "merge-base" if merge_base else "direct",
            "resolved_base": resolved_base,
            "file_changes": analyzed_changes,
            "changed_files": changed_files,
            "changed_file_count": len(analyzed_changes),
            "deleted_file_count": deleted_count,
            "renamed_file_count": renamed_count,
            "untracked_file_count": sum(1 for item in analyzed_changes if item.get("status") == "U"),
            "changed_symbols": details,
            "changed_symbol_count": len(details),
            "impacted_files": sorted(impacted_files)[:budget.items],
            "impacted_file_count": len(impacted_files),
            "impacted_files_truncated": len(impacted_files) > budget.items,
            "tests": sorted(tests.values(), key=lambda x: (x["path"], x["line"]))[:budget.items],
            "test_count": len(tests),
            "tests_truncated": len(tests) > budget.items,
            "possible_dynamic_reference_count": dynamic_reference_count,
            "unsupported_changed_files": unsupported,
            "truncated": len(changed_symbols) > budget.items,
            "limitations": [
                "deleted-file impact uses conservative base-side declaration extraction plus exact current-worktree lexical evidence; it is not an LSP call graph",
                "pure-rename impact uses current-path importers/references and may still miss runtime-only loading",
                "exact call edges are language-server-resolved and may omit runtime-only dispatch",
                "possible_dynamic_references classify exact LSP references heuristically; they are not runtime-proof call edges",
                "test discovery uses semantic references/callers plus test-path classification",
            ],
        }
        review_finished = time.perf_counter()
        data["_phase_ms"] = {
            "change_discovery": round(
                max(0.0, change_discovery_finished - review_started) * 1000,
                1,
            ),
            "review_analysis": round(
                max(0.0, review_finished - change_discovery_finished) * 1000,
                1,
            ),
        }
        return data
