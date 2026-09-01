use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
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
    exercise_idle_exit(&executable, temporary.path())?;
    #[cfg(target_os = "linux")]
    exercise_abstract_daemon(&executable)?;
    exercise_cli_transport(&executable, temporary.path())?;
    exercise_stale_restart(&executable, temporary.path())?;
    exercise_workspace_bound(&executable, temporary.path())?;
    exercise_workspace_idle_eviction(&executable, temporary.path())?;
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

fn exercise_idle_exit(executable: &Path, temporary: &Path) -> Result<(), String> {
    let socket = temporary.join("idle-runtime/codeq.sock");
    let mut daemon = Command::new(executable)
        .env("CODEQ2_DAEMON_IDLE_SECONDS", "0.1")
        .env("CODEQ2_MAINTENANCE_INTERVAL_SECONDS", "0.02")
        .arg("--internal-daemon")
        .arg("--socket")
        .arg(&socket)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| error.to_string())?;
    let result = (|| {
        wait_for_socket(&socket, &mut daemon)?;
        wait_for_exit(&mut daemon, "idle timeout")?;
        if socket.exists() {
            return Err("filesystem socket survived idle exit".to_owned());
        }
        Ok(())
    })();
    cleanup_child(&mut daemon, result.is_err());
    result
}

fn exercise_cli_transport(executable: &Path, temporary: &Path) -> Result<(), String> {
    let runtime = temporary.join("client-runtime");
    let socket = runtime.join("codeq.sock");
    let root = std::env::current_dir().map_err(|error| error.to_string())?;
    let daemon_output = Command::new(executable)
        .env("CODEQ2_RUNTIME_DIR", &runtime)
        .arg("--root")
        .arg(&root)
        .arg("context")
        .arg("missing/runtime-contract.py:12")
        .arg("--json")
        .output()
        .map_err(|error| error.to_string())?;
    if daemon_output.status.code() != Some(1) {
        return Err(format!(
            "daemon CLI query exited {:?}: {}",
            daemon_output.status.code(),
            String::from_utf8_lossy(&daemon_output.stderr)
        ));
    }
    let daemon_data: Value =
        serde_json::from_slice(&daemon_output.stdout).map_err(|error| error.to_string())?;
    if daemon_data
        .pointer("/_meta/transport")
        .and_then(Value::as_str)
        != Some("daemon")
    {
        return Err(format!(
            "default CLI query did not use daemon: {daemon_data}"
        ));
    }

    let one_shot_output = Command::new(executable)
        .env("CODEQ2_RUNTIME_DIR", &runtime)
        .arg("--root")
        .arg(&root)
        .arg("context")
        .arg("missing/runtime-contract.py:12")
        .arg("--json")
        .arg("--no-daemon")
        .output()
        .map_err(|error| error.to_string())?;
    let one_shot_data: Value =
        serde_json::from_slice(&one_shot_output.stdout).map_err(|error| error.to_string())?;
    if one_shot_data
        .pointer("/_meta/transport")
        .and_then(Value::as_str)
        != Some("in_process")
    {
        return Err(format!(
            "--no-daemon query used wrong transport: {one_shot_data}"
        ));
    }

    let status = request_path(&socket, status_request())?;
    if status.pointer("/data/workspaces").and_then(Value::as_u64) != Some(1) {
        return Err(format!("daemon did not retain one worktree: {status}"));
    }

    let shutdown = request_path(&socket, json!({"command": "_shutdown"}))?;
    assert_ok_status(&shutdown, "spawned CLI daemon shutdown")?;
    wait_for_path_removal(&socket, "CLI-spawned daemon")
}

