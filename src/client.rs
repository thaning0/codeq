use std::env;
use std::fs::{self, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::net::{SocketAddr, UnixStream};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(target_os = "linux")]
use std::os::linux::net::SocketAddrExt;

use serde_json::{Value, json};

use crate::cli::Cli;
use crate::daemon::{self, Endpoint};
use crate::runtime::DAEMON_PROTOCOL_VERSION;

const CONNECT_RETRY: Duration = Duration::from_millis(50);
const CONNECT_MINIMUM: Duration = Duration::from_secs(3);
const CONNECT_MAXIMUM: Duration = Duration::from_secs(10);

pub fn request(cli: &Cli, root: &Path) -> Result<Option<Value>, String> {
    request_once(cli, root, true)
}

fn request_once(cli: &Cli, root: &Path, allow_restart: bool) -> Result<Option<Value>, String> {
    let endpoint = daemon::default_endpoint()?;
    let timeout = request_timeout(cli.timeout);
    let mut stream = match connect_or_spawn(&endpoint, timeout)? {
        Some(stream) => stream,
        None => return Ok(None),
    };
    stream
        .set_read_timeout(Some(timeout))
        .map_err(|error| error.to_string())?;
    stream
        .set_write_timeout(Some(timeout))
        .map_err(|error| error.to_string())?;

    let mut request = serde_json::to_value(cli).map_err(|error| error.to_string())?;
    request["root"] = Value::String(root.to_string_lossy().into_owned());
    let response = exchange(
        &mut stream,
        json!({
            "command": "_query",
            "request": request,
            "_client_version": env!("CARGO_PKG_VERSION"),
            "_protocol_version": DAEMON_PROTOCOL_VERSION,
        }),
    )?;
    let mismatch = response.get("error_code").and_then(Value::as_str) == Some("version_mismatch")
        || response.get("server_version").and_then(Value::as_str)
            != Some(env!("CARGO_PKG_VERSION"))
        || response.get("protocol_version").and_then(Value::as_u64)
            != Some(u64::from(DAEMON_PROTOCOL_VERSION));
    if mismatch && allow_restart {
        restart(&endpoint)?;
        return request_once(cli, root, false);
    }
    if mismatch {
        return Err("codeq daemon version mismatch after restart".to_owned());
    }
    if response.get("ok").and_then(Value::as_bool) != Some(true) {
        return Err(response
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("unknown daemon error")
            .to_owned());
    }
    response
        .get("data")
        .cloned()
        .map(Some)
        .ok_or_else(|| "codeq daemon response has no data".to_owned())
}

fn connect_or_spawn(endpoint: &Endpoint, timeout: Duration) -> Result<Option<UnixStream>, String> {
    match connect(endpoint) {
        Ok(stream) => return Ok(Some(stream)),
        Err(error) if permanent_socket_error(&error) => return Ok(None),
        Err(_) => {}
    }
    if let Err(error) = spawn(endpoint) {
        if permanent_socket_error(&error) {
            return Ok(None);
        }
        return Err(format!("codeq daemon cannot be started: {error}"));
    }
    let wait = timeout.clamp(CONNECT_MINIMUM, CONNECT_MAXIMUM);
    let deadline = Instant::now() + wait;
    let mut last_error = None;
    while Instant::now() < deadline {
        thread::sleep(CONNECT_RETRY);
        match connect(endpoint) {
            Ok(stream) => return Ok(Some(stream)),
            Err(error) if permanent_socket_error(&error) => return Ok(None),
            Err(error) => last_error = Some(error),
        }
    }
    Err(format!(
        "codeq daemon failed to start: {}",
        last_error
            .map(|error| error.to_string())
            .unwrap_or_else(|| "socket remained unavailable".to_owned())
    ))
}

