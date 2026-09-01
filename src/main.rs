mod cli;
mod contracts;
mod runtime;

use std::process::ExitCode;

use clap::Parser;
use contracts::{SCHEMA_VERSION, Status, query_exit_code};
use serde::Serialize;

use crate::cli::Cli;

#[derive(Serialize)]
struct DevelopmentMeta {
    implementation: &'static str,
    runtime: runtime::RuntimeIdentity,
    request: serde_json::Value,
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

fn main() -> ExitCode {
    let cli = Cli::parse();
    let command = cli.command.name();
    let request =
        serde_json::to_value(&cli.command).expect("CLI request serialization must succeed");
    let response = UnavailableResponse {
        status: Status::Unavailable,
        command,
        reason: "the Rust semantic runtime is not implemented yet on the 2.0 development branch",
        meta: DevelopmentMeta {
            implementation: "rust",
            runtime: runtime::identity(),
            request,
        },
        schema_version: SCHEMA_VERSION,
    };

    if cli.json {
        println!(
            "{}",
            serde_json::to_string_pretty(&response).expect("response serialization must succeed")
        );
    } else {
        eprintln!("codeq: {}", response.reason);
    }

    // Until a command is implemented, fail closed using the existing query-failure
    // exit class. Parser/runtime failures remain clap's exit 2.
    ExitCode::from(query_exit_code(response.status))
}
