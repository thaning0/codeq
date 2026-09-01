use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Instant;
use std::time::UNIX_EPOCH;

use globset::{Glob, GlobSet, GlobSetBuilder};
use rusqlite::{Connection, params};
use serde_json::{Value, json};

use crate::cli::FindArgs;
use crate::target;
use crate::workspace::Workspace;

#[derive(Default)]
pub(crate) struct ConceptIndex {
    connection: Option<Connection>,
    fingerprint: Vec<(String, u64, u128)>,
    source_bytes: u64,
    index_bytes: u64,
}

struct SourceFile {
    path: PathBuf,
    relative: String,
    size: u64,
    modified_ns: u128,
}

struct Evidence {
    lines: Vec<Value>,
    terms: Vec<String>,
}

pub(crate) fn search(
    workspace: &Workspace,
    arguments: &FindArgs,
    limit: i64,
) -> Result<Value, String> {
    let root = workspace.root();
    let terms = lexical_terms(&arguments.query);
    let expression = terms
        .iter()
        .map(|term| format!("\"{}\"", term.replace('"', "\"\"")))
        .collect::<Vec<_>>()
        .join(" OR ");
    let common = || {
        json!({
            "mode": "fts5",
            "search_mode": "concept",
            "query": arguments.query,
            "kind": arguments.kind,
            "paths": arguments.paths,
            "filters": {
                "paths": arguments.paths,
                "globs": arguments.globs,
                "exclude_tests": arguments.exclude_tests,
            },
            "results": [],
            "result_count": 0,
            "total_candidates": 0,
            "truncated": false,
            "errors": [],
        })
    };
    if terms.is_empty() {
        let mut value = common();
        value["status"] = Value::String("invalid_query".to_owned());
        value["reason"] =
            Value::String("concept query must contain at least one lexical term".to_owned());
        return Ok(value);
    }
    if arguments.kind.is_some() {
        let mut value = common();
        value["status"] = Value::String("unsupported_target".to_owned());
        value["reason"] = Value::String(
            "--kind is unavailable for concept search; use symbol mode for symbol filtering"
                .to_owned(),
        );
        return Ok(value);
    }

    let files = visible_sources(root)?;
    let fingerprint: Vec<_> = files
        .iter()
        .map(|file| (file.relative.clone(), file.size, file.modified_ns))
        .collect();
    let mut index = workspace.concept_index();
    let refreshed = index.connection.is_none() || index.fingerprint != fingerprint;
    let build_ms = if refreshed {
        let build_started = Instant::now();
        let (connection, source_bytes, index_bytes) = build_index(&files)?;
        index.connection = Some(connection);
        index.fingerprint = fingerprint;
        index.source_bytes = source_bytes;
        index.index_bytes = index_bytes;
        build_started.elapsed().as_secs_f64() * 1000.0
    } else {
        0.0
    };

    let query_started = Instant::now();
    let mut statement = index
        .connection
        .as_ref()
        .expect("concept connection must exist after refresh")
        .prepare(
            "SELECT files.relative_path, bm25(source_fts) AS rank
             FROM source_fts JOIN files ON files.rowid = source_fts.rowid
             WHERE source_fts MATCH ?1 ORDER BY rank, files.relative_path",
        )
        .map_err(|error| error.to_string())?;
    let rows = statement
        .query_map([&expression], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, f64>(1)?))
        })
        .map_err(|error| format!("FTS5 query failed: {error}"))?;
    let globset = compile_globs(&arguments.globs)?;
    let mut matching = Vec::new();
    for row in rows {
        let (relative, rank) = row.map_err(|error| error.to_string())?;
        let path = root.join(&relative);
        if in_scope(
            root,
            &path,
            &relative,
            &arguments.paths,
            globset.as_ref(),
            arguments.exclude_tests,
        ) {
            matching.push((path, relative, rank));
        }
    }
    let query_ms = query_started.elapsed().as_secs_f64() * 1000.0;
    let public_limit = limit.max(1) as usize;
    let mut results = Vec::new();
    for (path, relative, rank) in matching.iter().take(public_limit) {
        let evidence = representative_evidence(path, &terms);
        let anchor = evidence.lines.first();
        let line = anchor
            .and_then(|value| value.get("line"))
            .and_then(Value::as_u64)
            .unwrap_or(1);
        let column = anchor
            .and_then(|value| value.get("column"))
            .and_then(Value::as_u64)
            .unwrap_or(1);
        let command = if anchor.is_some() {
            format!("codeq context {relative}:{line}:{column}")
        } else {
            format!("codeq context {relative}")
        };
        results.push(json!({
            "name": relative,
            "kind": "File",
            "container": "",
            "path": path,
            "relative_path": relative,
            "line": line,
            "column": column,
            "source": "fts5",
            "evidence": "lexical",
            "is_test": target::is_test_path(path),
            "bm25": rank,
            "matched_terms": evidence.terms,
            "representative_lines": evidence.lines,
            "selection_command": command,
        }));
    }
    let mut value = common();
    value["status"] = Value::String("ok".to_owned());
    value["results"] = Value::Array(results);
    value["result_count"] = Value::from(matching.len().min(public_limit));
    value["total_candidates"] = Value::from(matching.len());
    value["truncated"] = Value::Bool(matching.len() > public_limit);
    value["ranking"] = json!({
        "engine": "sqlite_fts5_bm25",
        "terms": terms,
        "match_expression": expression,
        "tie_breaker": "relative_path",
    });
    value["index"] = json!({
        "storage": "memory_contentless",
        "file_count": files.len(),
        "source_bytes": index.source_bytes,
        "index_bytes": index.index_bytes,
        "refreshed": refreshed,
        "build_ms": build_ms,
        "query_ms": query_ms,
    });
    Ok(value)
}