fn connect(endpoint: &Endpoint) -> io::Result<UnixStream> {
    let stream = match endpoint {
        Endpoint::Filesystem(path) => UnixStream::connect(path)?,
        Endpoint::Abstract(name) => {
            #[cfg(target_os = "linux")]
            {
                let address = SocketAddr::from_abstract_name(name.as_bytes())?;
                UnixStream::connect_addr(&address)?
            }
            #[cfg(not(target_os = "linux"))]
            {
                let _ = name;
                return Err(io::Error::new(
                    io::ErrorKind::Unsupported,
                    "abstract Unix sockets require Linux",
                ));
            }
        }
    };
    if !daemon::trusted_peer(&stream) {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "codeq daemon peer is not trusted",
        ));
    }
    Ok(stream)
}

fn spawn(endpoint: &Endpoint) -> io::Result<()> {
    if let Some(path) = endpoint.socket_path()
        && let Some(parent) = path.parent()
    {
        fs::create_dir_all(parent)?;
    }
    let executable = env::current_exe()?;
    let mut command = Command::new(executable);
    command.arg("--internal-daemon");
    match endpoint {
        Endpoint::Abstract(name) => {
            command.arg("--abstract").arg(name);
        }
        Endpoint::Filesystem(path) => {
            command.arg("--socket").arg(path);
        }
    }
    command.stdin(Stdio::null()).process_group(0);
    if let Some(log_path) = env::var_os("CODEQ2_DAEMON_LOG").filter(|value| !value.is_empty()) {
        let log_path = Path::new(&log_path);
        if let Some(parent) = log_path.parent() {
            fs::create_dir_all(parent)?;
        }
        let output = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(log_path)?;
        command.stdout(Stdio::from(output.try_clone()?));
        command.stderr(Stdio::from(output));
    } else {
        command.stdout(Stdio::null()).stderr(Stdio::null());
    }
    command.spawn()?;
    Ok(())
}

fn restart(endpoint: &Endpoint) -> Result<(), String> {
    if let Ok(mut stream) = connect(endpoint) {
        let _ = exchange(&mut stream, json!({"command": "_shutdown"}));
    }
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        if connect(endpoint).is_err() {
            if let Some(path) = endpoint.socket_path() {
                let _ = fs::remove_file(path);
            }
            return Ok(());
        }
        thread::sleep(CONNECT_RETRY);
    }
    Err("stale codeq daemon did not exit after version mismatch".to_owned())
}

fn exchange(stream: &mut UnixStream, payload: Value) -> Result<Value, String> {
    serde_json::to_writer(&mut *stream, &payload).map_err(|error| error.to_string())?;
    stream.write_all(b"\n").map_err(|error| error.to_string())?;
    stream.flush().map_err(|error| error.to_string())?;
    let mut line = String::new();
    BufReader::new(stream)
        .read_line(&mut line)
        .map_err(|error| error.to_string())?;
    if line.is_empty() {
        return Err("codeq daemon closed connection without a response".to_owned());
    }
    serde_json::from_str(&line).map_err(|error| format!("invalid daemon response: {error}"))
}

fn permanent_socket_error(error: &io::Error) -> bool {
    error.kind() == io::ErrorKind::PermissionDenied
        || matches!(
            error.raw_os_error(),
            Some(code) if code == nix::errno::Errno::EACCES as i32
                || code == nix::errno::Errno::EPERM as i32
        )
}

fn request_timeout(seconds: f64) -> Duration {
    let seconds = if seconds.is_nan() {
        1.0
    } else {
        seconds.clamp(1.0, 3600.0)
    };
    Duration::from_secs_f64(seconds)
}

#[cfg(test)]
mod tests {
    use std::io;

    use super::{permanent_socket_error, request_timeout};

    #[test]
    fn permission_errors_select_one_shot_fallback() {
        assert!(permanent_socket_error(&io::Error::from_raw_os_error(
            nix::errno::Errno::EPERM as i32
        )));
        assert!(!permanent_socket_error(&io::Error::from_raw_os_error(
            nix::errno::Errno::ECONNREFUSED as i32
        )));
    }

    #[test]
    fn daemon_timeout_is_bounded() {
        assert_eq!(request_timeout(-1.0).as_secs(), 1);
        assert_eq!(request_timeout(f64::NAN).as_secs(), 1);
        assert_eq!(request_timeout(f64::INFINITY).as_secs(), 3600);
    }
}
