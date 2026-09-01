use std::collections::HashMap;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::path::Path;
use std::path::PathBuf;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::Duration;
use std::time::Instant;

use serde::Serialize;
use serde_json::Value;

use crate::boundary;
use crate::cli::{Cli, Command, FindMode};
use crate::contracts::{SCHEMA_VERSION, Status};
use crate::runtime;
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
        let _workspace = &lease.runtime;
        Ok(execute(cli, &root, "daemon").data)
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

    pub fn evict_idle(&self, max_idle: Duration) -> Vec<PathBuf> {
        let mut state = self.lock_state();
        let now = Instant::now();
        let evicted_roots: Vec<_> = state
            .workspaces
            .iter()
            .filter(|(_, workspace)| {
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

#[derive(Serialize)]
struct DevelopmentMeta {
    implementation: &'static str,
    runtime: runtime::RuntimeIdentity,
    request: Value,
    transport: &'static str,
}

#[derive(Serialize)]
struct UnavailableResponse {
    status: Status,
    command: &'static str,
    reason: &'static str,
    #[serde(rename = "_meta")]
    meta: DevelopmentMeta,
    schema_version: u8,
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

    let reason = "the Rust semantic runtime is not implemented yet on the 2.0 development branch";
    let response = UnavailableResponse {
        status: Status::Unavailable,
        command: cli.command.name(),
        reason,
        meta: DevelopmentMeta {
            implementation: "rust",
            runtime: runtime::identity(),
            request: serde_json::to_value(&cli.command)
                .expect("CLI request serialization must succeed"),
            transport,
        },
        schema_version: SCHEMA_VERSION,
    };
    QueryResult {
        data: serde_json::to_value(response).expect("response serialization must succeed"),
        status: Status::Unavailable,
        plain: format!("codeq: {reason}"),
    }
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

pub fn received(data: Value, root: &Path) -> Result<QueryResult, String> {
    let status = data
        .get("status")
        .cloned()
        .ok_or_else(|| "daemon response has no status".to_owned())
        .and_then(|value| {
            serde_json::from_value(value)
                .map_err(|error| format!("daemon returned an invalid status: {error}"))
        })?;
    let reason = data
        .get("reason")
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
    Ok(QueryResult {
        data,
        status,
        plain,
    })
}