fn visible_sources(root: &Path) -> Result<Vec<SourceFile>, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args([
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ])
        .output()
        .map_err(|error| format!("cannot enumerate Git-visible files: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "git ls-files failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let mut relative: Vec<_> = output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|raw| !raw.is_empty())
        .map(|raw| String::from_utf8_lossy(raw).into_owned())
        .collect();
    relative.sort();
    relative.dedup();
    let mut files = Vec::new();
    for relative in relative {
        let path = root.join(&relative);
        if !target::is_semantic_source(&path) || !path.is_file() {
            continue;
        }
        let Ok(metadata) = fs::metadata(&path) else {
            continue;
        };
        files.push(SourceFile {
            path,
            relative: relative.replace('\\', "/"),
            size: metadata.len(),
            modified_ns: metadata
                .modified()
                .ok()
                .and_then(|modified| modified.duration_since(UNIX_EPOCH).ok())
                .map_or(0, |duration| duration.as_nanos()),
        });
    }
    Ok(files)
}

fn build_index(files: &[SourceFile]) -> Result<(Connection, u64, u64), String> {
    let mut connection = Connection::open_in_memory()
        .map_err(|error| format!("cannot open in-memory SQLite: {error}"))?;
    connection
        .execute_batch(
            "CREATE TABLE files(rowid INTEGER PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE);
             CREATE VIRTUAL TABLE source_fts USING fts5(body, content='', tokenize='unicode61');",
        )
        .map_err(|error| {
            let detail = error.to_string();
            if detail.to_ascii_lowercase().contains("fts5") {
                "SQLite FTS5 is unavailable; use an FTS5-enabled build or `codeq find --text QUERY`"
                    .to_owned()
            } else {
                format!("cannot initialize in-memory FTS5 index: {error}")
            }
        })?;
    let transaction = connection
        .transaction()
        .map_err(|error| format!("cannot start FTS5 build: {error}"))?;
    let mut source_bytes = 0u64;
    for (position, file) in files.iter().enumerate() {
        let Ok(body) = fs::read_to_string(&file.path) else {
            continue;
        };
        let rowid = (position + 1) as i64;
        transaction
            .execute(
                "INSERT INTO files(rowid, relative_path) VALUES (?1, ?2)",
                params![rowid, file.relative],
            )
            .and_then(|_| {
                transaction.execute(
                    "INSERT INTO source_fts(rowid, body) VALUES (?1, ?2)",
                    params![rowid, body],
                )
            })
            .map_err(|error| format!("cannot build FTS5 index: {error}"))?;
        source_bytes += file.size;
    }
    transaction
        .commit()
        .map_err(|error| format!("cannot commit FTS5 index: {error}"))?;
    let page_count: i64 = connection
        .query_row("PRAGMA page_count", [], |row| row.get(0))
        .map_err(|error| error.to_string())?;
    let page_size: i64 = connection
        .query_row("PRAGMA page_size", [], |row| row.get(0))
        .map_err(|error| error.to_string())?;
    let index_bytes = u64::try_from(page_count.saturating_mul(page_size))
        .map_err(|error| format!("invalid SQLite index size: {error}"))?;
    Ok((connection, source_bytes, index_bytes))
}

