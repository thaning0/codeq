use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use serde_json::{Map, Value, json};

use crate::cli::{ContextArgs, TraceArgs};
use crate::lsp::LspProcess;
use crate::symbol::{Location, Resolution, Symbol, lsp_location};
use crate::target;
use crate::textsearch;
use crate::workspace::Workspace;

const DEFAULT_SECTIONS: &[&str] = &[
    "source",
    "callers",
    "callees",
    "implementations",
    "tests",
    "references",
    "possible-dynamic-references",
];

pub(crate) fn context(
    workspace: &Workspace,
    arguments: &ContextArgs,
    limit: i64,
) -> Result<Value, String> {
    let started = Instant::now();
    let resolution = resolve(workspace, &arguments.target);
    let resolution_ms = started.elapsed().as_secs_f64() * 1000.0;
    let (symbol, requested_location, cursor_definition) = match resolution {
        Resolution::Found {
            symbol,
            requested_location,
            cursor_definition,
            ..
        } => (*symbol, requested_location, cursor_definition),
        other => return Ok(resolution_response(&arguments.target, other, resolution_ms)),
    };

    let selected = selected_sections(arguments);
    let project = workspace
        .project_for_path(&symbol.path)
        .ok_or_else(|| format!("unsupported source file: {}", symbol.path.display()))?;
    if cursor_definition
        || (is_qualified(&arguments.target)
            && matches!(
                symbol
                    .path
                    .extension()
                    .and_then(|extension| extension.to_str()),
                Some("py" | "pyi")
            ))
    {
        let _ = workspace.document_symbols(&symbol.path, Some(&project));
    }
    let session = workspace
        .session(&project)
        .map_err(|error| error.to_string())?;
    let budget = limit.max(1) as usize;
    let prewarm_started = Instant::now();
    if selected.iter().any(|section| {
        matches!(
            *section,
            "callers"
                | "callees"
                | "implementations"
                | "tests"
                | "references"
                | "possible-dynamic-references"
        )
    }) {
        workspace.prewarm_symbol(&symbol, budget as u64);
    }
    let prewarm_ms = prewarm_started.elapsed().as_secs_f64() * 1000.0;
    let neighborhood_started = Instant::now();

    let mut data = Map::new();
    data.insert("status".to_owned(), Value::String("ok".to_owned()));
    data.insert("evidence".to_owned(), Value::String("semantic".to_owned()));
    data.insert("target".to_owned(), Value::String(arguments.target.clone()));
    let mut resolved_symbol = symbol_value(&symbol, qualified_extras(&arguments.target, &symbol));
    if is_qualified(&arguments.target) {
        resolved_symbol["score"] = Value::from(10_000);
    }
    data.insert("symbol".to_owned(), resolved_symbol);
    data.insert(
        "section_selection".to_owned(),
        json!({
            "mode": if arguments.sections.is_empty() { "default" } else { "focused" },
            "selected": selected,
        }),
    );

    let hover = if selected.contains(&"source") {
        session
            .hover(&symbol.path, symbol.line, symbol.column)
            .ok()
            .map(|raw| hover_text(&raw))
            .unwrap_or_default()
    } else {
        String::new()
    };
    let references = if selected.iter().any(|section| {
        matches!(
            *section,
            "references" | "tests" | "possible-dynamic-references"
        )
    }) {
        locations(
            workspace.root(),
            session
                .references(&symbol.path, symbol.line, symbol.column)
                .unwrap_or_default(),
        )
    } else {
        Vec::new()
    };
    let callers = if selected
        .iter()
        .any(|section| matches!(*section, "callers" | "tests"))
    {
        call_neighbors(workspace.root(), &session, &symbol, "in")
    } else {
        Vec::new()
    };
    let callees = if selected.contains(&"callees") {
        call_neighbors(workspace.root(), &session, &symbol, "out")
    } else {
        Vec::new()
    };
    let implementations = if selected.contains(&"implementations") {
        let raw = session
            .implementations(&symbol.path, symbol.line, symbol.column)
            .unwrap_or_default();
        let mut out = Vec::new();
        let mut seen = HashSet::new();
        for item in raw {
            let Some((path, range)) = lsp_location(&item) else {
                continue;
            };
            if !repository_path(workspace.root(), &path) {
                continue;
            }
            if path == symbol.path
                && range.start.line + 1 == symbol.line
                && range.start.character + 1 == symbol.column
            {
                continue;
            }
            let location = Location {
                path,
                line: range.start.line + 1,
                column: range.start.character + 1,
                source: "lsp",
            };
            let Some(candidate) = workspace.symbol_at_location(&location) else {
                continue;
            };
            if candidate.path == symbol.path
                && candidate.line == symbol.line
                && candidate.name == symbol.name
            {
                continue;
            }
            if seen.insert((
                candidate.path.clone(),
                candidate.line,
                candidate.name.clone(),
            )) {
                out.push(symbol_value(&candidate, false));
            }
        }
        out
    } else {
        Vec::new()
    };

    let source_references: Vec<_> = references
        .iter()
        .filter(|reference| !is_test_path(value_path(reference)))
        .cloned()
        .collect();
    let mut section_metadata = Map::new();
    if selected.contains(&"source") {
        let (bounded_hover, hover_truncated) = bounded_text(&hover, 4_000);
        data.insert("hover".to_owned(), Value::String(bounded_hover));
        data.insert("hover_truncated".to_owned(), Value::Bool(hover_truncated));
        data.insert(
            "source".to_owned(),
            source_snippet(&symbol.path, symbol.line, 2, 12),
        );
    }
    if selected.contains(&"callers") {
        insert_section(
            &mut data,
            &mut section_metadata,
            "callers",
            callers.clone(),
            budget,
        );
    }
    if selected.contains(&"callees") {
        insert_section(&mut data, &mut section_metadata, "callees", callees, budget);
    }
    if selected.contains(&"implementations") {
        insert_section(
            &mut data,
            &mut section_metadata,
            "implementations",
            implementations,
            budget,
        );
    }
    if selected.contains(&"references") {
        insert_section(
            &mut data,
            &mut section_metadata,
            "references",
            source_references.clone(),
            budget,
        );
    }
    if selected.contains(&"tests") {
        let tests = test_evidence(workspace.root(), &symbol, &references, &callers, budget);
        let metadata = test_section_metadata(&tests, budget);
        data.insert(
            "tests".to_owned(),
            Value::Array(tests.into_iter().take(budget).collect()),
        );
        section_metadata.insert("tests".to_owned(), metadata);
    }
    if selected.contains(&"possible-dynamic-references") {
        insert_section(
            &mut data,
            &mut section_metadata,
            "possible_dynamic_references",
            Vec::new(),
            budget,
        );
    }
    data.insert(
        "section_metadata".to_owned(),
        Value::Object(section_metadata),
    );

    if let Some(requested) = requested_location {
        data.insert("requested_location".to_owned(), location_value(&requested));
        data.insert(
            "cursor_definition".to_owned(),
            Value::Bool(cursor_definition),
        );
        if selected.contains(&"source") {
            data.insert(
                "request_source".to_owned(),
                source_snippet(&requested.path, requested.line, 2, 4),
            );
        }
    }
    if let Some(lines) = arguments.lines {
        let anchor_path = requested_location_path(&data).unwrap_or(&symbol.path);
        let anchor_line = requested_location_line(&data).unwrap_or(symbol.line);
        data.insert(
            "line_window".to_owned(),
            source_window(workspace.root(), anchor_path, anchor_line, u64::from(lines)),
        );
    }
    data.insert(
        "_phase_ms".to_owned(),
        json!({
            "resolution": resolution_ms,
            "prewarm": prewarm_ms,
            "semantic_neighborhood": neighborhood_started.elapsed().as_secs_f64() * 1000.0,
        }),
    );
    Ok(Value::Object(data))
}

