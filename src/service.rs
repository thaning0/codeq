use std::path::Path;
use std::time::Instant;

use serde::Serialize;
use serde_json::Value;

use crate::boundary;
use crate::cli::Cli;
use crate::contracts::{SCHEMA_VERSION, Status};
use crate::runtime;

pub struct QueryResult {
    pub data: Value,
    pub status: Status,
    pub plain: String,
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
