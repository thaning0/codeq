from __future__ import annotations

import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dynamic import classify_dynamic_references
from .gitdiff import git_changed_ranges, merge_ranges
from .lsp import LspError, LspProcess
from .util import (
    fuzzy_score,
    guess_symbol_column,
    identifier_tokens,
    is_test_path,
    language_for,
    lexical_hits,
    lsp_location,
    parse_target,
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


class Workspace:
    def __init__(self, root: Path, timeout: float = 15.0):
        self.root = root.resolve()
        self.timeout = timeout
        self.projects = discover_projects(self.root)
        self._sessions: dict[Project, LspProcess] = {}
        self._prewarmed: set[tuple[str, str]] = set()
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
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
                }
                for project, session in self._sessions.items()
            ]

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
            server = self._server_command(project)
            if server is None:
                raise LspError(f"no {project.family} language server available for {project.root}")
            command, name = server
            session = LspProcess(command, project.root, name=name, timeout=self.timeout)
            self._sessions[project] = session
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

    def _candidate_projects(self, hits: list[dict[str, Any]]) -> list[Project]:
        selected: set[Project] = set()
        for hit in hits:
            project = self.project_for_path(Path(hit["path"]))
            if project:
                selected.add(project)
        return sorted(selected or set(self.projects), key=lambda p: (p.family, str(p.root)))

    def find(self, query: str, limit: int = 20, kind: str | None = None) -> dict[str, Any]:
        hits = lexical_hits(self.root, query, limit=max(40, limit * 3))
        projects = self._candidate_projects(hits)
        tokens = identifier_tokens(query)
        search_terms = tokens[:3] or [query]
        results: list[dict[str, Any]] = []
        errors: list[str] = []

        def search_project(project: Project) -> tuple[list[dict[str, Any]], str | None]:
            try:
                session = self._session(project)
                found: list[dict[str, Any]] = []
                seen: set[tuple[str, str, int]] = set()
                for term in search_terms:
                    for item in session.workspace_symbols(term, timeout=self.timeout):
                        entry = _workspace_symbol_entry(item)
                        if not entry:
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
                symbols = _flatten_document_symbols(self._session(project).document_symbols(path), path)
            except (LspError, OSError):
                continue
            hit_line = hit["line"]
            for symbol in symbols:
                rng = symbol.get("range") or {}
                start = int((rng.get("start") or {}).get("line", symbol["line"] - 1)) + 1
                end = int((rng.get("end") or {}).get("line", start - 1)) + 1
                score = fuzzy_score(query, symbol["name"], symbol.get("container", ""), symbol["path"])
                if start <= hit_line <= end:
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
            score = semantic_score + lexical_boost
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
        return {
            "query": query,
            "kind": kind,
            "results": ordered[:limit],
            "result_count": min(len(ordered), limit),
            "total_candidates": len(ordered),
            "errors": errors[:4],
        }

    def _resolve_qualified(self, target: str) -> dict[str, Any] | None:
        parts = [part for part in target.split(".") if part]
        if len(parts) < 2:
            return None
        container_name = parts[-2]
        member_name = parts[-1]
        found = self.find(container_name, limit=20)
        containers = [
            item
            for item in found.get("results", [])
            if item.get("source") == "lsp"
            and item.get("name") == container_name
            and item.get("kind") in {"Class", "Interface", "Struct", "Enum", "Namespace", "Module"}
        ]
        matches: list[dict[str, Any]] = []
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
                symbols = _flatten_document_symbols(self._session(project).document_symbols(path), path)
            except (LspError, OSError):
                continue
            for symbol in symbols:
                if symbol.get("name") != member_name:
                    continue
                combined = ".".join(
                    part for part in (str(symbol.get("container") or ""), member_name) if part
                )
                if combined == target or combined.endswith("." + target):
                    matches.append({**symbol, "score": 10000})
        if not matches:
            return None
        matches.sort(key=lambda item: (-_definition_priority(item), item["path"], item["line"]))
        best_priority = _definition_priority(matches[0])
        top = [item for item in matches if _definition_priority(item) == best_priority]
        unique = {(item["path"], item["line"]) for item in top}
        if len(unique) > 1:
            return {"status": "ambiguous", "target": target, "candidates": top[:8]}
        chosen = top[0]
        return {"status": "ok", "target": target, "symbol": chosen, "candidates": matches[1:5]}

    def resolve(self, target: str) -> dict[str, Any]:
        parsed = parse_target(target, self.root)
        if parsed["kind"] == "location":
            path = Path(parsed["path"])
            if not path.exists():
                return {"status": "not_found", "target": target, "reason": f"file not found: {path}"}
            project = self.project_for_path(path)
            if project is not None:
                try:
                    symbols = _flatten_document_symbols(self._session(project).document_symbols(path), path)
                except (LspError, OSError):
                    symbols = []
                line = int(parsed["line"])
                column = int(parsed["column"])
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

                containing = [symbol for symbol in symbols if contains(symbol)]
                if explicit_column:
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
                    }
            col = parsed["column"]
            if col <= 1:
                col = guess_symbol_column(path, parsed["line"]) + 1
            return {
                "status": "ok",
                "target": target,
                "symbol": {
                    "name": path.name,
                    "kind": "Location",
                    "container": "",
                    "path": str(path),
                    "line": parsed["line"],
                    "column": col,
                    "source": "explicit",
                },
            }

        qualified = self._resolve_qualified(target)
        if qualified is not None:
            return qualified

        found = self.find(target, limit=12)
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
                return {"status": "ambiguous", "target": target, "candidates": top[:8]}
        chosen = top[0]
        others = [c for c in candidates if c is not chosen]
        return {"status": "ok", "target": target, "symbol": chosen, "candidates": others[:4]}

    def _prewarm_symbol(self, project: Project, session: LspProcess, symbol: dict[str, Any], max_files: int = 12) -> None:
        name = str(symbol.get("name") or "").strip()
        if not name or symbol.get("source") == "explicit":
            return
        key = (str(project.root), name)
        if key in self._prewarmed:
            return
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
        for path in files:
            try:
                session.document_symbols(path, timeout=min(self.timeout, 6.0))
            except (LspError, OSError):
                continue
        self._prewarmed.add(key)

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

    def _call_neighbors(self, session: LspProcess, path: Path, line: int, column: int, direction: str) -> list[dict[str, Any]]:
        roots = session.prepare_call_hierarchy(path, line, column)
        if not roots:
            return []
        raw = session.incoming_calls(roots[0]) if direction == "in" else session.outgoing_calls(roots[0])
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

    def context(self, target: str, limit: int = 20) -> dict[str, Any]:
        resolved = self.resolve(target)
        if resolved["status"] != "ok":
            return resolved
        symbol = resolved["symbol"]
        try:
            session, project, path, line, column = self._session_and_position(symbol)
            self._prewarm_symbol(project, session, symbol)
        except LspError as exc:
            return {"status": "error", "target": target, "error": str(exc), "symbol": symbol}

        hover: Any = None
        try:
            hover = session.hover(path, line, column)
        except LspError:
            pass
        try:
            refs = [
                x
                for x in (lsp_location(r) for r in session.references(path, line, column))
                if x and self._is_repo_path(x["path"])
            ]
        except LspError:
            refs = []
        try:
            impls = [
                x
                for x in (lsp_location(r) for r in session.implementations(path, line, column))
                if x and self._is_repo_path(x["path"])
            ]
        except LspError:
            impls = []
        callers = self._call_neighbors(session, path, line, column, "in")
        callees = self._call_neighbors(session, path, line, column, "out")
        try:
            doc_symbols = _flatten_document_symbols(session.document_symbols(path), path)
        except LspError:
            doc_symbols = []

        tests = [r for r in refs if is_test_path(r["path"])]
        source_refs = [r for r in refs if not is_test_path(r["path"])]
        possible_dynamic = classify_dynamic_references(
            source_refs,
            str(symbol.get("name") or ""),
            limit=limit,
        )
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

        return {
            "status": "ok",
            "target": target,
            "symbol": symbol,
            "hover": hover_text[:4000],
            "source": source_snippet(path, line, before=2, after=12),
            "callers": callers[:limit],
            "callees": callees[:limit],
            "implementations": impls[:limit],
            "references": source_refs[:limit],
            "possible_dynamic_references": possible_dynamic,
            "tests": tests[:limit],
            "file_symbols": [
                {k: s[k] for k in ("name", "kind", "container", "path", "line", "column")}
                for s in doc_symbols[: min(limit, 30)]
            ],
        }

    def trace(self, target: str, direction: str, depth: int = 3, limit: int = 100) -> dict[str, Any]:
        resolved = self.resolve(target)
        if resolved["status"] != "ok":
            return resolved
        symbol = resolved["symbol"]
        try:
            session, project, path, line, column = self._session_and_position(symbol)
            self._prewarm_symbol(project, session, symbol)
        except LspError as exc:
            return {"status": "error", "target": target, "error": str(exc), "symbol": symbol}
        roots = session.prepare_call_hierarchy(path, line, column)
        if not roots:
            return {
                "status": "ok", "target": target, "direction": direction, "depth": depth,
                "root": symbol, "tree": {"node": symbol, "children": []}, "node_count": 1,
                "note": "language server returned no call hierarchy for this position",
            }
        root_item = roots[0]
        seen: set[tuple[str, int, str]] = set()
        count = 0

        def walk(item: dict[str, Any], remaining: int) -> dict[str, Any]:
            nonlocal count
            entry = _call_item_entry(item)
            key = (entry["path"], entry["line"], entry["name"])
            if key in seen:
                return {"node": entry, "cycle": True, "children": []}
            seen.add(key)
            count += 1
            node: dict[str, Any] = {"node": entry, "children": []}
            if remaining <= 0 or count >= limit:
                return node
            raw = session.incoming_calls(item) if direction == "in" else session.outgoing_calls(item)
            edge_key = "from" if direction == "in" else "to"
            for edge in raw:
                child = edge.get(edge_key)
                if isinstance(child, dict):
                    child_entry = _call_item_entry(child)
                    if self._is_repo_path(child_entry["path"]):
                        node["children"].append(walk(child, remaining - 1))
                if count >= limit:
                    break
            return node

        tree = walk(root_item, max(0, depth))
        return {
            "status": "ok",
            "target": target,
            "direction": direction,
            "depth": depth,
            "node_count": count,
            "truncated": count >= limit,
            "root": _call_item_entry(root_item),
            "tree": tree,
        }

    def _changed_symbols_for_file(self, path: Path, ranges: list[dict[str, int]]) -> list[dict[str, Any]]:
        project = self.project_for_path(path)
        if project is None or not path.exists():
            return []
        try:
            symbols = _flatten_document_symbols(self._session(project).document_symbols(path), path)
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

    def review(self, base: str, limit: int = 20) -> dict[str, Any]:
        merged = merge_ranges(git_changed_ranges(self.root, base))
        changed_symbols: list[dict[str, Any]] = []
        unsupported: list[str] = []
        for file_change in merged:
            path = Path(file_change["path"])
            if language_for(path) is None:
                unsupported.append(str(path))
                continue
            changed_symbols.extend(self._changed_symbols_for_file(path, file_change["ranges"]))

        seen_symbols: set[tuple[str, int, str]] = set()
        distinct: list[dict[str, Any]] = []
        for symbol in changed_symbols:
            key = (symbol["path"], symbol["line"], symbol["name"])
            if key not in seen_symbols:
                seen_symbols.add(key)
                distinct.append(symbol)
        distinct = distinct[:limit]

        details: list[dict[str, Any]] = []
        impacted_files: set[str] = set()
        tests: dict[tuple[str, int], dict[str, Any]] = {}
        for symbol in distinct:
            try:
                session, project, path, line, column = self._session_and_position(symbol)
                self._prewarm_symbol(project, session, symbol, max_files=8)
            except LspError:
                continue
            callers = self._call_neighbors(session, path, line, column, "in")
            try:
                refs = [
                    x
                    for x in (lsp_location(r) for r in session.references(path, line, column))
                    if x and self._is_repo_path(x["path"])
                ]
            except LspError:
                refs = []
            direct_tests = [r for r in refs if is_test_path(r["path"])]
            possible_dynamic = classify_dynamic_references(
                [r for r in refs if not is_test_path(r["path"])],
                str(symbol.get("name") or ""),
                limit=10,
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
                "callers": callers[:10],
                "possible_dynamic_references": possible_dynamic,
                "tests": direct_tests[:10],
                "reference_count": len(refs),
            })

        changed_files = [item["path"] for item in merged]
        impacted_files.difference_update(changed_files)
        dynamic_reference_count = sum(
            len(detail.get("possible_dynamic_references", [])) for detail in details
        )
        return {
            "status": "ok",
            "base": base,
            "changed_files": changed_files,
            "changed_file_count": len(changed_files),
            "changed_symbols": details,
            "changed_symbol_count": len(details),
            "impacted_files": sorted(impacted_files)[:100],
            "impacted_file_count": len(impacted_files),
            "tests": sorted(tests.values(), key=lambda x: (x["path"], x["line"]))[:100],
            "test_count": len(tests),
            "possible_dynamic_reference_count": dynamic_reference_count,
            "unsupported_changed_files": unsupported,
            "truncated": len(changed_symbols) > limit,
            "limitations": [
                "exact call edges are language-server-resolved and may omit runtime-only dispatch",
                "possible_dynamic_references classify exact LSP references heuristically; they are not runtime-proof call edges",
                "test discovery uses semantic references/callers plus test-path classification",
            ],
        }
