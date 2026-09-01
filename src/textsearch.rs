use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read, Seek};
use std::path::{Path, PathBuf};
use std::process::Command;

use globset::{GlobBuilder, GlobMatcher};
use serde::Serialize;
use serde_json::{Value, json};

use crate::target;

const TEXT_LINE_CHARS: usize = 500;

#[derive(Clone, Serialize)]
struct TextHit {
    path: PathBuf,
    relative_path: String,
    line: usize,
    column: usize,
    text: String,
    occurrences: usize,
    is_test: bool,
    tracked: bool,
    git_status: &'static str,
    source: &'static str,
    evidence: &'static str,
    text_truncated: bool,
    text_start_column: usize,
}

struct Scope {
    path_text: Vec<String>,
    path_prefixes: Vec<String>,
    glob_text: Vec<String>,
    globs: Vec<GlobMatcher>,
    exclude_tests: bool,
}

pub(crate) fn search(
    root: &Path,
    query: &str,
    limit: i64,
    paths: &[String],
    globs: &[String],
    exclude_tests: bool,
) -> Result<Value, String> {
    if query.is_empty() {
        return Ok(invalid(query, "text query must not be empty"));
    }
    if query.contains(['\0', '\n', '\r']) {
        return Ok(invalid(
            query,
            "text query must be a single line without NUL bytes",
        ));
    }
    let scope = Scope::new(root, paths, globs, exclude_tests)?;
    let mut hits = tracked_hits(root, query, &scope)?;
    hits.extend(untracked_hits(root, query, &scope)?);
    hits.sort_by(|left, right| {
        (&left.relative_path, left.line, !left.tracked).cmp(&(
            &right.relative_path,
            right.line,
            !right.tracked,
        ))
    });

    let items = usize::try_from(limit.max(1)).unwrap_or(usize::MAX);
    let bounded: Vec<_> = hits
        .iter()
        .take(items)
        .cloned()
        .map(|hit| bounded_hit(hit, TEXT_LINE_CHARS))
        .collect();
    let matching_files: HashSet<_> = hits.iter().map(|hit| &hit.path).collect();
    let match_count = hits.iter().map(|hit| hit.occurrences).sum::<usize>();
    let returned_match_count = bounded.iter().map(|hit| hit.occurrences).sum::<usize>();
    let test_line_count = hits.iter().filter(|hit| hit.is_test).count();
    let tracked_line_count = hits.iter().filter(|hit| hit.tracked).count();
    let untracked_line_count = hits.len() - tracked_line_count;
    Ok(json!({
        "status": "ok",
        "mode": "text",
        "evidence": "lexical",
        "query": query,
        "results": bounded,
        "match_count": match_count,
        "matching_line_count": hits.len(),
        "matching_file_count": matching_files.len(),
        "returned_line_count": bounded.len(),
        "returned_match_count": returned_match_count,
        "test_line_count": test_line_count,
        "tracked_line_count": tracked_line_count,
        "untracked_line_count": untracked_line_count,
        "truncated": hits.len() > bounded.len(),
        "filters": {
            "paths": scope.path_text,
            "globs": scope.glob_text,
            "exclude_tests": scope.exclude_tests,
            "include_untracked": true,
            "only_tests": false,
        },
    }))
}

impl Scope {
    fn new(
        root: &Path,
        paths: &[String],
        globs: &[String],
        exclude_tests: bool,
    ) -> Result<Self, String> {
        let path_text: Vec<_> = paths
            .iter()
            .filter(|value| !value.trim().is_empty())
            .cloned()
            .collect();
        let path_prefixes = path_text
            .iter()
            .map(|value| normalize_path_prefix(root, value))
            .collect();
        let glob_text: Vec<_> = globs
            .iter()
            .filter(|value| !value.trim().is_empty())
            .cloned()
            .collect();
        let compiled = glob_text
            .iter()
            .map(|pattern| compile_glob(pattern))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Self {
            path_text,
            path_prefixes,
            glob_text,
            globs: compiled,
            exclude_tests,
        })
    }

    fn matches(&self, relative: &str) -> bool {
        let test = target::is_test_path(Path::new(relative));
        if self.exclude_tests && test {
            return false;
        }
        if !self.path_prefixes.is_empty()
            && !self.path_prefixes.iter().any(|prefix| {
                !prefix.is_empty()
                    && (relative == prefix
                        || relative
                            .strip_prefix(prefix)
                            .is_some_and(|suffix| suffix.starts_with('/')))
            })
        {
            return false;
        }
        if self.globs.is_empty() {
            return true;
        }
        let basename = Path::new(relative)
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("");
        self.globs
            .iter()
            .any(|pattern| pattern.is_match(relative) || pattern.is_match(basename))
    }
}

