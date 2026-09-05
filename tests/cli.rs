use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::{Value, json};
use tempfile::TempDir;

struct Repository(TempDir);

impl Repository {
    fn new() -> Self {
        let repository = Self(tempfile::tempdir().unwrap());
        fs::create_dir(repository.root()).unwrap();
        for (name, source) in [
            ("app.py", include_str!("fixtures/app.py")),
            ("test_app.py", include_str!("fixtures/test_app.py")),
            ("web.ts", include_str!("fixtures/web.ts")),
            ("tsconfig.json", include_str!("fixtures/tsconfig.json")),
        ] {
            fs::write(repository.root().join(name), source).unwrap();
        }
        repository.git(&["init", "--quiet"]);
        repository.git(&["add", "."]);
        repository.git(&[
            "-c",
            "user.name=CodeQ tests",
            "-c",
            "user.email=codeq@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "baseline",
        ]);
        repository
    }

    fn root(&self) -> PathBuf {
        self.0.path().join("repository")
    }

    fn git(&self, arguments: &[&str]) {
        let output = Command::new("git")
            .current_dir(self.root())
            .args(arguments)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    fn command(&self) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_codeq"));
        command
            .arg("--root")
            .arg(self.root())
            .env("CODEQ2_RUNTIME_DIR", self.0.path().join("runtime"));
        command
    }

    fn run(&self, arguments: &[&str]) -> Output {
        self.command()
            .arg("--no-daemon")
            .args(arguments)
            .output()
            .unwrap()
    }

    fn query(&self, arguments: &[&str]) -> Value {
        let output = self
            .command()
            .args(["--no-daemon", "--json"])
            .args(arguments)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let data: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(data["schema_version"], 1);
        data
    }
}

impl Drop for Repository {
    fn drop(&mut self) {
        if let Ok(mut stream) = UnixStream::connect(self.0.path().join("runtime/codeq.sock")) {
            let _ = stream.write_all(b"{\"command\":\"_shutdown\"}\n");
            let _ = std::io::copy(&mut stream, &mut std::io::sink());
        }
    }
}

#[test]
fn cli_help_and_errors_use_the_public_entrypoint() {
    let repository = Repository::new();
    for (command, option) in [
        ("find", "--mode"),
        ("context", "--section"),
        ("trace", "--depth"),
        ("review", "--merge-base"),
    ] {
        let output = repository.run(&[command, "--help"]);
        assert!(output.status.success());
        let help = String::from_utf8(output.stdout).unwrap();
        assert!(help.contains(option) && help.contains("--limit"));
        assert!(!help.contains('\u{1b}'));
    }
    let version = repository.run(&["--version"]);
    assert!(version.status.success());
    assert_eq!(
        String::from_utf8(version.stdout).unwrap().trim(),
        concat!("codeq ", env!("CARGO_PKG_VERSION"))
    );
    for args in [
        vec!["context", "app.py", "--lines", "0"],
        vec!["find", "greet", "--text", "--mode", "symbol"],
        vec![
            "trace",
            "Greeter.greet",
            "--limit",
            "2",
            "--node-limit",
            "3",
        ],
    ] {
        let output = repository.run(&args);
        assert_eq!(output.status.code(), Some(2));
        assert!(!output.stderr.is_empty());
    }
}

#[test]
fn explicit_targets_fail_closed_in_both_transports() {
    let repository = Repository::new();
    fs::write(repository.root().join("settings.yaml"), "greet: true\n").unwrap();
    for (target, status) in [
        ("missing/app.py:3", "not_found"),
        ("settings.yaml:1", "unsupported_language"),
    ] {
        for daemon in [false, true] {
            let mut command = repository.command();
            if !daemon {
                command.arg("--no-daemon");
            }
            let output = command
                .args(["context", target, "--json"])
                .output()
                .unwrap();
            assert_eq!(output.status.code(), Some(1));
            let data: Value = serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(data["status"], status);
            assert_eq!(data["target"], target);
            assert!(data.get("symbol").is_none());
        }
    }
}

