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
