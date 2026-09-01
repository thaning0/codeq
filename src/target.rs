use std::fs;
use std::path::{Path, PathBuf};

use crate::repository;

const PATH_LIKE_SUFFIXES: &[&str] = &[
    "py", "pyi", "ts", "tsx", "js", "jsx", "mjs", "cjs", "sh", "bash", "zsh", "fish", "sql", "ps1",
    "psm1", "go", "rs", "java", "c", "h", "cc", "cpp", "hpp", "cs", "rb", "php", "swift", "kt",
    "kts", "scala", "lua", "r", "jl", "vue", "svelte", "md", "json", "toml", "yaml", "yml",
];
pub const SEMANTIC_SOURCE_SUFFIXES: &[&str] =
    &["py", "pyi", "rs", "ts", "tsx", "js", "jsx", "mjs", "cjs"];

pub struct ExplicitPath {
    pub path: PathBuf,
    pub inside_repository: bool,
    pub line: Option<u64>,
    pub column: Option<u64>,
}

impl ExplicitPath {
    pub const fn has_position(&self) -> bool {
        self.line.is_some() || self.column.is_some()
    }
}

pub fn explicit_path(target: &str, root: &Path) -> Option<ExplicitPath> {
    let (raw_path, line, column) = split_position(target);
    let candidate = Path::new(raw_path);
    let suffix = candidate
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default();
    let path_like = candidate.is_absolute()
        || raw_path.contains('/')
        || raw_path.contains('\\')
        || raw_path.starts_with("./")
        || raw_path.starts_with("../")
        || raw_path.starts_with("~/")
        || PATH_LIKE_SUFFIXES
            .iter()
            .any(|known| suffix.eq_ignore_ascii_case(known));
    if !path_like {
        return None;
    }

    let path = repository::absolute_path(root, candidate);
    let inside_repository = path.starts_with(root);
    Some(ExplicitPath {
        path,
        inside_repository,
        line,
        column,
    })
}

pub fn is_semantic_source(path: &Path) -> bool {
    path.extension()
        .and_then(|value| value.to_str())
        .is_some_and(|suffix| {
            SEMANTIC_SOURCE_SUFFIXES
                .iter()
                .any(|known| suffix.eq_ignore_ascii_case(known))
        })
}

pub fn is_test_path(path: &Path) -> bool {
    let value = format!(
        "/{}",
        path.to_string_lossy()
            .to_ascii_lowercase()
            .replace('\\', "/")
    );
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    value.contains("/tests/")
        || value.contains("/test/")
        || value.contains("/__tests__/")
        || name.starts_with("test_")
        || name.ends_with("_test.py")
        || name.contains(".test.")
        || name.contains(".spec.")
}

pub fn is_test_location(path: &Path, line: u64) -> bool {
    if is_test_path(path) {
        return true;
    }
    if path.extension().and_then(|extension| extension.to_str()) != Some("rs") {
        return false;
    }
    let Ok(source) = fs::read_to_string(path) else {
        return false;
    };
    rust_test_line(&source, line)
}

fn rust_test_line(source: &str, line: u64) -> bool {
    let target = line.saturating_sub(1) as usize;
    let mut depth = 0i64;
    let mut test_scopes = Vec::new();
    let mut pending_cfg_test = false;
    let mut pending_test = false;
    let mut awaiting_scope = false;
    for (index, text) in source.lines().enumerate() {
        test_scopes.retain(|scope| depth >= *scope);
        let inside_test = !test_scopes.is_empty();
        let trimmed = text.trim();
        let compact: String = trimmed
            .chars()
            .filter(|character| !character.is_whitespace())
            .collect();
        if compact.starts_with("#[") {
            pending_cfg_test |= compact.starts_with("#[cfg(") && compact.contains("test");
            pending_test |= rust_test_attribute(&compact);
            if index == target {
                return inside_test || pending_cfg_test || pending_test;
            }
            continue;
        }
        if trimmed.is_empty() || trimmed.starts_with("//") {
            if index == target && inside_test {
                return true;
            }
            continue;
        }

        let opens = text.chars().filter(|character| *character == '{').count() as i64;
        let closes = text.chars().filter(|character| *character == '}').count() as i64;
        let declares_test_scope = (pending_cfg_test && contains_rust_item(trimmed, "mod"))
            || (pending_test && contains_rust_item(trimmed, "fn"));
        let current_is_test = inside_test || declares_test_scope || awaiting_scope;
        if index == target && current_is_test {
            return true;
        }
        if declares_test_scope && opens == 0 {
            awaiting_scope = true;
        }
        if (declares_test_scope || awaiting_scope) && opens > 0 {
            test_scopes.push(depth + 1);
            awaiting_scope = false;
        }
        pending_cfg_test = false;
        pending_test = false;
        depth += opens - closes;
        if index >= target {
            break;
        }
    }
    false
}

