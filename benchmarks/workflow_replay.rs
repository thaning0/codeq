use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use clap::Parser;
use serde::{Deserialize, Serialize};
use serde_json::Value;

mod support;

use support::{resolve_executable, version};

type Result<T> = std::result::Result<T, String>;

#[derive(Debug, Parser)]
#[command(about = "Replay the committed CodeQ agent workflow workload")]
struct Options {
    #[arg(long, value_name = "PATH")]
    codeq: PathBuf,

    #[arg(long, value_name = "PATH")]
    root: PathBuf,

    #[arg(long, value_name = "PATH", default_value = "benchmarks/workflows.json")]
    workload: PathBuf,

    #[arg(long, value_name = "PATH")]
    output: Option<PathBuf>,

    #[arg(long, value_name = "SEC", default_value_t = 15)]
    case_timeout: u64,

    #[arg(long, value_name = "SEC", default_value_t = 3)]
    cleanup_wait: u64,
}

#[derive(Debug, Deserialize)]
struct Workload {
    schema_version: u64,
    name: String,
    provenance: String,
    privacy: String,
    cases: Vec<WorkloadCase>,
}

#[derive(Debug, Deserialize)]
struct WorkloadCase {
    id: String,
    category: String,
    args: Vec<String>,
    expected_status: String,
    actionable_pointer: String,
}

#[derive(Debug, Serialize)]
struct CaseResult {
    id: String,
    category: String,
    command: String,
    status: String,
    exit_code: i32,
    duration_ms: f64,
    actionable: bool,
    passed: bool,
}

#[derive(Debug, Serialize)]
struct Summary {
    queries: usize,
    passed: usize,
    actionable: usize,
    pass_rate_pct: f64,
    actionable_rate_pct: f64,
    p50_ms: f64,
    max_ms: f64,
    commands: BTreeMap<String, usize>,
    statuses: BTreeMap<String, usize>,
}

#[derive(Debug, Serialize)]
struct ReplayResult {
    schema_version: u64,
    codeq_version: String,
    root: String,
    root_revision: String,
    workload: String,
    provenance: String,
    privacy: String,
    summary: Summary,
    cases: Vec<CaseResult>,
}

fn main() -> ExitCode {
    match run(Options::parse()) {
        Ok(passed) => {
            if passed {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(1)
            }
        }
        Err(error) => {
            eprintln!("codeq workflow replay: {error}");
            ExitCode::from(2)
        }
    }
}

fn run(options: Options) -> Result<bool> {
    let executable = resolve_executable(&options.codeq)?;
    let root = options
        .root
        .canonicalize()
        .map_err(|error| format!("cannot resolve {}: {error}", options.root.display()))?;
    let workload: Workload = serde_json::from_slice(
        &fs::read(&options.workload)
            .map_err(|error| format!("cannot read {}: {error}", options.workload.display()))?,
    )
    .map_err(|error| format!("invalid workload {}: {error}", options.workload.display()))?;
    if workload.schema_version != 1 || workload.cases.is_empty() {
        return Err("workload must use schema_version=1 and contain cases".to_owned());
    }
    let runtime = tempfile::tempdir().map_err(|error| format!("cannot create runtime: {error}"))?;
    let runtime = runtime.path().join("codeq");
    fs::create_dir(&runtime)
        .map_err(|error| format!("cannot create {}: {error}", runtime.display()))?;

    let mut results = Vec::with_capacity(workload.cases.len());
    for case in &workload.cases {
        eprintln!("workflow replay: {}", case.id);
        results.push(run_case(
            &executable,
            &root,
            &runtime,
            case,
            options.case_timeout,
        )?);
    }
    thread::sleep(Duration::from_secs(options.cleanup_wait));
    let summary = summarize(&results);
    let passed = summary.passed == summary.queries && summary.actionable == summary.queries;
    let running_version = version(&executable)?;
    let result = ReplayResult {
        schema_version: 1,
        codeq_version: running_version
            .strip_prefix("codeq ")
            .unwrap_or(&running_version)
            .to_owned(),
        root: repository_label(&root),
        root_revision: repository_revision(&root)?,
        workload: workload.name,
        provenance: workload.provenance,
        privacy: workload.privacy,
        summary,
        cases: results,
    };
    let rendered = serde_json::to_string_pretty(&result)
        .map_err(|error| format!("cannot serialize result: {error}"))?
        + "\n";
    if let Some(path) = options.output {
        write_output(&path, &rendered)?;
    }
    print!("{rendered}");
    Ok(passed)
}

fn repository_label(root: &Path) -> String {
    root.file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("repository")
        .to_owned()
}

