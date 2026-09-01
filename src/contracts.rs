use std::path::{Path, PathBuf};

use serde::Serialize;

pub const SCHEMA_VERSION: u8 = 1;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
#[expect(
    dead_code,
    reason = "the complete frozen status vocabulary precedes its semantic producers"
)]
pub enum Status {
    Ok,
    NotFound,
    Ambiguous,
    UnsupportedLanguage,
    UnsupportedTarget,
    UnsupportedCapability,
    InvalidQuery,
    Error,
    Unavailable,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
#[expect(
    dead_code,
    reason = "the complete frozen evidence vocabulary precedes its semantic producers"
)]
pub enum Evidence {
    Semantic,
    Lexical,
    PossibleDynamic,
    BaseSideLexical,
    CurrentSemantic,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
#[expect(
    dead_code,
    reason = "the complete frozen test-evidence vocabulary precedes its semantic producers"
)]
pub enum TestEvidence {
    DirectSemanticReference,
    SemanticCaller,
    ModuleImport,
    ExactLexicalReference,
}

#[derive(Debug, Serialize)]
pub struct CacheDiagnostics {
    pub document_symbols_hit: u64,
    pub document_symbols_miss: u64,
    pub document_symbols_waited: u64,
    pub document_symbols_evicted: u64,
    pub document_symbol_entries: u64,
}

#[derive(Debug, Serialize)]
pub struct PhaseDurations {
    pub resolution: f64,
    pub prewarm: f64,
    pub semantic_neighborhood: f64,
}

#[derive(Debug, Serialize)]
pub struct QueryMeta {
    pub root: PathBuf,
    pub duration_ms: f64,
    pub queue_ms: f64,
    pub execution_ms: f64,
    pub lsp_sessions_before: Vec<serde_json::Value>,
    pub lsp_sessions: Vec<serde_json::Value>,
    pub lsp_started: bool,
    pub lsp_request_count: u64,
    pub prewarm_files: u64,
    pub prewarm_probes: u64,
    pub prewarm_early_stops: u64,
    pub cache: CacheDiagnostics,
    pub phase_ms: PhaseDurations,
    pub transport: &'static str,
}

impl QueryMeta {
    pub fn empty(root: &Path, duration_ms: f64) -> Self {
        Self {
            root: root.to_owned(),
            duration_ms,
            queue_ms: 0.0,
            execution_ms: duration_ms,
            lsp_sessions_before: Vec::new(),
            lsp_sessions: Vec::new(),
            lsp_started: false,
            lsp_request_count: 0,
            prewarm_files: 0,
            prewarm_probes: 0,
            prewarm_early_stops: 0,
            cache: CacheDiagnostics {
                document_symbols_hit: 0,
                document_symbols_miss: 0,
                document_symbols_waited: 0,
                document_symbols_evicted: 0,
                document_symbol_entries: 0,
            },
            phase_ms: PhaseDurations {
                resolution: duration_ms,
                prewarm: 0.0,
                semantic_neighborhood: 0.0,
            },
            transport: "in_process",
        }
    }
}

#[derive(Debug, Serialize)]
pub struct TargetFailureResponse<'a> {
    pub status: Status,
    pub target: &'a str,
    pub path: &'a Path,
    pub reason: String,
    #[serde(rename = "_meta")]
    pub meta: QueryMeta,
    pub schema_version: u8,
}

pub fn query_exit_code(status: Status) -> u8 {
    if status == Status::Ok { 0 } else { 1 }
}
