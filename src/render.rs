use std::collections::HashMap;
use std::path::Path;

use serde_json::Value;

use crate::cli::{Command, FindMode};

pub(crate) fn plain(data: &Value, command: &Command, root: &Path) -> String {
    if data["status"] == "unavailable" {
        return format!("codeq: {}", render_query_failure(data));
    }
    if data.get("status").and_then(Value::as_str) != Some("ok") {
        return render_query_failure(data);
    }
    match command {
        Command::Find(arguments) if arguments.text || arguments.mode == FindMode::Text => {
            render_text_search(data, root)
        }
        Command::Find(arguments) => render_find(data, arguments.files_only),
        Command::Context(_) if data.get("kind").and_then(Value::as_str) == Some("file") => {
            render_file_context(data)
        }
        Command::Context(_) => render_symbol_context(data),
        Command::Trace(_) => render_trace(data),
        Command::Review(_) => render_review(data),
    }
}

fn render_query_failure(data: &Value) -> String {
    let status = data
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("error");
    let target = data.get("target").and_then(Value::as_str).unwrap_or("");
    let candidates = data
        .get("candidates")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let mut lines = Vec::new();
    if status == "ambiguous" {
        lines.push(format!(
            "Ambiguous target: {}",
            display_message(data, target)
        ));
        for candidate in candidates {
            render_resolution_candidate(data, candidate, &mut lines);
        }
        return lines.join("\n");
    }
    let reason = data
        .get("reason")
        .or_else(|| data.get("error"))
        .and_then(Value::as_str)
        .unwrap_or("query failed");
    lines.push(display_message(data, reason));
    if let Some(recovery) = data
        .get("recovery_command")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        lines.push(format!("  try: {recovery}"));
    }
    if !candidates.is_empty() {
        lines.push("Possible exact-name matches:".to_owned());
        for candidate in candidates {
            render_resolution_candidate(data, candidate, &mut lines);
        }
    }
    lines.join("\n")
}

fn render_resolution_candidate(data: &Value, candidate: &Value, lines: &mut Vec<String>) {
    let container = candidate
        .get("container")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(|value| format!("{value}."))
        .unwrap_or_default();
    let selection = candidate
        .get("selection_command")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| {
            format!(
                "codeq context {}:{}:{}",
                shell_word(&display_path(
                    data,
                    candidate.get("path").and_then(Value::as_str).unwrap_or("")
                )),
                integer(candidate, "line"),
                integer(candidate, "column").max(1)
            )
        });
    lines.push(format!(
        "  {} {container}{}  try: {selection}",
        candidate
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or("Unknown"),
        candidate.get("name").and_then(Value::as_str).unwrap_or("")
    ));
}

