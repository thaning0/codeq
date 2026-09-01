use std::collections::HashMap;
use std::path::Path;
use std::path::PathBuf;
use std::sync::{Condvar, Mutex, MutexGuard};
use std::time::Duration;
use std::time::Instant;

use serde::Serialize;
use serde_json::Value;

use crate::boundary;
use crate::cli::Cli;
use crate::contracts::{SCHEMA_VERSION, Status};
use crate::runtime;

pub struct DaemonService {
    state: Mutex<DaemonState>,
    workspace_available: Condvar,
    max_workspaces: usize,
}

struct DaemonState {
    workspaces: HashMap<PathBuf, WorkspaceState>,
    last_activity: Instant,
}

struct WorkspaceState {
    active: usize,
    last_used: Instant,
}

struct WorkspaceLease<'a> {
    service: &'a DaemonService,
    root: PathBuf,
}

pub struct QueryResult {
    pub data: Value,
    pub status: Status,
    pub plain: String,
}

impl DaemonService {
    pub fn new(max_workspaces: usize) -> Self {
        Self {
            state: Mutex::new(DaemonState {
                workspaces: HashMap::new(),
                last_activity: Instant::now(),
            }),
            workspace_available: Condvar::new(),
            max_workspaces: max_workspaces.max(1),
        }
    }

    pub fn query(&self, cli: &Cli) -> Result<Value, String> {
        let root = cli.root.clone();
        let _lease = self.acquire(&root, query_timeout(cli.timeout))?;
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
        let mut evicted = Vec::new();
        state.workspaces.retain(|root, workspace| {
            let keep = workspace.active != 0 || now.duration_since(workspace.last_used) < max_idle;
            if !keep {
                evicted.push(root.clone());
            }
            keep
        });
        if !evicted.is_empty() {
            self.workspace_available.notify_all();
        }
        evicted
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
                state.last_activity = now;
                return Ok(WorkspaceLease {
                    service: self,
                    root,
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
                    state.workspaces.remove(&victim);
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
                },
            );
            state.last_activity = now;
            return Ok(WorkspaceLease {
                service: self,
                root,
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
