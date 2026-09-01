use std::collections::HashMap;
use std::collections::hash_map::DefaultHasher;
use std::fs;
use std::hash::{Hash, Hasher};
use std::path::Path;
use std::path::PathBuf;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::Duration;
use std::time::Instant;

use serde_json::Value;

use crate::boundary;
use crate::cli::{Cli, Command, FindMode};
use crate::concept;
use crate::contracts::{SCHEMA_VERSION, Status};
use crate::gitreview;
use crate::semantic;
use crate::textsearch;
use crate::workspace::Workspace;

pub struct DaemonService {
    state: Mutex<DaemonState>,
    workspace_available: Condvar,
    max_workspaces: usize,
    scratch_root: PathBuf,
}

struct DaemonState {
    workspaces: HashMap<PathBuf, WorkspaceState>,
    last_activity: Instant,
}

struct WorkspaceState {
    active: usize,
    last_used: Instant,
    runtime: Arc<Workspace>,
}

struct WorkspaceLease<'a> {
    service: &'a DaemonService,
    root: PathBuf,
    runtime: Arc<Workspace>,
}

pub struct QueryResult {
    pub data: Value,
    pub status: Status,
    pub plain: String,
}

impl DaemonService {
    pub fn new(max_workspaces: usize, scratch_root: PathBuf) -> Self {
        Self {
            state: Mutex::new(DaemonState {
                workspaces: HashMap::new(),
                last_activity: Instant::now(),
            }),
            workspace_available: Condvar::new(),
            max_workspaces: max_workspaces.max(1),
            scratch_root,
        }
    }

    pub fn query(&self, cli: &Cli) -> Result<Value, String> {
        let root = cli.root.clone();
        let lease = self.acquire(&root, query_timeout(cli.timeout))?;
        Ok(execute_with_workspace(cli, &root, "daemon", &lease.runtime).data)
    }

    pub fn status(&self) -> Value {
        let mut state = self.lock_state();
        state.last_activity = Instant::now();
        let mut roots: Vec<_> = state
            .workspaces
            .keys()
            .map(|root| root.to_string_lossy().into_owned())
            .collect();
        roots.sort();
        serde_json::json!({
            "status": "ok",
            "workspaces": roots.len(),
            "roots": roots,
            "schema_version": SCHEMA_VERSION,
        })
    }

    pub fn evict_idle(&self, workspace_idle: Duration, lsp_idle: Duration) -> Vec<PathBuf> {
        let mut state = self.lock_state();
        let now = Instant::now();
        let evicted_roots: Vec<_> = state
            .workspaces
            .iter()
            .filter(|(_, workspace)| {
                let max_idle = if workspace.runtime.has_live_sessions() {
                    lsp_idle
                } else {
                    workspace_idle
                };
                workspace.active == 0 && now.duration_since(workspace.last_used) >= max_idle
            })
            .map(|(root, _)| root.clone())
            .collect();
        let evicted: Vec<_> = evicted_roots
            .iter()
            .filter_map(|root| state.workspaces.remove(root))
            .collect();
        if !evicted_roots.is_empty() {
            self.workspace_available.notify_all();
        }
        drop(state);
        drop(evicted);
        evicted_roots
    }

    pub fn workspace_count(&self) -> usize {
        self.lock_state().workspaces.len()
    }

    pub fn idle_duration(&self) -> Duration {
        Instant::now().duration_since(self.lock_state().last_activity)
    }