fn compile_glob(pattern: &str) -> Result<GlobMatcher, String> {
    let build = |text: &str| {
        GlobBuilder::new(text)
            .literal_separator(false)
            .backslash_escape(false)
            .build()
    };
    build(pattern)
        .or_else(|_| build(&globset::escape(pattern)))
        .map(|glob| glob.compile_matcher())
        .map_err(|error| format!("invalid path glob {pattern:?}: {error}"))
}

fn invalid(query: &str, reason: &str) -> Value {
    json!({
        "status": "invalid_query",
        "mode": "text",
        "query": query,
        "reason": reason,
        "results": [],
        "match_count": 0,
        "matching_line_count": 0,
        "matching_file_count": 0,
        "returned_line_count": 0,
        "returned_match_count": 0,
        "test_line_count": 0,
        "tracked_line_count": 0,
        "untracked_line_count": 0,
        "truncated": false,
    })
}

fn tracked_hits(root: &Path, query: &str, scope: &Scope) -> Result<Vec<TextHit>, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["grep", "-n", "-I", "-F", "-z", "-e"])
        .arg(query)
        .arg("--")
        .output()
        .map_err(|error| format!("cannot run git grep: {error}"))?;
    if !matches!(output.status.code(), Some(0 | 1)) {
        let error = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        return Err(if error.is_empty() {
            "git grep failed".to_owned()
        } else {
            error
        });
    }
    let mut hits = Vec::new();
    for record in output.stdout.split(|byte| *byte == b'\n') {
        let mut fields = record.splitn(3, |byte| *byte == 0);
        let (Some(relative), Some(line), Some(text)) =
            (fields.next(), fields.next(), fields.next())
        else {
            continue;
        };
        let relative = String::from_utf8_lossy(relative).replace('\\', "/");
        if !scope.matches(&relative) {
            continue;
        }
        let Ok(line) = String::from_utf8_lossy(line).parse::<usize>() else {
            continue;
        };
        let text = String::from_utf8_lossy(text)
            .trim_end_matches('\r')
            .to_owned();
        if let Some(hit) = make_hit(root, query, relative, line, text, true) {
            hits.push(hit);
        }
    }
    Ok(hits)
}

fn untracked_hits(root: &Path, query: &str, scope: &Scope) -> Result<Vec<TextHit>, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["ls-files", "--others", "--exclude-standard", "-z"])
        .output()
        .map_err(|error| format!("cannot run git ls-files: {error}"))?;
    if !output.status.success() {
        let error = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        return Err(if error.is_empty() {
            "git ls-files failed".to_owned()
        } else {
            error
        });
    }
    let mut hits = Vec::new();
    for raw in output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|raw| !raw.is_empty())
    {
        let relative = String::from_utf8_lossy(raw).replace('\\', "/");
        if !scope.matches(&relative) {
            continue;
        }
        let unresolved = root.join(&relative);
        let path = fs::canonicalize(&unresolved).unwrap_or(unresolved);
        if !path.is_file() {
            continue;
        }
        let Ok(mut file) = File::open(&path) else {
            continue;
        };
        let mut prefix = [0_u8; 8192];
        let Ok(read) = file.read(&mut prefix) else {
            continue;
        };
        if prefix[..read].contains(&0) || file.rewind().is_err() {
            continue;
        }
        for (index, line) in BufReader::new(file).split(b'\n').enumerate() {
            let Ok(line) = line else {
                break;
            };
            let text = String::from_utf8_lossy(&line)
                .trim_end_matches('\r')
                .to_owned();
            if let Some(hit) = make_hit(root, query, relative.clone(), index + 1, text, false) {
                hits.push(hit);
            }
        }
    }
    Ok(hits)
}

fn make_hit(
    root: &Path,
    query: &str,
    relative_path: String,
    line: usize,
    text: String,
    tracked: bool,
) -> Option<TextHit> {
    let byte_column = text.find(query)?;
    let occurrences = text.matches(query).count();
    let column = text[..byte_column].chars().count() + 1;
    let unresolved = root.join(&relative_path);
    let path = fs::canonicalize(&unresolved).unwrap_or(unresolved);
    Some(TextHit {
        is_test: target::is_test_path(&path),
        path,
        relative_path,
        line,
        column,
        text,
        occurrences,
        tracked,
        git_status: if tracked { "tracked" } else { "untracked" },
        source: if tracked {
            "git-grep"
        } else {
            "untracked-scan"
        },
        evidence: "lexical",
        text_truncated: false,
        text_start_column: 1,
    })
}

