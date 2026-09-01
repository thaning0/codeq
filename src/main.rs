mod boundary;
mod cli;
mod contracts;
mod daemon;
mod repository;
mod runtime;
mod target;

use std::env;
use std::process::ExitCode;
use std::time::Instant;

use clap::{CommandFactory, Parser, error::ErrorKind};
use contracts::{SCHEMA_VERSION, Status, query_exit_code};
use serde::Serialize;

use crate::cli::{Cli, EarlyOutput};

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

fn emit_failure(response: &impl Serialize, status: Status, json: bool, plain: &str) -> ExitCode {
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(response)
                .expect("failure response serialization must succeed")
        );
    } else {
        eprintln!("{plain}");
    }
    ExitCode::from(query_exit_code(status))
}

fn main() -> ExitCode {
    let arguments: Vec<_> = env::args_os().collect();
    let raw_arguments = &arguments[1..];
    if raw_arguments
        .first()
        .is_some_and(|value| value == "--internal-daemon")
    {
        return match daemon::internal_main(&raw_arguments[1..]) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => {
                eprintln!("codeq daemon: {error}");
                ExitCode::from(2)
            }
        };
    }
    if let Some(output) = cli::early_output(raw_arguments) {
        match output {
            EarlyOutput::Help(help) => print!("{help}"),
            EarlyOutput::Version => println!("codeq {}", env!("CARGO_PKG_VERSION")),
        }
        return ExitCode::SUCCESS;
    }

    let cli = Cli::parse_from(&arguments);
    if let Err(message) = cli.validate(raw_arguments) {
        Cli::command()
            .error(ErrorKind::ArgumentConflict, message)
            .exit();
    }
    let started = Instant::now();
    let root = match repository::resolve_root(&cli.root) {
        Ok(root) => root,
        Err(error) => {
            eprintln!("codeq: {error}");
            return ExitCode::from(2);
        }
    };

    if let Some(failure) = boundary::evaluate(&cli, &root, started.elapsed().as_secs_f64() * 1000.0)
    {
        return emit_failure(&failure.response, failure.status, cli.json, &failure.plain);
    }

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