fn repository_revision(root: &Path) -> Result<String> {
    let output = Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(root)
        .output()
        .map_err(|error| format!("cannot inspect replay revision: {error}"))?;
    if !output.status.success() {
        return Err("workflow root has no Git revision".to_owned());
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn run_case(
    executable: &Path,
    root: &Path,
    runtime: &Path,
    case: &WorkloadCase,
    timeout_seconds: u64,
) -> Result<CaseResult> {
    let command_name = case
        .args
        .first()
        .ok_or_else(|| format!("{} has no command", case.id))?
        .clone();
    let mut child = Command::new(executable)
        .arg("--root")
        .arg(root)
        .args(&case.args)
        .arg("--json")
        .env("CODEQ_RUNTIME_DIR", runtime)
        .env("CODEQ2_RUNTIME_DIR", runtime)
        .env("CODEQ_DAEMON_IDLE_SECONDS", "1")
        .env("CODEQ_WORKSPACE_IDLE_SECONDS", "1")
        .env("CODEQ_LSP_IDLE_SECONDS", "1")
        .env("CODEQ2_DAEMON_IDLE_SECONDS", "1")
        .env("CODEQ2_MAINTENANCE_INTERVAL_SECONDS", "1")
        .env("CODEQ2_WORKSPACE_IDLE_SECONDS", "1")
        .env("CODEQ2_LSP_IDLE_SECONDS", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("cannot run {}: {error}", case.id))?;
    let mut stdout = child.stdout.take().ok_or("child stdout was not captured")?;
    let mut stderr = child.stderr.take().ok_or("child stderr was not captured")?;
    let stdout_reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout.read_to_end(&mut bytes).map(|_| bytes)
    });
    let stderr_reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr.read_to_end(&mut bytes).map(|_| bytes)
    });
    let started = Instant::now();
    let status = loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("cannot wait for {}: {error}", case.id))?
        {
            break status;
        }
        if started.elapsed() > Duration::from_secs(timeout_seconds) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!(
                "{} exceeded the {} second hard limit",
                case.id, timeout_seconds
            ));
        }
        thread::sleep(Duration::from_millis(2));
    };
    let stdout = stdout_reader
        .join()
        .map_err(|_| format!("{} stdout reader panicked", case.id))?
        .map_err(|error| format!("cannot read {} stdout: {error}", case.id))?;
    let stderr = stderr_reader
        .join()
        .map_err(|_| format!("{} stderr reader panicked", case.id))?
        .map_err(|error| format!("cannot read {} stderr: {error}", case.id))?;
    let value: Value = serde_json::from_slice(&stdout).map_err(|error| {
        format!(
            "{} returned invalid JSON ({error}); stderr={:?}",
            case.id,
            String::from_utf8_lossy(&stderr).trim()
        )
    })?;
    let actual_status = value
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("unknown")
        .to_owned();
    let actionable = value
        .pointer(&case.actionable_pointer)
        .is_some_and(actionable_value);
    let exit_code = status.code().unwrap_or(2);
    Ok(CaseResult {
        id: case.id.clone(),
        category: case.category.clone(),
        command: command_name,
        passed: actual_status == case.expected_status && exit_code == 0,
        status: actual_status,
        exit_code,
        duration_ms: round_one(started.elapsed().as_secs_f64() * 1000.0),
        actionable,
    })
}

fn actionable_value(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_u64().is_some_and(|value| value > 0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn summarize(results: &[CaseResult]) -> Summary {
    let mut durations: Vec<_> = results.iter().map(|result| result.duration_ms).collect();
    durations.sort_by(f64::total_cmp);
    let mut commands = BTreeMap::new();
    let mut statuses = BTreeMap::new();
    for result in results {
        *commands.entry(result.command.clone()).or_insert(0) += 1;
        *statuses.entry(result.status.clone()).or_insert(0) += 1;
    }
    let queries = results.len();
    let passed = results.iter().filter(|result| result.passed).count();
    let actionable = results.iter().filter(|result| result.actionable).count();
    Summary {
        queries,
        passed,
        actionable,
        pass_rate_pct: percentage(passed, queries),
        actionable_rate_pct: percentage(actionable, queries),
        p50_ms: durations
            .get(durations.len() / 2)
            .copied()
            .unwrap_or_default(),
        max_ms: durations.last().copied().unwrap_or_default(),
        commands,
        statuses,
    }
}

fn percentage(value: usize, total: usize) -> f64 {
    if total == 0 {
        0.0
    } else {
        round_one(value as f64 * 100.0 / total as f64)
    }
}

fn round_one(value: f64) -> f64 {
    (value * 10.0).round() / 10.0
}

fn write_output(path: &Path, contents: &str) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("cannot create {}: {error}", parent.display()))?;
    }
    fs::write(path, contents).map_err(|error| format!("cannot write {}: {error}", path.display()))
}
