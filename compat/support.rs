use std::env;
use std::path::{Path, PathBuf};
use std::process::Command;

pub type Result<T> = std::result::Result<T, String>;

pub fn resolve_executable(executable: &Path) -> Result<PathBuf> {
    if executable.components().count() > 1 || executable.is_absolute() {
        return executable
            .canonicalize()
            .map_err(|error| format!("cannot resolve {}: {error}", executable.display()));
    }
    let Some(paths) = env::var_os("PATH") else {
        return Err("PATH is not set".to_owned());
    };
    for directory in env::split_paths(&paths) {
        let candidate = directory.join(executable);
        if candidate.is_file() {
            return candidate
                .canonicalize()
                .map_err(|error| format!("cannot resolve {}: {error}", candidate.display()));
        }
    }
    Err(format!("executable not found: {}", executable.display()))
}

pub fn version(executable: &Path) -> Result<String> {
    let output = Command::new(executable)
        .arg("--version")
        .output()
        .map_err(|error| format!("cannot run {} --version: {error}", executable.display()))?;
    if !output.status.success() {
        return Err(format!(
            "{} --version exited {:?}: {}",
            executable.display(),
            output.status.code(),
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}