#[test]
fn text_and_failure_rendering_agree_across_transports() {
    let repository = Repository::new();
    for args in [
        vec!["find", "PUBLIC_MARKER", "--text"],
        vec!["context", "app.Greeter.greet", "--section", "unknown"],
    ] {
        let local = repository.run(&args);
        let remote = repository.command().args(&args).output().unwrap();
        assert_eq!(local.status.code(), remote.status.code());
        assert_eq!(local.stderr, remote.stderr);
        let local = String::from_utf8(local.stdout).unwrap();
        let remote = String::from_utf8(remote.stdout).unwrap();
        // The final summary contains elapsed time; the evidence and paths must agree.
        assert_eq!(
            local
                .lines()
                .filter(|line| !line.ends_with(" ms]"))
                .collect::<Vec<_>>(),
            remote
                .lines()
                .filter(|line| !line.ends_with(" ms]"))
                .collect::<Vec<_>>()
        );
    }
}

#[test]
fn semantic_navigation_preserves_identity_evidence_and_bounds() {
    let repository = Repository::new();
    let found = repository.query(&["find", "Greeter", "--limit", "1"]);
    assert_eq!(found["results"][0]["name"], "Greeter");
    assert!(found["results"].as_array().unwrap().len() <= 1);
    let context = repository.query(&[
        "context",
        "app.Greeter.greet",
        "--section",
        "callers",
        "--section",
        "lexical-references",
        "--lexical-references",
        "PUBLIC_MARKER",
        "--limit",
        "1",
    ]);
    assert_eq!(context["symbol"]["name"], "greet");
    assert!(context.get("references").is_none());
    assert_eq!(context["lexical_references"]["evidence"], "lexical");
    assert!(!context["callers"].as_array().unwrap().is_empty());
    assert!(context["callers"].as_array().unwrap().len() <= 1);
    assert!(context["section_metadata"].get("callers").is_some());
    for (target, expected) in [
        ("app.Greeter.greet", "greet"),
        ("web.renderGreeting", "renderGreeting"),
    ] {
        let trace = repository.query(&["trace", target, "--out", "--depth", "2", "--limit", "1"]);
        assert_eq!(trace["root"]["name"], expected);
        assert_eq!(trace["node_count"], 1);
        assert_eq!(trace["node_limit"], 1);
    }
    let missing = repository.run(&["context", "wrong.Greeter.greet", "--json"]);
    assert_eq!(missing.status.code(), Some(1));
    assert_eq!(
        serde_json::from_slice::<Value>(&missing.stdout).unwrap()["status"],
        "not_found"
    );
}

#[test]
fn unicode_source_windows_are_bounded_and_continuable() {
    let repository = Repository::new();
    let source = format!("# {}\n", "é🦀".repeat(300)).repeat(250);
    fs::write(repository.root().join("long.py"), source).unwrap();
    let data = repository.query(&["context", "long.py", "--lines", "250"]);
    let window = &data["line_window"];
    assert_eq!(window["line_truncated"], true);
    assert_eq!(window["payload_truncated"], true);
    let text = window["text"].as_str().unwrap();
    assert!(text.chars().count() <= 100_000);
    assert!(text.lines().all(|line| line.chars().count() <= 507));
    assert_eq!(
        window["next_line"].as_u64().unwrap(),
        window["returned_line_count"].as_u64().unwrap() + 1
    );
    assert!(
        window["recovery_command"]
            .as_str()
            .unwrap()
            .contains("--lines")
    );
}

#[test]
fn review_preserves_git_changes_and_semantic_owners() {
    let repository = Repository::new();
    let app = repository.root().join("app.py");
    fs::write(
        &app,
        fs::read_to_string(&app)
            .unwrap()
            .replace("Hello,", "Welcome,"),
    )
    .unwrap();
    repository.git(&["mv", "web.ts", "renamed_web.ts"]);
    fs::remove_file(repository.root().join("test_app.py")).unwrap();
    fs::write(
        repository.root().join("added.py"),
        "def added():\n    return 1\n",
    )
    .unwrap();
    repository.git(&["add", "added.py"]);
    fs::write(repository.root().join("new.yaml"), "value: true\n").unwrap();
    let data = repository.query(&["review", "--base", "HEAD"]);
    let changes: BTreeMap<_, _> = data["file_changes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| {
            (
                Path::new(item["path"].as_str().unwrap())
                    .file_name()
                    .unwrap()
                    .to_str()
                    .unwrap(),
                item["status"].as_str().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        changes,
        BTreeMap::from([
            ("app.py", "M"),
            ("renamed_web.ts", "R"),
            ("test_app.py", "D"),
            ("added.py", "A"),
            ("new.yaml", "U")
        ])
    );
    assert!(
        data["changed_symbols"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item["symbol"]["name"] == "format_greeting")
    );
    assert_eq!(data["changed_file_count"], json!(5));
}