pub(crate) fn trace(
    workspace: &Workspace,
    arguments: &TraceArgs,
    global_limit: i64,
) -> Result<Value, String> {
    let resolution = resolve(workspace, &arguments.target);
    let symbol = match resolution {
        Resolution::Found { symbol, .. } => *symbol,
        other => return Ok(resolution_response(&arguments.target, other, 0.0)),
    };
    let project = workspace
        .project_for_path(&symbol.path)
        .ok_or_else(|| format!("unsupported source file: {}", symbol.path.display()))?;
    if is_qualified(&arguments.target)
        && matches!(
            symbol
                .path
                .extension()
                .and_then(|extension| extension.to_str()),
            Some("py" | "pyi")
        )
    {
        let _ = workspace.document_symbols(&symbol.path, Some(&project));
    }
    let session = workspace
        .session(&project)
        .map_err(|error| error.to_string())?;
    let _global_limit = global_limit;
    let limit = arguments.node_limit.max(1) as usize;
    let direction = if arguments.incoming {
        "in"
    } else if arguments.outgoing {
        "out"
    } else {
        "both"
    };
    if matches!(direction, "in" | "both") {
        workspace.prewarm_symbol(&symbol, limit as u64);
        let _ = session.prepare_call_hierarchy(&symbol.path, symbol.line, symbol.column);
    }
    let roots = session
        .prepare_call_hierarchy(&symbol.path, symbol.line, symbol.column)
        .map_err(|error| error.to_string())?;
    let root = roots.first().cloned();
    if direction != "both" {
        let branch = build_trace_branch(
            workspace.root(),
            &session,
            &arguments.target,
            &symbol,
            root.as_ref(),
            direction,
            arguments.depth,
            limit,
        );
        return Ok(json!({"status": "ok", "evidence": "semantic"})
            .as_object()
            .unwrap()
            .iter()
            .chain(branch.as_object().unwrap().iter())
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect::<Map<String, Value>>()
            .into());
    }
    let incoming = build_trace_branch(
        workspace.root(),
        &session,
        &arguments.target,
        &symbol,
        root.as_ref(),
        "in",
        arguments.depth,
        limit,
    );
    let outgoing = build_trace_branch(
        workspace.root(),
        &session,
        &arguments.target,
        &symbol,
        root.as_ref(),
        "out",
        arguments.depth,
        limit,
    );
    Ok(json!({
        "status": "ok",
        "evidence": "semantic",
        "target": arguments.target,
        "direction": "both",
        "depth": arguments.depth,
        "node_count": integer(&incoming, "node_count") + integer(&outgoing, "node_count"),
        "node_limit": limit,
        "truncated": boolean(&incoming, "truncated") || boolean(&outgoing, "truncated"),
        "root": incoming.get("root").cloned().unwrap_or_else(|| symbol_summary(&symbol)),
        "traces": {"in": incoming, "out": outgoing},
        "hint": "trace defaults to both directions; use --in or --out to narrow future queries",
    }))
}