fn render_find(data: &Value, files_only: bool) -> String {
    let query = data.get("query").and_then(Value::as_str).unwrap_or("");
    let duration = duration(data);
    let results = data
        .get("results")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    if data.get("search_mode").and_then(Value::as_str) == Some("concept") {
        let mut lines = vec![
            format!("Concept search: {}", python_repr(query)),
            String::new(),
        ];
        for (index, result) in results.iter().enumerate() {
            let marker = if result.get("is_test").and_then(Value::as_bool) == Some(true) {
                " [test]"
            } else {
                ""
            };
            if files_only {
                lines.push(format!(
                    "{:<12} {}{marker}",
                    result.get("kind").and_then(Value::as_str).unwrap_or("File"),
                    display_path(
                        data,
                        result.get("path").and_then(Value::as_str).unwrap_or("")
                    )
                ));
                continue;
            }
            let path = display_path(
                data,
                result.get("path").and_then(Value::as_str).unwrap_or(""),
            );
            let line = integer(result, "line");
            let column = integer(result, "column");
            lines.push(format!("{}. {path}:{line}:{column}{marker}", index + 1));
            if let Some(evidence) = result.get("representative_lines").and_then(Value::as_array) {
                for item in evidence {
                    lines.push(format!(
                        "   {:>5} | {}",
                        integer(item, "line"),
                        item.get("text").and_then(Value::as_str).unwrap_or("")
                    ));
                }
            }
            let terms = result
                .get("matched_terms")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ");
            lines.push(format!("   Matched terms: {terms}"));
            lines.push(format!(
                "   → {}",
                result
                    .get("selection_command")
                    .and_then(Value::as_str)
                    .unwrap_or("")
            ));
            lines.push(String::new());
        }
        if results.is_empty() {
            lines.push("No matches.".to_owned());
            render_find_recovery(&mut lines, query, &["symbol", "text"]);
        }
        if data.get("truncated").and_then(Value::as_bool) == Some(true) {
            lines.push("... more matching files available; increase --limit".to_owned());
        }
        if files_only || results.is_empty() {
            lines.push(String::new());
        }
        lines.push(format!(
            "[showing {} of {} files; {duration} ms]",
            integer(data, "result_count"),
            integer(data, "total_candidates")
        ));
        return lines.join("\n");
    }

    let mut lines = vec![
        format!("Symbol search: {}", python_repr(query)),
        String::new(),
    ];
    for result in results {
        let container = result
            .get("container")
            .and_then(Value::as_str)
            .unwrap_or("");
        let name = result.get("name").and_then(Value::as_str).unwrap_or("");
        let qualified = if container.is_empty() {
            name.to_owned()
        } else {
            format!("{container}.{name}")
        };
        lines.push(format!(
            "{:<12} {}  {}:{}:{}",
            result
                .get("kind")
                .and_then(Value::as_str)
                .unwrap_or("Unknown"),
            qualified,
            display_path(
                data,
                result.get("path").and_then(Value::as_str).unwrap_or("")
            ),
            integer(result, "line"),
            integer(result, "column")
        ));
    }
    if results.is_empty() {
        lines.push("No matches.".to_owned());
        render_find_recovery(&mut lines, query, &["concept", "text"]);
    }
    if data.get("truncated").and_then(Value::as_bool) == Some(true) {
        lines.push("... more semantic candidates available; increase --limit".to_owned());
    }
    lines.push(String::new());
    lines.push(format!(
        "[showing {} of {} candidates; {duration} ms]",
        integer(data, "result_count"),
        integer(data, "total_candidates")
    ));
    lines.join("\n")
}

fn render_find_recovery(lines: &mut Vec<String>, query: &str, modes: &[&str]) {
    lines.push("Try:".to_owned());
    for mode in modes {
        lines.push(format!("  codeq find --mode {mode} {}", shell_word(query)));
    }
}

fn render_symbol_context(data: &Value) -> String {
    let symbol = data.get("symbol").unwrap_or(&Value::Null);
    let container = symbol
        .get("container")
        .and_then(Value::as_str)
        .unwrap_or("");
    let name = symbol.get("name").and_then(Value::as_str).unwrap_or("");
    let qualified = if container.is_empty() {
        name.to_owned()
    } else {
        format!("{container}.{name}")
    };
    let mut lines = vec![
        format!(
            "{} {qualified}",
            symbol
                .get("kind")
                .and_then(Value::as_str)
                .unwrap_or("Symbol")
        ),
        format!(
            "{}:{}:{}",
            display_path(
                data,
                symbol.get("path").and_then(Value::as_str).unwrap_or("")
            ),
            integer(symbol, "line"),
            integer(symbol, "column")
        ),
    ];
    if data
        .pointer("/section_selection/mode")
        .and_then(Value::as_str)
        == Some("focused")
    {
        let selected = data
            .pointer("/section_selection/selected")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join(", ");
        lines.push(format!("Focused sections: {selected}"));
    }
    if let Some(requested) = data
        .get("requested_location")
        .filter(|value| value.is_object())
    {
        let suffix = if data.get("cursor_definition").and_then(Value::as_bool) == Some(true) {
            " -> cursor definition"
        } else {
            ""
        };
        lines.push(format!(
            "Requested at: {}{suffix}",
            display_location(data, requested)
        ));
    }
    if let Some(note) = data
        .get("definition_note")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        lines.push(format!("Definition note: {}", display_message(data, note)));
    }
    if let Some(hover) = data
        .get("hover")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        lines.extend([String::new(), "Hover".to_owned(), hover.trim().to_owned()]);
    }
    render_line_window(data, data.get("line_window"), &mut lines);
    let request_source = data
        .pointer("/request_source/text")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty());
    if data.get("line_window").is_none()
        && let Some(request_source) = request_source
    {
        lines.extend([
            String::new(),
            "Request source".to_owned(),
            request_source.to_owned(),
        ]);
    }
    if let Some(source) = data.pointer("/source/text").and_then(Value::as_str) {
        let title = if request_source.is_some() || data.get("line_window").is_some() {
            "Definition source"
        } else {
            "Source"
        };
        lines.extend([String::new(), title.to_owned(), source.to_owned()]);
    }
    lines.push(String::new());
    for (key, title) in [
        ("callers", "Callers"),
        ("callees", "Callees"),
        ("implementations", "Implementations"),
    ] {
        if let Some(items) = data.get(key).and_then(Value::as_array) {
            render_locations(
                data,
                title,
                items,
                data.pointer(&format!("/section_metadata/{key}")),
                &mut lines,
            );
        }
    }
    if let Some(tests) = data.get("tests").and_then(Value::as_array) {
        render_test_evidence(
            data,
            tests,
            data.pointer("/section_metadata/tests"),
            &mut lines,
        );
    }
    if let Some(items) = data.get("references").and_then(Value::as_array) {
        render_locations(
            data,
            "References",
            items,
            data.pointer("/section_metadata/references"),
            &mut lines,
        );
    }
    if let Some(items) = data
        .get("possible_dynamic_references")
        .and_then(Value::as_array)
    {
        render_dynamic_references(
            data,
            items,
            data.pointer("/section_metadata/possible_dynamic_references"),
            &mut lines,
        );
    }
    if let Some(lexical) = data
        .get("lexical_references")
        .filter(|value| value.is_object())
    {
        lines.push(String::new());
        render_text_evidence(data, "Lexical references", lexical, &mut lines);
    }
    if let Some(topology) = data.get("file_topology").filter(|value| value.is_object()) {
        render_file_topology(data, topology, Some("Containing file topology"), &mut lines);
    }
    lines.extend([String::new(), format!("[{} ms]", duration(data))]);
    lines.join("\n")
}

