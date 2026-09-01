use std::path::Path;

use serde::Serialize;

use crate::cli::{Cli, Command, ContextArgs, FindMode};
use crate::contracts::{
    CONTEXT_SECTIONS, ContextValidationResponse, FindFailureResponse, QueryMeta, SCHEMA_VERSION,
    Status, TargetFailureResponse,
};
use crate::target::{self, ExplicitPath};

#[derive(Serialize)]
#[serde(untagged)]
pub enum Response<'a> {
    ContextValidation(ContextValidationResponse<'a>),
    Find(FindFailureResponse<'a>),
    Target(TargetFailureResponse<'a>),
}

pub struct Failure<'a> {
    pub response: Response<'a>,
    pub status: Status,
    pub plain: String,
}

pub fn evaluate<'a>(
    cli: &'a Cli,
    root: &Path,
    duration_ms: f64,
    transport: &'static str,
) -> Option<Failure<'a>> {
    if let Command::Context(arguments) = &cli.command
        && let Some(failure) = context_validation(arguments, root, duration_ms, transport)
    {
        return Some(failure);
    }
    if let Command::Find(arguments) = &cli.command
        && !arguments.text
        && arguments.mode != FindMode::Text
        && let Some(intent) = target::explicit_path(&arguments.query, root)
    {
        return Some(find_path_failure(
            &arguments.query,
            intent,
            root,
            duration_ms,
            transport,
        ));
    }

    let query = cli.command.target()?;
    let intent = target::explicit_path(query, root)?;
    let (status, reason) = if !intent.inside_repository {
        (
            Status::NotFound,
            format!("path is outside repository root: {}", intent.path.display()),
        )
    } else if !intent.path.is_file() {
        (
            Status::NotFound,
            format!("file not found: {}", intent.path.display()),
        )
    } else if !target::is_semantic_source(&intent.path) {
        (
            Status::UnsupportedLanguage,
            format!(
                "unsupported source language: {}",
                target::source_suffix(&intent.path)
            ),
        )
    } else if matches!(cli.command, Command::Trace(_)) && !intent.has_position() {
        (
            Status::UnsupportedTarget,
            "source-file targets are supported by `codeq context`, not symbol tracing".to_owned(),
        )
    } else {
        return None;
    };
    let plain = display_message(&reason, root);
    Some(Failure {
        response: Response::Target(TargetFailureResponse {
            status,
            target: query,
            path: intent.path,
            reason,
            meta: QueryMeta::empty(root, duration_ms, transport),
            schema_version: SCHEMA_VERSION,
        }),
        status,
        plain,
    })
}

fn context_validation<'a>(
    arguments: &'a ContextArgs,
    root: &Path,
    duration_ms: f64,
    transport: &'static str,
) -> Option<Failure<'a>> {
    let mut requested = Vec::new();
    for section in &arguments.sections {
        let section = section.trim();
        if !section.is_empty() && !requested.contains(&section) {
            requested.push(section);
        }
    }
    let invalid: Vec<_> = requested
        .iter()
        .copied()
        .filter(|section| !CONTEXT_SECTIONS.contains(section))
        .collect();
    let (reason, recovery_command) = if !invalid.is_empty() {
        (
            format!(
                "unknown context section(s): {}; allowed values: {}",
                invalid.join(", "),
                CONTEXT_SECTIONS.join(", ")
            ),
            format!("codeq context {}", shell_quote(&arguments.target)),
        )
    } else if requested.contains(&"lexical-references") && arguments.lexical_references.is_none() {
        (
            "section lexical-references requires --lexical-references".to_owned(),
            format!(
                "codeq context {} --section lexical-references --lexical-references",
                shell_quote(&arguments.target)
            ),
        )
    } else {
        return None;
    };
    let plain = format!("{reason}\n  try: {recovery_command}");
    Some(Failure {
        response: Response::ContextValidation(ContextValidationResponse {
            status: Status::InvalidQuery,
            target: &arguments.target,
            reason,
            allowed_sections: CONTEXT_SECTIONS,
            recovery_command,
            meta: QueryMeta::empty(root, duration_ms, transport),
            schema_version: SCHEMA_VERSION,
        }),
        status: Status::InvalidQuery,
        plain,
    })
}

fn find_path_failure<'a>(
    query: &'a str,
    intent: ExplicitPath,
    root: &Path,
    duration_ms: f64,
    transport: &'static str,
) -> Failure<'a> {
    let (status, reason) = if !intent.inside_repository {
        (
            Status::NotFound,
            format!("path is outside repository root: {}", intent.path.display()),
        )
    } else if !intent.path.is_file() {
        (
            Status::NotFound,
            format!("file not found: {}", intent.path.display()),
        )
    } else if intent.has_position() {
        (
            Status::UnsupportedTarget,
            "use `codeq context PATH:LINE[:COLUMN]` for a source location".to_owned(),
        )
    } else if target::is_semantic_source(&intent.path) {
        (
            Status::UnsupportedTarget,
            "use `codeq context FILE` for a source-file target".to_owned(),
        )
    } else {
        (
            Status::UnsupportedLanguage,
            format!(
                "unsupported source language: {}",
                target::source_suffix(&intent.path)
            ),
        )
    };
    let plain = display_message(&reason, root);
    Failure {
        response: Response::Find(FindFailureResponse {
            query,
            path: intent.path,
            results: Vec::new(),
            result_count: 0,
            total_candidates: 0,
            truncated: false,
            errors: Vec::new(),
            status,
            reason,
            meta: QueryMeta::empty(root, duration_ms, transport),
            schema_version: SCHEMA_VERSION,
        }),
        status,
        plain,
    }
}

fn display_message(message: &str, root: &Path) -> String {
    message.replace(
        &format!("{}{}", root.display(), std::path::MAIN_SEPARATOR),
        "",
    )
}

fn shell_quote(value: &str) -> String {
    if !value.is_empty()
        && value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || "_@%+=:,./-".contains(character))
    {
        value.to_owned()
    } else {
        format!("'{}'", value.replace('\'', "'\"'\"'"))
    }
}
