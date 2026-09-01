use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::Arc;
use std::time::Instant;

use serde_json::{Value, json};

use crate::cli::ReviewArgs;
use crate::dynamic;
use crate::lsp::LspProcess;
use crate::symbol::{Symbol, lsp_location};
use crate::target;
use crate::textsearch;
use crate::workspace::Workspace;

const LIMITATIONS: &[&str] = &[
    "deleted-file impact uses conservative base-side declaration extraction plus exact current-worktree lexical evidence; it is not an LSP call graph",
    "pure-rename impact uses current-path importers/references and may still miss runtime-only loading",
    "exact call edges are language-server-resolved and may omit runtime-only dispatch",
    "possible_dynamic_references classify exact LSP references heuristically; they are not runtime-proof call edges",
    "test discovery uses semantic references/callers plus test-path classification",
];

struct Change {
    status: char,
    similarity: Option<String>,
    old_path: Option<PathBuf>,
    path: PathBuf,
}

pub(crate) fn review(
    workspace: &Workspace,
    arguments: &ReviewArgs,
    limit: i64,
) -> Result<Value, String> {
    let started = Instant::now();
    let root = workspace.root();
    let resolved_base = if arguments.merge_base {
        git_text(root, &["merge-base", &arguments.base, "HEAD"])?
    } else {
        git_text(
            root,
            &[
                "rev-parse",
                "--verify",
                &format!("{}^{{commit}}", arguments.base),
            ],
        )?
    };
    let mut changes = changed_files(root, &resolved_base)?;
    let tracked: HashSet<_> = changes.iter().map(|change| change.path.clone()).collect();
    changes.extend(
        untracked_files(root)?
            .into_iter()
            .filter(|change| !tracked.contains(&change.path)),
    );
    let ranges = changed_ranges(root, &resolved_base)?;
    let discovery_ms = started.elapsed().as_secs_f64() * 1000.0;
    let analysis_started = Instant::now();
    let budget = limit.max(1) as usize;
    let mut annotated = Vec::new();
    let mut changed_symbols = Vec::new();
    let mut unsupported = Vec::new();
    for change in &changes {
        let mut item = change_value(change);
        if change.status == 'D' {
            item["base_analysis"] =
                deleted_base_analysis(root, &resolved_base, &change.path, budget);
            item["semantic_status"] = Value::String("deleted_base_analyzed".to_owned());
        } else if !target::is_semantic_source(&change.path) {
            item["semantic_status"] = Value::String("unsupported_language".to_owned());
            unsupported.push(Value::String(change.path.to_string_lossy().into_owned()));
        } else if !change.path.is_file() {
            item["semantic_status"] = Value::String("missing_from_worktree".to_owned());
        } else if let Some(spans) = ranges.get(&change.path) {
            let symbols = changed_symbols_for_file(workspace, &change.path, spans);
            item["semantic_status"] = Value::String(
                if symbols.is_empty() {
                    "no_enclosing_symbol"
                } else {
                    "analyzed"
                }
                .to_owned(),
            );
            changed_symbols.extend(symbols);
        } else if change.status == 'R' {
            item["rename_analysis"] = rename_analysis(workspace, &change.path, budget);
            item["semantic_status"] = Value::String("rename_analyzed".to_owned());
        } else {
            item["semantic_status"] =
                Value::String("rename_or_copy_without_content_changes".to_owned());
        }
        annotated.push(item);
    }
    changed_symbols.sort_by(|left, right| {
        (&left.path, left.line, &left.name).cmp(&(&right.path, right.line, &right.name))
    });
    changed_symbols.dedup_by(|left, right| {
        left.path == right.path && left.line == right.line && left.name == right.name
    });
    let truncated = changed_symbols.len() > budget;
    changed_symbols.truncate(budget);

    let mut details = Vec::new();
    let mut dynamic_reference_count = 0usize;
    let mut impacted_files = BTreeSet::new();
    let mut tests: BTreeMap<(PathBuf, u64), Value> = BTreeMap::new();
    for symbol in changed_symbols {
        let Some(project) = workspace.project_for_path(&symbol.path) else {
            continue;
        };
        let Ok(session) = workspace.session(&project) else {
            continue;
        };
        workspace.prewarm_documents(&symbol, 8);
        let eager_callers =
            if can_derive_python_callers(&symbol) && session.semantic_navigation_warmed() {
                None
            } else {
                Some(incoming_callers(root, &session, &symbol))
            };
        let references = semantic_references(root, &session, &symbol);
        let callers = eager_callers.unwrap_or_else(|| {
            python_callers_from_references(workspace, &references, &symbol)
                .unwrap_or_else(|| incoming_callers(root, &session, &symbol))
        });
        let direct_tests: Vec<_> = references
            .iter()
            .filter(|reference| is_test_path(value_path(reference)))
            .cloned()
            .collect();
        let source_references: Vec<_> = references
            .iter()
            .filter(|reference| !is_test_path(value_path(reference)))
            .cloned()
            .collect();
        let possible_dynamic =
            dynamic::classify_references(&source_references, &symbol.name, budget.min(5));
        dynamic_reference_count += possible_dynamic.len();
        for caller in &callers {
            let path = value_path(caller).to_owned();
            impacted_files.insert(path.clone());
            if is_test_path(&path) {
                tests.insert((path, integer(caller, "line")), caller.clone());
            }
        }
        for reference in &references {
            impacted_files.insert(value_path(reference).to_owned());
        }
        for test in &direct_tests {
            tests.insert(
                (value_path(test).to_owned(), integer(test, "line")),
                test.clone(),
            );
        }
        details.push(json!({
            "symbol": symbol_summary(&symbol),
            "callers": callers.into_iter().take(budget.min(5)).collect::<Vec<_>>(),
            "possible_dynamic_references": possible_dynamic,
            "tests": direct_tests.into_iter().take(budget.min(5)).collect::<Vec<_>>(),
            "reference_count": references.len(),
        }));
    }
    let changed_files: Vec<_> = annotated
        .iter()
        .filter_map(|item| item.get("path").cloned())
        .collect();
    for path in &changed_files {
        if let Some(path) = path.as_str() {
            impacted_files.remove(Path::new(path));
        }
    }
    let impacted_count = impacted_files.len();
    let test_count = tests.len();
    let impacted: Vec<_> = impacted_files
        .into_iter()
        .take(budget)
        .map(|path| Value::String(path.to_string_lossy().into_owned()))
        .collect();
    let returned_tests: Vec<_> = tests.into_values().take(budget).collect();
    Ok(json!({
        "status": "ok",
        "base": arguments.base,
        "requested_base": arguments.base,
        "base_mode": if arguments.merge_base { "merge-base" } else { "direct" },
        "resolved_base": resolved_base,
        "file_changes": annotated,
        "changed_files": changed_files,
        "changed_file_count": changes.len(),
        "deleted_file_count": changes.iter().filter(|change| change.status == 'D').count(),
        "renamed_file_count": changes.iter().filter(|change| change.status == 'R').count(),
        "untracked_file_count": changes.iter().filter(|change| change.status == 'U').count(),
        "changed_symbols": details,
        "changed_symbol_count": details.len(),
        "impacted_files": impacted,
        "impacted_file_count": impacted_count,
        "impacted_files_truncated": impacted_count > budget,
        "tests": returned_tests,
        "test_count": test_count,
        "tests_truncated": test_count > budget,
        "possible_dynamic_reference_count": dynamic_reference_count,
        "unsupported_changed_files": unsupported,
        "truncated": truncated,
        "limitations": LIMITATIONS,
        "_phase_ms": {
            "change_discovery": discovery_ms,
            "review_analysis": analysis_started.elapsed().as_secs_f64() * 1000.0,
        },
    }))
}

