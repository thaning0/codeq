use std::path::{Path, PathBuf};

use crate::repository;

const PATH_LIKE_SUFFIXES: &[&str] = &[
    "py", "pyi", "ts", "tsx", "js", "jsx", "mjs", "cjs", "sh", "bash", "zsh", "fish", "sql", "ps1",
    "psm1", "go", "rs", "java", "c", "h", "cc", "cpp", "hpp", "cs", "rb", "php", "swift", "kt",
    "kts", "scala", "lua", "r", "jl", "vue", "svelte", "md", "json", "toml", "yaml", "yml",
];

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
            ["py", "pyi", "ts", "tsx", "js", "jsx", "mjs", "cjs"]
                .iter()
                .any(|known| suffix.eq_ignore_ascii_case(known))
        })
}

pub fn source_suffix(path: &Path) -> String {
    path.extension()
        .and_then(|value| value.to_str())
        .map_or_else(|| "<no extension>".to_owned(), |value| format!(".{value}"))
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
    use std::path::Path;

    use super::{explicit_path, split_position};

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
}
