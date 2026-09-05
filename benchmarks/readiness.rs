use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use clap::Parser;
use serde::Serialize;
use serde_json::Value;

mod support;

use support::{resolve_executable, version};

type Result<T> = std::result::Result<T, String>;
type Measurements = (
    BTreeMap<&'static str, Series>,
    BTreeMap<&'static str, Series>,
);

#[derive(Debug, Parser)]
#[command(about = "Run the committed end-to-end CodeQ readiness workload")]
struct Options {
    #[arg(long, value_name = "PATH")]
    codeq: PathBuf,

    #[arg(long, value_name = "PATH")]
    root: PathBuf,

    #[arg(long, value_name = "N", default_value_t = 3, value_parser = clap::value_parser!(u16).range(1..))]
    reps: u16,

    #[arg(long, value_name = "PATH")]
    output: Option<PathBuf>,

    #[arg(long, value_name = "SEC", default_value_t = 7)]
    cleanup_wait: u64,

    #[arg(long, value_name = "SEC", default_value_t = 15)]
    case_timeout: u64,
}

struct BenchmarkCase {
    name: &'static str,
    args: &'static [&'static str],
}

#[derive(Debug, Serialize)]
struct Sample {
    status: String,
    exit_code: i32,
    duration_ms: f64,
    phase_ms: Value,
    lsp_requests: u64,
    sessions_started: u64,
    prewarm_files: u64,
    prewarm_probes: u64,
    prewarm_early_stops: u64,
    document_symbols_hit: u64,
    document_symbols_miss: u64,
    process_rss_kb: u64,
    daemon_rss_kb: u64,
    lsp_rss_kb: u64,
}

#[derive(Debug, Serialize)]
struct Series {
    runs: usize,
    p50_ms: f64,
    p95_ms: f64,
    max_ms: f64,
    max_process_rss_kb: u64,
    max_daemon_rss_kb: u64,
    max_lsp_rss_kb: u64,
    samples: Vec<Sample>,
}

#[derive(Debug, Serialize)]
struct BenchmarkResult {
    codeq_version: String,
    root: String,
    root_revision: String,
    reps: u16,
    cold: BTreeMap<&'static str, Series>,
    warm: BTreeMap<&'static str, Series>,
}

#[derive(Default)]
struct RuntimeRss {
    daemon_kb: u64,
    lsp_kb: u64,
}

fn main() -> ExitCode {
    match run(Options::parse()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("codeq readiness: {error}");
            ExitCode::from(2)
        }
    }
}

