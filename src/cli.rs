use std::ffi::OsString;
use std::path::PathBuf;

use clap::{Args, Parser, Subcommand, ValueEnum};
use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize, Parser, Serialize)]
#[command(
    name = "codeq",
    version,
    about = "Small, read-only code-intelligence CLI for coding agents.",
    subcommand_required = true,
    arg_required_else_help = true,
    disable_help_subcommand = true,
    disable_version_flag = true
)]
pub struct Cli {
    #[arg(long, global = true, value_name = "PATH", default_value = ".")]
    pub root: PathBuf,

    #[arg(long, global = true)]
    pub json: bool,

    #[arg(
        long,
        global = true,
        value_name = "N",
        default_value_t = 20,
        allow_hyphen_values = true
    )]
    pub limit: i64,

    #[arg(
        long,
        global = true,
        value_name = "SEC",
        default_value_t = 20.0,
        allow_hyphen_values = true
    )]
    pub timeout: f64,

    #[arg(long, global = true)]
    pub no_daemon: bool,

    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Deserialize, Serialize, Subcommand)]
#[serde(tag = "command", rename_all = "snake_case")]
pub enum Command {
    #[command(
        visible_alias = "search",
        about = "Find symbols or related source from a name or short description."
    )]
    Find(FindArgs),

    #[command(about = "Get one target's definition, callers, callees, references, and tests.")]
    Context(ContextArgs),

    #[command(about = "Trace a bounded incoming or outgoing call hierarchy.")]
    Trace(TraceArgs),

    #[command(about = "Summarize semantic impact of changes relative to a Git base ref.")]
    Review(ReviewArgs),
}

impl Command {
    pub const fn name(&self) -> &'static str {
        match self {
            Self::Find(_) => "find",
            Self::Context(_) => "context",
            Self::Trace(_) => "trace",
            Self::Review(_) => "review",
        }
    }

    pub fn target(&self) -> Option<&str> {
        match self {
            Self::Context(arguments) => Some(&arguments.target),
            Self::Trace(arguments) => Some(&arguments.target),
            Self::Find(_) | Self::Review(_) => None,
        }
    }
}

impl Cli {
    pub fn validate(&self, arguments: &[OsString]) -> Result<(), &'static str> {
        match &self.command {
            Command::Find(find) => {
                if find.text && !matches!(find.mode, FindMode::Auto | FindMode::Text) {
                    return Err("--text cannot be combined with --mode symbol or --mode concept");
                }
                let effective_mode = if find.text { FindMode::Text } else { find.mode };
                if find.kind.is_some()
                    && matches!(effective_mode, FindMode::Concept | FindMode::Text)
                {
                    return Err("--kind requires auto or symbol mode");
                }
                if find.files_only && self.json {
                    return Err(
                        "--files-only controls plain output and cannot be combined with --json",
                    );
                }
            }
            Command::Context(context) => {
                if (!context.globs.is_empty() || context.exclude_tests)
                    && context.lexical_references.is_none()
                {
                    return Err(
                        "--glob/--exclude-tests require --lexical-references; --path also scopes symbol resolution",
                    );
                }
            }
            Command::Trace(trace) => {
                if argument_was_supplied(arguments, "--limit")
                    && argument_was_supplied(arguments, "--node-limit")
                    && self.limit != trace.node_limit
                {
                    return Err(
                        "conflicting trace limits: --limit and --node-limit must have the same value when both are supplied",
                    );
                }
            }
            Command::Review(_) => {}
        }
        Ok(())
    }

    pub fn apply_compatibility_aliases(&mut self, arguments: &[OsString]) {
        if let Command::Trace(trace) = &mut self.command
            && argument_was_supplied(arguments, "--limit")
            && !argument_was_supplied(arguments, "--node-limit")
        {
            trace.node_limit = self.limit;
        }
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize, ValueEnum)]
#[serde(rename_all = "snake_case")]
pub enum FindMode {
    #[default]
    Auto,
    Symbol,
    Concept,
    Text,
}

#[derive(Debug, Deserialize, Serialize, Args)]
pub struct FindArgs {
    #[arg(value_name = "QUERY")]
    pub query: String,

