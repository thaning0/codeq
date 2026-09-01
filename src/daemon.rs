use std::env;
use std::ffi::OsString;
use std::fs;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::net::{SocketAddr, UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(target_os = "linux")]
use std::os::linux::net::SocketAddrExt;

use nix::sys::socket::{getsockopt, sockopt};
use nix::unistd::Uid;
use serde_json::{Value, json};
use signal_hook::consts::{SIGINT, SIGTERM};

use crate::runtime::{DAEMON_PROTOCOL_VERSION, DEVELOPMENT_NAMESPACE, DEVELOPMENT_RUNTIME_ENV};
use crate::{cli::Cli, service};

const MAX_REQUEST_BYTES: u64 = 4 * 1024 * 1024;
const SOCKET_MODE: u32 = 0o600;
const RUNTIME_MODE: u32 = 0o700;
const ACCEPT_POLL: Duration = Duration::from_millis(20);
const DEFAULT_DAEMON_IDLE: Duration = Duration::from_secs(900);
const DEFAULT_MAINTENANCE_INTERVAL: Duration = Duration::from_secs(5);
const DEFAULT_WORKSPACE_IDLE: Duration = Duration::from_secs(300);
const DEFAULT_MAX_WORKSPACES: usize = 4;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Endpoint {
    Abstract(String),
    Filesystem(PathBuf),
}

impl Endpoint {
    pub(crate) fn socket_path(&self) -> Option<&Path> {
        match self {
            Self::Abstract(_) => None,
            Self::Filesystem(path) => Some(path),
        }
    }
}

pub fn internal_main(arguments: &[OsString]) -> Result<(), String> {
    let endpoint = match arguments {
        [option, value] if option == "--abstract" => Endpoint::Abstract(
            value
                .to_str()
                .ok_or("abstract socket name must be UTF-8")?
                .to_owned(),
        ),
        [option, value] if option == "--socket" => Endpoint::Filesystem(PathBuf::from(value)),
        [] => default_endpoint()?,
        _ => return Err("expected --abstract NAME or --socket PATH".to_owned()),
    };
    run(endpoint).map_err(|error| error.to_string())
}

pub fn default_endpoint() -> Result<Endpoint, String> {
    if let Some(explicit) = env::var_os(DEVELOPMENT_RUNTIME_ENV).filter(|value| !value.is_empty()) {
        let runtime = prepare_runtime_dir(Path::new(&explicit)).map_err(|error| {
            format!(
                "{DEVELOPMENT_RUNTIME_ENV} is not usable: {}: {error}",
                Path::new(&explicit).display()
            )
        })?;
        return Ok(Endpoint::Filesystem(runtime.join("codeq.sock")));
    }
    #[cfg(target_os = "linux")]
    {
        return Ok(Endpoint::Abstract(abstract_name(Uid::current().as_raw())));
    }
    #[allow(unreachable_code)]
    Ok(Endpoint::Filesystem(
        default_runtime_dir()?.join("codeq.sock"),
    ))
}

pub fn run(endpoint: Endpoint) -> io::Result<()> {
    let listener = bind(&endpoint)?;
    let Some(listener) = listener else {
        return Ok(());
    };
    let runtime_dir = runtime_directory(&endpoint)?;
    listener.set_nonblocking(true)?;
    let stopping = Arc::new(AtomicBool::new(false));
    signal_hook::flag::register(SIGTERM, Arc::clone(&stopping))?;
    signal_hook::flag::register(SIGINT, Arc::clone(&stopping))?;
    let idle_timeout = daemon_idle_timeout();
    let maintenance_interval = environment_duration(
        "CODEQ2_MAINTENANCE_INTERVAL_SECONDS",
        DEFAULT_MAINTENANCE_INTERVAL,
    );
    let workspace_idle =
        environment_duration("CODEQ2_WORKSPACE_IDLE_SECONDS", DEFAULT_WORKSPACE_IDLE);
    let service = Arc::new(service::DaemonService::new(
        environment_usize("CODEQ2_MAX_WORKSPACES", DEFAULT_MAX_WORKSPACES),
        runtime_dir.join("workspaces"),
    ));
    let mut next_maintenance = Instant::now() + maintenance_interval;
    while !stopping.load(Ordering::Acquire) {
        match listener.accept() {
            Ok((stream, _)) => {
                if !trusted_peer(&stream) {
                    continue;
                }
                let connection_stop = Arc::clone(&stopping);
                let connection_service = Arc::clone(&service);
                thread::spawn(move || {
                    let _ = serve_connection(stream, &connection_stop, &connection_service);
                });
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                let now = Instant::now();
                if now >= next_maintenance {
                    service.evict_idle(workspace_idle);
                    if service.workspace_count() == 0 && service.idle_duration() >= idle_timeout {
                        break;
                    }
                    next_maintenance = now + maintenance_interval;
                }
                thread::sleep(ACCEPT_POLL);
            }
            Err(error) => {
                cleanup(&endpoint);
                return Err(error);
            }
        }
    }
    cleanup(&endpoint);
    Ok(())
}