fn display_location(data: &Value, item: &Value) -> String {
    format!(
        "{}:{}:{}",
        display_path(data, item.get("path").and_then(Value::as_str).unwrap_or("")),
        integer(item, "line"),
        integer(item, "column").max(1)
    )
}

fn display_message(data: &Value, message: &str) -> String {
    let Some(root) = data.pointer("/_meta/root").and_then(Value::as_str) else {
        return message.to_owned();
    };
    let prefix = format!("{root}{}", std::path::MAIN_SEPARATOR);
    message.replace(&prefix, "")
}

fn disclosure_count(items: &[Value], disclosure: Option<&Value>) -> String {
    let returned = disclosure
        .and_then(|value| value.get("returned_count"))
        .and_then(Value::as_u64)
        .unwrap_or(items.len() as u64);
    let truncated = disclosure
        .and_then(|value| value.get("truncated"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let total = disclosure
        .and_then(|value| value.get("total_count"))
        .and_then(Value::as_u64);
    match (truncated, total) {
        (true, Some(total)) => format!("showing {returned} of {total}"),
        (true, None) => format!("showing {returned}+"),
        (false, _) => returned.to_string(),
    }
}

fn render_locations(
    data: &Value,
    title: &str,
    items: &[Value],
    disclosure: Option<&Value>,
    lines: &mut Vec<String>,
) {
    lines.push(format!("{title} ({})", disclosure_count(items, disclosure)));
    if items.is_empty() {
        lines.push("  -".to_owned());
    }
    for item in items {
        let name = item
            .get("name")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(|value| format!("{value}  "))
            .unwrap_or_default();
        lines.push(format!("  {name}{}", display_location(data, item)));
    }
    if disclosure
        .and_then(|value| value.get("truncated"))
        .and_then(Value::as_bool)
        == Some(true)
    {
        lines.push(format!(
            "  ... more {} available; increase --limit",
            title.to_ascii_lowercase()
        ));
    }
}

fn render_test_evidence(
    data: &Value,
    items: &[Value],
    disclosure: Option<&Value>,
    lines: &mut Vec<String>,
) {
    lines.push(format!("Tests ({})", disclosure_count(items, disclosure)));
    if items.is_empty() {
        lines.push("  -".to_owned());
    }
    for item in items {
        let evidence = match item
            .get("evidence_type")
            .and_then(Value::as_str)
            .unwrap_or("direct_semantic_reference")
        {
            "direct_semantic_reference" => "direct semantic reference",
            "semantic_caller" => "candidate: semantic caller",
            "module_import" => "candidate: module import",
            "exact_lexical_reference" => "candidate: exact lexical reference",
            other => other,
        };
        let name = item
            .get("name")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(|value| format!("{value}  "))
            .unwrap_or_default();
        lines.push(format!(
            "  [{evidence}] {name}{}",
            display_location(data, item)
        ));
    }
    if disclosure
        .and_then(|value| value.get("truncated"))
        .and_then(Value::as_bool)
        == Some(true)
    {
        lines.push("  ... more test evidence available; increase --limit".to_owned());
    }
}

fn render_dynamic_references(
    data: &Value,
    items: &[Value],
    disclosure: Option<&Value>,
    lines: &mut Vec<String>,
) {
    lines.push(format!(
        "Possible dynamic references ({})",
        disclosure_count(items, disclosure)
    ));
    if items.is_empty() {
        lines.push("  -".to_owned());
    }
    for item in items {
        let reason = item
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("possible");
        let text = item
            .get("text")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(|value| format!("  {value}"))
            .unwrap_or_default();
        lines.push(format!(
            "  [{reason}] {}{text}",
            display_location(data, item)
        ));
    }
    if disclosure
        .and_then(|value| value.get("truncated"))
        .and_then(Value::as_bool)
        == Some(true)
    {
        lines.push("  ... more possible dynamic references available; increase --limit".to_owned());
    }
}

fn render_line_window(data: &Value, window: Option<&Value>, lines: &mut Vec<String>) {
    let Some(window) = window.filter(|value| value.is_object()) else {
        return;
    };
    let start = integer(window, "start_line").max(1);
    let end = window
        .get("end_line")
        .and_then(Value::as_u64)
        .unwrap_or_else(|| start.saturating_sub(1));
    lines.extend([
        String::new(),
        format!(
            "Source window ({}:{start}-{end})",
            display_path(
                data,
                window.get("path").and_then(Value::as_str).unwrap_or("")
            )
        ),
        window
            .get("text")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .unwrap_or("  -")
            .to_owned(),
    ]);
    if window.get("payload_truncated").and_then(Value::as_bool) == Some(true) {
        lines.push("  ... source window truncated by the character budget".to_owned());
        if let Some(recovery) = window.get("recovery_command").and_then(Value::as_str) {
            lines.push(format!("  next: {recovery}"));
        }
    } else if window.get("line_truncated").and_then(Value::as_bool) == Some(true) {
        lines.push("  ... one or more long source lines were truncated".to_owned());
    }
}

fn render_text_evidence(data: &Value, title: &str, evidence: &Value, lines: &mut Vec<String>) {
    let query = evidence.get("query").and_then(Value::as_str).unwrap_or("");
    lines.push(format!(
        "{title} ({} matches across {} lines)  {}",
        integer(evidence, "match_count"),
        integer(evidence, "matching_line_count"),
        python_repr(query)
    ));
    let items = evidence
        .get("results")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    if items.is_empty() {
        lines.push("  -".to_owned());
    }
    for item in items {
        let mut markers = Vec::new();
        if item.get("tracked").and_then(Value::as_bool) == Some(false) {
            markers.push("untracked");
        }
        if item.get("is_test").and_then(Value::as_bool) == Some(true) {
            markers.push("test");
        }
        let marker = if markers.is_empty() {
            String::new()
        } else {
            format!(" [{}]", markers.join(" "))
        };
        let occurrences = integer(item, "occurrences").max(1);
        let repeated = if occurrences > 1 {
            format!(" x{occurrences}")
        } else {
            String::new()
        };
        let text = item
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        lines.push(format!(
            "  {}{marker}{repeated}  {text}",
            display_location(data, item)
        ));
    }
    if evidence.get("truncated").and_then(Value::as_bool) == Some(true) {
        lines.push("  ... more matching lines available; increase --limit".to_owned());
    }
}

fn render_file_topology(
    data: &Value,
    topology: &Value,
    heading: Option<&str>,
    lines: &mut Vec<String>,
) {
    if let Some(heading) = heading {
        lines.extend([
            String::new(),
            format!(
                "{heading} ({})",
                display_path(
                    data,
                    topology.get("path").and_then(Value::as_str).unwrap_or("")
                )
            ),
        ]);
    }
    let imports = topology
        .get("imports")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    lines.extend([
        String::new(),
        format!(
            "Imports (showing {} of {})",
            imports.len(),
            integer(topology, "import_count")
        ),
    ]);
    if imports.is_empty() {
        lines.push("  -".to_owned());
    }
    for item in imports {
        let names = item
            .get("names")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join(", ");
        let names = if names.is_empty() {
            String::new()
        } else {
            format!(" [{names}]")
        };
        let resolved = item
            .get("resolved_paths")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(|path| display_path(data, path))
            .collect::<Vec<_>>()
            .join(", ");
        let resolved = if resolved.is_empty() {
            String::new()
        } else {
            format!(" -> {resolved}")
        };
        lines.push(format!(
            "  {}:{}{names}{resolved}",
            item.get("specifier").and_then(Value::as_str).unwrap_or(""),
            integer(item, "line").max(1)
        ));
    }
    if topology.get("imports_truncated").and_then(Value::as_bool) == Some(true) {
        lines.push("  ... more imports available; increase --limit".to_owned());
    }
    let importers = topology
        .get("importers")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let suffix = if topology.get("importers_truncated").and_then(Value::as_bool) == Some(true) {
        "+"
    } else {
        ""
    };
    lines.extend([
        String::new(),
        format!("Imported by (showing {}{suffix})", importers.len()),
    ]);
    if importers.is_empty() {
        lines.push("  -".to_owned());
    }
    for item in importers {
        let text = item
            .get("text")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(|value| format!("  {value}"))
            .unwrap_or_default();
        lines.push(format!("  {}{text}", display_location(data, item)));
    }
}

fn render_file_context(data: &Value) -> String {
    let file = data.get("file").unwrap_or(&Value::Null);
    let mut lines = vec![
        format!(
            "File {}",
            display_path(data, file.get("path").and_then(Value::as_str).unwrap_or(""))
        ),
        format!(
            "Language: {}",
            file.get("language").and_then(Value::as_str).unwrap_or("")
        ),
        String::new(),
        format!(
            "Outline (showing {} of {} matching; {} total symbols)",
            integer(data, "outline_count"),
            integer(data, "outline_matching_count"),
            integer(data, "symbol_count")
        ),
    ];
    if let Some(outline) = data.get("outline").and_then(Value::as_array) {
        if outline.is_empty() {
            lines.push("  -".to_owned());
        }
        for item in outline {
            let container = item
                .get("container")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(|value| format!("{value}."))
                .unwrap_or_default();
            lines.push(format!(
                "  {:<12} {container}{}  line {}",
                item.get("kind")
                    .and_then(Value::as_str)
                    .unwrap_or("Unknown"),
                item.get("name").and_then(Value::as_str).unwrap_or(""),
                integer(item, "line")
            ));
        }
    }
    if data.get("outline_truncated").and_then(Value::as_bool) == Some(true) {
        lines.push("  ... more matching symbols available; increase --limit".to_owned());
    }
    if data.get("outline_kind").and_then(Value::as_str).is_none()
        && data.get("container").and_then(Value::as_str).is_none()
    {
        lines.push(
            "  next: use --outline-depth 2, --kind KIND, or --container NAME to disclose more"
                .to_owned(),
        );
    }
    render_line_window(data, data.get("line_window"), &mut lines);
    if data.get("topology_loaded").and_then(Value::as_bool) == Some(true) {
        render_file_topology(data, data, None, &mut lines);
    } else {
        lines.extend([String::new(), format!(
            "Topology: hidden ({} direct imports; use --topology to disclose imports/importers)",
            integer(data, "import_count")
        )]);
    }
    if let Some(lexical) = data
        .get("lexical_references")
        .filter(|value| value.is_object())
    {
        lines.push(String::new());
        render_text_evidence(data, "Lexical references", lexical, &mut lines);
    }
    lines.extend([String::new(), format!("[{} ms]", duration(data))]);
    lines.join("\n")
}

fn render_trace(data: &Value) -> String {
    let mut lines = Vec::new();
    if data.get("direction").and_then(Value::as_str) == Some("both") {
        if let Some(incoming) = data.pointer("/traces/in").filter(|value| value.is_object()) {
            lines.push("Incoming callers".to_owned());
            render_trace_branch(data, incoming, false, &mut lines);
        }
        if let Some(outgoing) = data
            .pointer("/traces/out")
            .filter(|value| value.is_object())
        {
            if !lines.is_empty() {
                lines.push(String::new());
            }
            lines.push("Outgoing callees".to_owned());
            render_trace_branch(data, outgoing, false, &mut lines);
        }
        if let Some(hint) = data
            .get("hint")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        {
            lines.extend([
                String::new(),
                format!("Hint: {}", display_message(data, hint)),
            ]);
        }
        lines.extend([
            String::new(),
            format!(
                "[both directions; node_limit={} per direction; {} ms]",
                integer(data, "node_limit"),
                duration(data)
            ),
        ]);
    } else {
        render_trace_branch(data, data, true, &mut lines);
    }
    lines.join("\n")
}

fn render_trace_branch(
    data: &Value,
    branch: &Value,
    include_duration: bool,
    lines: &mut Vec<String>,
) {
    render_trace_node(
        data,
        branch.get("tree").unwrap_or(&Value::Null),
        "",
        true,
        true,
        lines,
    );
    if let Some(note) = branch
        .get("note")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        lines.extend([
            String::new(),
            format!("Note: {}", display_message(data, note)),
        ]);
    }
    let truncated = branch
        .get("truncated")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if truncated {
        let node_limit = integer(branch, "node_limit");
        lines.extend([
            String::new(),
            format!(
                "... emitted call tree is incomplete; traversal reached node_limit={node_limit}."
            ),
        ]);
        let target = branch
            .get("target")
            .or_else(|| data.get("target"))
            .and_then(Value::as_str)
            .unwrap_or("");
        let direction = branch
            .get("direction")
            .and_then(Value::as_str)
            .unwrap_or("");
        if !target.is_empty() && matches!(direction, "in" | "out") {
            let next_limit = (node_limit + 1).max(node_limit.saturating_mul(2));
            lines.push(format!(
                "next: codeq trace {} --{direction} --depth {} --limit {next_limit}",
                shell_word(&display_message(data, target)),
                integer(branch, "depth").max(integer(data, "depth"))
            ));
        }
    }
    let timing = if include_duration {
        format!("; {} ms", duration(data))
    } else {
        String::new()
    };
    lines.extend([
        String::new(),
        format!(
            "[{} nodes; depth={}; node_limit={}; truncated={truncated}{timing}]",
            integer(branch, "node_count"),
            integer(branch, "depth").max(integer(data, "depth")),
            integer(branch, "node_limit")
        ),
    ]);
}

fn render_trace_node(
    data: &Value,
    tree: &Value,
    prefix: &str,
    last: bool,
    root: bool,
    lines: &mut Vec<String>,
) {
    let node = tree.get("node").unwrap_or(&Value::Null);
    let branch = if root {
        ""
    } else if last {
        "└─ "
    } else {
        "├─ "
    };
    let cycle = if tree.get("cycle").and_then(Value::as_bool) == Some(true) {
        " [cycle]"
    } else {
        ""
    };
    lines.push(format!(
        "{prefix}{branch}{}  {}:{}{cycle}",
        node.get("name").and_then(Value::as_str).unwrap_or(""),
        display_path(data, node.get("path").and_then(Value::as_str).unwrap_or("")),
        integer(node, "line")
    ));
    if let Some(children) = tree.get("children").and_then(Value::as_array) {
        for (index, child) in children.iter().enumerate() {
            let child_prefix = if root {
                String::new()
            } else if last {
                format!("{prefix}   ")
            } else {
                format!("{prefix}│  ")
            };
            render_trace_node(
                data,
                child,
                &child_prefix,
                index + 1 == children.len(),
                false,
                lines,
            );
        }
    }
}

fn render_review(data: &Value) -> String {
    let requested_base = data
        .get("requested_base")
        .or_else(|| data.get("base"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let mut lines = vec![
        format!("Base: {requested_base}"),
        format!(
            "Base mode: {}",
            data.get("base_mode")
                .and_then(Value::as_str)
                .unwrap_or("direct")
        ),
    ];
    if let Some(resolved) = data
        .get("resolved_base")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        lines.push(format!("Resolved base: {resolved}"));
    }
    lines.push(format!(
        "Changed files: {}",
        integer(data, "changed_file_count")
    ));
    let changes = data
        .get("file_changes")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    if changes.is_empty() {
        for path in data
            .get("changed_files")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            lines.push(format!("  {}", display_path(data, path)));
        }
    }
    for change in changes {
        let status = change.get("status").and_then(Value::as_str).unwrap_or("?");
        let path = display_path(
            data,
            change.get("path").and_then(Value::as_str).unwrap_or(""),
        );
        if matches!(status, "R" | "C") {
            if let Some(old_path) = change.get("old_path").and_then(Value::as_str) {
                lines.push(format!(
                    "  {status} {} -> {path}",
                    display_path(data, old_path)
                ));
            } else {
                lines.push(format!("  {status} {path}"));
            }
        } else {
            lines.push(format!("  {status} {path}"));
        }
        if matches!(
            change.get("semantic_status").and_then(Value::as_str),
            Some("deleted_base_analyzed" | "deleted_base_unavailable")
        ) {
            let analysis = change.get("base_analysis").unwrap_or(&Value::Null);
            lines.push(format!(
                "    base-side impact: {} ({} symbols; lexical evidence)",
                analysis
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or("unavailable"),
                integer(analysis, "base_symbol_count")
            ));
            for item in analysis
                .get("base_symbols")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .take(5)
            {
                let symbol = item.get("symbol").unwrap_or(&Value::Null);
                lines.push(format!(
                    "      {} {}  residual={} tests={}",
                    symbol.get("kind").and_then(Value::as_str).unwrap_or("?"),
                    symbol.get("name").and_then(Value::as_str).unwrap_or("?"),
                    integer(item, "residual_match_count"),
                    item.get("tests")
                        .and_then(Value::as_array)
                        .map(Vec::len)
                        .unwrap_or(0)
                ));
            }
        }
        if change.get("semantic_status").and_then(Value::as_str) == Some("rename_analyzed") {
            let analysis = change.get("rename_analysis").unwrap_or(&Value::Null);
            lines.push(format!(
                "    rename impact: importers={} symbols={} (current semantic)",
                integer(analysis, "importer_count"),
                analysis
                    .get("symbols")
                    .and_then(Value::as_array)
                    .map(Vec::len)
                    .unwrap_or(0)
            ));
            for item in analysis
                .get("symbols")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .take(5)
            {
                let symbol = item.get("symbol").unwrap_or(&Value::Null);
                lines.push(format!(
                    "      {} {}  references={} tests={}",
                    symbol.get("kind").and_then(Value::as_str).unwrap_or("?"),
                    symbol.get("name").and_then(Value::as_str).unwrap_or("?"),
                    integer(item, "reference_count"),
                    item.get("tests")
                        .and_then(Value::as_array)
                        .map(Vec::len)
                        .unwrap_or(0)
                ));
            }
        }
    }
    lines.extend([
        String::new(),
        format!("Changed symbols: {}", integer(data, "changed_symbol_count")),
    ]);
    for detail in data
        .get("changed_symbols")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let symbol = detail.get("symbol").unwrap_or(&Value::Null);
        let container = symbol
            .get("container")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(|value| format!("{value}."))
            .unwrap_or_default();
        lines.push(format!(
            "  {} {container}{}  {}:{}",
            symbol.get("kind").and_then(Value::as_str).unwrap_or("?"),
            symbol.get("name").and_then(Value::as_str).unwrap_or(""),
            display_path(
                data,
                symbol.get("path").and_then(Value::as_str).unwrap_or("")
            ),
            integer(symbol, "line")
        ));
        for caller in detail
            .get("callers")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .take(5)
        {
            lines.push(format!(
                "    <- {}  {}:{}",
                caller.get("name").and_then(Value::as_str).unwrap_or(""),
                display_path(
                    data,
                    caller.get("path").and_then(Value::as_str).unwrap_or("")
                ),
                integer(caller, "line")
            ));
        }
        for dynamic in detail
            .get("possible_dynamic_references")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .take(5)
        {
            lines.push(format!(
                "    ? {}  {}:{}",
                dynamic
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("possible"),
                display_path(
                    data,
                    dynamic.get("path").and_then(Value::as_str).unwrap_or("")
                ),
                integer(dynamic, "line")
            ));
        }
    }
    lines.extend([
        String::new(),
        format!("Affected files: {}", integer(data, "impacted_file_count")),
    ]);
    for path in data
        .get("impacted_files")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
    {
        lines.push(format!("  {}", display_path(data, path)));
    }
    if data
        .get("impacted_files_truncated")
        .and_then(Value::as_bool)
        == Some(true)
    {
        lines.push("  ... more affected files available; increase --limit".to_owned());
    }
    lines.extend([
        String::new(),
        format!(
            "Possible dynamic references: {}",
            integer(data, "possible_dynamic_reference_count")
        ),
        String::new(),
        format!("Likely tests: {}", integer(data, "test_count")),
    ]);
    render_review_tests(data, &mut lines);
    if data.get("tests_truncated").and_then(Value::as_bool) == Some(true) {
        lines.push("  ... more likely tests available; increase --limit".to_owned());
    }
    if data.get("truncated").and_then(Value::as_bool) == Some(true) {
        lines.extend([String::new(), "Result truncated by --limit.".to_owned()]);
    }
    lines.extend([String::new(), format!("[{} ms]", duration(data))]);
    lines.join("\n")
}