fn resolve(workspace: &Workspace, target_value: &str) -> Resolution {
    if let Some(explicit) = target::explicit_path(target_value, workspace.root()) {
        return workspace.resolve_location(
            &explicit.path,
            explicit.line.unwrap_or(1),
            explicit.column.unwrap_or(1),
            explicit.column.is_some() && target_value.matches(':').count() >= 2,
        );
    }
    if is_qualified(target_value) {
        workspace.resolve_qualified(target_value)
    } else {
        workspace.resolve_bare(target_value)
    }
}

fn resolution_response(target: &str, resolution: Resolution, resolution_ms: f64) -> Value {
    let (status, reason, candidates) = match resolution {
        Resolution::NotFound { reason, candidates } => ("not_found", reason, candidates),
        Resolution::Ambiguous { reason, candidates } => ("ambiguous", reason, candidates),
        Resolution::Found { .. } => unreachable!(),
    };
    json!({
        "status": status,
        "target": target,
        "reason": reason,
        "candidates": candidates.into_iter().map(|symbol| symbol_value(&symbol, false)).collect::<Vec<_>>(),
        "_phase_ms": {"resolution": resolution_ms, "prewarm": 0.0, "semantic_neighborhood": 0.0},
    })
}

fn selected_sections(arguments: &ContextArgs) -> Vec<&str> {
    let mut selected = Vec::new();
    let source: Vec<&str> = if arguments.sections.is_empty() {
        DEFAULT_SECTIONS.to_vec()
    } else {
        arguments.sections.iter().map(String::as_str).collect()
    };
    for section in source {
        if !selected.contains(&section) {
            selected.push(section);
        }
    }
    if arguments.lexical_references.is_some() && !selected.contains(&"lexical-references") {
        selected.push("lexical-references");
    }
    selected
}

fn is_qualified(value: &str) -> bool {
    let mut parts = value.split('.');
    let Some(first) = parts.next() else {
        return false;
    };
    let remaining: Vec<_> = parts.collect();
    !remaining.is_empty() && identifier(first) && remaining.iter().all(|part| identifier(part))
}

