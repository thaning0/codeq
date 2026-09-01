use std::env;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::process::Command;

pub fn resolve_root(input: &Path) -> Result<PathBuf, String> {
    let expanded = expand_user(input);
    let absolute = if expanded.is_absolute() {
        expanded
    } else {
        env::current_dir()
            .map_err(|error| format!("cannot resolve current directory: {error}"))?
            .join(expanded)
    };
    let resolved = canonicalize_allow_missing(&absolute);
    let output = Command::new("git")
        .arg("-C")
        .arg(&resolved)
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .map_err(|error| format!("cannot run git: {error}"))?;

    if output.status.success() {
        let root = String::from_utf8(output.stdout)
            .map_err(|error| format!("git returned a non-UTF-8 repository root: {error}"))?;
        let root = root.trim();
        if !root.is_empty() {
            let path = PathBuf::from(root);
            return Ok(fs::canonicalize(&path).unwrap_or(path));
        }
    }

    if resolved.is_dir() {
        Ok(resolved)
    } else {
        Ok(resolved.parent().map(Path::to_owned).unwrap_or(resolved))
    }
}

pub fn absolute_path(root: &Path, input: &Path) -> PathBuf {
    let expanded = expand_user(input);
    let joined = if expanded.is_absolute() {
        expanded
    } else {
        root.join(expanded)
    };
    canonicalize_allow_missing(&joined)
}

fn expand_user(path: &Path) -> PathBuf {
    let mut components = path.components();
    if components.next().map(Component::as_os_str) != Some(OsStr::new("~")) {
        return path.to_owned();
    }
    let Some(home) = env::var_os("HOME") else {
        return path.to_owned();
    };
    let mut expanded = PathBuf::from(home);
    expanded.extend(components);
    expanded
}

fn normalize(path: &Path) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            Component::Prefix(_) | Component::RootDir | Component::Normal(_) => {
                normalized.push(component.as_os_str());
            }
        }
    }
    normalized
}

fn canonicalize_allow_missing(path: &Path) -> PathBuf {
    let normalized = normalize(path);
    let mut ancestor = normalized.as_path();
    let mut missing: Vec<OsString> = Vec::new();
    loop {
        if let Ok(mut resolved) = fs::canonicalize(ancestor) {
            for component in missing.iter().rev() {
                resolved.push(component);
            }
            return resolved;
        }
        let (Some(name), Some(parent)) = (ancestor.file_name(), ancestor.parent()) else {
            return normalized;
        };
        missing.push(name.to_owned());
        ancestor = parent;
    }
}