fn changed_files(root: &Path, base: &str) -> Result<Vec<Change>, String> {
    let output = git_output(
        root,
        &[
            "diff",
            "--no-ext-diff",
            "--name-status",
            "-z",
            "-M",
            base,
            "--",
        ],
    )?;
    let fields: Vec<_> = output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|field| !field.is_empty())
        .map(|field| String::from_utf8_lossy(field).into_owned())
        .collect();
    let mut changes = Vec::new();
    let mut index = 0;
    while index < fields.len() {
        let raw = &fields[index];
        index += 1;
        let Some(status) = raw.chars().next() else {
            continue;
        };
        if matches!(status, 'R' | 'C') {
            if index + 1 >= fields.len() {
                break;
            }
            let old_path = root.join(&fields[index]);
            let path = root.join(&fields[index + 1]);
            index += 2;
            changes.push(Change {
                status,
                similarity: raw
                    .get(1..)
                    .filter(|value| !value.is_empty())
                    .map(str::to_owned),
                old_path: Some(old_path),
                path,
            });
        } else {
            if index >= fields.len() {
                break;
            }
            let path = root.join(&fields[index]);
            index += 1;
            changes.push(Change {
                status,
                similarity: None,
                old_path: None,
                path,
            });
        }
    }
    Ok(changes)
}