    fn acquire<'a>(&'a self, root: &Path, timeout: Duration) -> Result<WorkspaceLease<'a>, String> {
        let deadline = Instant::now() + timeout;
        let root = root.to_owned();
        let mut state = self.lock_state();
        loop {
            let now = Instant::now();
            if let Some(workspace) = state.workspaces.get_mut(&root) {
                workspace.active += 1;
                workspace.last_used = now;
                let runtime = Arc::clone(&workspace.runtime);
                state.last_activity = now;
                return Ok(WorkspaceLease {
                    service: self,
                    root,
                    runtime,
                });
            }
            if state.workspaces.len() >= self.max_workspaces {
                let victim = state
                    .workspaces
                    .iter()
                    .filter(|(_, workspace)| workspace.active == 0)
                    .min_by_key(|(_, workspace)| workspace.last_used)
                    .map(|(candidate, _)| candidate.clone());
                if let Some(victim) = victim {
                    let evicted = state.workspaces.remove(&victim);
                    drop(state);
                    drop(evicted);
                    state = self.lock_state();
                    continue;
                } else {
                    let remaining = deadline.saturating_duration_since(now);
                    if remaining.is_zero() {
                        return Err(format!(
                            "workspace capacity timed out after {}s",
                            timeout.as_secs_f64()
                        ));
                    }
                    let waited = self
                        .workspace_available
                        .wait_timeout(state, remaining)
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                    state = waited.0;
                    if waited.1.timed_out() {
                        return Err(format!(
                            "workspace capacity timed out after {}s",
                            timeout.as_secs_f64()
                        ));
                    }
                    continue;
                }
            }
            state.workspaces.insert(
                root.clone(),
                WorkspaceState {
                    active: 1,
                    last_used: now,
                    runtime: Arc::new(Workspace::new(
                        &root,
                        self.workspace_scratch(&root),
                        timeout,
                    )),
                },
            );
            state.last_activity = now;
            let runtime = Arc::clone(
                &state
                    .workspaces
                    .get(&root)
                    .expect("inserted workspace must exist")
                    .runtime,
            );
            return Ok(WorkspaceLease {
                service: self,
                root,
                runtime,
            });
        }
    }

    fn release(&self, root: &Path) {
        let mut state = self.lock_state();
        let now = Instant::now();
        if let Some(workspace) = state.workspaces.get_mut(root) {
            workspace.active = workspace.active.saturating_sub(1);
            workspace.last_used = now;
        }
        state.last_activity = now;
        self.workspace_available.notify_all();
    }

    fn lock_state(&self) -> MutexGuard<'_, DaemonState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn workspace_scratch(&self, root: &Path) -> PathBuf {
        let mut hasher = DefaultHasher::new();
        root.hash(&mut hasher);
        self.scratch_root.join(format!("{:016x}", hasher.finish()))
    }
}

impl Drop for WorkspaceLease<'_> {
    fn drop(&mut self) {
        self.service.release(&self.root);
    }
}

fn query_timeout(seconds: f64) -> Duration {
    let seconds = if seconds.is_nan() {
        1.0
    } else {
        seconds.clamp(1.0, 3600.0)
    };
    Duration::from_secs_f64(seconds)
}

pub fn execute(cli: &Cli, root: &Path, transport: &'static str) -> QueryResult {
    let started = Instant::now();
    if let Some(failure) = boundary::evaluate(
        cli,
        root,
        started.elapsed().as_secs_f64() * 1000.0,
        transport,
    ) {
        return QueryResult {
            data: serde_json::to_value(&failure.response)
                .expect("boundary response serialization must succeed"),
            status: failure.status,
            plain: failure.plain,
        };
    }

    if let Command::Find(arguments) = &cli.command
        && (arguments.text || arguments.mode == FindMode::Text)
    {
        return execute_text_search(cli, arguments, root, transport, started);
    }
    let scratch = std::env::temp_dir().join(format!("codeq-2-oneshot-{}", std::process::id()));
    let workspace = Workspace::new(root, scratch.clone(), query_timeout(cli.timeout));
    let result = execute_with_workspace(cli, root, transport, &workspace);
    workspace.close();
    let _ = fs::remove_dir_all(scratch);
    result
}