fn run(options: Options) -> Result<()> {
    let executable = resolve_executable(&options.codeq)?;
    let root = options
        .root
        .canonicalize()
        .map_err(|error| format!("cannot resolve {}: {error}", options.root.display()))?;
    let runtime = tempfile::tempdir().map_err(|error| format!("cannot create runtime: {error}"))?;
    let runtime_path = runtime.path().join("codeq");
    fs::create_dir(&runtime_path)
        .map_err(|error| format!("cannot create {}: {error}", runtime_path.display()))?;
    let measurements = collect_measurements(
        &executable,
        &root,
        &runtime_path,
        options.reps,
        options.case_timeout,
        &cases(),
    );

    thread::sleep(Duration::from_secs(options.cleanup_wait));
    let survivors = runtime_processes(&runtime_path)?;
    if !survivors.is_empty() {
        return Err(format!(
            "runtime processes survived the cleanup window: {}",
            survivors.join(", ")
        ));
    }
    let (cold, warm) = measurements?;

    let codeq_version = version(&executable)?;
    let result = BenchmarkResult {
        codeq_version: codeq_version
            .strip_prefix("codeq ")
            .unwrap_or(&codeq_version)
            .to_owned(),
        root: repository_label(&root),
        root_revision: repository_revision(&root)?,
        reps: options.reps,
        cold,
        warm,
    };
    let rendered = serde_json::to_string_pretty(&result)
        .map_err(|error| format!("cannot serialize result: {error}"))?
        + "\n";
    if let Some(path) = options.output {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("cannot create {}: {error}", parent.display()))?;
        }
        fs::write(&path, &rendered)
            .map_err(|error| format!("cannot write {}: {error}", path.display()))?;
    }
    print!("{rendered}");
    Ok(())
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
        .map_err(|error| format!("cannot inspect benchmark revision: {error}"))?;
    if !output.status.success() {
        return Err("benchmark root has no Git revision".to_owned());
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn collect_measurements(
    executable: &Path,
    root: &Path,
    runtime: &Path,
    reps: u16,
    case_timeout: u64,
    cases: &[BenchmarkCase],
) -> Result<Measurements> {
    let mut cold = BTreeMap::new();
    for case in cases {
        let mut samples = Vec::with_capacity(usize::from(reps));
        for _ in 0..reps {
            eprintln!("readiness: cold {}", case.name);
            samples.push(run_case(
                executable,
                root,
                runtime,
                case,
                true,
                case_timeout,
            )?);
        }
        cold.insert(case.name, summarize(samples));
    }

    for case in cases {
        eprintln!("readiness: warmup {}", case.name);
        run_case(executable, root, runtime, case, false, case_timeout)?;
    }
    let mut warm = BTreeMap::new();
    for case in cases {
        let mut samples = Vec::with_capacity(usize::from(reps));
        for _ in 0..reps {
            eprintln!("readiness: warm {}", case.name);
            samples.push(run_case(
                executable,
                root,
                runtime,
                case,
                false,
                case_timeout,
            )?);
        }
        warm.insert(case.name, summarize(samples));
    }
    Ok((cold, warm))
}

fn cases() -> Vec<BenchmarkCase> {
    vec![
        BenchmarkCase {
            name: "find_exact",
            args: &["find", "BacktestService", "--limit", "8"],
        },
        BenchmarkCase {
            name: "find_concept",
            args: &["find", "SSE backtest logs", "--limit", "8"],
        },
        BenchmarkCase {
            name: "context_symbol",
            args: &[
                "context",
                "BacktestService.stream_backtest_logs",
                "--limit",
                "10",
            ],
        },
        BenchmarkCase {
            name: "context_reference_store",
            args: &["context", "DuckDbReferenceStore", "--limit", "12"],
        },
        BenchmarkCase {
            name: "context_cursor",
            args: &[
                "context",
                "backend/src/app/api/backtest.py:175:17",
                "--limit",
                "10",
            ],
        },
        BenchmarkCase {
            name: "context_lexical",
            args: &[
                "context",
                "backend/src/app/api/backtest.py:175:17",
                "--lexical-references",
                "/logs/stream",
                "--path",
                "frontend",
                "--exclude-tests",
                "--limit",
                "10",
            ],
        },
        BenchmarkCase {
            name: "trace_in",
            args: &[
                "trace",
                "BacktestService.stream_backtest_logs",
                "--in",
                "--depth",
                "2",
                "--limit",
                "20",
            ],
        },
        BenchmarkCase {
            name: "text_env",
            args: &[
                "find",
                "BACKTEST_QUESTDB_QUERY_TARGET_ROWS",
                "--text",
                "--limit",
                "12",
            ],
        },
        BenchmarkCase {
            name: "review",
            args: &["review", "--base", "HEAD~1", "--limit", "10"],
        },
        BenchmarkCase {
            name: "review_broad",
            args: &["review", "--base", "HEAD~4", "--limit", "30"],
        },
    ]
}

fn run_case(
    executable: &Path,
    root: &Path,
    runtime: &Path,
    case: &BenchmarkCase,
    cold: bool,
    timeout_seconds: u64,
) -> Result<Sample> {
    let mut command = Command::new(executable);
    command
        .arg("--root")
        .arg(root)
        .args(case.args)
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
        .stderr(Stdio::piped());
    if cold {
        command.arg("--no-daemon");
    }

    let started = Instant::now();
    let mut child = command
        .spawn()
        .map_err(|error| format!("cannot run {}: {error}", case.name))?;
    let pid = child.id();
    let mut stdout = child
        .stdout
        .take()
        .ok_or_else(|| format!("{} stdout was not captured", case.name))?;
    let mut stderr = child
        .stderr
        .take()
        .ok_or_else(|| format!("{} stderr was not captured", case.name))?;
    let stdout_reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout.read_to_end(&mut bytes).map(|_| bytes)
    });
    let stderr_reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr.read_to_end(&mut bytes).map(|_| bytes)
    });
    let mut process_rss_kb = 0;
    let mut tree_snapshots = Vec::new();
    let mut sampling_error = None;
    let mut next_tree_sample = Instant::now();
    let exit_status = loop {
        process_rss_kb = process_rss_kb.max(read_rss_kb(pid));
        if Instant::now() >= next_tree_sample {
            match process_tree_snapshot(pid) {
                Ok(snapshot) => tree_snapshots.push(snapshot),
                Err(error) => {
                    sampling_error.get_or_insert(error);
                }
            };
            next_tree_sample = Instant::now() + Duration::from_millis(100);
        }
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("cannot wait for {}: {error}", case.name))?
        {
            break status;
        }
        if started.elapsed() > Duration::from_secs(timeout_seconds) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!(
                "{} exceeded the {} second hard limit",
                case.name, timeout_seconds
            ));
        }
        thread::sleep(Duration::from_micros(200));
    };
    let stdout = stdout_reader
        .join()
        .map_err(|_| format!("stdout reader panicked for {}", case.name))?
        .map_err(|error| format!("cannot read {} stdout: {error}", case.name))?;
    let stderr = stderr_reader
        .join()
        .map_err(|_| format!("stderr reader panicked for {}", case.name))?
        .map_err(|error| format!("cannot read {} stderr: {error}", case.name))?;
    if let Some(error) = sampling_error {
        return Err(format!("cannot sample {} process tree: {error}", case.name));
    }
    let duration_ms = started.elapsed().as_secs_f64() * 1000.0;
    let stdout = String::from_utf8(stdout)
        .map_err(|error| format!("{} stdout is not UTF-8: {error}", case.name))?;
    let stderr = String::from_utf8_lossy(&stderr);
    let data: Value = serde_json::from_str(&stdout).map_err(|error| {
        format!(
            "{} returned invalid JSON ({error}); exit={:?}; stderr={:?}",
            case.name,
            exit_status.code(),
            stderr.trim()
        )
    })?;
    let status = data
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    if !exit_status.success() || status != "ok" {
        let reason = data
            .get("reason")
            .or_else(|| data.get("error"))
            .and_then(Value::as_str)
            .unwrap_or("no reason reported");
        return Err(format!(
            "{} failed readiness: status={status}, exit={:?}, reason={reason}",
            case.name,
            exit_status.code()
        ));
    }
    let meta = data.get("_meta").and_then(Value::as_object);
    let cache = meta
        .and_then(|value| value.get("cache"))
        .and_then(Value::as_object);
    let roots = lsp_roots(meta);
    let mut rss = runtime_rss(runtime, &roots)?;
    for snapshot in tree_snapshots {
        rss.lsp_kb = rss.lsp_kb.max(lsp_tree_rss(&snapshot, &roots));
    }
    Ok(Sample {
        status: status.to_owned(),
        exit_code: exit_status.code().unwrap_or(2),
        duration_ms: round_one(duration_ms),
        phase_ms: meta
            .and_then(|value| value.get("phase_ms"))
            .cloned()
            .unwrap_or_else(|| Value::Object(Default::default())),
        lsp_requests: object_u64(meta, "lsp_request_count"),
        sessions_started: u64::from(object_bool(meta, "lsp_started")),
        prewarm_files: object_u64(meta, "prewarm_files"),
        prewarm_probes: object_u64(meta, "prewarm_probes"),
        prewarm_early_stops: object_u64(meta, "prewarm_early_stops"),
        document_symbols_hit: object_u64(cache, "document_symbols_hit"),
        document_symbols_miss: object_u64(cache, "document_symbols_miss"),
        process_rss_kb,
        daemon_rss_kb: rss.daemon_kb,
        lsp_rss_kb: rss.lsp_kb,
    })
}