fn untracked_files(root: &Path) -> Result<Vec<Change>, String> {
    let output = git_output(root, &["ls-files", "--others", "--exclude-standard", "-z"])?;
    Ok(output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|field| !field.is_empty())
        .map(|field| root.join(String::from_utf8_lossy(field).as_ref()))
        .filter(|path| path.is_file())
        .map(|path| Change {
            status: 'U',
            similarity: None,
            old_path: None,
            path,
        })
        .collect())
}

fn changed_ranges(root: &Path, base: &str) -> Result<HashMap<PathBuf, Vec<(u64, u64)>>, String> {
    let output = git_output(
        root,
        &[
            "diff",
            "--no-ext-diff",
            "--unified=0",
            "--find-renames",
            "--diff-filter=AMRC",
            base,
            "--",
        ],
    )?;
    let text = String::from_utf8_lossy(&output.stdout);
    let mut current = None;
    let mut ranges: HashMap<PathBuf, Vec<(u64, u64)>> = HashMap::new();
    for line in text.lines() {
        if let Some(path) = line.strip_prefix("+++ b/") {
            current = Some(root.join(path));
            continue;
        }
        if line == "+++ /dev/null" {
            current = None;
            continue;
        }
        let Some(path) = current.as_ref() else {
            continue;
        };
        let Some(rest) = line.strip_prefix("@@ ") else {
            continue;
        };
        let Some(new_range) = rest.split_whitespace().nth(1) else {
            continue;
        };
        let Some(new_range) = new_range.strip_prefix('+') else {
            continue;
        };
        let mut parts = new_range.split(',');
        let Ok(start) = parts.next().unwrap_or("0").parse::<u64>() else {
            continue;
        };
        let count = parts
            .next()
            .and_then(|value| value.parse().ok())
            .unwrap_or(1);
        let end = if count == 0 { start } else { start + count - 1 };
        ranges.entry(path.clone()).or_default().push((start, end));
    }
    for spans in ranges.values_mut() {
        spans.sort();
        let mut merged: Vec<(u64, u64)> = Vec::new();
        for (start, end) in spans.drain(..) {
            if let Some(last) = merged.last_mut()
                && start <= last.1 + 1
            {
                last.1 = last.1.max(end);
            } else {
                merged.push((start, end));
            }
        }
        *spans = merged;
    }
    Ok(ranges)
}

