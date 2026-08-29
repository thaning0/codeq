from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .util import git_visible_files, language_for


class FtsUnavailable(RuntimeError):
    """Raised when the active Python SQLite build does not provide FTS5."""


@dataclass(frozen=True)
class FtsHit:
    path: Path
    relative_path: str
    bm25: float


@dataclass(frozen=True)
class FtsSearch:
    hits: list[FtsHit]
    terms: list[str]
    match_expression: str
    file_count: int
    source_bytes: int
    index_bytes: int
    refreshed: bool
    build_ms: float
    query_ms: float


@dataclass(frozen=True)
class FtsEvidenceLine:
    line: int
    column: int
    text: str
    matched_terms: tuple[str, ...]
    text_truncated: bool
    text_start_column: int


@dataclass(frozen=True)
class FtsEvidence:
    lines: tuple[FtsEvidenceLine, ...]
    matched_terms: tuple[str, ...]


def lexical_terms(query: str) -> list[str]:
    """Return distinct lexical terms in user order for deterministic FTS queries."""
    tokens = re.findall(r"[^\W\d_]\w*|_[A-Za-z0-9_]+", query, flags=re.UNICODE)
    seen: set[str] = set()
    terms: list[str] = []
    for token in tokens:
        normalized = token.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        terms.append(token)
    return terms


