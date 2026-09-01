use std::path::PathBuf;

use clap::{Args, Parser, Subcommand, ValueEnum};
use serde::Serialize;

#[derive(Debug, Parser)]
#[command(
    name = "codeq",
    version,
    about = "Small, read-only code-intelligence CLI for coding agents.",
    subcommand_required = true,
    arg_required_else_help = true
)]
pub struct Cli {
    #[arg(long, global = true, value_name = "PATH", default_value = ".")]
    pub root: PathBuf,

    #[arg(long, global = true)]
    pub json: bool,

    #[arg(long, global = true, value_name = "N", default_value_t = 20)]
    pub limit: usize,

    #[arg(long, global = true, value_name = "SEC", default_value_t = 20.0)]
    pub timeout: f64,

    #[arg(long, global = true)]
    pub no_daemon: bool,

    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Serialize, Subcommand)]
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
}

#[derive(Clone, Copy, Debug, Default, Serialize, ValueEnum)]
#[serde(rename_all = "snake_case")]
pub enum FindMode {
    #[default]
    Auto,
    Symbol,
    Concept,
    Text,
}

#[derive(Debug, Serialize, Args)]
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

#[derive(Clone, Copy, Debug, Serialize, ValueEnum)]
#[serde(rename_all = "kebab-case")]
pub enum ContextSection {
    Source,
    Callers,
    Callees,
    Implementations,
    Tests,
    References,
    PossibleDynamicReferences,
    LexicalReferences,
}

#[derive(Debug, Serialize, Args)]
pub struct ContextArgs {
    #[arg(value_name = "TARGET")]
    pub target: String,

    #[arg(long, value_name = "N", value_parser = clap::value_parser!(u16).range(1..=1000))]
    pub lines: Option<u16>,

    #[arg(long, value_name = "N", default_value_t = 1)]
    pub outline_depth: usize,

    #[arg(long, value_name = "KIND")]
    pub kind: Option<String>,

    #[arg(long, value_name = "NAME")]
    pub container: Option<String>,

    #[arg(long)]
    pub topology: bool,

    #[arg(long = "section", value_enum, value_name = "SECTION")]
    pub sections: Vec<ContextSection>,

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

#[derive(Debug, Serialize, Args)]
pub struct TraceArgs {
    #[arg(value_name = "TARGET")]
    pub target: String,

    #[arg(long = "in", conflicts_with = "outgoing")]
    pub incoming: bool,

    #[arg(long = "out", conflicts_with = "incoming")]
    pub outgoing: bool,

    #[arg(long, value_name = "N", default_value_t = 1)]
    pub depth: usize,

    #[arg(long, value_name = "N", default_value_t = 100)]
    pub node_limit: usize,
}

#[derive(Debug, Serialize, Args)]
pub struct ReviewArgs {
    #[arg(long, value_name = "REF", default_value = "HEAD~1")]
    pub base: String,

    #[arg(long)]
    pub merge_base: bool,
}
