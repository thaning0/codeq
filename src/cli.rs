use std::path::PathBuf;

use clap::{ArgMatches, Args, Parser, Subcommand, ValueEnum, parser::ValueSource};
use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize, Parser, Serialize)]
#[command(
    name = "codeq",
    version,
    about = "Small, read-only code-intelligence CLI for coding agents.",
    styles = clap::builder::Styles::plain(),
    subcommand_required = true,
    arg_required_else_help = true,
    disable_help_subcommand = true,
    propagate_version = true,
    after_help = r#"Workflow:
  codeq find 'request retry policy' --limit 8
  codeq context RetryPolicy.should_retry
  codeq trace RetryPolicy.should_retry --in --depth 2
  codeq review --base HEAD~1

Use qualified symbols or exact PATH:LINE[:COLUMN] targets. Explicit paths and
qualified symbols fail closed. Use rg for known runtime/configuration strings.
Run codeq COMMAND --help for focused options and examples."#
)]
pub struct Cli {
    /// Repository/worktree path; resolves its Git root
    #[arg(long, global = true, value_name = "PATH", default_value = ".")]
    pub root: PathBuf,

    /// Emit a structured JSON response instead of plain text
    #[arg(long, global = true)]
    pub json: bool,

    /// Bound returned matches and evidence; trace defaults to 100 nodes per direction
    #[arg(
        long,
        global = true,
        value_name = "N",
        default_value_t = 20,
        allow_hyphen_values = true
    )]
    pub limit: i64,

    /// Language-server request timeout in seconds
    #[arg(
        long,
        global = true,
        value_name = "SEC",
        default_value_t = 20.0,
        allow_hyphen_values = true
    )]
    pub timeout: f64,

    /// Run in-process without reusing daemon language servers
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
        about = "Find symbols or related source from a name or short description.",
        after_help = r#"Auto mode uses symbol search for an identifier and lexical concept discovery for
multiple terms. Concept results include representative source lines and a context
command; lexical evidence is kept separate from semantic symbols. Filters apply
before the result limit.

Examples:
  codeq find RetryPolicy --kind class
  codeq find 'request retry policy' --path src --exclude-tests
  codeq find retry --mode concept --files-only
  codeq find --text 'DATABASE_URL' --glob '*.yaml'

Next: codeq context TARGET"#
    )]
    Find(FindArgs),

    #[command(
        about = "Get one target's definition, callers, callees, references, and tests.",
        after_help = r#"Targets: qualified/bare symbol, source file, unique source basename, dotted Python
module, or PATH:LINE[:COLUMN]. PATH:LINE selects its enclosing declaration; adding
a column prefers the repository definition under the cursor.

Symbol context includes bounded source, hover, callers, callees, implementations,
references, tests, and possible dynamic evidence. Repeat --section to focus it.
File context starts with an outline; expand with --container, --kind, or --topology.
--topology on a symbol adds imports/importers of its containing file.

Examples:
  codeq context RetryPolicy.should_retry --section callers
  codeq context src/retry.py:20 --lines 80
  codeq context src/retry.py --container RetryPolicy
  codeq context RetryPolicy.should_retry --topology
  codeq context RetryPolicy --lexical-references RETRY_KEY --path config

Use trace for relationships beyond the direct neighborhood."#
    )]
    Context(ContextArgs),

    #[command(
        about = "Trace a bounded incoming or outgoing call hierarchy.",
        after_help = r#"With no direction flag, trace returns both trees. Use --in for impact radius and
--out for execution flow. Depth counts call edges; depth 0 returns only the root.
Traversal is cycle-protected and restricted to repository source. The node budget
applies separately to each direction.

Examples:
  codeq trace RetryPolicy.should_retry --in --depth 2
  codeq trace fetchBars --out --depth 3 --limit 30
  codeq trace fetchBars --json"#
    )]
    Trace(TraceArgs),

    #[command(
        about = "Summarize semantic impact of changes relative to a Git base ref.",
        after_help = r#"Includes staged, unstaged, and non-ignored untracked changes; reports changed
symbols, callers, references, likely tests, and affected files. Deleted-file
evidence is base-side lexical; rename evidence uses the current path.

Examples:
  codeq review --base HEAD~1
  codeq review --base origin/main --merge-base --limit 15 --json"#
    )]
    Review(ReviewArgs),
}

impl Command {
    pub fn target(&self) -> Option<&str> {
        match self {
            Self::Context(arguments) => Some(&arguments.target),
            Self::Trace(arguments) => Some(&arguments.target),
            Self::Find(_) | Self::Review(_) => None,
        }
    }
}

