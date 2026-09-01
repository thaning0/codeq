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

pub fn query_exit_code(status: Status) -> u8 {
    if status == Status::Ok { 0 } else { 1 }
}