fn execute_with_workspace(
    cli: &Cli,
    root: &Path,
    transport: &'static str,
    workspace: &Workspace,
) -> QueryResult {
    let started = Instant::now();
    if let Some(failure) = boundary::evaluate(cli, root, 0.0, transport) {
        return QueryResult {
            data: serde_json::to_value(&failure.response)
                .expect("boundary response serialization must succeed"),
            status: failure.status,
            plain: failure.plain,
        };
    }
    if let Command::Find(arguments) = &cli.command
        && (arguments.text || arguments.mode == FindMode::Text)
    {
        return execute_text_search(cli, arguments, root, transport, started);
    }

    let sessions_before = workspace.session_stats();
    let metrics_before = workspace.metrics();
    let execution_started = Instant::now();
    let produced = match &cli.command {
        Command::Context(arguments) => semantic::context(workspace, arguments, cli.limit),
        Command::Trace(arguments) => semantic::trace(workspace, arguments, cli.limit),
        Command::Find(arguments)
            if arguments.mode == FindMode::Concept
                || (arguments.mode == FindMode::Auto
                    && concept::has_multiple_terms(&arguments.query)) =>
        {
            concept::search(workspace, arguments, cli.limit)
        }
        Command::Find(arguments) => semantic::find(workspace, arguments, cli.limit),
        Command::Review(arguments) => gitreview::review(workspace, arguments, cli.limit),
    };
    let mut data = produced.unwrap_or_else(|error| {
        serde_json::json!({
            "status": "error",
            "target": cli.command.target(),
            "error": error,
        })
    });
    let phase_ms = data
        .as_object_mut()
        .and_then(|object| object.remove("_phase_ms"));
    let sessions = workspace.session_stats();
    let metrics = workspace.metrics();
    let mut meta = serde_json::json!({
        "root": workspace.root(),
        "duration_ms": started.elapsed().as_secs_f64() * 1000.0,
        "queue_ms": 0.0,
        "execution_ms": execution_started.elapsed().as_secs_f64() * 1000.0,
        "lsp_sessions_before": sessions_before,
        "lsp_sessions": sessions,
        "lsp_started": metrics.sessions_started > metrics_before.sessions_started,
        "lsp_request_count": metrics.lsp_request_count.saturating_sub(metrics_before.lsp_request_count),
        "prewarm_files": metrics.prewarm_files.saturating_sub(metrics_before.prewarm_files),
        "prewarm_probes": metrics.prewarm_probes.saturating_sub(metrics_before.prewarm_probes),
        "prewarm_early_stops": metrics.prewarm_early_stops.saturating_sub(metrics_before.prewarm_early_stops),
        "cache": {
            "document_symbols_hit": metrics.document_symbols_hit.saturating_sub(metrics_before.document_symbols_hit),
            "document_symbols_miss": metrics.document_symbols_miss.saturating_sub(metrics_before.document_symbols_miss),
            "document_symbols_waited": metrics.document_symbols_waited.saturating_sub(metrics_before.document_symbols_waited),
            "document_symbols_evicted": metrics.document_symbols_evicted.saturating_sub(metrics_before.document_symbols_evicted),
            "document_symbol_entries": metrics.document_symbol_entries,
        },
        "transport": transport,
    });
    if let Some(phase_ms) = phase_ms {
        meta["phase_ms"] = phase_ms;
    }
    if let Some(lexical) = data.get("lexical_references") {
        meta["text"] = serde_json::json!({
            "matching_file_count": integer(lexical, "matching_file_count"),
            "tracked_matching_lines": integer(lexical, "tracked_line_count"),
            "untracked_matching_lines": integer(lexical, "untracked_line_count"),
        });
    }
    data["_meta"] = meta;
    data["schema_version"] = Value::from(SCHEMA_VERSION);
    let status = data
        .get("status")
        .cloned()
        .and_then(|value| serde_json::from_value(value).ok())
        .unwrap_or(Status::Error);
    let plain = render_semantic(&data, cli.command.name());
    QueryResult {
        data,
        status,
        plain,
    }
}

fn render_semantic(data: &Value, command: &str) -> String {
    if data.get("status").and_then(Value::as_str) != Some("ok") {
        return data
            .get("reason")
            .or_else(|| data.get("error"))
            .and_then(Value::as_str)
            .unwrap_or("query failed")
            .to_owned();
    }
    match command {
        "find" => render_find(data),
        "context" if data.get("kind").and_then(Value::as_str) == Some("file") => {
            render_file_context(data)
        }
        "context" => render_symbol_context(data),
        "trace" => render_trace(data),
        "review" => render_review(data),
        _ => "ok".to_owned(),
    }
}

