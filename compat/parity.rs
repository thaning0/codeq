use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Output};

use clap::Parser;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tempfile::TempDir;

mod support;

use support::{resolve_executable, version};

type Result<T> = std::result::Result<T, String>;
const FROZEN_ORACLE_COMMIT: &str = "56fadc0a3485531da83851fbde69f2dc1126463b";

#[derive(Debug, Parser)]
#[command(about = "Run CodeQ black-box parity cases against two arbitrary executables")]
struct Options {
    #[arg(long, value_name = "PATH")]
    oracle: Option<PathBuf>,

    #[arg(long, value_name = "PATH", default_value = "compat/expected.json")]
    expected: PathBuf,

    #[arg(long, requires = "oracle")]
    update_expected: bool,

    #[arg(long, value_name = "PATH")]
    candidate: Option<PathBuf>,

    #[arg(long, value_name = "PATH", default_value = "compat/cases.json")]
    cases: PathBuf,

    #[arg(long, value_name = "PATH", default_value = "compat/corpus")]
    corpus: PathBuf,

    #[arg(long, value_name = "PATH")]
    report: Option<PathBuf>,

    #[arg(long)]
    verbose: bool,
}

#[derive(Debug, Deserialize)]
struct CaseManifest {
    schema_version: u8,
    cases: Vec<Case>,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum OutputKind {
    Json,
    Text,
}

#[derive(Debug, Deserialize)]
struct Case {
    name: String,
    args: Vec<String>,
    output: OutputKind,
    with_root: bool,
    #[serde(default)]
    scenario: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct Invocation {
    exit_code: i32,
    stderr: String,
    #[serde(rename = "stdout")]
    normalized: Value,
}

#[derive(Debug, Deserialize, Serialize)]
struct ExpectedManifest {
    schema_version: u8,
    oracle_version: String,
    oracle_commit: String,
    cases: BTreeMap<String, Invocation>,
}

#[derive(Debug, Serialize)]
struct Difference {
    path: String,
    oracle: Value,
    candidate: Value,
}

#[derive(Debug, Serialize)]
struct CaseReport {
    name: String,
    matched: bool,
    oracle_exit_code: i32,
    candidate_exit_code: i32,
    differences: Vec<Difference>,
}

#[derive(Debug, Serialize)]
struct Summary {
    total: usize,
    matched: usize,
    different: usize,
}

#[derive(Debug, Serialize)]
struct Report {
    schema_version: u8,
    oracle_version: String,
    candidate_version: String,
    normalization: Vec<&'static str>,
    summary: Summary,
    cases: Vec<CaseReport>,
}

fn main() -> ExitCode {
    match run(Options::parse()) {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::from(1),
        Err(error) => {
            eprintln!("codeq parity: {error}");
            ExitCode::from(2)
        }
    }
}

fn run(options: Options) -> Result<bool> {
    let current = PathBuf::from(env!("CARGO_BIN_EXE_codeq"));
    let candidate = resolve_executable(options.candidate.as_deref().unwrap_or(&current))?;
    let manifest: CaseManifest = serde_json::from_slice(
        &fs::read(&options.cases)
            .map_err(|error| format!("cannot read {}: {error}", options.cases.display()))?,
    )
    .map_err(|error| format!("invalid {}: {error}", options.cases.display()))?;
    if manifest.schema_version != 1 {
        return Err(format!(
            "unsupported case schema version: {}",
            manifest.schema_version
        ));
    }

    let sandbox = prepare_corpus(&options.corpus)?;
    let repository = sandbox.path().join("repository");
    let oracle_runtime = sandbox.path().join("oracle-runtime");
    let candidate_runtime = sandbox.path().join("candidate-runtime");
    fs::create_dir(&oracle_runtime)
        .map_err(|error| format!("cannot create oracle runtime: {error}"))?;
    fs::create_dir(&candidate_runtime)
        .map_err(|error| format!("cannot create candidate runtime: {error}"))?;

    let candidate_version = version(&candidate)?;
    let oracle = options
        .oracle
        .as_deref()
        .map(resolve_executable)
        .transpose()?;
    let expected = if oracle.is_none() {
        let expected: ExpectedManifest = serde_json::from_slice(
            &fs::read(&options.expected)
                .map_err(|error| format!("cannot read {}: {error}", options.expected.display()))?,
        )
        .map_err(|error| format!("invalid {}: {error}", options.expected.display()))?;
        if expected.schema_version != 1 {
            return Err(format!(
                "unsupported expected-result schema version: {}",
                expected.schema_version
            ));
        }
        if expected.oracle_commit != FROZEN_ORACLE_COMMIT {
            return Err(format!(
                "{} targets oracle commit {}, expected {}",
                options.expected.display(),
                expected.oracle_commit,
                FROZEN_ORACLE_COMMIT
            ));
        }
        let manifest_names = manifest
            .cases
            .iter()
            .map(|case| case.name.as_str())
            .collect::<BTreeSet<_>>();
        let expected_names = expected
            .cases
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>();
        if manifest_names != expected_names {
            return Err(format!(
                "{} does not contain exactly the cases declared by {}",
                options.expected.display(),
                options.cases.display()
            ));
        }
        Some(expected)
    } else {
        None
    };
    let oracle_version = match (&oracle, &expected) {
        (Some(executable), _) => version(executable)?,
        (None, Some(expected)) => expected.oracle_version.clone(),
        (None, None) => {
            return Err("an oracle executable or expected results are required".to_owned());
        }
    };
    let mut case_reports = Vec::with_capacity(manifest.cases.len());
    let mut captured = BTreeMap::new();
    for case in &manifest.cases {
        if let Some(scenario) = &case.scenario {
            apply_scenario(&repository, scenario)?;
        }
        let oracle_result = if let Some(executable) = &oracle {
            invoke(
                executable,
                case,
                &repository,
                "CODEQ_RUNTIME_DIR",
                &oracle_runtime,
            )?
        } else {
            expected
                .as_ref()
                .and_then(|expected| expected.cases.get(&case.name))
                .cloned()
                .ok_or_else(|| format!("missing expected result for {}", case.name))?
        };
        captured.insert(case.name.clone(), oracle_result.clone());
        let candidate_result = invoke(
            &candidate,
            case,
            &repository,
            "CODEQ2_RUNTIME_DIR",
            &candidate_runtime,
        )?;
        case_reports.push(compare(case.name.clone(), oracle_result, candidate_result));
        if case.scenario.is_some() {
            restore_default_mutation(&repository)?;
        }
    }

    if options.update_expected {
        let captured = ExpectedManifest {
            schema_version: 1,
            oracle_version: oracle_version.clone(),
            oracle_commit: FROZEN_ORACLE_COMMIT.to_owned(),
            cases: captured,
        };
        let rendered = serde_json::to_string_pretty(&captured)
            .map_err(|error| format!("cannot serialize expected results: {error}"))?
            + "\n";
        if let Some(parent) = options.expected.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("cannot create {}: {error}", parent.display()))?;
        }
        fs::write(&options.expected, rendered)
            .map_err(|error| format!("cannot write {}: {error}", options.expected.display()))?;
    }