fn changed_symbols_for_file(
    workspace: &Workspace,
    path: &Path,
    ranges: &[(u64, u64)],
) -> Vec<Symbol> {
    let Some(project) = workspace.project_for_path(path) else {
        return Vec::new();
    };
    let Ok(symbols) = workspace.document_symbols(path, Some(&project)) else {
        return Vec::new();
    };
    let mut selected = Vec::new();
    for &(changed_start, changed_end) in ranges {
        let intersecting: Vec<_> = symbols
            .iter()
            .filter(|symbol| {
                let start = symbol.range.start.line + 1;
                let end = symbol.range.end.line + 1;
                !(end < changed_start || start > changed_end)
            })
            .collect();
        let mut pool: Vec<_> = intersecting
            .iter()
            .filter(|symbol| matches!(symbol.kind.as_str(), "Function" | "Method" | "Constructor"))
            .copied()
            .collect();
        if pool.is_empty() {
            pool = intersecting
                .iter()
                .filter(|symbol| {
                    matches!(
                        symbol.kind.as_str(),
                        "Class" | "Interface" | "Enum" | "Struct"
                    )
                })
                .copied()
                .collect();
        }
        if pool.is_empty() {
            if let Some(symbol) = intersecting.into_iter().min_by_key(|symbol| {
                symbol
                    .range
                    .end
                    .line
                    .saturating_sub(symbol.range.start.line)
            }) {
                selected.push(symbol.clone());
            }
            continue;
        }
        for candidate in &pool {
            let ancestor = pool.iter().any(|other| {
                candidate.name != other.name
                    && candidate.range.start.line <= other.range.start.line
                    && other.range.end.line <= candidate.range.end.line
            });
            if !ancestor {
                selected.push((*candidate).clone());
            }
        }
    }
    selected
}

fn semantic_references(root: &Path, session: &Arc<LspProcess>, symbol: &Symbol) -> Vec<Value> {
    let raw = session
        .references(&symbol.path, symbol.line, symbol.column)
        .unwrap_or_default();
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for item in raw {
        let Some((path, range)) = lsp_location(&item) else {
            continue;
        };
        let line = range.start.line + 1;
        let column = range.start.character + 1;
        if path.starts_with(root) && seen.insert((path.clone(), line, column)) {
            out.push(json!({"path": path, "line": line, "column": column}));
        }
    }
    out
}

fn incoming_callers(root: &Path, session: &Arc<LspProcess>, symbol: &Symbol) -> Vec<Value> {
    let item = python_call_item(symbol).or_else(|| {
        session
            .prepare_call_hierarchy(&symbol.path, symbol.line, symbol.column)
            .ok()
            .and_then(|items| items.into_iter().next())
    });
    let Some(item) = item else {
        return Vec::new();
    };
    let raw = session.incoming_calls(item).unwrap_or_default();
    let mut out = Vec::new();
    for edge in raw {
        let Some(item) = edge.get("from") else {
            continue;
        };
        let Some(entry) = call_item(item) else {
            continue;
        };
        if value_path(&entry).starts_with(root) {
            out.push(entry);
        }
    }
    out
}

fn python_callers_from_references(
    workspace: &Workspace,
    references: &[Value],
    symbol: &Symbol,
) -> Option<Vec<Value>> {
    if !can_derive_python_callers(symbol) {
        return None;
    }
    let mut callers = Vec::new();
    let mut seen = HashSet::new();
    for reference in references {
        if !dynamic::classify_python_call_reference(reference, &symbol.name)? {
            continue;
        }
        let location = crate::symbol::Location {
            path: value_path(reference).to_owned(),
            line: integer(reference, "line"),
            column: integer(reference, "column").max(1),
            source: "lsp",
        };
        let caller = workspace.symbol_at_location(&location)?;
        if !seen.insert((caller.path.clone(), caller.line, caller.name.clone())) {
            continue;
        }
        callers.push(json!({
            "name": caller.name,
            "kind": if matches!(caller.kind.as_str(), "Function" | "Method" | "Constructor") {
                "Function"
            } else {
                caller.kind.as_str()
            },
            "path": caller.path,
            "line": caller.line,
            "column": caller.column,
            "detail": "",
        }));
    }
    Some(callers)
}

fn can_derive_python_callers(symbol: &Symbol) -> bool {
    matches!(
        symbol
            .path
            .extension()
            .and_then(|extension| extension.to_str()),
        Some("py" | "pyi")
    ) && matches!(
        symbol.kind.as_str(),
        "Function" | "Method" | "Constructor" | "Class"
    ) && !dynamic::is_python_property(&symbol.path, symbol.line)
}

