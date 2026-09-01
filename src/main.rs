mod boundary;
mod cli;
mod client;
mod concept;
mod contracts;
mod daemon;
mod gitreview;
// Phase 3 lands transport and ownership before every semantic handler consumes it.
#[allow(dead_code)]
mod lsp;
mod repository;
mod runtime;
mod semantic;
mod service;
mod symbol;
mod target;
mod textsearch;
#[allow(dead_code)]
mod workspace;

use std::env;
use std::process::ExitCode;

use clap::{CommandFactory, Parser, error::ErrorKind};

use crate::cli::{Cli, EarlyOutput};
use crate::contracts::query_exit_code;

fn emit_result(result: service::QueryResult, json: bool) -> ExitCode {
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&result.data)
                .expect("response serialization must succeed")
        );
    } else if result.status == contracts::Status::Ok {
        println!("{}", result.plain);
    } else {
        eprintln!("{}", result.plain);
    }
    ExitCode::from(query_exit_code(result.status))
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

    let mut cli = Cli::parse_from(&arguments);
    if let Err(message) = cli.validate(raw_arguments) {
        Cli::command()
            .error(ErrorKind::ArgumentConflict, message)
            .exit();
    }
    cli.apply_compatibility_aliases(raw_arguments);
    let root = match repository::resolve_root(&cli.root) {
        Ok(root) => root,
        Err(error) => {
            eprintln!("codeq: {error}");
            return ExitCode::from(2);
        }
    };
    let result = if cli.no_daemon {
        service::execute(&cli, &root, "in_process")
    } else {
        match client::request(&cli, &root) {
            Ok(Some(data)) => match service::received(data, &root) {
                Ok(result) => result,
                Err(error) => {
                    eprintln!("codeq: {error}");
                    return ExitCode::from(2);
                }
            },
            Ok(None) => service::execute(&cli, &root, "in_process"),
            Err(error) => {
                eprintln!("codeq: {error}");
                return ExitCode::from(2);
            }
        }
    };
    emit_result(result, cli.json)
}