fn bind(endpoint: &Endpoint) -> io::Result<Option<UnixListener>> {
    match endpoint {
        Endpoint::Abstract(name) => {
            #[cfg(target_os = "linux")]
            {
                let address = SocketAddr::from_abstract_name(name.as_bytes())?;
                match UnixListener::bind_addr(&address) {
                    Ok(listener) => Ok(Some(listener)),
                    Err(error)
                        if error.raw_os_error() == Some(nix::errno::Errno::EADDRINUSE as i32) =>
                    {
                        Ok(None)
                    }
                    Err(error) => Err(error),
                }
            }
            #[cfg(not(target_os = "linux"))]
            {
                let _ = name;
                Err(io::Error::new(
                    io::ErrorKind::Unsupported,
                    "abstract Unix sockets require Linux",
                ))
            }
        }
        Endpoint::Filesystem(path) => {
            let parent = path.parent().ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, "socket path has no parent")
            })?;
            prepare_runtime_dir(parent)?;
            if path.exists() {
                if UnixStream::connect(path).is_ok() {
                    return Ok(None);
                }
                fs::remove_file(path)?;
            }
            let listener = UnixListener::bind(path)?;
            fs::set_permissions(path, fs::Permissions::from_mode(SOCKET_MODE))?;
            Ok(Some(listener))
        }
    }
}

fn serve_connection(
    mut stream: UnixStream,
    stopping: &AtomicBool,
    service: &service::DaemonService,
) -> io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(30)))?;
    stream.set_write_timeout(Some(Duration::from_secs(30)))?;
    let mut line = String::new();
    BufReader::new(&stream)
        .take(MAX_REQUEST_BYTES + 1)
        .read_line(&mut line)?;
    if line.is_empty() {
        return Ok(());
    }
    let (response, shutdown_requested) = match serde_json::from_str::<Value>(&line) {
        Ok(request) => response(&request, service),
        Err(error) => (
            wire_error(None, format!("invalid JSON request: {error}")),
            false,
        ),
    };
    serde_json::to_writer(&mut stream, &response)?;
    stream.write_all(b"\n")?;
    stream.flush()?;
    if shutdown_requested {
        stopping.store(true, Ordering::Release);
    }
    Ok(())
}

fn response(request: &Value, service: &service::DaemonService) -> (Value, bool) {
    let command = request.get("command").and_then(Value::as_str);
    if command == Some("_shutdown") {
        return (wire_ok(json!({"status": "ok"})), true);
    }
    let client_version = request.get("_client_version").and_then(Value::as_str);
    let protocol = request.get("_protocol_version").and_then(Value::as_u64);
    if client_version != Some(env!("CARGO_PKG_VERSION"))
        || protocol != Some(u64::from(DAEMON_PROTOCOL_VERSION))
    {
        return (
            wire_error(
                Some("version_mismatch"),
                "codeq client/daemon version mismatch".to_owned(),
            ),
            false,
        );
    }
    if command == Some("_status") {
        return (wire_ok(service.status()), false);
    }
    if command == Some("_query") {
        let Some(query) = request.get("request") else {
            return (
                wire_error(None, "query request is missing".to_owned()),
                false,
            );
        };
        let cli: Cli = match serde_json::from_value(query.clone()) {
            Ok(cli) => cli,
            Err(error) => {
                return (
                    wire_error(None, format!("invalid query request: {error}")),
                    false,
                );
            }
        };
        return match service.query(&cli) {
            Ok(data) => (wire_ok(data), false),
            Err(error) => (wire_error(None, error), false),
        };
    }
    (wire_error(None, "unknown daemon command".to_owned()), false)
}

