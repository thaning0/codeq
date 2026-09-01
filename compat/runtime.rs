use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(target_os = "linux")]
use std::os::linux::net::SocketAddrExt;
#[cfg(target_os = "linux")]
use std::os::unix::net::SocketAddr;

use clap::Parser;
use nix::sys::signal::{Signal, kill};
use nix::unistd::Pid;
use serde_json::{Value, json};
use tempfile::TempDir;

#[derive(Parser)]
struct Options {
    #[arg(long, value_name = "PATH")]
    codeq: Option<PathBuf>,
}

fn main() {
    if let Err(error) = run(Options::parse()) {
        eprintln!("runtime contract failed: {error}");
        std::process::exit(1);
    }
    println!("codeq runtime contract: UDS handshake, isolation, signals, and cleanup passed");
}

fn run(options: Options) -> Result<(), String> {
    let executable = options
        .codeq
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_codeq")));
    let temporary = TempDir::new().map_err(|error| error.to_string())?;
    let runtime = temporary.path().join("runtime");
    let socket = runtime.join("codeq.sock");

    let mut daemon = spawn_filesystem(&executable, &socket)?;
    let result = exercise_filesystem_daemon(&socket, &runtime, &mut daemon);
    cleanup_child(&mut daemon, result.is_err());
    result?;

    exercise_signal_cleanup(&executable, &socket)?;
    #[cfg(target_os = "linux")]
    exercise_abstract_daemon(&executable)?;
    Ok(())
}

fn spawn_filesystem(executable: &Path, socket: &Path) -> Result<Child, String> {
    Command::new(executable)
        .arg("--internal-daemon")
        .arg("--socket")
        .arg(socket)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("cannot spawn {}: {error}", executable.display()))
}

fn exercise_filesystem_daemon(
    socket: &Path,
    runtime: &Path,
    daemon: &mut Child,
) -> Result<(), String> {
    wait_for_socket(socket, daemon)?;
    let runtime_mode = fs::metadata(runtime)
        .map_err(|error| error.to_string())?
        .permissions()
        .mode()
        & 0o777;
    let socket_mode = fs::metadata(socket)
        .map_err(|error| error.to_string())?
        .permissions()
        .mode()
        & 0o777;
    if runtime_mode != 0o700 || socket_mode != 0o600 {
        return Err(format!(
            "private runtime modes differ: runtime={runtime_mode:o}, socket={socket_mode:o}"
        ));
    }

    let mismatch = request_path(
        socket,
        json!({
            "command": "_status",
            "_client_version": "stale-version",
            "_protocol_version": 1,
        }),
    )?;
    if mismatch.get("error_code").and_then(Value::as_str) != Some("version_mismatch") {
        return Err(format!(
            "expected version mismatch response, got {mismatch}"
        ));
    }

    let status = request_path(socket, status_request())?;
    assert_ok_status(&status, "filesystem daemon")?;
    let shutdown = request_path(socket, json!({"command": "_shutdown"}))?;
    assert_ok_status(&shutdown, "shutdown acknowledgement")?;
    wait_for_exit(daemon, "shutdown")?;
    if socket.exists() {
        return Err("filesystem socket survived normal daemon shutdown".to_owned());
    }
    Ok(())
}

fn exercise_signal_cleanup(executable: &Path, socket: &Path) -> Result<(), String> {
    let mut daemon = spawn_filesystem(executable, socket)?;
    let result = (|| {
        wait_for_socket(socket, &mut daemon)?;
        kill(Pid::from_raw(daemon.id() as i32), Signal::SIGTERM)
            .map_err(|error| error.to_string())?;
        wait_for_exit(&mut daemon, "SIGTERM")?;
        if socket.exists() {
            return Err("filesystem socket survived SIGTERM".to_owned());
        }
        Ok(())
    })();
    cleanup_child(&mut daemon, result.is_err());
    result
}

#[cfg(target_os = "linux")]
fn exercise_abstract_daemon(executable: &Path) -> Result<(), String> {
    let name = format!("codeq-2.0-rust-dev-test-{}", std::process::id());
    let address =
        SocketAddr::from_abstract_name(name.as_bytes()).map_err(|error| error.to_string())?;
    let mut daemon = Command::new(executable)
        .arg("--internal-daemon")
        .arg("--abstract")
        .arg(&name)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| error.to_string())?;
    let result = (|| {
        let deadline = Instant::now() + Duration::from_secs(3);
        let stream = loop {
            match UnixStream::connect_addr(&address) {
                Ok(stream) => break stream,
                Err(_) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
                Err(error) => return Err(format!("abstract daemon did not become ready: {error}")),
            }
        };
        let status = request_stream(stream, status_request())?;
        assert_ok_status(&status, "abstract daemon")?;
        let stream = UnixStream::connect_addr(&address).map_err(|error| error.to_string())?;
        let shutdown = request_stream(stream, json!({"command": "_shutdown"}))?;
        assert_ok_status(&shutdown, "abstract shutdown")?;
        wait_for_exit(&mut daemon, "abstract shutdown")
    })();
    cleanup_child(&mut daemon, result.is_err());
    result
}

fn wait_for_socket(socket: &Path, daemon: &mut Child) -> Result<(), String> {
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        if socket.exists() && UnixStream::connect(socket).is_ok() {
            return Ok(());
        }
        if let Some(status) = daemon.try_wait().map_err(|error| error.to_string())? {
            let stderr = daemon
                .stderr
                .take()
                .map(|stream| std::io::read_to_string(stream).unwrap_or_default())
                .unwrap_or_default();
            return Err(format!("daemon exited early with {status}: {stderr}"));
        }
        thread::sleep(Duration::from_millis(20));
    }
    Err("daemon socket did not become ready".to_owned())
}

fn wait_for_exit(daemon: &mut Child, action: &str) -> Result<(), String> {
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        if daemon
            .try_wait()
            .map_err(|error| error.to_string())?
            .is_some()
        {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(20));
    }
    Err(format!("daemon did not exit after {action}"))
}

fn request_path(socket: &Path, payload: Value) -> Result<Value, String> {
    let stream = UnixStream::connect(socket).map_err(|error| error.to_string())?;
    request_stream(stream, payload)
}

fn request_stream(mut stream: UnixStream, payload: Value) -> Result<Value, String> {
    serde_json::to_writer(&mut stream, &payload).map_err(|error| error.to_string())?;
    stream.write_all(b"\n").map_err(|error| error.to_string())?;
    stream.flush().map_err(|error| error.to_string())?;
    let mut line = String::new();
    BufReader::new(stream)
        .read_line(&mut line)
        .map_err(|error| error.to_string())?;
    serde_json::from_str(&line).map_err(|error| format!("invalid daemon response: {error}"))
}

fn status_request() -> Value {
    json!({
        "command": "_status",
        "_client_version": env!("CARGO_PKG_VERSION"),
        "_protocol_version": 1,
    })
}

fn assert_ok_status(response: &Value, context: &str) -> Result<(), String> {
    if response.pointer("/data/status").and_then(Value::as_str) == Some("ok") {
        Ok(())
    } else {
        Err(format!("expected {context} status, got {response}"))
    }
}

fn cleanup_child(daemon: &mut Child, force: bool) {
    if force {
        let _ = daemon.kill();
    }
    let _ = daemon.wait();
}