fn rust_test_attribute(compact: &str) -> bool {
    let attribute = compact
        .strip_prefix("#[")
        .and_then(|value| value.strip_suffix(']'))
        .unwrap_or(compact);
    let path = attribute.split('(').next().unwrap_or(attribute);
    matches!(
        path.rsplit("::").next().unwrap_or(path),
        "test" | "rstest" | "test_case"
    )
}

fn contains_rust_item(line: &str, keyword: &str) -> bool {
    line.split(|character: char| !(character.is_alphanumeric() || character == '_'))
        .any(|part| part == keyword)
}

pub fn source_suffix(path: &Path) -> String {
    path.extension()
        .and_then(|value| value.to_str())
        .map_or_else(|| "<no extension>".to_owned(), |value| format!(".{value}"))
}

pub fn qualified_symbol_parts(value: &str) -> Option<Vec<&str>> {
    if value.replace("::", "").contains(':') {
        return None;
    }
    let parts: Vec<_> = value
        .split("::")
        .flat_map(|segment| segment.split('.'))
        .filter(|part| !part.is_empty())
        .collect();
    (parts.len() >= 2 && parts.iter().all(|part| identifier(part))).then_some(parts)
}

fn identifier(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphabetic() || matches!(byte, b'_' | b'$'))
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'$'))
}

fn split_position(target: &str) -> (&str, Option<u64>, Option<u64>) {
    let Some((before_last, last)) = target.rsplit_once(':') else {
        return (target, None, None);
    };
    let Ok(last_number) = last.parse() else {
        return (target, None, None);
    };
    if let Some((path, line)) = before_last.rsplit_once(':')
        && let Ok(line) = line.parse()
    {
        return (path, Some(line), Some(last_number));
    }
    (before_last, Some(last_number), Some(1))
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::Path;

    use tempfile::TempDir;

    use super::{explicit_path, is_test_location, qualified_symbol_parts, split_position};

    #[test]
    fn splits_numeric_positions_from_the_right() {
        assert_eq!(
            split_position("src/main.rs:12"),
            ("src/main.rs", Some(12), Some(1))
        );
        assert_eq!(
            split_position("C:\\repo\\main.rs:12:3"),
            ("C:\\repo\\main.rs", Some(12), Some(3))
        );
        assert_eq!(
            split_position("directory:with-colon/main.rs:12"),
            ("directory:with-colon/main.rs", Some(12), Some(1))
        );
        assert_eq!(
            split_position("module.symbol"),
            ("module.symbol", None, None)
        );
    }

    #[test]
    fn distinguishes_explicit_paths_from_dotted_symbols() {
        let root = Path::new("/repository");
        assert!(explicit_path("Service.run", root).is_none());
        assert!(explicit_path("missing/module:12", root).is_some());
        assert!(explicit_path("missing.py", root).is_some());
        assert!(explicit_path("README", root).is_none());
    }

    #[test]
    fn recognizes_dotted_and_rust_qualified_symbols() {
        assert_eq!(
            qualified_symbol_parts("crate::workspace::LanguageFamily::as_str"),
            Some(vec!["crate", "workspace", "LanguageFamily", "as_str"])
        );
        assert_eq!(
            qualified_symbol_parts("workspace.LanguageFamily.as_str"),
            Some(vec!["workspace", "LanguageFamily", "as_str"])
        );
        assert_eq!(qualified_symbol_parts("src/main.rs:12"), None);
    }

    #[test]
    fn recognizes_inline_rust_test_locations() {
        let temporary = TempDir::new().expect("temporary directory");
        let path = temporary.path().join("lib.rs");
        fs::write(
            &path,
            "fn production() {}\n\n#[test]\nfn direct_test() {\n    production();\n}\n\n#[cfg(test)]\nmod tests {\n    fn helper() {\n        production();\n    }\n}\n",
        )
        .expect("Rust source");
        assert!(!is_test_location(&path, 1));
        assert!(is_test_location(&path, 4));
        assert!(is_test_location(&path, 5));
        assert!(is_test_location(&path, 10));
        assert!(is_test_location(&path, 11));
    }
}