fn wire_ok(data: Value) -> Value {
    json!({
        "ok": true,
        "data": data,
        "server_version": env!("CARGO_PKG_VERSION"),
        "protocol_version": DAEMON_PROTOCOL_VERSION,
    })
}

fn wire_error(error_code: Option<&str>, error: String) -> Value {
    let mut response = json!({
        "ok": false,
        "error": error,
        "server_version": env!("CARGO_PKG_VERSION"),
        "protocol_version": DAEMON_PROTOCOL_VERSION,
    });
    if let Some(error_code) = error_code {
        response["error_code"] = Value::String(error_code.to_owned());
    }
    response
}

pub(crate) fn trusted_peer(stream: &UnixStream) -> bool {
    getsockopt(stream, sockopt::PeerCredentials)
        .map(|credentials| credentials.uid() == Uid::current().as_raw())
        .unwrap_or(false)
}

fn prepare_runtime_dir(path: &Path) -> io::Result<PathBuf> {
    fs::create_dir_all(path)?;
    let metadata = fs::metadata(path)?;
    if metadata.uid() != Uid::current().as_raw() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!(
                "runtime directory is not owned by current user: {}",
                path.display()
            ),
        ));
    }
    fs::set_permissions(path, fs::Permissions::from_mode(RUNTIME_MODE))?;
    Ok(path.to_owned())
}

fn default_runtime_dir() -> Result<PathBuf, String> {
    if let Some(xdg) = env::var_os("XDG_RUNTIME_DIR").filter(|value| !value.is_empty()) {
        let candidate = PathBuf::from(xdg).join(DEVELOPMENT_NAMESPACE);
        if let Ok(runtime) = prepare_runtime_dir(&candidate) {
            return Ok(runtime);
        }
    }
    let fallback = PathBuf::from(format!(
        "/tmp/{DEVELOPMENT_NAMESPACE}-{}",
        Uid::current().as_raw()
    ));
    prepare_runtime_dir(&fallback)
        .map_err(|error| format!("no usable Rust codeq runtime directory: {error}"))
}

fn runtime_directory(endpoint: &Endpoint) -> io::Result<PathBuf> {
    match endpoint {
        Endpoint::Filesystem(path) => path
            .parent()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "socket path has no parent"))
            .and_then(prepare_runtime_dir),
        Endpoint::Abstract(_) => default_runtime_dir().map_err(io::Error::other),
    }
}

fn cleanup(endpoint: &Endpoint) {
    if let Some(path) = endpoint.socket_path() {
        let _ = fs::remove_file(path);
    }
}

fn abstract_name(uid: u32) -> String {
    format!("{DEVELOPMENT_NAMESPACE}-{uid}-p{DAEMON_PROTOCOL_VERSION}")
}

fn daemon_idle_timeout() -> Duration {
    environment_duration("CODEQ2_DAEMON_IDLE_SECONDS", DEFAULT_DAEMON_IDLE)
}

fn environment_duration(name: &str, default: Duration) -> Duration {
    env::var(name)
        .ok()
        .and_then(|value| value.parse::<f64>().ok())
        .filter(|value| value.is_finite() && *value >= 0.0)
        .map(Duration::from_secs_f64)
        .unwrap_or(default)
}

fn environment_usize(name: &str, default: usize) -> usize {
    env::var(name)
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(default)
}

#[cfg(test)]
mod tests {
    use super::abstract_name;

    #[test]
    fn development_abstract_namespace_cannot_collide_with_rc13() {
        let development = abstract_name(1000);
        assert!(development.starts_with("codeq-2.0-rust-dev-"));
        assert_ne!(development, "codeq-1000-p1");
        assert!(development.len() < 108);
    }
}