    let matched = case_reports.iter().filter(|case| case.matched).count();
    let different = case_reports.len() - matched;
    let report = Report {
        schema_version: 1,
        oracle_version,
        candidate_version,
        normalization: vec![
            "JSON object key order",
            "diagnostic fields whose key ends in _ms",
            "process identifiers stored under a pid key",
            "temporary-corpus Git commit identifiers stored under resolved_base",
            "CRLF line endings in text output",
            "plain-output millisecond timings",
        ],
        summary: Summary {
            total: case_reports.len(),
            matched,
            different,
        },
        cases: case_reports,
    };
    let rendered = serde_json::to_string_pretty(&report)
        .map_err(|error| format!("cannot serialize parity report: {error}"))?
        + "\n";
    let report_written = options.report.is_some();
    if let Some(path) = options.report {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("cannot create {}: {error}", parent.display()))?;
        }
        fs::write(&path, &rendered)
            .map_err(|error| format!("cannot write {}: {error}", path.display()))?;
    }
    if options.verbose || (different != 0 && !report_written) {
        print!("{rendered}");
    } else {
        println!(
            "codeq parity: {matched}/{} cases matched; {} differed",
            report.summary.total, different
        );
    }
    Ok(different == 0)
}

fn prepare_corpus(source: &Path) -> Result<TempDir> {
    let sandbox = tempfile::tempdir().map_err(|error| format!("cannot create sandbox: {error}"))?;
    let repository = sandbox.path().join("repository");
    copy_tree(source, &repository)?;
    git(&repository, ["init", "--quiet"])?;
    git(&repository, ["config", "user.name", "CodeQ parity"])?;
    git(
        &repository,
        ["config", "user.email", "codeq-parity@example.invalid"],
    )?;
    git(&repository, ["add", "."])?;
    git(&repository, ["commit", "--quiet", "-m", "parity baseline"])?;

    apply_default_mutation(&repository)?;
    Ok(sandbox)
}

