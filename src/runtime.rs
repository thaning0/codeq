use serde::Serialize;

pub const DAEMON_PROTOCOL_VERSION: u16 = 1;
pub const DEVELOPMENT_NAMESPACE: &str = "codeq-2.0-rust-dev";
pub const DEVELOPMENT_RUNTIME_ENV: &str = "CODEQ2_RUNTIME_DIR";

#[derive(Debug, Serialize)]
pub struct RuntimeIdentity {
    pub namespace: &'static str,
    pub runtime_env: &'static str,
    pub daemon_protocol_version: u16,
}

pub const fn identity() -> RuntimeIdentity {
    RuntimeIdentity {
        namespace: DEVELOPMENT_NAMESPACE,
        runtime_env: DEVELOPMENT_RUNTIME_ENV,
        daemon_protocol_version: DAEMON_PROTOCOL_VERSION,
    }
}