fn lexical_terms(query: &str) -> Vec<String> {
    let mut terms = Vec::new();
    let mut seen = HashSet::new();
    let mut current = String::new();
    for character in query.chars().chain(std::iter::once(' ')) {
        let continues = if current.is_empty() {
            character.is_alphabetic() || character == '_'
        } else {
            character.is_alphanumeric() || character == '_'
        };
        if continues {
            current.push(character);
            continue;
        }
        if !current.is_empty() {
            let folded = current.to_lowercase();
            if seen.insert(folded) {
                terms.push(std::mem::take(&mut current));
            } else {
                current.clear();
            }
        }
    }
    terms
}

pub(crate) fn has_multiple_terms(query: &str) -> bool {
    lexical_terms(query).len() >= 2
}

fn representative_evidence(path: &Path, terms: &[String]) -> Evidence {
    let source = fs::read_to_string(path).unwrap_or_default();
    let lowered_terms: Vec<_> = terms.iter().map(|term| term.to_lowercase()).collect();
    let mut candidates = Vec::new();
    let mut all_matched = HashSet::new();
    for (index, text) in source.lines().enumerate() {
        let lowered = text.to_lowercase();
        let mut matched = Vec::new();
        let mut occurrences = 0usize;
        let mut first = None;
        for (term, lowered_term) in terms.iter().zip(&lowered_terms) {
            let positions: Vec<_> = lowered
                .match_indices(lowered_term)
                .map(|(at, _)| at)
                .collect();
            if positions.is_empty() {
                continue;
            }
            matched.push(term.clone());
            occurrences += positions.len();
            first = Some(first.map_or(positions[0], |current: usize| current.min(positions[0])));
            all_matched.insert(lowered_term.clone());
        }
        let Some(first) = first else {
            continue;
        };
        candidates.push((
            matched.len(),
            occurrences,
            index + 1,
            json!({
                "line": index + 1,
                "column": first + 1,
                "text": text,
                "matched_terms": matched,
                "text_truncated": false,
                "text_start_column": 1,
            }),
        ));
    }
    candidates.sort_by_key(|(matched, occurrences, line, _)| {
        (
            std::cmp::Reverse(*matched),
            std::cmp::Reverse(*occurrences),
            *line,
        )
    });
    let mut selected = Vec::new();
    let mut covered = HashSet::new();
    for (_, _, _, evidence) in candidates {
        let normalized: HashSet<_> = evidence["matched_terms"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_lowercase)
            .collect();
        if !selected.is_empty() && normalized.is_subset(&covered) {
            continue;
        }
        covered.extend(normalized);
        selected.push(evidence);
        if selected.len() >= 3 || covered.is_superset(&all_matched) {
            break;
        }
    }
    Evidence {
        lines: selected,
        terms: terms
            .iter()
            .filter(|term| all_matched.contains(&term.to_lowercase()))
            .cloned()
            .collect(),
    }
}

fn compile_globs(patterns: &[String]) -> Result<Option<GlobSet>, String> {
    if patterns.is_empty() {
        return Ok(None);
    }
    let mut builder = GlobSetBuilder::new();
    for pattern in patterns {
        builder
            .add(Glob::new(pattern).map_err(|error| format!("invalid glob {pattern:?}: {error}"))?);
    }
    builder
        .build()
        .map(Some)
        .map_err(|error| format!("invalid glob filter: {error}"))
}

fn in_scope(
    root: &Path,
    path: &Path,
    relative: &str,
    prefixes: &[String],
    globs: Option<&GlobSet>,
    exclude_tests: bool,
) -> bool {
    if exclude_tests && target::is_test_path(path) {
        return false;
    }
    if !prefixes.is_empty()
        && !prefixes.iter().any(|prefix| {
            let absolute = if Path::new(prefix).is_absolute() {
                PathBuf::from(prefix)
            } else {
                root.join(prefix)
            };
            path == absolute || path.starts_with(&absolute)
        })
    {
        return false;
    }
    globs.is_none_or(|set| {
        set.is_match(relative) || set.is_match(path.file_name().unwrap_or_default())
    })
}