fn apply_default_mutation(repository: &Path) -> Result<()> {
    let app = repository.join("app.py");
    let current = fs::read_to_string(&app)
        .map_err(|error| format!("cannot read {}: {error}", app.display()))?;
    let changed = current.replace(
        "return f\"Hello, {normalized}!\"",
        "return f\"Hello, {normalized}!!\"",
    );
    if current == changed {
        return Err("parity corpus mutation marker was not found".to_owned());
    }
    fs::write(&app, changed)
        .map_err(|error| format!("cannot update {}: {error}", app.display()))?;
    Ok(())
}

fn restore_default_mutation(repository: &Path) -> Result<()> {
    git(repository, ["reset", "--hard", "--quiet", "HEAD"])?;
    git(repository, ["clean", "-fdq"])?;
    apply_default_mutation(repository)
}

fn apply_scenario(repository: &Path, scenario: &str) -> Result<()> {
    restore_default_mutation(repository)?;
    match scenario {
        "review_statuses" => {
            git(repository, ["mv", "web.ts", "renamed_web.ts"])?;
            fs::remove_file(repository.join("dynamic.py"))
                .map_err(|error| format!("cannot delete scenario file: {error}"))?;
            fs::write(
                repository.join("added.py"),
                "def newly_added(value: str) -> str:\n    return value.casefold()\n",
            )
            .map_err(|error| format!("cannot add scenario file: {error}"))?;
            Ok(())
        }
        other => Err(format!("unknown parity scenario: {other}")),
    }
}

fn copy_tree(source: &Path, destination: &Path) -> Result<()> {
    fs::create_dir_all(destination)
        .map_err(|error| format!("cannot create {}: {error}", destination.display()))?;
    for entry in fs::read_dir(source)
        .map_err(|error| format!("cannot read {}: {error}", source.display()))?
    {
        let entry = entry.map_err(|error| format!("cannot read directory entry: {error}"))?;
        let target = destination.join(entry.file_name());
        if entry
            .file_type()
            .map_err(|error| format!("cannot inspect {}: {error}", entry.path().display()))?
            .is_dir()
        {
            copy_tree(&entry.path(), &target)?;
        } else {
            fs::copy(entry.path(), &target).map_err(|error| {
                format!(
                    "cannot copy {} to {}: {error}",
                    entry.path().display(),
                    target.display()
                )
            })?;
        }
    }
    Ok(())
}

fn git<I, S>(repository: &Path, args: I) -> Result<()>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    let output = Command::new("git")
        .args(args)
        .current_dir(repository)
        .output()
        .map_err(|error| format!("cannot run git in {}: {error}", repository.display()))?;
    if output.status.success() {
        return Ok(());
    }
    Err(format!(
        "git failed in {}: {}",
        repository.display(),
        String::from_utf8_lossy(&output.stderr).trim()
    ))
}

fn invoke(
    executable: &Path,
    case: &Case,
    repository: &Path,
    runtime_env: &str,
    runtime: &Path,
) -> Result<Invocation> {
    let mut command = Command::new(executable);
    if case.with_root {
        command.arg("--root").arg(repository);
    }
    let output = command
        .args(&case.args)
        .env(runtime_env, runtime)
        .current_dir(repository)
        .output()
        .map_err(|error| {
            format!(
                "cannot run {} for {}: {error}",
                executable.display(),
                case.name
            )
        })?;
    invocation(case.output, output, repository)
        .map_err(|error| format!("case {} with {}: {error}", case.name, executable.display()))
}

fn invocation(kind: OutputKind, output: Output, repository: &Path) -> Result<Invocation> {
    let exit_code = output.status.code().unwrap_or(2);
    let repository = repository.to_string_lossy();
    let stdout = String::from_utf8(output.stdout)
        .map_err(|error| format!("stdout is not UTF-8: {error}"))?
        .replace("\r\n", "\n");
    let stderr = String::from_utf8(output.stderr)
        .map_err(|error| format!("stderr is not UTF-8: {error}"))?
        .replace("\r\n", "\n")
        .replace(repository.as_ref(), "<ROOT>");
    let normalized = match kind {
        OutputKind::Json => {
            let mut value = serde_json::from_str(&stdout).map_err(|error| {
                format!(
                    "expected JSON output, got {error}; exit={exit_code}; stderr={stderr:?}; stdout={stdout:?}"
                )
            })?;
            normalize_json(&mut value, &repository);
            value
        }
        OutputKind::Text => Value::String(normalize_text(&stdout, &repository)),
    };
    Ok(Invocation {
        exit_code,
        stderr,
        normalized,
    })
}

