mod boundary;
mod cli;
mod client;
mod concept;
mod contracts;
mod daemon;
mod dynamic;
mod gitreview;
mod lsp;
mod render;
mod repository;
mod runtime;
mod semantic;
mod service;
mod symbol;
mod target;
mod textsearch;
mod workspace;

use std::env;
use std::path::Path;
use std::process::ExitCode;

use clap::{CommandFactory, FromArgMatches, error::ErrorKind};
use serde_json::Value;

use crate::cli::Cli;
use crate::contracts::query_exit_code;

fn emit_result(data: Value, cli: &Cli, root: &Path) -> ExitCode {
    let status = match serde_json::from_value(data["status"].clone()) {
        Ok(status) => status,
        Err(error) => {
            eprintln!("codeq: invalid query response status: {error}");
            return ExitCode::from(2);
        }
    };
    if cli.json {
        println!(
            "{}",
            serde_json::to_string_pretty(&data).expect("response serialization must succeed")
        );
    } else {
        let plain = render::plain(&data, &cli.command, root);
        if status == contracts::Status::Ok {
            println!("{plain}");
        } else {
            eprintln!("{plain}");
        }
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
    let matches = Cli::command().get_matches_from(&arguments);
    let mut cli = Cli::from_arg_matches(&matches).unwrap_or_else(|error| error.exit());
    if let Err(message) = cli.validate(&matches) {
        Cli::command()
            .error(ErrorKind::ArgumentConflict, message)
            .exit();
    }
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
            Ok(Some(data)) => data,
            Ok(None) => service::execute(&cli, &root, "in_process"),
            Err(error) => {
                eprintln!("codeq: {error}");
                return ExitCode::from(2);
            }
        }
    };
    emit_result(result, &cli, &root)
}
