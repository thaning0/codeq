use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;
use std::time::Instant;

use globset::Glob;
use serde_json::{Map, Value, json};

use crate::cli::{ContextArgs, FindArgs, FindMode, TraceArgs};
use crate::dynamic;
use crate::lsp::LspProcess;
use crate::symbol::{Location, Position, Range, Resolution, Symbol, lsp_location};
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

pub(crate) fn find(
    workspace: &Workspace,
    arguments: &FindArgs,
    limit: i64,
) -> Result<Value, String> {
    let query = arguments.query.trim();
    let hits = lexical_hits(workspace.root(), query, 80.max(limit.max(1) as usize * 8));
    let mut projects = Vec::new();
    for hit in &hits {
        if let Some(project) = workspace.project_for_path(value_path(hit))
            && !projects.contains(&project)
        {
            projects.push(project);
        }
    }
    if projects.is_empty() {
        projects.extend_from_slice(workspace.projects());
    }
    projects.sort();

    let mut results = Vec::new();
    if identifier(query) {
        results.extend(
            workspace
                .exact_document_candidates(query, 80.max(limit.max(1) as usize * 4))
                .into_iter()
                .filter(|symbol| in_find_scope(workspace.root(), &symbol.path, arguments))
                .map(|symbol| symbol_value(&symbol, true)),
        );
    }
    let search_terms = identifier_tokens(query);
    for project in &projects {
        let Ok(session) = workspace.session(project) else {
            continue;
        };
        if project.family.as_str() == "typescript" {
            let mut primed = 0;
            for hit in &hits {
                let path = value_path(hit);
                if workspace.project_for_path(path).as_ref() != Some(project) {
                    continue;
                }
                if workspace.document_symbols(path, Some(project)).is_ok() {
                    primed += 1;
                }
                if primed >= 4 {
                    break;
                }
            }
        }
        for term in search_terms.iter().take(3) {
            for item in session.workspace_symbols(term).unwrap_or_default() {
                if let Some(value) = workspace_symbol_value(&item)
                    && in_find_scope(workspace.root(), value_path(&value), arguments)
                {
                    results.push(value);
                }
            }
        }
    }
    for hit in hits.iter().take(16) {
        let path = value_path(hit);
        let Some(project) = workspace.project_for_path(path) else {
            continue;
        };
        let Ok(symbols) = workspace.document_symbols(path, Some(&project)) else {
            continue;
        };
        let hit_line = integer(hit, "line");
        let mut mapped = false;
        for symbol in &symbols {
            let start = symbol.range.start.line + 1;
            let end = symbol.range.end.line + 1;
            let score = fuzzy_score(query, &symbol.name, &symbol.container, &symbol.path);
            if start <= hit_line && hit_line <= end {
                mapped = true;
                let mut value = symbol_value(symbol, false);
                value["lexical_match_score"] = Value::from(integer(hit, "match_score"));
                value["match_text"] = hit.get("text").cloned().unwrap_or(Value::Null);
                results.push(value);
            } else if score > 0 {
                results.push(symbol_value(symbol, false));
            }
        }
        if !mapped && !symbols.is_empty() {
            let following = symbols
                .iter()
                .filter(|symbol| symbol.line >= hit_line && symbol.line - hit_line <= 12)
                .min_by_key(|symbol| (symbol.line - hit_line, std::cmp::Reverse(priority(symbol))));
            let nearest = following.or_else(|| {
                symbols.iter().min_by_key(|symbol| {
                    (
                        symbol.line.abs_diff(hit_line),
                        std::cmp::Reverse(priority(symbol)),
                    )
                })
            });
            if let Some(symbol) = nearest
                && symbol.line.abs_diff(hit_line) <= if following.is_some() { 12 } else { 8 }
            {
                let mut value = symbol_value(symbol, false);
                value["lexical_match_score"] = Value::from(integer(hit, "match_score"));
                value["match_text"] = hit.get("text").cloned().unwrap_or(Value::Null);
                results.push(value);
            }
        }
    }

    let mut deduplicated: std::collections::HashMap<(PathBuf, u64, String), Value> =
        std::collections::HashMap::new();
    for mut item in results {
        let path = value_path(&item).to_owned();
        if !in_find_scope(workspace.root(), &path, arguments) {
            continue;
        }
        let name = item
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        let container = item.get("container").and_then(Value::as_str).unwrap_or("");
        let score = (fuzzy_score(query, &name, container, &path) as i64
            + (integer(&item, "lexical_match_score").min(5) * 700) as i64
            + agent_ranking_adjustment(query, arguments.kind.as_deref(), &item))
        .max(0) as u64;
        item["score"] = Value::from(score);
        let key = (path, integer(&item, "line"), name);
        let replace = deduplicated
            .get(&key)
            .is_none_or(|current| integer(current, "score") < score);
        if replace {
            deduplicated.insert(key, item);
        }
    }
    let mut ordered: Vec<_> = deduplicated.into_values().collect();
    if let Some(kind) = arguments.kind.as_deref() {
        let requested = kind.trim().to_ascii_lowercase();
        ordered.retain(|item| {
            let actual = item
                .get("kind")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_ascii_lowercase();
            match requested.as_str() {
                "function" => matches!(actual.as_str(), "function" | "method" | "constructor"),
                "class" => matches!(actual.as_str(), "class" | "interface" | "struct" | "enum"),
                "test" => is_test_path(value_path(item)),
                _ => actual == requested,
            }
        });
    }
    ordered.sort_by(|left, right| {
        (
            std::cmp::Reverse(integer(left, "score")),
            std::cmp::Reverse(value_priority(left)),
            value_path(left),
            integer(left, "line"),
        )
            .cmp(&(
                std::cmp::Reverse(integer(right, "score")),
                std::cmp::Reverse(value_priority(right)),
                value_path(right),
                integer(right, "line"),
            ))
    });
    for item in &mut ordered {
        let path = value_path(item);
        let relative = path.strip_prefix(workspace.root()).unwrap_or(path);
        item["selection_command"] = Value::String(format!(
            "codeq context {}:{}:{}",
            relative.display(),
            integer(item, "line"),
            integer(item, "column")
        ));
    }
    let total = ordered.len();
    let public_limit = limit.max(1) as usize;
    ordered.truncate(public_limit);
    Ok(json!({
        "status": "ok",
        "mode": "symbol",
        "search_mode": "symbol",
        "query": arguments.query,
        "kind": arguments.kind,
        "paths": arguments.paths,
        "filters": {
            "paths": arguments.paths,
            "globs": arguments.globs,
            "exclude_tests": arguments.exclude_tests,
        },
        "results": ordered,
        "result_count": total.min(public_limit),
        "total_candidates": total,
        "truncated": total > public_limit,
        "errors": [],
    }))
}