fn normalize_text(value: &str, repository: &str) -> String {
    let mut normalized = value.replace(repository, "<ROOT>");
    let mut search_from = 0;
    while let Some(offset) = normalized[search_from..].find(" ms") {
        let end = search_from + offset;
        let mut start = end;
        while start > 0
            && (normalized.as_bytes()[start - 1].is_ascii_digit()
                || normalized.as_bytes()[start - 1] == b'.')
        {
            start -= 1;
        }
        if start < end
            && normalized[start..end]
                .chars()
                .any(|character| character.is_ascii_digit())
        {
            normalized.replace_range(start..end, "<timing>");
            search_from = start + "<timing> ms".len();
        } else {
            search_from = end + 3;
        }
    }
    normalized
}

fn normalize_json(value: &mut Value, repository: &str) {
    match value {
        Value::Array(items) => {
            for item in items {
                normalize_json(item, repository);
            }
        }
        Value::Object(object) => {
            object.retain(|key, _| !key.ends_with("_ms"));
            if object.contains_key("pid") {
                object.insert("pid".to_owned(), Value::String("<pid>".to_owned()));
            }
            if object.contains_key("resolved_base") {
                object.insert(
                    "resolved_base".to_owned(),
                    Value::String("<git-commit>".to_owned()),
                );
            }
            for nested in object.values_mut() {
                normalize_json(nested, repository);
            }
        }
        Value::String(text) => *text = text.replace(repository, "<ROOT>"),
        Value::Null | Value::Bool(_) | Value::Number(_) => {}
    }
}

fn compare(name: String, oracle: Invocation, candidate: Invocation) -> CaseReport {
    let mut differences = Vec::new();
    if oracle.exit_code != candidate.exit_code {
        differences.push(Difference {
            path: "$exit_code".to_owned(),
            oracle: Value::from(oracle.exit_code),
            candidate: Value::from(candidate.exit_code),
        });
    }
    collect_differences(
        "$stdout",
        &oracle.normalized,
        &candidate.normalized,
        &mut differences,
    );
    if oracle.stderr != candidate.stderr {
        differences.push(Difference {
            path: "$stderr".to_owned(),
            oracle: Value::String(oracle.stderr),
            candidate: Value::String(candidate.stderr),
        });
    }
    CaseReport {
        name,
        matched: differences.is_empty(),
        oracle_exit_code: oracle.exit_code,
        candidate_exit_code: candidate.exit_code,
        differences,
    }
}

fn collect_differences(path: &str, oracle: &Value, candidate: &Value, out: &mut Vec<Difference>) {
    if out.len() >= 100 || oracle == candidate {
        return;
    }
    match (oracle, candidate) {
        (Value::Object(left), Value::Object(right)) => {
            let keys: BTreeSet<&String> = left.keys().chain(right.keys()).collect();
            for key in keys {
                let nested_path = format!("{path}.{}", json_path_key(key));
                match (left.get(key), right.get(key)) {
                    (Some(left_value), Some(right_value)) => {
                        collect_differences(&nested_path, left_value, right_value, out);
                    }
                    (left_value, right_value) => out.push(Difference {
                        path: nested_path,
                        oracle: left_value.cloned().unwrap_or(Value::Null),
                        candidate: right_value.cloned().unwrap_or(Value::Null),
                    }),
                }
                if out.len() >= 100 {
                    break;
                }
            }
        }
        (Value::Array(left), Value::Array(right)) => {
            let shared = left.len().min(right.len());
            for index in 0..shared {
                collect_differences(
                    &format!("{path}[{index}]"),
                    &left[index],
                    &right[index],
                    out,
                );
                if out.len() >= 100 {
                    return;
                }
            }
            if left.len() != right.len() {
                out.push(Difference {
                    path: format!("{path}.length"),
                    oracle: Value::from(left.len()),
                    candidate: Value::from(right.len()),
                });
            }
        }
        _ => out.push(Difference {
            path: path.to_owned(),
            oracle: oracle.clone(),
            candidate: candidate.clone(),
        }),
    }
}

fn json_path_key(key: &str) -> String {
    if key
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || character == '_')
    {
        key.to_owned()
    } else {
        format!("[{key:?}]")
    }
}