fn python_call_item(symbol: &Symbol) -> Option<Value> {
    if !matches!(
        symbol
            .path
            .extension()
            .and_then(|extension| extension.to_str()),
        Some("py" | "pyi")
    ) {
        return None;
    }
    let kind = match symbol.kind.as_str() {
        "Class" => 5,
        "Method" => 6,
        "Constructor" => 9,
        "Function" => 12,
        _ => return None,
    };
    let start = json!({
        "line": symbol.line.saturating_sub(1),
        "character": symbol.column.saturating_sub(1),
    });
    let end = json!({
        "line": symbol.line.saturating_sub(1),
        "character": symbol.column.saturating_sub(1) + symbol.name.encode_utf16().count() as u64,
    });
    Some(json!({
        "name": symbol.name,
        "kind": kind,
        "uri": format!("file://{}", symbol.path.display()),
        "range": {"start": start, "end": end},
        "selectionRange": {"start": start, "end": end},
    }))
}

fn call_item(item: &Value) -> Option<Value> {
    let range = item.get("selectionRange").or_else(|| item.get("range"))?;
    let raw = json!({"uri": item.get("uri")?, "range": range});
    let (path, range) = lsp_location(&raw)?;
    Some(json!({
        "name": item.get("name").and_then(Value::as_str).unwrap_or(""),
        "kind": kind_name(item.get("kind").and_then(Value::as_u64)),
        "path": path,
        "line": range.start.line + 1,
        "column": range.start.character + 1,
        "detail": item.get("detail").and_then(Value::as_str).unwrap_or(""),
    }))
}

fn deleted_base_analysis(root: &Path, base: &str, path: &Path, limit: usize) -> Value {
    let Ok(relative) = path.strip_prefix(root) else {
        return unavailable_base_analysis();
    };
    let Ok(text) = git_text(root, &["show", &format!("{base}:{}", relative.display())]) else {
        return unavailable_base_analysis();
    };
    let declarations = base_declarations(path, &text);
    let mut analyzed = Vec::new();
    for declaration in declarations.iter().take(limit) {
        let name = declaration["name"].as_str().unwrap_or("");
        let search = textsearch::search(root, name, limit.min(5) as i64, &[], &[], false)
            .unwrap_or_else(|_| json!({"results": [], "match_count": 0, "matching_line_count": 0, "truncated": false}));
        let results = search
            .get("results")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let (tests, references): (Vec<_>, Vec<_>) = results
            .into_iter()
            .partition(|item| item.get("is_test").and_then(Value::as_bool) == Some(true));
        analyzed.push(json!({
            "symbol": declaration,
            "evidence": "lexical",
            "residual_match_count": integer(&search, "match_count"),
            "residual_matching_line_count": integer(&search, "matching_line_count"),
            "residual_references": references,
            "tests": tests,
            "truncated": search.get("truncated").and_then(Value::as_bool).unwrap_or(false),
        }));
    }
    json!({
        "status": "ok",
        "evidence": "base_side_lexical",
        "base_symbol_count": declarations.len(),
        "base_symbols": analyzed,
        "truncated": declarations.len() > limit,
    })
}

fn unavailable_base_analysis() -> Value {
    json!({
        "status": "unavailable",
        "evidence": "base_side_lexical",
        "base_symbol_count": 0,
        "base_symbols": [],
        "truncated": false,
    })
}

fn base_declarations(path: &Path, text: &str) -> Vec<Value> {
    let mut declarations = Vec::new();
    for (index, line) in text.lines().enumerate() {
        let trimmed = line.trim_start();
        let (kind, tail) = if let Some(tail) = trimmed.strip_prefix("def ") {
            ("Function", tail)
        } else if let Some(tail) = trimmed.strip_prefix("async def ") {
            ("Function", tail)
        } else if let Some(tail) = trimmed.strip_prefix("class ") {
            ("Class", tail)
        } else if let Some(at) = ["function ", "class ", "interface ", "enum "]
            .iter()
            .find_map(|marker| trimmed.find(marker).map(|at| (marker, at)))
        {
            let (marker, start) = at;
            let kind = if *marker == "function " {
                "Function"
            } else {
                "Class"
            };
            let tail = &trimmed[start + marker.len()..];
            (kind, tail)
        } else {
            continue;
        };
        let name: String = tail
            .chars()
            .take_while(|character| character.is_alphanumeric() || matches!(*character, '_' | '$'))
            .collect();
        if name.is_empty() {
            continue;
        }
        declarations.push(json!({
            "name": name,
            "kind": kind,
            "path": path,
            "line": index + 1,
        }));
    }
    declarations
}