fn render_review_tests(data: &Value, lines: &mut Vec<String>) {
    let mut files: Vec<(String, Vec<String>)> = Vec::new();
    let mut file_indexes: HashMap<String, usize> = HashMap::new();
    for test in data
        .get("tests")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let path = display_path(data, test.get("path").and_then(Value::as_str).unwrap_or(""));
        let mut location = integer(test, "line").to_string();
        if let Some(name) = test
            .get("name")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        {
            location.push_str(&format!(" ({name})"));
        }
        if let Some(index) = file_indexes.get(&path).copied() {
            files[index].1.push(location);
        } else {
            file_indexes.insert(path.clone(), files.len());
            files.push((path, vec![location]));
        }
    }
    for (path, locations) in files {
        lines.push(format!("  {path}:{}", locations.join(", ")));
    }
}

fn display_path(data: &Value, path: &str) -> String {
    let Some(root) = data.pointer("/_meta/root").and_then(Value::as_str) else {
        return path.to_owned();
    };
    path.strip_prefix(root)
        .and_then(|value| value.strip_prefix(std::path::MAIN_SEPARATOR))
        .unwrap_or(path)
        .to_owned()
}

fn duration(data: &Value) -> String {
    data.pointer("/_meta/duration_ms")
        .and_then(Value::as_f64)
        .map(|value| format!("{value:.1}"))
        .unwrap_or_else(|| "?".to_owned())
}