fn render_find(data: &Value) -> String {
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
            let path = display_path(
                data,
                result.get("path").and_then(Value::as_str).unwrap_or(""),
            );
            let line = integer(result, "line");
            let column = integer(result, "column");
            let marker = if result.get("is_test").and_then(Value::as_bool) == Some(true) {
                " [test]"
            } else {
                ""
            };
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
    if let Some(hover) = data
        .get("hover")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        lines.extend([String::new(), "Hover".to_owned(), hover.to_owned()]);
    }
    if let Some(source) = data.pointer("/source/text").and_then(Value::as_str) {
        lines.extend([
            String::new(),
            "Source".to_owned(),
            source.to_owned(),
            String::new(),
        ]);
    }
    for (key, title) in [
        ("callers", "Callers"),
        ("callees", "Callees"),
        ("implementations", "Implementations"),
    ] {
        if let Some(items) = data.get(key).and_then(Value::as_array) {
            lines.push(format!("{title} ({})", items.len()));
            if items.is_empty() {
                lines.push("  -".to_owned());
            } else {
                for item in items {
                    lines.push(format!(
                        "  {}  {}:{}:{}",
                        item.get("name").and_then(Value::as_str).unwrap_or(""),
                        display_path(data, item.get("path").and_then(Value::as_str).unwrap_or("")),
                        integer(item, "line"),
                        integer(item, "column")
                    ));
                }
            }
        }
    }
    if let Some(tests) = data.get("tests").and_then(Value::as_array) {
        let meta = data
            .pointer("/section_metadata/tests")
            .unwrap_or(&Value::Null);
        lines.push(format!(
            "Tests (showing {} of {})",
            tests.len(),
            integer(meta, "total_count")
        ));
        for item in tests {
            let evidence = item
                .get("evidence_type")
                .and_then(Value::as_str)
                .unwrap_or("")
                .replace('_', " ");
            let confidence = item.get("confidence").and_then(Value::as_str).unwrap_or("");
            let prefix = if confidence == "direct" {
                format!("[{evidence}]")
            } else {
                format!("[{confidence}: {evidence}]")
            };
            let item_name = item
                .get("name")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(|value| format!("{value}  "))
                .unwrap_or_default();
            lines.push(format!(
                "  {prefix} {item_name}{}:{}:{}",
                display_path(data, item.get("path").and_then(Value::as_str).unwrap_or("")),
                integer(item, "line"),
                integer(item, "column")
            ));
        }
        if meta.get("truncated").and_then(Value::as_bool) == Some(true) {
            lines.push("  ... more test evidence available; increase --limit".to_owned());
        }
    }
    for (key, title) in [
        ("references", "References"),
        ("possible_dynamic_references", "Possible dynamic references"),
    ] {
        if let Some(items) = data.get(key).and_then(Value::as_array) {
            lines.push(format!("{title} ({})", items.len()));
            if items.is_empty() {
                lines.push("  -".to_owned());
            } else {
                for item in items {
                    lines.push(format!(
                        "  {}:{}:{}",
                        display_path(data, item.get("path").and_then(Value::as_str).unwrap_or("")),
                        integer(item, "line"),
                        integer(item, "column")
                    ));
                }
            }
        }
    }
    lines.extend([String::new(), format!("[{} ms]", duration(data))]);
    lines.join("\n")
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
        for item in outline {
            lines.push(format!(
                "  {:<12} {}  line {}",
                item.get("kind")
                    .and_then(Value::as_str)
                    .unwrap_or("Unknown"),
                item.get("name").and_then(Value::as_str).unwrap_or(""),
                integer(item, "line")
            ));
        }
    }
    lines.push(
        "  next: use --outline-depth 2, --kind KIND, or --container NAME to disclose more"
            .to_owned(),
    );
    lines.push(String::new());
    if data.get("topology_loaded").and_then(Value::as_bool) == Some(true) {
        lines.push(format!(
            "Topology: {} imports, {} importers",
            integer(data, "import_count"),
            integer(data, "importer_count")
        ));
    } else {
        lines.push(format!(
            "Topology: hidden ({} direct imports; use --topology to disclose imports/importers)",
            integer(data, "import_count")
        ));
    }
    lines.extend([String::new(), format!("[{} ms]", duration(data))]);
    lines.join("\n")
}