    #[arg(long, value_enum, default_value_t = FindMode::Auto)]
    pub mode: FindMode,

    #[arg(long, value_name = "KIND", conflicts_with = "text")]
    pub kind: Option<String>,

    #[arg(long)]
    pub text: bool,

    #[arg(long)]
    pub files_only: bool,

    #[arg(long = "path", value_name = "PREFIX")]
    pub paths: Vec<String>,

    #[arg(long = "glob", value_name = "PATTERN")]
    pub globs: Vec<String>,

    #[arg(long)]
    pub exclude_tests: bool,
}

#[derive(Debug, Deserialize, Serialize, Args)]
pub struct ContextArgs {
    #[arg(value_name = "TARGET")]
    pub target: String,

    #[arg(
        long,
        value_name = "N",
        value_parser = clap::value_parser!(u16).range(1..=1000),
        allow_hyphen_values = true
    )]
    pub lines: Option<u16>,

    #[arg(
        long,
        value_name = "N",
        default_value_t = 1,
        allow_hyphen_values = true
    )]
    pub outline_depth: i64,

    #[arg(long, value_name = "KIND")]
    pub kind: Option<String>,

    #[arg(long, value_name = "NAME")]
    pub container: Option<String>,

    #[arg(long)]
    pub topology: bool,

    #[arg(long = "section", value_name = "SECTION")]
    pub sections: Vec<String>,

    #[arg(long, num_args = 0..=1, value_name = "TEXT", default_missing_value = "")]
    pub lexical_references: Option<String>,

    #[arg(long = "path", value_name = "PREFIX")]
    pub paths: Vec<String>,

    #[arg(long = "symbol-path", value_name = "PREFIX")]
    pub symbol_paths: Vec<String>,

    #[arg(long = "glob", value_name = "PATTERN")]
    pub globs: Vec<String>,

    #[arg(long)]
    pub exclude_tests: bool,
}

#[derive(Debug, Deserialize, Serialize, Args)]
pub struct TraceArgs {
    #[arg(value_name = "TARGET")]
    pub target: String,

    #[arg(long = "in", conflicts_with = "outgoing")]
    pub incoming: bool,

    #[arg(long = "out", conflicts_with = "incoming")]
    pub outgoing: bool,

    #[arg(
        long,
        value_name = "N",
        default_value_t = 1,
        allow_hyphen_values = true
    )]
    pub depth: usize,

    #[arg(
        long,
        value_name = "N",
        default_value_t = 100,
        allow_hyphen_values = true
    )]
    pub node_limit: i64,
}

#[derive(Debug, Deserialize, Serialize, Args)]
pub struct ReviewArgs {
    #[arg(long, value_name = "REF", default_value = "HEAD~1")]
    pub base: String,

    #[arg(long)]
    pub merge_base: bool,
}

pub enum EarlyOutput {
    Help(&'static str),
    Version,
}

pub fn early_output(arguments: &[OsString]) -> Option<EarlyOutput> {
    let mut command = None;
    let mut index = 0;
    while index < arguments.len() {
        let argument = arguments[index].to_str()?;
        if argument == "--" {
            return None;
        }
        if argument == "-h" || argument == "--help" {
            let help = match command {
                Some("find" | "search") => include_str!("help/find.txt"),
                Some("context") => include_str!("help/context.txt"),
                Some("trace") => include_str!("help/trace.txt"),
                Some("review") => include_str!("help/review.txt"),
                _ => include_str!("help/top.txt"),
            };
            return Some(EarlyOutput::Help(help));
        }
        if argument == "--version" {
            return Some(EarlyOutput::Version);
        }
        if command.is_none() {
            match argument {
                "find" | "search" | "context" | "trace" | "review" => {
                    command = Some(argument);
                }
                "--root" | "--limit" | "--timeout" => {
                    index += 1;
                }
                _ => {}
            }
        }
        index += 1;
    }
    None
}

fn argument_was_supplied(arguments: &[OsString], option: &str) -> bool {
    arguments.iter().any(|argument| {
        argument == option
            || argument
                .to_string_lossy()
                .starts_with(&format!("{option}="))
    })
}
