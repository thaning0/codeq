use std::collections::{HashMap, HashSet};
use std::env;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use crate::lsp::{LspError, LspProcess};

const PROJECT_SCAN_DEPTH: usize = 4;

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub(crate) enum LanguageFamily {
    Python,
    TypeScript,
}

impl LanguageFamily {
    pub(crate) const fn as_str(self) -> &'static str {
        match self {
            Self::Python => "python",
            Self::TypeScript => "typescript",
        }
    }

    fn for_path(path: &Path) -> Option<Self> {
        match path
            .extension()
            .and_then(OsStr::to_str)
            .map(str::to_ascii_lowercase)
            .as_deref()
        {
            Some("py" | "pyi") => Some(Self::Python),
            Some("ts" | "tsx" | "js" | "jsx" | "mjs" | "cjs") => Some(Self::TypeScript),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub(crate) struct Project {
    pub(crate) root: PathBuf,
    pub(crate) family: LanguageFamily,
}

#[derive(Clone)]
struct ServerCommand {
    program: OsString,
    arguments: Vec<OsString>,
    environment: Vec<(OsString, OsString)>,
    name: String,
}

enum ServerLocator {
    SearchPath,
    #[cfg(test)]
    Fixed(ServerCommand),
}

impl ServerLocator {
    fn locate(&self, project: &Project) -> Option<ServerCommand> {
        match self {
            Self::SearchPath => locate_server(project),
            #[cfg(test)]
            Self::Fixed(command) => Some(command.clone()),
        }
    }
}

enum SessionEntry {
    Starting,
    Ready(Arc<LspProcess>),
}

struct SessionRegistry {
    closed: bool,
    starts: u64,
    sessions: HashMap<Project, SessionEntry>,
}

pub(crate) struct Workspace {
    root: PathBuf,
    scratch: PathBuf,
    timeout: Duration,
    projects: Vec<Project>,
    sessions: Mutex<SessionRegistry>,
    session_changed: Condvar,
    locator: ServerLocator,
}

impl Workspace {
    pub(crate) fn new(root: &Path, scratch: PathBuf, timeout: Duration) -> Self {
        let root = fs::canonicalize(root).unwrap_or_else(|_| root.to_owned());
        let projects = discover_projects(&root);
        Self {
            root,
            scratch,
            timeout,
            projects,
            sessions: Mutex::new(SessionRegistry {
                closed: false,
                starts: 0,
                sessions: HashMap::new(),
            }),
            session_changed: Condvar::new(),
            locator: ServerLocator::SearchPath,
        }
    }

    pub(crate) fn project_for_path(&self, path: &Path) -> Option<Project> {
        let path = fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
        let family = LanguageFamily::for_path(&path)?;
        if let Some(project) = self
            .projects
            .iter()
            .filter(|project| project.family == family && path.starts_with(&project.root))
            .max_by_key(|project| project.root.components().count())
            .cloned()
        {
            return Some(project);
        }
        if self.projects.iter().any(|project| project.family == family) {
            None
        } else {
            Some(Project {
                root: self.root.clone(),
                family,
            })
        }
    }

    pub(crate) fn session(&self, project: &Project) -> Result<Arc<LspProcess>, LspError> {
        let deadline = Instant::now() + self.timeout.max(Duration::from_secs(1));
        loop {
            let mut registry = self.lock_sessions();
            if registry.closed {
                return Err(LspError::new(format!(
                    "workspace is closed: {}",
                    self.root.display()
                )));
            }
            match registry.sessions.get(project) {
                Some(SessionEntry::Ready(session)) if session.is_alive() => {
                    return Ok(Arc::clone(session));
                }
                Some(SessionEntry::Starting) => {
                    let remaining = deadline.saturating_duration_since(Instant::now());
                    if remaining.is_zero() {
                        return Err(LspError::new(format!(
                            "timed out waiting for {} language server startup: {}",
                            project.family.as_str(),
                            project.root.display()
                        )));
                    }
                    let waited = self
                        .session_changed
                        .wait_timeout(registry, remaining)
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                    if waited.1.timed_out() {
                        return Err(LspError::new(format!(
                            "timed out waiting for {} language server startup: {}",
                            project.family.as_str(),
                            project.root.display()
                        )));
                    }
                    continue;
                }
                Some(SessionEntry::Ready(_)) => {
                    registry.sessions.remove(project);
                }
                None => {}
            }
            registry
                .sessions
                .insert(project.clone(), SessionEntry::Starting);
            break;
        }

        let started = self.start_session(project);
        let mut registry = self.lock_sessions();
        if registry.closed {
            if let Ok(session) = &started {
                session.close();
            }
            registry.sessions.remove(project);
            self.session_changed.notify_all();
            return Err(LspError::new(format!(
                "workspace is closed: {}",
                self.root.display()
            )));
        }
        match started {
            Ok(session) => {
                let session = Arc::new(session);
                registry.starts += 1;
                registry
                    .sessions
                    .insert(project.clone(), SessionEntry::Ready(Arc::clone(&session)));
                self.session_changed.notify_all();
                Ok(session)
            }
            Err(error) => {
                registry.sessions.remove(project);
                self.session_changed.notify_all();
                Err(error)
            }
        }
    }

    pub(crate) fn close(&self) {
        let sessions = {
            let mut registry = self.lock_sessions();
            if registry.closed {
                return;
            }
            registry.closed = true;
            let sessions = registry
                .sessions
                .drain()
                .filter_map(|(_, entry)| match entry {
                    SessionEntry::Ready(session) => Some(session),
                    SessionEntry::Starting => None,
                })
                .collect::<Vec<_>>();
            self.session_changed.notify_all();
            sessions
        };
        for session in sessions {
            session.close();
        }
    }

    fn start_session(&self, project: &Project) -> Result<LspProcess, LspError> {
        let command = self.locator.locate(project).ok_or_else(|| {
            LspError::new(format!(
                "no {} language server available for {}",
                project.family.as_str(),
                project.root.display()
            ))
        })?;
        LspProcess::start(
            &command.program,
            &command.arguments,
            &project.root,
            &self.scratch,
            &command.environment,
            &command.name,
            self.timeout,
        )
    }

    fn lock_sessions(&self) -> MutexGuard<'_, SessionRegistry> {
        self.sessions
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    #[cfg(test)]
    fn with_server(
        root: &Path,
        scratch: PathBuf,
        timeout: Duration,
        command: ServerCommand,
    ) -> Self {
        let mut workspace = Self::new(root, scratch, timeout);
        workspace.locator = ServerLocator::Fixed(command);
        workspace
    }
}

impl Drop for Workspace {
    fn drop(&mut self) {
        self.close();
    }
}

pub(crate) fn discover_projects(root: &Path) -> Vec<Project> {
    let root = fs::canonicalize(root).unwrap_or_else(|_| root.to_owned());
    let mut projects = HashSet::new();
    scan_projects(&root, &root, 0, &mut projects);
    let mut projects: Vec<_> = projects.into_iter().collect();
    projects.sort();
    projects
}

fn scan_projects(root: &Path, path: &Path, depth: usize, projects: &mut HashSet<Project>) {
    if path != root && path.join(".git").is_file() {
        return;
    }
    let Ok(entries) = fs::read_dir(path) else {
        return;
    };
    let mut names = HashSet::new();
    let mut directories = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name();
        names.insert(name.to_string_lossy().into_owned());
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        if !file_type.is_dir() {
            continue;
        }
        let name_text = name.to_string_lossy();
        if skip_directory(&name_text) || (name_text.starts_with('.') && name_text != ".github") {
            continue;
        }
        directories.push(entry.path());
    }
    if names.contains("pyproject.toml") {
        let pyproject = fs::read_to_string(path.join("pyproject.toml")).unwrap_or_default();
        if pyproject.contains("[tool.basedpyright]")
            || pyproject.contains("[tool.pyright]")
            || pyproject.contains("[project]")
        {
            projects.insert(Project {
                root: path.to_owned(),
                family: LanguageFamily::Python,
            });
        }
    }
    if names.contains("tsconfig.json") {
        projects.insert(Project {
            root: path.to_owned(),
            family: LanguageFamily::TypeScript,
        });
    }
    if depth >= PROJECT_SCAN_DEPTH {
        return;
    }
    directories.sort();
    for directory in directories {
        scan_projects(root, &directory, depth + 1, projects);
    }
}

fn skip_directory(name: &str) -> bool {
    matches!(
        name,
        ".git"
            | ".hg"
            | ".svn"
            | ".venv"
            | "venv"
            | "node_modules"
            | ".next"
            | "dist"
            | "build"
            | "coverage"
            | "__pycache__"
            | ".mypy_cache"
            | ".pytest_cache"
            | "Quant-worktrees"
            | "worktrees"
    )
}

fn locate_server(project: &Project) -> Option<ServerCommand> {
    match project.family {
        LanguageFamily::Python => ["basedpyright-langserver", "pyright-langserver"]
            .into_iter()
            .find_map(|name| {
                executable_on_path(name).map(|program| ServerCommand {
                    program,
                    arguments: vec![OsString::from("--stdio")],
                    environment: Vec::new(),
                    name: name.to_owned(),
                })
            }),
        LanguageFamily::TypeScript => {
            let global = executable_on_path("typescript-language-server");
            let local = project
                .root
                .join("node_modules/.bin/typescript-language-server");
            let vendored = Path::new(env!("CARGO_MANIFEST_DIR"))
                .join(".vendor/node_modules/.bin/typescript-language-server");
            global
                .into_iter()
                .chain([local.into_os_string(), vendored.into_os_string()])
                .find(|candidate| is_executable(Path::new(candidate)))
                .map(|program| ServerCommand {
                    program,
                    arguments: vec![OsString::from("--stdio")],
                    environment: Vec::new(),
                    name: "typescript-language-server".to_owned(),
                })
        }
    }
}

fn executable_on_path(name: &str) -> Option<OsString> {
    let path = env::var_os("PATH")?;
    env::split_paths(&path)
        .map(|directory| directory.join(name))
        .find(|candidate| is_executable(candidate))
        .map(PathBuf::into_os_string)
}

fn is_executable(path: &Path) -> bool {
    fs::metadata(path)
        .map(|metadata| metadata.is_file() && metadata.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use std::env;
    use std::ffi::OsString;
    use std::fs;
    use std::io::{BufReader, Write};
    use std::os::unix::fs::PermissionsExt;
    use std::sync::{Arc, Barrier};
    use std::thread;
    use std::time::Duration;

    use serde_json::{Value, json};
    use tempfile::TempDir;

    use super::{LanguageFamily, ServerCommand, Workspace, discover_projects};
    use crate::lsp::read_message;

    #[test]
    fn discovers_nested_projects_and_selects_the_nearest_root() {
        let temporary = TempDir::new().expect("temporary directory");
        let root = temporary.path();
        fs::write(root.join("pyproject.toml"), "[project]\nname='root'\n").expect("root project");
        fs::create_dir_all(root.join("packages/web/src")).expect("web tree");
        fs::write(root.join("packages/web/tsconfig.json"), "{}\n").expect("TypeScript project");
        fs::create_dir_all(root.join("packages/python/lib")).expect("Python tree");
        fs::write(
            root.join("packages/python/pyproject.toml"),
            "[tool.basedpyright]\n",
        )
        .expect("nested Python project");
        let source = root.join("packages/python/lib/example.py");
        fs::write(&source, "value = 1\n").expect("Python source");

        let projects = discover_projects(root);
        assert_eq!(projects.len(), 3);
        let workspace = Workspace::new(root, root.join("scratch"), Duration::from_secs(1));
        let project = workspace
            .project_for_path(&source)
            .expect("project for source");
        assert_eq!(project.family, LanguageFamily::Python);
        assert_eq!(project.root, root.join("packages/python"));
    }

    #[test]
    fn same_project_start_is_single_flight_and_close_reaps_the_child() {
        if env::var_os("CODEQ_WORKSPACE_LSP_TEST_SERVER").is_some() {
            fake_server();
            return;
        }
        let temporary = TempDir::new().expect("temporary directory");
        fs::write(
            temporary.path().join("pyproject.toml"),
            "[project]\nname='test'\n",
        )
        .expect("project file");
        let executable = env::current_exe().expect("test executable");
        let command = ServerCommand {
            program: executable.into_os_string(),
            arguments: vec![
                OsString::from("--exact"),
                OsString::from(
                    "workspace::tests::same_project_start_is_single_flight_and_close_reaps_the_child",
                ),
                OsString::from("--nocapture"),
                OsString::from("--quiet"),
            ],
            environment: vec![(
                OsString::from("CODEQ_WORKSPACE_LSP_TEST_SERVER"),
                OsString::from("1"),
            )],
            name: "basedpyright-test".to_owned(),
        };
        let workspace = Arc::new(Workspace::with_server(
            temporary.path(),
            temporary.path().join("scratch"),
            Duration::from_secs(2),
            command,
        ));
        let project = workspace.projects[0].clone();
        let barrier = Arc::new(Barrier::new(5));
        let workers: Vec<_> = (0..4)
            .map(|_| {
                let workspace = Arc::clone(&workspace);
                let project = project.clone();
                let barrier = Arc::clone(&barrier);
                thread::spawn(move || {
                    barrier.wait();
                    workspace.session(&project).expect("shared session")
                })
            })
            .collect();
        barrier.wait();
        let sessions: Vec<_> = workers
            .into_iter()
            .map(|worker| worker.join().expect("worker"))
            .collect();
        let pid = sessions[0].pid();
        assert!(sessions.iter().all(|session| session.pid() == pid));
        assert_eq!(workspace.lock_sessions().starts, 1);
        assert_eq!(
            fs::metadata(temporary.path().join("scratch"))
                .expect("scratch metadata")
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        workspace.close();
        assert!(sessions.iter().all(|session| !session.is_alive()));
    }

    #[test]
    #[ignore = "requires basedpyright and typescript-language-server on PATH"]
    fn supported_real_language_servers_complete_document_symbol_round_trips() {
        let temporary = TempDir::new().expect("temporary directory");
        fs::write(
            temporary.path().join("pyproject.toml"),
            "[project]\nname='lsp-smoke'\nversion='0.0.0'\n",
        )
        .expect("Python project");
        let python = temporary.path().join("app.py");
        fs::write(
            &python,
            "def greet(name: str) -> str:\n    return f'Hi {name}'\n",
        )
        .expect("Python source");
        fs::write(
            temporary.path().join("tsconfig.json"),
            "{\"compilerOptions\": {\"strict\": true}}\n",
        )
        .expect("TypeScript project");
        let typescript = temporary.path().join("web.ts");
        fs::write(
            &typescript,
            "export function renderGreeting(name: string): string { return `Hi ${name}`; }\n",
        )
        .expect("TypeScript source");

        let workspace = Workspace::new(
            temporary.path(),
            temporary.path().join("scratch"),
            Duration::from_secs(15),
        );
        for (path, expected) in [(&python, "greet"), (&typescript, "renderGreeting")] {
            let project = workspace
                .project_for_path(path)
                .expect("project for source");
            let session = workspace.session(&project).expect("real language server");
            let symbols = session.document_symbols(path).expect("document symbols");
            assert!(
                symbols
                    .iter()
                    .any(|symbol| symbol.get("name").and_then(Value::as_str) == Some(expected)),
                "{expected} missing from {symbols:?}"
            );
        }
        workspace.close();
    }

    fn fake_server() {
        let stdin = std::io::stdin();
        let mut reader = BufReader::new(stdin.lock());
        let mut stdout = std::io::stdout().lock();
        while let Some(message) = read_message(&mut reader).expect("read client message") {
            let method = message.get("method").and_then(Value::as_str);
            match method {
                Some("initialize") => write_frame(
                    &mut stdout,
                    &json!({
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"capabilities": {}},
                    }),
                ),
                Some("shutdown") => write_frame(
                    &mut stdout,
                    &json!({"jsonrpc": "2.0", "id": message["id"], "result": null}),
                ),
                Some("exit") => break,
                _ => {}
            }
        }
    }

    fn write_frame(writer: &mut impl Write, message: &Value) {
        let body = serde_json::to_vec(message).expect("encode response");
        write!(writer, "Content-Length: {}\r\n\r\n", body.len()).expect("write header");
        writer.write_all(&body).expect("write response");
        writer.flush().expect("flush response");
    }
}