fn render_trace(data: &Value) -> String {
    let tree = data.get("tree").unwrap_or(&Value::Null);
    let mut lines = Vec::new();
    render_trace_node(data, tree, "", true, true, &mut lines);
    lines.push(String::new());
    lines.push(format!(
        "[{} nodes; depth={}; node_limit={}; truncated={}; {} ms]",
        integer(data, "node_count"),
        integer(data, "depth"),
        integer(data, "node_limit"),
        data.get("truncated")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        duration(data)
    ));
    lines.join("\n")
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
    lines.push(format!(
        "{prefix}{branch}{}  {}:{}",
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
    format!(
        "Review against {}\n\n{} changed files; {} changed symbols; {} impacted files; {} tests\n\n[{} ms]",
        data.get("base").and_then(Value::as_str).unwrap_or(""),
        integer(data, "changed_file_count"),
        integer(data, "changed_symbol_count"),
        integer(data, "impacted_file_count"),
        integer(data, "test_count"),
        duration(data)
    )
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

fn execute_text_search(
    cli: &Cli,
    arguments: &crate::cli::FindArgs,
    root: &Path,
    transport: &'static str,
    started: Instant,
) -> QueryResult {
    let mut data = match textsearch::search(
        root,
        &arguments.query,
        cli.limit,
        &arguments.paths,
        &arguments.globs,
        arguments.exclude_tests,
    ) {
        Ok(data) => data,
        Err(error) => serde_json::json!({
            "status": "error",
            "mode": "text",
            "search_mode": "text",
            "query": arguments.query,
            "reason": error,
            "results": [],
        }),
    };
    data["search_mode"] = Value::String("text".to_owned());
    let duration_ms = started.elapsed().as_secs_f64() * 1000.0;
    data["_meta"] = serde_json::json!({
        "root": root,
        "duration_ms": duration_ms,
        "queue_ms": 0.0,
        "execution_ms": duration_ms,
        "lsp_sessions_before": [],
        "lsp_sessions": [],
        "lsp_started": false,
        "lsp_request_count": 0,
        "prewarm_files": 0,
        "prewarm_probes": 0,
        "prewarm_early_stops": 0,
        "cache": {
            "document_symbols_hit": 0,
            "document_symbols_miss": 0,
            "document_symbols_waited": 0,
            "document_symbols_evicted": 0,
            "document_symbol_entries": 0,
        },
        "text": {
            "matching_file_count": data.get("matching_file_count").and_then(Value::as_u64).unwrap_or(0),
            "tracked_matching_lines": data.get("tracked_line_count").and_then(Value::as_u64).unwrap_or(0),
            "untracked_matching_lines": data.get("untracked_line_count").and_then(Value::as_u64).unwrap_or(0),
        },
        "transport": transport,
    });
    data["schema_version"] = Value::from(SCHEMA_VERSION);
    let status = data
        .get("status")
        .cloned()
        .and_then(|value| serde_json::from_value(value).ok())
        .unwrap_or(Status::Error);
    let plain = render_text_search(&data, root);
    QueryResult {
        data,
        status,
        plain,
    }
}

fn render_text_search(data: &Value, root: &Path) -> String {
    if data.get("status").and_then(Value::as_str) != Some("ok") {
        return data
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("find failed")
            .to_owned();
    }
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

pub fn received(data: Value, cli: &Cli, root: &Path) -> Result<QueryResult, String> {
    let status = data
        .get("status")
        .cloned()
        .ok_or_else(|| "daemon response has no status".to_owned())
        .and_then(|value| {
            serde_json::from_value(value)
                .map_err(|error| format!("daemon returned an invalid status: {error}"))
        })?;
    let plain = if status == Status::Ok {
        match &cli.command {
            Command::Find(arguments) if arguments.text || arguments.mode == FindMode::Text => {
                render_text_search(&data, root)
            }
            command => render_semantic(&data, command.name()),
        }
    } else {
        let reason = data
            .get("reason")
            .or_else(|| data.get("error"))
            .and_then(Value::as_str)
            .unwrap_or("query failed");
        let prefix = format!("{}{}", root.display(), std::path::MAIN_SEPARATOR);
        let mut plain = reason.replace(&prefix, "");
        if status == Status::Unavailable {
            plain = format!("codeq: {plain}");
        } else if let Some(recovery) = data.get("recovery_command").and_then(Value::as_str) {
            plain.push_str("\n  try: ");
            plain.push_str(recovery);
        }
        plain
    };
    Ok(QueryResult {
        data,
        status,
        plain,
    })
}
