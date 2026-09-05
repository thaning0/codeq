use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::thread;
use std::time::{Duration, Instant};

use clap::Parser;
use serde_json::{Value, json};
use tempfile::TempDir;

type Result<T> = std::result::Result<T, String>;

#[derive(Parser)]
struct Options {
    #[arg(long, value_name = "PATH")]
    codeq: Option<PathBuf>,
}

fn main() -> ExitCode {
    match run(Options::parse()) {
        Ok(()) => {
            println!("codeq workspace contract: FTS refresh and worktree isolation passed");
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("workspace contract failed: {error}");
            ExitCode::from(1)
        }
    }
}

fn run(options: Options) -> Result<()> {
    let executable = options
        .codeq
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_codeq")));
    let temporary = TempDir::new().map_err(|error| error.to_string())?;
    let runtime = temporary.path().join("runtime");
    let first = temporary.path().join("first");
    let second = temporary.path().join("second");
    prepare_repository(&first, "alpha cache marker")?;
    prepare_repository(&second, "isolated other phrase")?;

    let initial = concept(&executable, &runtime, &first, "alpha cache")?;
    assert_bool(&initial, "/index/refreshed", true)?;
    assert_u64(&initial, "/result_count", 1)?;
    let reused = concept(&executable, &runtime, &first, "alpha cache")?;
    assert_bool(&reused, "/index/refreshed", false)?;
    let rust = concept(&executable, &runtime, &first, "rustonlytoken")?;
    assert_u64(&rust, "/result_count", 1)?;

    let isolated = concept(&executable, &runtime, &second, "alpha cache")?;
    assert_bool(&isolated, "/index/refreshed", true)?;
    assert_u64(&isolated, "/result_count", 0)?;

    fs::write(
        first.join("source.py"),
        "def refreshed() -> str:\n    return \"refresh changed token\"\n",
    )
    .map_err(|error| error.to_string())?;
    let refreshed = concept(&executable, &runtime, &first, "refresh token")?;
    assert_bool(&refreshed, "/index/refreshed", true)?;
    assert_u64(&refreshed, "/result_count", 1)?;

    shutdown(&runtime.join("codeq.sock"))?;
    wait_for_absence(&runtime.join("codeq.sock"))
}

fn prepare_repository(path: &Path, marker: &str) -> Result<()> {
    fs::create_dir_all(path).map_err(|error| error.to_string())?;
    fs::write(
        path.join("source.py"),
        format!("def marker() -> str:\n    return {marker:?}\n"),
    )
    .map_err(|error| error.to_string())?;
    fs::write(
        path.join("source.rs"),
        "pub const RUST_MARKER: &str = \"rustonlytoken\";\n",
    )
    .map_err(|error| error.to_string())?;
    git(path, &["init", "--quiet"])?;
    git(path, &["config", "user.name", "CodeQ workspace contract"])?;
    git(
        path,
        &["config", "user.email", "workspace-contract@example.invalid"],
    )?;
    git(path, &["add", "."])?;
    git(path, &["commit", "--quiet", "-m", "baseline"])
}

fn git(path: &Path, arguments: &[&str]) -> Result<()> {
    let output = Command::new("git")
        .args(arguments)
        .current_dir(path)
        .output()
        .map_err(|error| error.to_string())?;
    if output.status.success() {
        Ok(())
    } else {
        Err(String::from_utf8_lossy(&output.stderr).trim().to_owned())
    }
}

fn concept(executable: &Path, runtime: &Path, root: &Path, query: &str) -> Result<Value> {
    let output = Command::new(executable)
        .args(["--root"])
        .arg(root)
        .args(["find", query, "--mode", "concept", "--json"])
        .env("CODEQ2_RUNTIME_DIR", runtime)
        .env("CODEQ2_DAEMON_IDLE_SECONDS", "60")
        .output()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(format!(
            "concept query failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    serde_json::from_slice(&output.stdout).map_err(|error| error.to_string())
}

fn assert_bool(value: &Value, pointer: &str, expected: bool) -> Result<()> {
    let actual = value.pointer(pointer).and_then(Value::as_bool);
    if actual == Some(expected) {
        Ok(())
    } else {
        Err(format!("{pointer} was {actual:?}, expected {expected}"))
    }
}

fn assert_u64(value: &Value, pointer: &str, expected: u64) -> Result<()> {
    let actual = value.pointer(pointer).and_then(Value::as_u64);
    if actual == Some(expected) {
        Ok(())
    } else {
        Err(format!("{pointer} was {actual:?}, expected {expected}"))
    }
}

fn shutdown(socket: &Path) -> Result<()> {
    let mut stream = UnixStream::connect(socket).map_err(|error| error.to_string())?;
    serde_json::to_writer(&mut stream, &json!({"command": "_shutdown"}))
        .map_err(|error| error.to_string())?;
    stream.write_all(b"\n").map_err(|error| error.to_string())?;
    stream.flush().map_err(|error| error.to_string())?;
    let mut response = String::new();
    BufReader::new(stream)
        .read_line(&mut response)
        .map_err(|error| error.to_string())?;
    let response: Value = serde_json::from_str(&response).map_err(|error| error.to_string())?;
    if response.pointer("/data/status").and_then(Value::as_str) == Some("ok") {
        Ok(())
    } else {
        Err(format!("daemon rejected shutdown: {response}"))
    }
}

fn wait_for_absence(socket: &Path) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if !socket.exists() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(25));
    }
    Err("workspace daemon socket survived shutdown".to_owned())
}