fn exercise_stale_restart(executable: &Path, temporary: &Path) -> Result<(), String> {
    let runtime = temporary.join("stale-runtime");
    fs::create_dir_all(&runtime).map_err(|error| error.to_string())?;
    fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700))
        .map_err(|error| error.to_string())?;
    let socket = runtime.join("codeq.sock");
    let listener = UnixListener::bind(&socket).map_err(|error| error.to_string())?;
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))
        .map_err(|error| error.to_string())?;
    listener
        .set_nonblocking(true)
        .map_err(|error| error.to_string())?;
    let stale_socket = socket.clone();
    let stale = thread::spawn(move || serve_stale(listener, &stale_socket));

    let root = std::env::current_dir().map_err(|error| error.to_string())?;
    let output = Command::new(executable)
        .env("CODEQ2_RUNTIME_DIR", &runtime)
        .arg("--root")
        .arg(root)
        .arg("context")
        .arg("missing/stale-restart.py:12")
        .arg("--json")
        .output()
        .map_err(|error| error.to_string())?;
    let stale_result = stale
        .join()
        .map_err(|_| "stale daemon thread panicked".to_owned())?;
    stale_result?;
    if output.status.code() != Some(1) {
        return Err(format!(
            "query after stale restart exited {:?}: {}",
            output.status.code(),
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    let data: Value = serde_json::from_slice(&output.stdout).map_err(|error| error.to_string())?;
    if data.pointer("/_meta/transport").and_then(Value::as_str) != Some("daemon") {
        return Err(format!("query did not recover onto current daemon: {data}"));
    }
    let shutdown = request_path(&socket, json!({"command": "_shutdown"}))?;
    assert_ok_status(&shutdown, "restarted daemon shutdown")?;
    wait_for_path_removal(&socket, "restarted daemon")
}

fn exercise_workspace_bound(executable: &Path, temporary: &Path) -> Result<(), String> {
    let runtime = temporary.join("bounded-runtime");
    let roots = [
        temporary.join("workspace-a"),
        temporary.join("workspace-b"),
        temporary.join("workspace-c"),
    ];
    for root in &roots {
        fs::create_dir_all(root).map_err(|error| error.to_string())?;
        let output = Command::new(executable)
            .env("CODEQ2_RUNTIME_DIR", &runtime)
            .env("CODEQ2_MAX_WORKSPACES", "2")
            .arg("--root")
            .arg(root)
            .arg("context")
            .arg("missing.py:1")
            .arg("--json")
            .output()
            .map_err(|error| error.to_string())?;
        if output.status.code() != Some(1) {
            return Err(format!("bounded workspace query failed: {output:?}"));
        }
    }
    let socket = runtime.join("codeq.sock");
    let status = request_path(&socket, status_request())?;
    let retained = status
        .pointer("/data/roots")
        .and_then(Value::as_array)
        .ok_or_else(|| format!("workspace status has no roots: {status}"))?;
    let expected: Vec<_> = roots[1..]
        .iter()
        .map(|root| Value::String(root.to_string_lossy().into_owned()))
        .collect();
    if retained != &expected {
        return Err(format!(
            "workspace LRU bound differs: expected {expected:?}, got {retained:?}"
        ));
    }
    let shutdown = request_path(&socket, json!({"command": "_shutdown"}))?;
    assert_ok_status(&shutdown, "bounded daemon shutdown")?;
    wait_for_path_removal(&socket, "bounded daemon")
}

fn exercise_workspace_idle_eviction(executable: &Path, temporary: &Path) -> Result<(), String> {
    let runtime = temporary.join("workspace-idle-runtime");
    let root = temporary.join("workspace-idle-root");
    fs::create_dir_all(&root).map_err(|error| error.to_string())?;
    let output = Command::new(executable)
        .env("CODEQ2_RUNTIME_DIR", &runtime)
        .env("CODEQ2_WORKSPACE_IDLE_SECONDS", "0.05")
        .env("CODEQ2_MAINTENANCE_INTERVAL_SECONDS", "0.02")
        .arg("--root")
        .arg(&root)
        .arg("context")
        .arg("missing.py:1")
        .arg("--json")
        .output()
        .map_err(|error| error.to_string())?;
    if output.status.code() != Some(1) {
        return Err("workspace idle fixture query failed".to_owned());
    }
    let socket = runtime.join("codeq.sock");
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        let status = request_path(&socket, status_request())?;
        if status.pointer("/data/workspaces").and_then(Value::as_u64) == Some(0) {
            break;
        }
        if Instant::now() >= deadline {
            return Err(format!("idle workspace was not evicted: {status}"));
        }
        thread::sleep(Duration::from_millis(20));
    }
    let shutdown = request_path(&socket, json!({"command": "_shutdown"}))?;
    assert_ok_status(&shutdown, "workspace-idle daemon shutdown")?;
    wait_for_path_removal(&socket, "workspace-idle daemon")
}

fn serve_stale(listener: UnixListener, socket: &Path) -> Result<(), String> {
    for response in [
        json!({
            "ok": false,
            "error_code": "version_mismatch",
            "error": "stale daemon",
            "server_version": "0.0.0-stale",
            "protocol_version": 1,
        }),
        json!({
            "ok": true,
            "data": {"status": "ok"},
            "server_version": "0.0.0-stale",
            "protocol_version": 1,
        }),
    ] {
        let deadline = Instant::now() + Duration::from_secs(3);
        let mut stream = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(error)
                    if error.kind() == std::io::ErrorKind::WouldBlock
                        && Instant::now() < deadline =>
                {
                    thread::sleep(Duration::from_millis(20));
                }
                Err(error) => return Err(format!("stale daemon accept failed: {error}")),
            }
        };
        let mut request = String::new();
        BufReader::new(&stream)
            .read_line(&mut request)
            .map_err(|error| error.to_string())?;
        serde_json::to_writer(&mut stream, &response).map_err(|error| error.to_string())?;
        stream.write_all(b"\n").map_err(|error| error.to_string())?;
        stream.flush().map_err(|error| error.to_string())?;
    }
    drop(listener);
    fs::remove_file(socket).map_err(|error| error.to_string())
}

#[cfg(target_os = "linux")]
fn exercise_abstract_daemon(executable: &Path) -> Result<(), String> {
    let name = format!("codeq-2-test-{}", std::process::id());
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

fn wait_for_path_removal(socket: &Path, context: &str) -> Result<(), String> {
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        if !socket.exists() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(20));
    }
    Err(format!("{context} socket survived shutdown"))
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