fn identifier(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphabetic() || matches!(byte, b'_' | b'$'))
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'$'))
}

fn qualified_extras(target: &str, symbol: &Symbol) -> bool {
    is_qualified(target)
        && matches!(
            symbol
                .path
                .extension()
                .and_then(|extension| extension.to_str()),
            Some("ts" | "tsx" | "js" | "jsx" | "mjs" | "cjs")
        )
}

fn symbol_value(symbol: &Symbol, exact_definition: bool) -> Value {
    let mut value = serde_json::to_value(symbol).expect("symbol serialization");
    if exact_definition {
        value["exact_definition"] = Value::Bool(true);
    }
    value
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

fn location_value(location: &Location) -> Value {
    json!({
        "path": location.path,
        "line": location.line,
        "column": location.column,
        "source": location.source,
    })
}

fn locations(root: &Path, raw: Vec<Value>) -> Vec<Value> {
    let mut locations = Vec::new();
    let mut seen = HashSet::new();
    for item in raw {
        let Some((path, range)) = lsp_location(&item) else {
            continue;
        };
        let line = range.start.line + 1;
        let column = range.start.character + 1;
        if repository_path(root, &path) && seen.insert((path.clone(), line, column)) {
            locations.push(json!({"path": path, "line": line, "column": column}));
        }
    }
    locations
}

fn call_neighbors(
    root: &Path,
    session: &Arc<LspProcess>,
    symbol: &Symbol,
    direction: &str,
) -> Vec<Value> {
    let Ok(roots) = session.prepare_call_hierarchy(&symbol.path, symbol.line, symbol.column) else {
        return Vec::new();
    };
    let Some(root_item) = roots.into_iter().next() else {
        return Vec::new();
    };
    let raw = if direction == "in" {
        session.incoming_calls(root_item)
    } else {
        session.outgoing_calls(root_item)
    }
    .unwrap_or_default();
    let edge = if direction == "in" { "from" } else { "to" };
    let mut out = Vec::new();
    let mut seen = HashSet::new();
    for item in raw {
        let Some(call) = item.get(edge) else {
            continue;
        };
        let Some(entry) = call_item(call) else {
            continue;
        };
        let path = value_path(&entry);
        let line = entry.get("line").and_then(Value::as_u64).unwrap_or(1);
        let name = entry.get("name").and_then(Value::as_str).unwrap_or("");
        if repository_path(root, path) && seen.insert((path.to_owned(), line, name.to_owned())) {
            out.push(entry);
        }
    }
    out
}

fn call_item(item: &Value) -> Option<Value> {
    let uri = item.get("uri")?.as_str()?;
    let range = item.get("selectionRange").or_else(|| item.get("range"))?;
    let synthetic = json!({"uri": uri, "range": range});
    let (path, range) = lsp_location(&synthetic)?;
    Some(json!({
        "name": item.get("name").and_then(Value::as_str).unwrap_or(""),
        "kind": kind_name(item.get("kind").and_then(Value::as_u64)),
        "path": path,
        "line": range.start.line + 1,
        "column": range.start.character + 1,
        "detail": item.get("detail").and_then(Value::as_str).unwrap_or(""),
    }))
}

#[allow(clippy::too_many_arguments)]
fn build_trace_branch(
    root: &Path,
    session: &Arc<LspProcess>,
    target: &str,
    symbol: &Symbol,
    root_item: Option<&Value>,
    direction: &str,
    depth: usize,
    limit: usize,
) -> Value {
    let Some(root_item) = root_item else {
        let root_value = symbol_summary(symbol);
        return json!({
            "target": target,
            "direction": direction,
            "depth": depth,
            "root": root_value,
            "tree": {"node": symbol_summary(symbol), "children": []},
            "node_count": 1,
            "node_limit": limit,
            "truncated": false,
            "note": "language server returned no call hierarchy for this position",
        });
    };
    let mut seen = HashSet::new();
    let mut count = 0usize;
    let mut truncated = false;
    let tree = walk_trace(
        root,
        session,
        root_item,
        direction,
        depth,
        limit,
        &mut seen,
        &mut count,
        &mut truncated,
    )
    .unwrap_or_else(|| json!({"node": symbol_summary(symbol), "children": []}));
    json!({
        "target": target,
        "direction": direction,
        "depth": depth,
        "node_count": count,
        "node_limit": limit,
        "truncated": truncated,
        "root": call_item(root_item).unwrap_or_else(|| symbol_summary(symbol)),
        "tree": tree,
    })
}

#[allow(clippy::too_many_arguments)]
fn walk_trace(
    root: &Path,
    session: &Arc<LspProcess>,
    item: &Value,
    direction: &str,
    remaining: usize,
    limit: usize,
    seen: &mut HashSet<(PathBuf, u64, String)>,
    count: &mut usize,
    truncated: &mut bool,
) -> Option<Value> {
    if *count >= limit {
        *truncated = true;
        return None;
    }
    let entry = call_item(item)?;
    let key = (
        value_path(&entry).to_owned(),
        entry.get("line").and_then(Value::as_u64).unwrap_or(1),
        entry
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
    );
    let cycle = !seen.insert(key);
    *count += 1;
    let mut node = json!({"node": entry, "children": []});
    if cycle {
        node["cycle"] = Value::Bool(true);
        return Some(node);
    }
    if remaining == 0 {
        return Some(node);
    }
    let raw = if direction == "in" {
        session.incoming_calls(item.clone())
    } else {
        session.outgoing_calls(item.clone())
    }
    .unwrap_or_default();
    let edge = if direction == "in" { "from" } else { "to" };
    for call in raw {
        let Some(child) = call.get(edge) else {
            continue;
        };
        let Some(entry) = call_item(child) else {
            continue;
        };
        if !repository_path(root, value_path(&entry)) {
            continue;
        }
        if *count >= limit {
            *truncated = true;
            break;
        }
        if let Some(child_node) = walk_trace(
            root,
            session,
            child,
            direction,
            remaining - 1,
            limit,
            seen,
            count,
            truncated,
        ) {
            node["children"]
                .as_array_mut()
                .expect("trace children array")
                .push(child_node);
        }
    }
    Some(node)
}

fn hover_text(raw: &Value) -> String {
    match raw.get("contents") {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Object(value)) => value
            .get("value")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        Some(Value::Array(values)) => values
            .iter()
            .filter_map(|value| match value {
                Value::String(value) => Some(value.as_str()),
                Value::Object(value) => value.get("value").and_then(Value::as_str),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

fn insert_section(
    data: &mut Map<String, Value>,
    metadata: &mut Map<String, Value>,
    name: &str,
    values: Vec<Value>,
    limit: usize,
) {
    let total = values.len();
    data.insert(
        name.to_owned(),
        Value::Array(values.into_iter().take(limit).collect()),
    );
    metadata.insert(name.to_owned(), known_metadata(total, limit));
}

fn known_metadata(total: usize, limit: usize) -> Value {
    json!({
        "returned_count": total.min(limit),
        "total_count": total,
        "total_is_exact": true,
        "total_lower_bound": total,
        "truncated": total > limit,
    })
}

fn test_section_metadata(tests: &[Value], limit: usize) -> Value {
    let mut direct = 0;
    let mut caller = 0;
    let mut module_import = 0;
    let mut lexical = 0;
    for test in tests.iter().take(limit) {
        match test.get("evidence_type").and_then(Value::as_str) {
            Some("direct_semantic_reference") => direct += 1,
            Some("semantic_caller") => caller += 1,
            Some("module_import") => module_import += 1,
            Some("exact_lexical_reference") => lexical += 1,
            _ => {}
        }
    }
    let mut metadata = known_metadata(tests.len(), limit);
    metadata["discovery_truncated"] = Value::Bool(false);
    metadata["returned_evidence_counts"] = json!({
        "direct_semantic_reference": direct,
        "semantic_caller": caller,
        "module_import": module_import,
        "exact_lexical_reference": lexical,
    });
    metadata
}

fn test_evidence(
    root: &Path,
    symbol: &Symbol,
    references: &[Value],
    callers: &[Value],
    limit: usize,
) -> Vec<Value> {
    let mut tests = Vec::new();
    for reference in references {
        if !is_test_path(value_path(reference)) {
            continue;
        }
        tests.push(json!({
            "path": value_path(reference),
            "line": integer(reference, "line"),
            "column": integer(reference, "column"),
            "evidence": "semantic",
            "evidence_type": "direct_semantic_reference",
            "confidence": "direct",
            "reason": {
                "relationship": "references_symbol",
                "source": "language_server",
                "symbol": symbol.name,
            },
        }));
    }
    for caller in callers {
        if !is_test_path(value_path(caller)) {
            continue;
        }
        let mut value = caller.clone();
        value["evidence"] = Value::String("semantic".to_owned());
        value["evidence_type"] = Value::String("semantic_caller".to_owned());
        value["confidence"] = Value::String("candidate".to_owned());
        value["reason"] = json!({
            "relationship": "contains_incoming_caller",
            "source": "language_server",
            "symbol": symbol.name,
            "caller": caller.get("name").and_then(Value::as_str).unwrap_or(""),
        });
        tests.push(value);
    }
    tests.extend(module_import_evidence(root, symbol));
    let direct_lines: HashSet<_> = references
        .iter()
        .filter(|reference| is_test_path(value_path(reference)))
        .map(|reference| (value_path(reference).to_owned(), integer(reference, "line")))
        .collect();
    if let Ok(search) = textsearch::search(root, &symbol.name, i64::MAX, &[], &[], false)
        && let Some(results) = search.get("results").and_then(Value::as_array)
    {
        for result in results {
            if result.get("is_test").and_then(Value::as_bool) != Some(true) {
                continue;
            }
            if direct_lines.contains(&(value_path(result).to_owned(), integer(result, "line"))) {
                continue;
            }
            let mut value = result.clone();
            value["evidence"] = Value::String("lexical".to_owned());
            value["evidence_type"] = Value::String("exact_lexical_reference".to_owned());
            value["confidence"] = Value::String("candidate".to_owned());
            value["reason"] = json!({
                "relationship": "contains_exact_symbol_text",
                "source": "git-grep",
                "symbol": symbol.name,
                "query": symbol.name,
            });
            tests.push(value);
            if tests.len() >= limit.saturating_mul(4).max(20) {
                break;
            }
        }
    }
    tests
}

fn module_import_evidence(root: &Path, symbol: &Symbol) -> Vec<Value> {
    if symbol
        .path
        .extension()
        .and_then(|extension| extension.to_str())
        != Some("py")
    {
        return Vec::new();
    }
    let module = symbol
        .path
        .strip_prefix(root)
        .unwrap_or(&symbol.path)
        .with_extension("")
        .components()
        .map(|component| component.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join(".");
    let mut out = Vec::new();
    for path in visible_source_files(root) {
        if !is_test_path(&path)
            || path.extension().and_then(|extension| extension.to_str()) != Some("py")
        {
            continue;
        }
        let Ok(source) = fs::read_to_string(&path) else {
            continue;
        };
        for (index, line) in source.lines().enumerate() {
            let trimmed = line.trim_start();
            let prefix = format!("from {module} import ");
            if !trimmed.starts_with(&prefix) || !trimmed[prefix.len()..].contains(&symbol.name) {
                continue;
            }
            let leading = line.len() - trimmed.len();
            out.push(json!({
                "path": path,
                "line": index + 1,
                "column": leading + 6,
                "text": line,
                "specifier": module,
                "evidence": "lexical",
                "evidence_type": "module_import",
                "confidence": "candidate",
                "reason": {
                    "relationship": "imports_defining_module",
                    "source": "resolved_import",
                    "symbol": symbol.name,
                    "specifier": module,
                },
            }));
        }
    }
    out
}

fn visible_source_files(root: &Path) -> Vec<PathBuf> {
    let output = std::process::Command::new("git")
        .arg("-C")
        .arg(root)
        .args([
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ])
        .output();
    let Ok(output) = output else {
        return Vec::new();
    };
    if !output.status.success() {
        return Vec::new();
    }
    let mut paths: Vec<_> = output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|raw| !raw.is_empty())
        .map(|raw| root.join(String::from_utf8_lossy(raw).as_ref()))
        .filter(|path| target::is_semantic_source(path) && path.is_file())
        .collect();
    paths.sort();
    paths
}

fn source_snippet(path: &Path, line: u64, before: u64, after: u64) -> Value {
    let Ok(source) = fs::read_to_string(path) else {
        return json!({"start_line": line, "text": "", "truncated": false});
    };
    let lines: Vec<_> = source.lines().collect();
    let start = line.saturating_sub(before).max(1);
    let end = (line + after).min(lines.len() as u64);
    let rendered = (start..=end)
        .map(|number| format!("{number:>5}  {}", lines[(number - 1) as usize]))
        .collect::<Vec<_>>()
        .join("\n");
    json!({"start_line": start, "end_line": end, "text": rendered, "truncated": false})
}

fn source_window(root: &Path, path: &Path, start: u64, requested: u64) -> Value {
    let source = fs::read_to_string(path).unwrap_or_default();
    let lines: Vec<_> = source.lines().collect();
    let available_end = (start + requested - 1).min(lines.len() as u64);
    let mut rendered = Vec::new();
    let mut line_truncated = false;
    for number in start..=available_end {
        let raw = lines[(number - 1) as usize];
        let mut content: String = raw.chars().take(500).collect();
        if raw.chars().count() > 500 {
            content.truncate(content.len().saturating_sub(3));
            content.push_str("...");
            line_truncated = true;
        }
        rendered.push(format!("{number:>5}  {content}"));
    }
    let mut text = rendered.join("\n");
    let mut payload_truncated = false;
    let mut returned = rendered.len() as u64;
    if text.chars().count() > 100_000 {
        payload_truncated = true;
        while text.chars().count() > 100_000 && returned > 0 {
            returned -= 1;
            text = rendered[..returned as usize].join("\n");
        }
    }
    let next = if start + returned <= available_end {
        Some(start + returned)
    } else {
        None
    };
    let mut value = json!({
        "path": path,
        "start_line": start,
        "end_line": if returned == 0 { start - 1 } else { start + returned - 1 },
        "requested_line_count": requested,
        "returned_line_count": returned,
        "text": text,
        "line_truncated": line_truncated,
        "payload_truncated": payload_truncated,
        "truncated": line_truncated || payload_truncated,
        "next_line": next,
    });
    if payload_truncated {
        let relative = path.strip_prefix(root).unwrap_or(path).to_string_lossy();
        value["recovery_command"] = Value::String(format!(
            "codeq context {relative}:{} --lines {}",
            next.unwrap_or(start),
            requested.saturating_sub(returned).max(1)
        ));
    }
    value
}

fn requested_location_path(data: &Map<String, Value>) -> Option<&Path> {
    data.get("requested_location")
        .and_then(|value| value.get("path"))
        .and_then(Value::as_str)
        .map(Path::new)
}

fn requested_location_line(data: &Map<String, Value>) -> Option<u64> {
    data.get("requested_location")
        .and_then(|value| value.get("line"))
        .and_then(Value::as_u64)
}

fn bounded_text(value: &str, max_chars: usize) -> (String, bool) {
    if value.chars().count() <= max_chars {
        return (value.to_owned(), false);
    }
    let mut bounded: String = value.chars().take(max_chars.saturating_sub(3)).collect();
    bounded.push_str("...");
    (bounded, true)
}

fn repository_path(root: &Path, path: &Path) -> bool {
    path.starts_with(root)
        && !path.components().any(|component| {
            matches!(
                component.as_os_str().to_str(),
                Some("node_modules" | ".venv" | "venv" | ".next" | "dist" | "build")
            )
        })
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

fn boolean(value: &Value, key: &str) -> bool {
    value.get(key).and_then(Value::as_bool).unwrap_or(false)
}

fn kind_name(kind: Option<u64>) -> &'static str {
    match kind {
        Some(1) => "File",
        Some(2) => "Module",
        Some(3) => "Namespace",
        Some(4) => "Package",
        Some(5) => "Class",
        Some(6) => "Method",
        Some(7) => "Property",
        Some(8) => "Field",
        Some(9) => "Constructor",
        Some(10) => "Enum",
        Some(11) => "Interface",
        Some(12) => "Function",
        Some(13) => "Variable",
        Some(14) => "Constant",
        Some(15) => "String",
        Some(16) => "Number",
        Some(17) => "Boolean",
        Some(18) => "Array",
        Some(19) => "Object",
        Some(20) => "Key",
        Some(21) => "Null",
        Some(22) => "EnumMember",
        Some(23) => "Struct",
        Some(24) => "Event",
        Some(25) => "Operator",
        Some(26) => "TypeParameter",
        _ => "Unknown",
    }
}