fn summarize(samples: Vec<Sample>) -> Series {
    let mut durations: Vec<f64> = samples.iter().map(|sample| sample.duration_ms).collect();
    durations.sort_by(f64::total_cmp);
    let p50 = durations[durations.len() / 2];
    let p95_index = ((durations.len() - 1) * 95).div_ceil(100);
    Series {
        runs: samples.len(),
        p50_ms: round_one(p50),
        p95_ms: round_one(durations[p95_index]),
        max_ms: round_one(*durations.last().expect("samples are non-empty")),
        max_process_rss_kb: samples
            .iter()
            .map(|sample| sample.process_rss_kb)
            .max()
            .unwrap_or(0),
        max_daemon_rss_kb: samples
            .iter()
            .map(|sample| sample.daemon_rss_kb)
            .max()
            .unwrap_or(0),
        max_lsp_rss_kb: samples
            .iter()
            .map(|sample| sample.lsp_rss_kb)
            .max()
            .unwrap_or(0),
        samples,
    }
}

fn object_u64(object: Option<&serde_json::Map<String, Value>>, key: &str) -> u64 {
    object
        .and_then(|value| value.get(key))
        .and_then(Value::as_u64)
        .unwrap_or(0)
}

fn object_bool(object: Option<&serde_json::Map<String, Value>>, key: &str) -> bool {
    object
        .and_then(|value| value.get(key))
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

fn round_one(value: f64) -> f64 {
    (value * 10.0).round() / 10.0
}

fn process_status(pid: u32) -> (u32, u64) {
    let Ok(status) = fs::read_to_string(format!("/proc/{pid}/status")) else {
        return (0, 0);
    };
    let mut parent = 0;
    let mut rss = 0;
    for line in status.lines() {
        if let Some(value) = line.strip_prefix("PPid:") {
            parent = value.trim().parse().unwrap_or(0);
        } else if let Some(value) = line.strip_prefix("VmRSS:") {
            rss = value
                .split_whitespace()
                .next()
                .and_then(|value| value.parse().ok())
                .unwrap_or(0);
        }
    }
    (parent, rss)
}

fn read_rss_kb(pid: u32) -> u64 {
    process_status(pid).1
}

fn lsp_roots(meta: Option<&serde_json::Map<String, Value>>) -> BTreeSet<u32> {
    let mut roots = BTreeSet::new();
    for key in ["lsp_sessions_before", "lsp_sessions"] {
        let Some(sessions) = meta
            .and_then(|value| value.get(key))
            .and_then(Value::as_array)
        else {
            continue;
        };
        for pid in sessions
            .iter()
            .filter_map(|session| session.get("pid"))
            .filter_map(Value::as_u64)
            .filter_map(|pid| u32::try_from(pid).ok())
        {
            roots.insert(pid);
        }
    }
    roots
}

fn runtime_rss(runtime: &Path, lsp_roots: &BTreeSet<u32>) -> Result<RuntimeRss> {
    let marker = runtime.display().to_string();
    let processes: Vec<ProcessRecord> = process_records(true)?
        .into_iter()
        .filter(|process| process.environment.contains(&marker))
        .collect();
    let parents: BTreeMap<u32, u32> = processes
        .iter()
        .map(|process| (process.pid, process.parent))
        .collect();
    let mut effective_lsp_roots = lsp_roots.clone();
    effective_lsp_roots.extend(
        processes
            .iter()
            .filter(|process| is_language_server(&process.command))
            .map(|process| process.pid),
    );
    let mut result = RuntimeRss::default();
    for process in processes {
        if has_ancestor(process.pid, &effective_lsp_roots, &parents) {
            result.lsp_kb += process.rss_kb;
        } else {
            result.daemon_kb += process.rss_kb;
        }
    }
    Ok(result)
}

fn is_language_server(command: &str) -> bool {
    [
        "basedpyright-langserver",
        "pyright-langserver",
        "rust-analyzer",
        "typescript-language-server",
        "tsserver.js",
    ]
    .iter()
    .any(|marker| command.contains(marker))
}

fn process_tree_snapshot(root: u32) -> Result<Vec<ProcessRecord>> {
    let processes = process_records(false)?;
    let parents: BTreeMap<u32, u32> = processes
        .iter()
        .map(|process| (process.pid, process.parent))
        .collect();
    let roots = BTreeSet::from([root]);
    Ok(processes
        .into_iter()
        .filter(|process| has_ancestor(process.pid, &roots, &parents))
        .collect())
}

fn lsp_tree_rss(processes: &[ProcessRecord], lsp_roots: &BTreeSet<u32>) -> u64 {
    let parents: BTreeMap<u32, u32> = processes
        .iter()
        .map(|process| (process.pid, process.parent))
        .collect();
    processes
        .iter()
        .filter(|process| has_ancestor(process.pid, lsp_roots, &parents))
        .map(|process| process.rss_kb)
        .sum()
}

fn has_ancestor(mut pid: u32, roots: &BTreeSet<u32>, parents: &BTreeMap<u32, u32>) -> bool {
    let mut visited = BTreeSet::new();
    while pid > 1 && visited.insert(pid) {
        if roots.contains(&pid) {
            return true;
        }
        let Some(parent) = parents.get(&pid) else {
            return false;
        };
        pid = *parent;
    }
    false
}

fn runtime_processes(runtime: &Path) -> Result<Vec<String>> {
    let marker = runtime.display().to_string();
    Ok(process_records(true)?
        .into_iter()
        .filter(|process| process.environment.contains(&marker))
        .map(|process| format!("{}:{}", process.pid, process.command))
        .collect())
}

struct ProcessRecord {
    pid: u32,
    parent: u32,
    command: String,
    environment: String,
    rss_kb: u64,
}

fn process_records(include_environment: bool) -> Result<Vec<ProcessRecord>> {
    let mut records = Vec::new();
    let entries = fs::read_dir("/proc").map_err(|error| format!("cannot read /proc: {error}"))?;
    for entry in entries.flatten() {
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|value| value.parse::<u32>().ok())
        else {
            continue;
        };
        let environment = if include_environment {
            fs::read(entry.path().join("environ"))
                .map(|bytes| String::from_utf8_lossy(&bytes).replace('\0', " "))
                .unwrap_or_default()
        } else {
            String::new()
        };
        if include_environment && environment.is_empty() {
            continue;
        }
        let command = fs::read(entry.path().join("cmdline"))
            .map(|bytes| String::from_utf8_lossy(&bytes).replace('\0', " "))
            .unwrap_or_default();
        let (parent, rss_kb) = process_status(pid);
        records.push(ProcessRecord {
            pid,
            parent,
            command,
            environment,
            rss_kb,
        });
    }
    Ok(records)
}
