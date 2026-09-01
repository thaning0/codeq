mod cli;
mod contracts;
mod repository;
mod runtime;
mod target;

use std::env;
use std::process::ExitCode;
use std::time::Instant;

use clap::{CommandFactory, Parser, error::ErrorKind};
use contracts::{QueryMeta, SCHEMA_VERSION, Status, TargetFailureResponse, query_exit_code};
use serde::Serialize;

use crate::cli::{Cli, Command, EarlyOutput};

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
    let arguments: Vec<_> = env::args_os().collect();
    let raw_arguments = &arguments[1..];
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

    if let Command::Context(arguments) = &cli.command
        && let Some(intent) = target::explicit_path(&arguments.target, &root)
        && (!intent.inside_repository || !intent.path.exists())
    {
        let reason = if intent.inside_repository {
            format!("file not found: {}", intent.path.display())
        } else {
            format!("path is outside repository root: {}", intent.path.display())
        };
        let response = TargetFailureResponse {
            status: Status::NotFound,
            target: &arguments.target,
            path: &intent.path,
            reason,
            meta: QueryMeta::empty(&root, started.elapsed().as_secs_f64() * 1000.0),
            schema_version: SCHEMA_VERSION,
        };
        if cli.json {
            println!(
                "{}",
                serde_json::to_string_pretty(&response)
                    .expect("failure response serialization must succeed")
            );
        } else {
            let display_path = intent
                .path
                .strip_prefix(&root)
                .unwrap_or(&intent.path)
                .display();
            if intent.inside_repository {
                eprintln!("file not found: {display_path}");
            } else {
                eprintln!("path is outside repository root: {display_path}");
            }
        }
        return ExitCode::from(query_exit_code(response.status));
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