pub(crate) fn context(
    workspace: &Workspace,
    arguments: &ContextArgs,
    limit: i64,
) -> Result<Value, String> {
    let started = Instant::now();
    if let Some(explicit) = target::explicit_path(&arguments.target, workspace.root())
        && !explicit.has_position()
    {
        return file_context(workspace, arguments, &explicit.path, limit, started);
    }
    let module_candidates = dotted_module_candidates(workspace.root(), &arguments.target);
    if module_candidates.len() == 1 {
        return file_context(workspace, arguments, &module_candidates[0], limit, started);
    }
    if module_candidates.len() > 1 {
        return Ok(json!({
            "status": "ambiguous",
            "target": arguments.target,
            "reason": "multiple source files match the requested module",
            "candidates": module_candidates.iter().map(|path| json!({
                "path": path,
                "selection_command": format!("codeq context {}", path.strip_prefix(workspace.root()).unwrap_or(path).display()),
            })).collect::<Vec<_>>(),
            "_phase_ms": {"resolution": started.elapsed().as_secs_f64() * 1000.0, "prewarm": 0.0, "semantic_neighborhood": 0.0},
        }));
    }
    let bare_candidate = if target::explicit_path(&arguments.target, workspace.root()).is_none()
        && !is_qualified(&arguments.target)
    {
        find(
            workspace,
            &FindArgs {
                query: arguments.target.clone(),
                mode: FindMode::Symbol,
                kind: None,
                text: false,
                files_only: false,
                paths: arguments.symbol_paths.clone(),
                globs: Vec::new(),
                exclude_tests: false,
            },
            limit.max(1),
        )
        .ok()
        .and_then(|data| data.get("results").and_then(Value::as_array).cloned())
        .and_then(|results| {
            let first = results.first()?.clone();
            let first_score = integer(&first, "score");
            let unique_top = results
                .iter()
                .filter(|candidate| integer(candidate, "score") == first_score)
                .count()
                == 1;
            Some((first, unique_top))
        })
    } else {
        None
    };
    let resolution = if let Some((candidate, true)) = &bare_candidate {
        symbol_from_value(candidate).map_or_else(
            || resolve(workspace, &arguments.target),
            |symbol| Resolution::Found {
                symbol: Box::new(symbol),
                candidates: Vec::new(),
                requested_location: None,
                cursor_definition: false,
            },
        )
    } else {
        resolve(workspace, &arguments.target)
    };
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
            && !symbol.container.is_empty()
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
    let mut resolved_symbol = symbol_value(
        &symbol,
        is_qualified(&arguments.target) && symbol.container.is_empty(),
    );
    if is_qualified(&arguments.target) {
        resolved_symbol["score"] = Value::from(10_000);
    }
    if let Some(candidate) = bare_candidate
        .map(|(candidate, _)| candidate)
        .filter(|candidate| {
            value_path(candidate) == symbol.path
                && integer(candidate, "line") == symbol.line
                && candidate.get("name").and_then(Value::as_str) == Some(symbol.name.as_str())
        })
    {
        for key in [
            "exact_definition",
            "lexical_match_score",
            "match_text",
            "score",
            "selection_command",
        ] {
            if let Some(value) = candidate.get(key) {
                resolved_symbol[key] = value.clone();
            }
        }
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
        let (tests, metadata) =
            test_evidence(workspace.root(), &symbol, &references, &callers, budget);
        data.insert("tests".to_owned(), Value::Array(tests));
        section_metadata.insert("tests".to_owned(), metadata);
    }
    if selected.contains(&"possible-dynamic-references") {
        insert_bounded_section(
            &mut data,
            &mut section_metadata,
            "possible_dynamic_references",
            dynamic::classify_references(
                &source_references,
                &symbol.name,
                budget.saturating_add(1),
            ),
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
    if selected.contains(&"lexical-references") {
        let query = arguments
            .lexical_references
            .as_deref()
            .filter(|query| !query.is_empty())
            .unwrap_or(&symbol.name);
        data.insert(
            "lexical_references".to_owned(),
            textsearch::search(
                workspace.root(),
                query,
                limit,
                &arguments.paths,
                &arguments.globs,
                arguments.exclude_tests,
            )?,
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
        workspace.prewarm_trace(&symbol, limit as u64);
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
        "hint": "Use --in for callers or --out for callees to trace one direction and reduce output.",
    }))
}

fn file_context(
    workspace: &Workspace,
    arguments: &ContextArgs,
    path: &Path,
    limit: i64,
    started: Instant,
) -> Result<Value, String> {
    if !arguments.sections.is_empty() {
        let relative = path.strip_prefix(workspace.root()).unwrap_or(path);
        return Ok(json!({
            "status": "invalid_query",
            "target": arguments.target,
            "reason": "--section applies only to symbol context; the target resolved to a file",
            "recovery_command": format!("codeq context {}", relative.display()),
            "_phase_ms": {"resolution": started.elapsed().as_secs_f64() * 1000.0, "prewarm": 0.0, "semantic_neighborhood": 0.0},
        }));
    }
    let path = fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
    let project = workspace
        .project_for_path(&path)
        .ok_or_else(|| format!("no language project found for {}", path.display()))?;
    let symbols = workspace
        .document_symbols(&path, Some(&project))
        .map_err(|error| error.to_string())?;
    let budget = limit.max(1) as usize;
    let normalized_container = arguments.container.as_deref().unwrap_or("").trim();
    let requested_kind = arguments
        .kind
        .as_deref()
        .map(str::trim)
        .filter(|kind| !kind.is_empty());
    let mut selected = Vec::new();
    for symbol in &symbols {
        if !normalized_container.is_empty() {
            let in_container = symbol.name == normalized_container
                || symbol.container == normalized_container
                || symbol
                    .container
                    .strip_prefix(normalized_container)
                    .is_some_and(|suffix| suffix.starts_with('.'));
            if !in_container {
                continue;
            }
            let relative_depth =
                if symbol.name == normalized_container && symbol.container.is_empty() {
                    0
                } else if symbol.container == normalized_container {
                    1
                } else {
                    symbol
                        .container
                        .strip_prefix(normalized_container)
                        .unwrap_or("")
                        .trim_start_matches('.')
                        .split('.')
                        .filter(|part| !part.is_empty())
                        .count()
                        + 1
                };
            if relative_depth > arguments.outline_depth.max(0) as usize {
                continue;
            }
        } else if requested_kind.is_none()
            && symbol
                .container
                .split('.')
                .filter(|part| !part.is_empty())
                .count()
                + 1
                > arguments.outline_depth.max(1) as usize
        {
            continue;
        }
        if let Some(kind) = requested_kind
            && !kind_matches(&symbol.kind, kind)
        {
            continue;
        }
        selected.push(json!({
            "name": symbol.name,
            "kind": symbol.kind,
            "container": symbol.container,
            "path": symbol.path,
            "line": symbol.line,
            "column": symbol.column,
        }));
    }
    let matching_count = selected.len();
    selected.truncate(budget);
    let imports = extract_imports(&path);
    let topology = if arguments.topology {
        file_topology(workspace, &path, &project, imports, budget)
    } else {
        json!({
            "imports": [],
            "import_count": imports.len(),
            "imports_truncated": false,
            "importers": [],
            "importer_count": 0,
            "importers_truncated": false,
        })
    };
    let mut data = json!({
        "status": "ok",
        "target": path,
        "kind": "file",
        "file": {
            "path": path,
            "language": project.family.as_str(),
            "project_root": project.root,
        },
        "outline": selected,
        "symbol_count": symbols.len(),
        "outline_count": matching_count.min(budget),
        "outline_matching_count": matching_count,
        "outline_truncated": matching_count > budget,
        "outline_depth": arguments.outline_depth,
        "outline_kind": arguments.kind,
        "container": arguments.container,
        "topology_loaded": arguments.topology,
        "imports": topology["imports"],
        "import_count": topology["import_count"],
        "imports_truncated": topology["imports_truncated"],
        "importers": topology["importers"],
        "importer_count": topology["importer_count"],
        "importers_truncated": topology["importers_truncated"],
    });
    if let Some(lines) = arguments.lines {
        data["line_window"] = source_window(workspace.root(), &path, 1, u64::from(lines));
    }
    if let Some(query) = &arguments.lexical_references {
        data["lexical_references"] = textsearch::search(
            workspace.root(),
            if query.is_empty() {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .unwrap_or("")
            } else {
                query
            },
            limit,
            &arguments.paths,
            &arguments.globs,
            arguments.exclude_tests,
        )?;
    }
    data["_phase_ms"] = json!({
        "resolution": started.elapsed().as_secs_f64() * 1000.0,
        "prewarm": 0.0,
        "semantic_neighborhood": 0.0,
    });
    Ok(data)
}

fn kind_matches(actual: &str, requested: &str) -> bool {
    let actual = actual.to_ascii_lowercase();
    match requested.to_ascii_lowercase().as_str() {
        "function" => matches!(actual.as_str(), "function" | "method" | "constructor"),
        "class" => matches!(actual.as_str(), "class" | "interface" | "struct" | "enum"),
        requested => actual == requested,
    }
}

fn dotted_module_candidates(root: &Path, target: &str) -> Vec<PathBuf> {
    if target.contains('/') || target.contains('\\') || !target.split('.').all(identifier) {
        return Vec::new();
    }
    let module_path = target.replace('.', "/");
    let mut candidates: Vec<_> = visible_source_files(root)
        .into_iter()
        .filter(|path| {
            let relative = path.strip_prefix(root).unwrap_or(path).with_extension("");
            let relative = relative.to_string_lossy().replace('\\', "/");
            relative == module_path
                || relative.ends_with(&format!("/{module_path}"))
                || relative.strip_suffix("/__init__") == Some(module_path.as_str())
        })
        .collect();
    candidates.sort();
    candidates.dedup();
    candidates
}

fn extract_imports(path: &Path) -> Vec<Value> {
    let source = fs::read_to_string(path).unwrap_or_default();
    let mut imports = Vec::new();
    let python = matches!(
        path.extension().and_then(|extension| extension.to_str()),
        Some("py" | "pyi")
    );
    for (index, line) in source.lines().enumerate() {
        let trimmed = line.trim_start();
        let leading = line.len() - trimmed.len();
        if python {
            if let Some(rest) = trimmed.strip_prefix("from ")
                && let Some((specifier, _)) = rest.split_once(" import ")
            {
                imports.push(json!({
                    "path": path,
                    "line": index + 1,
                    "column": leading + 6,
                    "text": line,
                    "specifier": specifier,
                }));
            } else if let Some(rest) = trimmed.strip_prefix("import ") {
                for specifier in rest
                    .split(',')
                    .map(str::trim)
                    .filter(|value| !value.is_empty())
                {
                    let specifier = specifier.split_whitespace().next().unwrap_or(specifier);
                    imports.push(json!({
                        "path": path,
                        "line": index + 1,
                        "column": leading + 8,
                        "text": line,
                        "specifier": specifier,
                    }));
                }
            }
        } else if trimmed.starts_with("import ")
            && let Some((_, tail)) = trimmed.rsplit_once(" from ")
        {
            let specifier = tail.trim().trim_matches(';').trim_matches(['\'', '"']);
            imports.push(json!({
                "path": path,
                "line": index + 1,
                "column": line.find(specifier).unwrap_or(0) + 1,
                "text": line,
                "specifier": specifier,
            }));
        }
    }
    imports
}

fn file_topology(
    workspace: &Workspace,
    path: &Path,
    _project: &crate::workspace::Project,
    imports: Vec<Value>,
    limit: usize,
) -> Value {
    let module = path
        .strip_prefix(workspace.root())
        .unwrap_or(path)
        .with_extension("")
        .to_string_lossy()
        .replace(['/', '\\'], ".")
        .trim_end_matches(".__init__")
        .to_owned();
    let mut importers = Vec::new();
    for candidate in visible_source_files(workspace.root()) {
        if candidate == path {
            continue;
        }
        let candidate_imports = extract_imports(&candidate);
        for item in candidate_imports {
            let specifier = item.get("specifier").and_then(Value::as_str).unwrap_or("");
            if specifier == module || module.ends_with(&format!(".{specifier}")) {
                importers.push(item);
            }
        }
    }
    importers.sort_by(|left, right| {
        (
            value_path(left),
            integer(left, "line"),
            integer(left, "column"),
        )
            .cmp(&(
                value_path(right),
                integer(right, "line"),
                integer(right, "column"),
            ))
    });
    importers.dedup_by(|left, right| {
        value_path(left) == value_path(right)
            && integer(left, "line") == integer(right, "line")
            && integer(left, "column") == integer(right, "column")
    });
    let importer_count = importers.len();
    importers.truncate(limit);
    let import_count = imports.len();
    let mut returned_imports = imports;
    returned_imports.truncate(limit);

    json!({
        "imports": returned_imports,
        "import_count": import_count,
        "imports_truncated": import_count > limit,
        "importers": importers,
        "importer_count": importer_count,
        "importers_truncated": importer_count > limit,
    })
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

fn lexical_hits(root: &Path, query: &str, limit: usize) -> Vec<Value> {
    let tokens = identifier_tokens(query);
    if tokens.is_empty() {
        return Vec::new();
    }
    let mut command = Command::new("rg");
    command
        .current_dir(root)
        .args(["--json", "-n", "--hidden", "--max-count", "20"])
        .args(["-g", "*.py", "-g", "*.pyi", "-g", "*.ts", "-g", "*.tsx"])
        .args(["-g", "*.js", "-g", "*.jsx", "-g", "*.mjs", "-g", "*.cjs"])
        .args([
            "-g",
            "!node_modules/**",
            "-g",
            "!.git/**",
            "-g",
            "!.next/**",
        ])
        .args(["-g", "!dist/**", "-g", "!build/**"]);
    for token in tokens.iter().take(3) {
        command.arg("-e").arg(regex_escape(token));
    }
    command.arg(".");
    let Ok(output) = command.output() else {
        return Vec::new();
    };
    let lowered_query = query.to_lowercase();
    let lowered_tokens: Vec<_> = tokens.iter().map(|token| token.to_lowercase()).collect();
    let mut hits = Vec::new();
    for line in output.stdout.split(|byte| *byte == b'\n') {
        let Ok(event) = serde_json::from_slice::<Value>(line) else {
            continue;
        };
        if event.get("type").and_then(Value::as_str) != Some("match") {
            continue;
        }
        let Some(relative) = event.pointer("/data/path/text").and_then(Value::as_str) else {
            continue;
        };
        let Some(line_number) = event.pointer("/data/line_number").and_then(Value::as_u64) else {
            continue;
        };
        let text = event
            .pointer("/data/lines/text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim_end()
            .to_owned();
        let lowered = text.to_lowercase();
        let coverage = lowered_tokens
            .iter()
            .filter(|token| lowered.contains(token.as_str()))
            .count() as u64;
        let phrase_bonus =
            u64::from(!lowered_query.is_empty() && lowered.contains(&lowered_query)) * 3;
        let path = fs::canonicalize(root.join(relative)).unwrap_or_else(|_| root.join(relative));
        hits.push(json!({
            "path": path,
            "line": line_number,
            "column": 1,
            "source": "rg",
            "text": text,
            "match_score": coverage + phrase_bonus,
        }));
    }
    hits.sort_by(|left, right| {
        (
            std::cmp::Reverse(integer(left, "match_score")),
            value_path(left),
            integer(left, "line"),
        )
            .cmp(&(
                std::cmp::Reverse(integer(right, "match_score")),
                value_path(right),
                integer(right, "line"),
            ))
    });
    hits.truncate(limit);
    hits
}

fn identifier_tokens(query: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    for character in query.chars().chain(std::iter::once(' ')) {
        let accepted = if current.is_empty() {
            character.is_alphabetic() || character == '_'
        } else {
            character.is_alphanumeric() || character == '_'
        };
        if accepted {
            current.push(character);
            continue;
        }
        if current.chars().count() >= 2 {
            tokens.push(std::mem::take(&mut current));
        } else {
            current.clear();
        }
    }
    tokens.sort_by_key(|token| std::cmp::Reverse(token.chars().count()));
    tokens.dedup_by(|left, right| left.eq_ignore_ascii_case(right));
    tokens
}

fn regex_escape(value: &str) -> String {
    value
        .chars()
        .flat_map(|character| {
            if matches!(
                character,
                '.' | '+' | '*' | '?' | '(' | ')' | '|' | '[' | ']' | '{' | '}' | '^' | '$' | '\\'
            ) {
                vec!['\\', character]
            } else {
                vec![character]
            }
        })
        .collect()
}

fn workspace_symbol_value(item: &Value) -> Option<Value> {
    let location = item.get("location")?;
    let (path, range) = lsp_location(location)?;
    Some(json!({
        "name": item.get("name").and_then(Value::as_str).unwrap_or(""),
        "kind": kind_name(item.get("kind").and_then(Value::as_u64)),
        "container": item.get("containerName").and_then(Value::as_str).unwrap_or(""),
        "path": path,
        "line": range.start.line + 1,
        "column": range.start.character + 1,
        "source": "lsp",
        "origin": "workspace",
    }))
}

fn fuzzy_score(query: &str, name: &str, container: &str, path: &Path) -> u64 {
    let query = query.trim().to_lowercase();
    let name = name.to_lowercase();
    let combined = format!("{container}.{name}")
        .trim_matches('.')
        .to_lowercase();
    if query.is_empty() {
        return 0;
    }
    if query == combined || query == name {
        return 10_000;
    }
    if combined.ends_with(&query) {
        return 9_000;
    }
    if name.starts_with(&query) {
        return 8_000;
    }
    if name.contains(&query) {
        return 7_000;
    }
    let tokens = identifier_tokens(&query);
    if tokens.is_empty() {
        return 0;
    }
    let haystack = format!("{combined} {}", path.display()).to_lowercase();
    let coverage = tokens
        .iter()
        .filter(|token| haystack.contains(&token.to_lowercase()))
        .count() as u64;
    if coverage == 0 {
        0
    } else {
        coverage * 1_000 + name.chars().count().min(200) as u64
    }
}

fn priority(symbol: &Symbol) -> u64 {
    let base = match symbol.kind.as_str() {
        "Function" | "Method" | "Class" | "Interface" | "Enum" | "Constructor" | "Struct"
        | "TypeParameter" => 30,
        "Constant" | "Property" | "Field" => 20,
        "Variable" => 10,
        _ => 0,
    };
    base + u64::from(symbol.origin == "document") * 2
}

fn value_priority(item: &Value) -> u64 {
    let base = match item.get("kind").and_then(Value::as_str).unwrap_or("") {
        "Function" | "Method" | "Class" | "Interface" | "Enum" | "Constructor" | "Struct"
        | "TypeParameter" => 30,
        "Constant" | "Property" | "Field" => 20,
        "Variable" => 10,
        _ => 0,
    };
    base + u64::from(item.get("origin").and_then(Value::as_str) == Some("document")) * 2
}

fn agent_ranking_adjustment(query: &str, kind: Option<&str>, item: &Value) -> i64 {
    let lowered = query.to_lowercase();
    let seeks_tests = kind.is_some_and(|value| value.eq_ignore_ascii_case("test"))
        || ["test", "tests", "pytest", "fixture", "mock", "spec", "测试"]
            .iter()
            .any(|cue| lowered.contains(cue));
    let mut adjustment = 0i64;
    let path = value_path(item).to_string_lossy().to_lowercase();
    if is_test_path(value_path(item)) && !seeks_tests {
        adjustment -= 2_500;
    }
    if ["/generated/", "/fixtures/", "/snapshots/"]
        .iter()
        .any(|segment| path.contains(segment))
    {
        adjustment -= 700;
    }
    if path.contains("/examples/") {
        adjustment -= 500;
    }
    if item.get("origin").and_then(Value::as_str) == Some("document") {
        adjustment += 150;
    }
    adjustment
}

fn in_find_scope(root: &Path, path: &Path, arguments: &FindArgs) -> bool {
    if arguments.exclude_tests && is_test_path(path) {
        return false;
    }
    if !arguments.paths.is_empty()
        && !arguments.paths.iter().any(|prefix| {
            let prefix = if Path::new(prefix).is_absolute() {
                PathBuf::from(prefix)
            } else {
                root.join(prefix)
            };
            path == prefix || path.starts_with(prefix)
        })
    {
        return false;
    }
    if arguments.globs.is_empty() {
        return true;
    }
    let relative = path.strip_prefix(root).unwrap_or(path);
    arguments.globs.iter().any(|pattern| {
        Glob::new(pattern).is_ok_and(|glob| {
            let matcher = glob.compile_matcher();
            matcher.is_match(relative) || matcher.is_match(path.file_name().unwrap_or_default())
        })
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

fn symbol_value(symbol: &Symbol, exact_definition: bool) -> Value {
    let mut value = serde_json::to_value(symbol).expect("symbol serialization");
    if exact_definition {
        value["exact_definition"] = Value::Bool(true);
    }
    value
}

fn symbol_from_value(value: &Value) -> Option<Symbol> {
    let range = value.get("range")?;
    let position = |name: &str| -> Option<Position> {
        let point = range.get(name)?;
        Some(Position {
            line: point.get("line")?.as_u64()?,
            character: point.get("character")?.as_u64()?,
        })
    };
    Some(Symbol {
        name: value.get("name")?.as_str()?.to_owned(),
        kind: value.get("kind")?.as_str()?.to_owned(),
        container: value
            .get("container")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        path: PathBuf::from(value.get("path")?.as_str()?),
        line: integer(value, "line"),
        column: integer(value, "column"),
        range: Range {
            start: position("start")?,
            end: position("end")?,
        },
        source: "lsp",
        origin: "document",
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

fn insert_bounded_section(
    data: &mut Map<String, Value>,
    metadata: &mut Map<String, Value>,
    name: &str,
    values: Vec<Value>,
    limit: usize,
) {
    let returned = values.len().min(limit);
    let overflow = values.len() > returned;
    data.insert(
        name.to_owned(),
        Value::Array(values.iter().take(limit).cloned().collect()),
    );
    metadata.insert(
        name.to_owned(),
        json!({
            "returned_count": returned,
            "total_count": if overflow { Value::Null } else { Value::from(values.len()) },
            "total_is_exact": !overflow,
            "total_lower_bound": values.len(),
            "truncated": overflow,
        }),
    );
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

fn test_section_metadata(tests: &[Value], returned: &[Value], discovery_truncated: bool) -> Value {
    let mut direct = 0;
    let mut caller = 0;
    let mut module_import = 0;
    let mut lexical = 0;
    for test in returned {
        match test.get("evidence_type").and_then(Value::as_str) {
            Some("direct_semantic_reference") => direct += 1,
            Some("semantic_caller") => caller += 1,
            Some("module_import") => module_import += 1,
            Some("exact_lexical_reference") => lexical += 1,
            _ => {}
        }
    }
    let mut metadata = if discovery_truncated {
        json!({
            "returned_count": returned.len(),
            "total_count": null,
            "total_is_exact": false,
            "total_lower_bound": tests.len(),
            "truncated": true,
        })
    } else {
        json!({
            "returned_count": returned.len(),
            "total_count": tests.len(),
            "total_is_exact": true,
            "total_lower_bound": tests.len(),
            "truncated": returned.len() < tests.len(),
        })
    };
    metadata["discovery_truncated"] = Value::Bool(discovery_truncated);
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
) -> (Vec<Value>, Value) {
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
    let importers = module_import_evidence(root, symbol);
    let probe_limit = limit.saturating_add(1);
    let import_discovery_truncated = importers.len() > probe_limit;
    tests.extend(importers.into_iter().take(probe_limit));
    let direct_lines: HashSet<_> = references
        .iter()
        .filter(|reference| is_test_path(value_path(reference)))
        .map(|reference| (value_path(reference).to_owned(), integer(reference, "line")))
        .collect();
    let mut lexical_discovery_truncated = false;
    if let Ok(search) = textsearch::search(root, &symbol.name, i64::MAX, &[], &[], false)
        && let Some(results) = search.get("results").and_then(Value::as_array)
    {
        let test_results: Vec<_> = results
            .iter()
            .filter(|result| result.get("is_test").and_then(Value::as_bool) == Some(true))
            .collect();
        lexical_discovery_truncated = test_results.len() > probe_limit;
        for result in test_results.into_iter().take(probe_limit) {
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
        }
    }
    let mut deduplicated = Vec::new();
    let mut seen = HashSet::new();
    for item in tests {
        let key = (
            value_path(&item).to_owned(),
            integer(&item, "line"),
            integer(&item, "column"),
        );
        if seen.insert(key) {
            deduplicated.push(item);
        }
    }
    let discovery_truncated = import_discovery_truncated || lexical_discovery_truncated;
    let returned: Vec<_> = deduplicated.iter().take(limit).cloned().collect();
    let metadata = test_section_metadata(&deduplicated, &returned, discovery_truncated);
    (returned, metadata)
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