def _match_expression(terms: list[str]) -> str:
    # Quoted FTS phrases make punctuation/operators in user input inert. Each
    # extracted term is one phrase and OR keeps partial lexical matches visible;
    # built-in BM25 naturally rewards files matching more of the query vocabulary.
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _bounded_evidence_text(text: str, match_index: int, max_chars: int) -> tuple[str, bool, int]:
    if len(text) <= max_chars:
        return text, False, 1
    context = max(0, max_chars // 3)
    start = max(0, match_index - context)
    if start + max_chars > len(text):
        start = max(0, len(text) - max_chars)
    end = min(len(text), start + max_chars)
    window = text[start:end]
    if start > 0 and max_chars >= 3:
        window = "..." + window[3:]
    if end < len(text) and max_chars >= 3:
        window = window[:-3] + "..."
    return window, True, start + 1


def representative_evidence(
    path: Path,
    terms: list[str],
    *,
    limit: int = 3,
    max_chars: int = 500,
) -> FtsEvidence:
    """Return bounded source lines that explain why one ranked file matched."""
    try:
        source_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return FtsEvidence(lines=(), matched_terms=())

    patterns = [
        (term, re.compile(re.escape(term), flags=re.IGNORECASE))
        for term in terms
    ]
    candidates: list[tuple[int, int, int, FtsEvidenceLine]] = []
    all_matched: set[str] = set()
    for line_number, text in enumerate(source_lines, start=1):
        matched: list[str] = []
        occurrence_count = 0
        first_index: int | None = None
        for term, pattern in patterns:
            occurrences = list(pattern.finditer(text))
            if not occurrences:
                continue
            matched.append(term)
            occurrence_count += len(occurrences)
            candidate_index = occurrences[0].start()
            first_index = candidate_index if first_index is None else min(first_index, candidate_index)
        if first_index is None:
            continue
        all_matched.update(term.casefold() for term in matched)
        bounded, truncated, text_start_column = _bounded_evidence_text(
            text,
            first_index,
            max(1, max_chars),
        )
        evidence = FtsEvidenceLine(
            line=line_number,
            column=first_index + 1,
            text=bounded,
            matched_terms=tuple(matched),
            text_truncated=truncated,
            text_start_column=text_start_column,
        )
        candidates.append((len(matched), occurrence_count, line_number, evidence))

    ordered = sorted(candidates, key=lambda item: (-item[0], -item[1], item[2]))
    selected: list[FtsEvidenceLine] = []
    covered: set[str] = set()
    for _, _, _, evidence in ordered:
        normalized = {term.casefold() for term in evidence.matched_terms}
        if selected and normalized <= covered:
            continue
        selected.append(evidence)
        covered.update(normalized)
        if len(selected) >= max(1, limit) or covered >= all_matched:
            break

    matched_terms = tuple(term for term in terms if term.casefold() in all_matched)
    return FtsEvidence(lines=tuple(selected), matched_terms=matched_terms)


class WorkspaceFtsIndex:
    """Lazy, contentless, in-memory FTS5 index owned by one Workspace."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._connection: sqlite3.Connection | None = None
        self._fingerprint: tuple[tuple[str, int, int], ...] = ()
        self._file_count = 0
        self._source_bytes = 0
        self._index_bytes = 0
        self._closed = False
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            connection = self._connection
            self._connection = None
            self._fingerprint = ()
        if connection is not None:
            connection.close()

    def search(self, query: str) -> FtsSearch:
        terms = lexical_terms(query)
        expression = _match_expression(terms)
        with self._lock:
            if self._closed:
                raise RuntimeError(f"workspace FTS index is closed: {self.root}")
            refreshed, build_ms = self._refresh()
            connection = self._connection
            if connection is None:
                raise RuntimeError("workspace FTS index was not initialized")
            started = time.perf_counter()
            rows = connection.execute(
                """
                SELECT files.relative_path, bm25(source_fts) AS rank
                FROM source_fts
                JOIN files ON files.rowid = source_fts.rowid
                WHERE source_fts MATCH ?
                ORDER BY rank, files.relative_path
                """,
                (expression,),
            ).fetchall()
            query_ms = (time.perf_counter() - started) * 1000
            hits = [
                FtsHit(
                    path=(self.root / str(relative_path)).resolve(),
                    relative_path=str(relative_path),
                    bm25=float(rank),
                )
                for relative_path, rank in rows
            ]
            return FtsSearch(
                hits=hits,
                terms=terms,
                match_expression=expression,
                file_count=self._file_count,
                source_bytes=self._source_bytes,
                index_bytes=self._index_bytes,
                refreshed=refreshed,
                build_ms=build_ms,
                query_ms=query_ms,
            )

    def _source_files(self) -> list[tuple[Path, str, int, int]]:
        files: list[tuple[Path, str, int, int]] = []
        for path in git_visible_files(self.root):
            if language_for(path) is None:
                continue
            try:
                stat = path.stat()
                relative_path = os.path.relpath(path, self.root).replace(os.sep, "/")
            except OSError:
                continue
            if relative_path == ".." or relative_path.startswith("../"):
                continue
            files.append((path, relative_path, int(stat.st_mtime_ns), int(stat.st_size)))
        return files

    def _refresh(self) -> tuple[bool, float]:
        files = self._source_files()
        fingerprint = tuple((relative, mtime_ns, size) for _, relative, mtime_ns, size in files)
        if self._connection is not None and fingerprint == self._fingerprint:
            return False, 0.0

        started = time.perf_counter()
        connection = self._new_connection()
        source_bytes = 0
        try:
            connection.execute("BEGIN")
            for rowid, (path, relative_path, _, size) in enumerate(files, start=1):
                try:
                    body = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                connection.execute(
                    "INSERT INTO files(rowid, relative_path) VALUES (?, ?)",
                    (rowid, relative_path),
                )
                connection.execute(
                    "INSERT INTO source_fts(rowid, body) VALUES (?, ?)",
                    (rowid, body),
                )
                source_bytes += size
            connection.commit()
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        except Exception:
            connection.close()
            raise

        previous = self._connection
        self._connection = connection
        self._fingerprint = fingerprint
        self._file_count = int(connection.execute("SELECT count(*) FROM files").fetchone()[0])
        self._source_bytes = source_bytes
        self._index_bytes = page_count * page_size
        if previous is not None:
            previous.close()
        return True, (time.perf_counter() - started) * 1000

    @staticmethod
    def _new_connection() -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        try:
            connection.execute(
                "CREATE TABLE files(rowid INTEGER PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE)"
            )
            connection.execute(
                "CREATE VIRTUAL TABLE source_fts USING fts5("
                "body, content='', tokenize='unicode61'"
                ")"
            )
        except sqlite3.OperationalError as exc:
            connection.close()
            if "fts5" in str(exc).casefold():
                raise FtsUnavailable(
                    "SQLite FTS5 is unavailable in this Python build; use an FTS5-enabled "
                    "Python/SQLite build or run `codeq find --text QUERY` for exact text"
                ) from exc
            raise
        return connection