fn render_text_search(data: &Value, root: &Path) -> String {
    let query = data.get("query").and_then(Value::as_str).unwrap_or("");
    let mut lines = vec![
        format!("Exact text search: {}", python_repr(query)),
        String::new(),
    ];
    if let Some(results) = data.get("results").and_then(Value::as_array) {
        for item in results {
            let path = item.get("path").and_then(Value::as_str).unwrap_or("");
            let displayed = Path::new(path)
                .strip_prefix(root)
                .map(|relative| relative.to_string_lossy().into_owned())
                .unwrap_or_else(|_| path.to_owned());
            let line = item.get("line").and_then(Value::as_u64).unwrap_or(1);
            let column = item.get("column").and_then(Value::as_u64).unwrap_or(1);
            let mut markers = Vec::new();
            if item.get("tracked").and_then(Value::as_bool) == Some(false) {
                markers.push("untracked");
            }
            if item.get("is_test").and_then(Value::as_bool) == Some(true) {
                markers.push("test");
            }
            let marker = if markers.is_empty() {
                String::new()
            } else {
                format!(" [{}]", markers.join(" "))
            };
            let occurrences = item.get("occurrences").and_then(Value::as_u64).unwrap_or(1);
            let repeated = if occurrences > 1 {
                format!(" x{occurrences}")
            } else {
                String::new()
            };
            let text = item.get("text").and_then(Value::as_str).unwrap_or("");
            lines.push(format!(
                "{displayed}:{line}:{column}{marker}{repeated}  {}",
                text.trim()
            ));
        }
        if results.is_empty() {
            lines.push("No matches.".to_owned());
            lines.push("Try:".to_owned());
            lines.push(format!("  codeq find --mode concept {}", shell_word(query)));
        }
    }
    if data.get("truncated").and_then(Value::as_bool) == Some(true) {
        lines.push("... more matching lines available; increase --limit".to_owned());
    }
    lines.push(String::new());
    let duration = data
        .pointer("/_meta/duration_ms")
        .and_then(Value::as_f64)
        .map(|value| format!("{value:.1}"))
        .unwrap_or_else(|| "?".to_owned());
    lines.push(format!(
        "[{} exact matches across {} lines / {} files; tracked={} untracked={} tests={}; showing {} lines; {} ms]",
        integer(data, "match_count"),
        integer(data, "matching_line_count"),
        integer(data, "matching_file_count"),
        integer(data, "tracked_line_count"),
        integer(data, "untracked_line_count"),
        integer(data, "test_line_count"),
        integer(data, "returned_line_count"),
        duration,
    ));
    lines.join("\n")
}

fn integer(data: &Value, key: &str) -> u64 {
    data.get(key).and_then(Value::as_u64).unwrap_or(0)
}

fn python_repr(value: &str) -> String {
    format!(
        "'{}'",
        value
            .replace('\\', "\\\\")
            .replace('\'', "\\'")
            .replace('\n', "\\n")
            .replace('\r', "\\r")
            .replace('\t', "\\t")
    )
}

fn shell_word(value: &str) -> String {
    if !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&byte))
    {
        value.to_owned()
    } else {
        format!("'{}'", value.replace('\'', "'\\''"))
    }
}