fn rename_analysis(workspace: &Workspace, path: &Path, limit: usize) -> Value {
    let Some(project) = workspace.project_for_path(path) else {
        return json!({"status": "unavailable", "evidence": "current_semantic", "reason": "file context unavailable"});
    };
    let symbols = workspace
        .document_symbols(path, Some(&project))
        .unwrap_or_default();
    let Ok(session) = workspace.session(&project) else {
        return json!({"status": "unavailable", "evidence": "current_semantic", "reason": "language server unavailable"});
    };
    let mut summaries = Vec::new();
    for symbol in symbols.into_iter().filter(|symbol| {
        matches!(
            symbol.kind.as_str(),
            "Function"
                | "Method"
                | "Constructor"
                | "Class"
                | "Interface"
                | "Enum"
                | "Struct"
                | "Constant"
        )
    }) {
        workspace.prewarm_documents(&symbol, 12);
        let references = semantic_references(workspace.root(), &session, &symbol);
        let (tests, sources): (Vec<_>, Vec<_>) = references
            .into_iter()
            .partition(|item| is_test_path(value_path(item)));
        summaries.push(json!({
            "symbol": symbol_summary(&symbol),
            "reference_count": tests.len() + sources.len(),
            "references": sources.into_iter().take(limit.min(5)).collect::<Vec<_>>(),
            "tests": tests.into_iter().take(limit.min(5)).collect::<Vec<_>>(),
        }));
        if summaries.len() >= limit {
            break;
        }
    }
    json!({
        "status": "ok",
        "evidence": "current_semantic",
        "importers": [],
        "importer_count": 0,
        "importers_truncated": false,
        "symbols": summaries,
        "symbols_truncated": false,
    })
}

fn change_value(change: &Change) -> Value {
    json!({
        "status": change.status.to_string(),
        "similarity": change.similarity,
        "old_path": change.old_path,
        "path": change.path,
    })
}

fn symbol_summary(symbol: &Symbol) -> Value {
    json!({
        "name": symbol.name,
        "kind": symbol.kind,
        "container": symbol.container,
        "path": symbol.path,
        "line": symbol.line,
        "column": symbol.column,
    })
}

fn git_text(root: &Path, arguments: &[&str]) -> Result<String, String> {
    let output = git_output(root, arguments)?;
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn git_output(root: &Path, arguments: &[&str]) -> Result<Output, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(arguments)
        .output()
        .map_err(|error| format!("cannot run git: {error}"))?;
    if output.status.success() {
        Ok(output)
    } else {
        Err(String::from_utf8_lossy(&output.stderr).trim().to_owned())
    }
}

fn value_path(value: &Value) -> &Path {
    value
        .get("path")
        .and_then(Value::as_str)
        .map(Path::new)
        .unwrap_or_else(|| Path::new(""))
}

fn integer(value: &Value, key: &str) -> u64 {
    value.get(key).and_then(Value::as_u64).unwrap_or(0)
}

fn is_test_path(path: &Path) -> bool {
    let value = format!(
        "/{}",
        path.to_string_lossy()
            .to_ascii_lowercase()
            .replace('\\', "/")
    );
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    value.contains("/tests/")
        || value.contains("/test/")
        || value.contains("/__tests__/")
        || name.starts_with("test_")
        || name.ends_with("_test.py")
        || name.contains(".test.")
        || name.contains(".spec.")
}

fn kind_name(kind: Option<u64>) -> &'static str {
    match kind {
        Some(5) => "Class",
        Some(6) => "Method",
        Some(9) => "Constructor",
        Some(11) => "Interface",
        Some(12) => "Function",
        Some(13) => "Variable",
        Some(14) => "Constant",
        _ => "Unknown",
    }
}