impl Cli {
    pub fn validate(&mut self, matches: &ArgMatches) -> Result<(), &'static str> {
        match &mut self.command {
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
                let trace_matches = matches
                    .subcommand_matches("trace")
                    .expect("parsed trace command");
                if trace_matches.value_source("limit") == Some(ValueSource::CommandLine) {
                    if trace_matches.value_source("node_limit") == Some(ValueSource::CommandLine)
                        && self.limit != trace.node_limit
                    {
                        return Err(
                            "conflicting trace limits: --limit and --node-limit must have the same value when both are supplied",
                        );
                    }
                    trace.node_limit = self.limit;
                }
            }
            Command::Review(_) => {}
        }
        Ok(())
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
    /// Symbol name, short source-code description, or exact text
    #[arg(value_name = "QUERY")]
    pub query: String,

    /// Select symbol, lexical concept, or exact-text search explicitly
    #[arg(long, value_enum, default_value_t = FindMode::Auto)]
    pub mode: FindMode,

    /// Symbol kind, e.g. function, method, class, interface, test
    #[arg(long, value_name = "KIND", conflicts_with = "text")]
    pub kind: Option<String>,

    /// Shortcut for --mode text: search Git-visible text literally
    #[arg(long)]
    pub text: bool,

    /// Concept plain output: list ranked files without representative lines
    #[arg(long)]
    pub files_only: bool,

    /// Repository-relative path prefix; repeat for OR matching
    #[arg(long = "path", value_name = "PREFIX")]
    pub paths: Vec<String>,

    /// Shell-style path glob; repeat for OR matching
    #[arg(long = "glob", value_name = "PATTERN")]
    pub globs: Vec<String>,

    /// Exclude test candidates from results and counts
    #[arg(long)]
    pub exclude_tests: bool,
}

#[derive(Debug, Deserialize, Serialize, Args)]
pub struct ContextArgs {
    /// Qualified/bare symbol, source file, or PATH:LINE[:COLUMN]
    #[arg(value_name = "TARGET")]
    pub target: String,

    /// Add a source window at the target (1-1000 lines; 500 characters per line, 100000 total)
    #[arg(
        long,
        value_name = "N",
        value_parser = clap::value_parser!(u16).range(1..=1000),
        allow_hyphen_values = true
    )]
    pub lines: Option<u16>,

    /// File outline nesting depth; 1 shows top-level declarations
    #[arg(
        long,
        value_name = "N",
        default_value_t = 1,
        allow_hyphen_values = true
    )]
    pub outline_depth: i64,

    /// File context: show symbols of one kind across the file
    #[arg(long, value_name = "KIND")]
    pub kind: Option<String>,

    /// File context: show one class/container and its children
    #[arg(long, value_name = "NAME")]
    pub container: Option<String>,

    /// Add bounded imports/importers for the target's file
    #[arg(long)]
    pub topology: bool,

    /// Focus symbol context; repeat to combine source, callers, callees, implementations, tests, references, possible-dynamic-references, lexical-references
    #[arg(long = "section", value_name = "SECTION")]
    pub sections: Vec<String>,

    /// Attach exact-text evidence; omit TEXT to search the resolved symbol/file name
    #[arg(long, num_args = 0..=1, value_name = "TEXT", default_missing_value = "")]
    pub lexical_references: Option<String>,

    /// Scope symbols, or text evidence when --lexical-references is used; repeat for OR
    #[arg(long = "path", value_name = "PREFIX")]
    pub paths: Vec<String>,

    /// Always scope symbolic target resolution; repeat for OR matching
    #[arg(long = "symbol-path", value_name = "PREFIX")]
    pub symbol_paths: Vec<String>,

    /// Scope lexical-reference paths by glob; repeat for OR matching
    #[arg(long = "glob", value_name = "PATTERN")]
    pub globs: Vec<String>,

    /// Exclude test paths from lexical references
    #[arg(long)]
    pub exclude_tests: bool,
}

#[derive(Debug, Deserialize, Serialize, Args)]
pub struct TraceArgs {
    /// Qualified/bare symbol or PATH:LINE[:COLUMN]
    #[arg(value_name = "TARGET")]
    pub target: String,

    /// Trace incoming calls toward callers and entry points
    #[arg(long = "in", conflicts_with = "outgoing")]
    pub incoming: bool,

    /// Trace outgoing calls toward implementations
    #[arg(long = "out", conflicts_with = "incoming")]
    pub outgoing: bool,

    /// Maximum call-edge depth; 0 returns only the root
    #[arg(
        long,
        value_name = "N",
        default_value_t = 1,
        allow_hyphen_values = true
    )]
    pub depth: usize,

    /// Trace-specific alias for --limit; explicit values must agree
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
    /// Git base commit/ref for the current worktree diff
    #[arg(long, value_name = "REF", default_value = "HEAD~1")]
    pub base: String,

    /// Compare with the merge base of REF and HEAD
    #[arg(long)]
    pub merge_base: bool,
}

#[cfg(test)]
mod tests {
    use clap::CommandFactory;

    use super::Cli;

    #[test]
    fn generated_terminal_help_has_no_ansi_styles() {
        let error = Cli::command()
            .try_get_matches_from(["codeq"])
            .expect_err("a missing subcommand must display help");
        let rendered = error.render().ansi().to_string();

        assert!(!rendered.contains('\u{1b}'), "styled help: {rendered:?}");
    }
}