fn bounded_hit(mut hit: TextHit, max_chars: usize) -> TextHit {
    let chars: Vec<_> = hit.text.chars().collect();
    if chars.len() <= max_chars {
        return hit;
    }
    let match_index = hit.column.saturating_sub(1);
    let context = max_chars / 3;
    let mut start = match_index.saturating_sub(context);
    if start + max_chars > chars.len() {
        start = chars.len().saturating_sub(max_chars);
    }
    let end = chars.len().min(start + max_chars);
    let mut window: Vec<_> = chars[start..end].to_vec();
    if start > 0 && max_chars >= 3 {
        window[..3].copy_from_slice(&['.', '.', '.']);
    }
    if end < chars.len() && max_chars >= 3 {
        let length = window.len();
        window[length - 3..].copy_from_slice(&['.', '.', '.']);
    }
    hit.text = window.into_iter().collect();
    hit.text_truncated = true;
    hit.text_start_column = start + 1;
    hit
}

fn normalize_path_prefix(root: &Path, value: &str) -> String {
    let raw = value.trim();
    if raw.is_empty() {
        return String::new();
    }
    let path = Path::new(raw);
    if path.is_absolute() {
        let resolved = fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
        return resolved
            .strip_prefix(root)
            .map(|relative| relative.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| "__outside_repository__".to_owned())
            .trim_end_matches('/')
            .to_owned();
    }
    raw.replace('\\', "/")
        .trim_start_matches("./")
        .trim_matches('/')
        .to_owned()
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::process::Command;

    use tempfile::TempDir;

    use super::search;

    #[test]
    fn counts_tracked_untracked_and_filtered_exact_text() {
        let temporary = TempDir::new().expect("temporary directory");
        let root = temporary.path();
        git(root, &["init", "-q"]);
        git(root, &["config", "user.email", "codeq@example.invalid"]);
        git(root, &["config", "user.name", "codeq-test"]);
        fs::write(root.join(".gitignore"), "ignored.env\n").expect("gitignore");
        fs::write(
            root.join("app.py"),
            "KEY = 'TARGET'\nprint('TARGET', 'TARGET')\n",
        )
        .expect("source");
        fs::create_dir(root.join("tests")).expect("tests directory");
        fs::write(root.join("tests/test_app.py"), "assert 'TARGET'\n").expect("test");
        fs::write(root.join("untracked.yaml"), "value: TARGET\n").expect("untracked");
        fs::write(root.join("ignored.env"), "TARGET=1\n").expect("ignored");
        git(root, &["add", ".gitignore", "app.py", "tests/test_app.py"]);
        git(root, &["commit", "-qm", "base"]);

        let result = search(root, "TARGET", 3, &[], &[], false).expect("search");
        assert_eq!(result["match_count"], 5);
        assert_eq!(result["matching_line_count"], 4);
        assert_eq!(result["matching_file_count"], 3);
        assert_eq!(result["returned_line_count"], 3);
        assert_eq!(result["tracked_line_count"], 3);
        assert_eq!(result["untracked_line_count"], 1);
        assert_eq!(result["test_line_count"], 1);
        assert_eq!(result["truncated"], true);

        let filtered = search(
            root,
            "TARGET",
            20,
            &["tests".to_owned()],
            &["*.py".to_owned()],
            false,
        )
        .expect("filtered search");
        assert_eq!(filtered["matching_line_count"], 1);
        assert_eq!(filtered["results"][0]["relative_path"], "tests/test_app.py");
    }

    #[test]
    fn bounds_disclosed_lines_without_losing_complete_counts() {
        let temporary = TempDir::new().expect("temporary directory");
        let root = temporary.path();
        git(root, &["init", "-q"]);
        git(root, &["config", "user.email", "codeq@example.invalid"]);
        git(root, &["config", "user.name", "codeq-test"]);
        let long_line = format!("{} NEEDLE {}\n", "x".repeat(1000), "y".repeat(1000));
        for index in 0..4 {
            fs::write(root.join(format!("file{index}.txt")), &long_line).expect("source");
        }
        git(root, &["add", "."]);
        git(root, &["commit", "-qm", "base"]);

        let result = search(root, "NEEDLE", 2, &[], &[], false).expect("search");
        assert_eq!(result["matching_line_count"], 4);
        assert_eq!(result["returned_line_count"], 2);
        assert_eq!(result["truncated"], true);
        for item in result["results"].as_array().expect("results") {
            assert!(item["text"].as_str().expect("text").chars().count() <= 500);
            assert_eq!(item["text_truncated"], true);
        }

        let invalid = search(root, "", 2, &[], &[], false).expect("invalid response");
        assert_eq!(invalid["status"], "invalid_query");
    }

    fn git(root: &std::path::Path, arguments: &[&str]) {
        let status = Command::new("git")
            .arg("-C")
            .arg(root)
            .args(arguments)
            .status()
            .expect("run git");
        assert!(status.success(), "git {arguments:?}");
    }
}
