use std::collections::{HashMap, HashSet};
use std::env;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::lsp::{LspError, LspProcess};
use crate::symbol::{
    Location, Position, Range, Resolution, Symbol, flatten_document_symbols, lsp_location,
};

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

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct FileMarker {
    modified_ns: u128,
    size: u64,
}

struct DocumentEntry {
    marker: FileMarker,
    symbols: Vec<Symbol>,
    last_used: u64,
}

struct DocumentRegistry {
    closed: bool,
    clock: u64,
    hits: u64,
    misses: u64,
    waited: u64,
    evicted: u64,
    cache: HashMap<PathBuf, DocumentEntry>,
    flights: HashSet<(PathBuf, FileMarker)>,
}

pub(crate) struct Workspace {
    root: PathBuf,
    scratch: PathBuf,
    timeout: Duration,
    projects: Vec<Project>,
    sessions: Mutex<SessionRegistry>,
    session_changed: Condvar,
    documents: Mutex<DocumentRegistry>,
    document_changed: Condvar,
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
            documents: Mutex::new(DocumentRegistry {
                closed: false,
                clock: 0,
                hits: 0,
                misses: 0,
                waited: 0,
                evicted: 0,
                cache: HashMap::new(),
                flights: HashSet::new(),
            }),
            document_changed: Condvar::new(),
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

    pub(crate) fn document_symbols(
        &self,
        path: &Path,
        project: Option<&Project>,
    ) -> Result<Vec<Symbol>, LspError> {
        let path = fs::canonicalize(path)
            .map_err(|error| LspError::new(format!("cannot resolve source file: {error}")))?;
        let deadline = Instant::now() + self.timeout.max(Duration::from_secs(1));
        let marker = file_marker(&path)?;
        let flight = (path.clone(), marker);
        loop {
            let mut documents = self.lock_documents();
            if documents.closed {
                return Err(LspError::new(format!(
                    "workspace is closed: {}",
                    self.root.display()
                )));
            }
            if documents
                .cache
                .get(&path)
                .is_some_and(|entry| entry.marker == marker)
            {
                documents.clock += 1;
                let clock = documents.clock;
                let entry = documents
                    .cache
                    .get_mut(&path)
                    .expect("checked document cache entry must exist");
                entry.last_used = clock;
                let symbols = entry.symbols.clone();
                documents.hits += 1;
                return Ok(symbols);
            }
            if documents.flights.insert(flight.clone()) {
                documents.misses += 1;
                break;
            }
            documents.waited += 1;
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(LspError::new(format!(
                    "timed out waiting for document symbols: {}",
                    path.display()
                )));
            }
            let waited = self
                .document_changed
                .wait_timeout(documents, remaining)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if waited.1.timed_out() {
                return Err(LspError::new(format!(
                    "timed out waiting for document symbols: {}",
                    path.display()
                )));
            }
        }

        let fetched = (|| {
            let selected = project.cloned().or_else(|| self.project_for_path(&path));
            let Some(selected) = selected else {
                return Ok(Vec::new());
            };
            let raw = self.session(&selected)?.document_symbols(&path)?;
            Ok(flatten_document_symbols(&raw, &path))
        })();
        let mut documents = self.lock_documents();
        documents.flights.remove(&flight);
        self.document_changed.notify_all();
        let symbols = fetched?;
        if documents.closed {
            return Err(LspError::new(format!(
                "workspace is closed: {}",
                self.root.display()
            )));
        }
        documents.clock += 1;
        let clock = documents.clock;
        documents.cache.insert(
            path,
            DocumentEntry {
                marker,
                symbols: symbols.clone(),
                last_used: clock,
            },
        );
        if documents.cache.len() > 256
            && let Some(victim) = documents
                .cache
                .iter()
                .min_by_key(|(_, entry)| entry.last_used)
                .map(|(path, _)| path.clone())
        {
            documents.cache.remove(&victim);
            documents.evicted += 1;
        }
        Ok(symbols)
    }

    pub(crate) fn resolve_qualified(&self, target: &str) -> Resolution {
        let parts: Vec<_> = target.split('.').filter(|part| !part.is_empty()).collect();
        if parts.len() < 2 {
            return Resolution::NotFound {
                reason: format!("qualified target not found: {target}"),
                candidates: Vec::new(),
            };
        }
        let container_name = parts[parts.len() - 2];
        let member_name = parts[parts.len() - 1];
        let leaf_candidates = self.exact_document_candidates(member_name, 80);
        let mut matches = Vec::new();
        for symbol in &leaf_candidates {
            let mut semantic_parts: Vec<_> = symbol
                .container
                .split('.')
                .filter(|part| !part.is_empty())
                .collect();
            semantic_parts.push(member_name);
            if parts.ends_with(&semantic_parts)
                && self.module_qualifier_matches(
                    &symbol.path,
                    &parts[..parts.len() - semantic_parts.len()],
                )
            {
                matches.push(symbol.clone());
            }
        }

        let container_kinds = [
            "Class",
            "Interface",
            "Struct",
            "Enum",
            "Namespace",
            "Module",
        ];
        let containers: Vec<_> = self
            .exact_document_candidates(container_name, 80)
            .into_iter()
            .filter(|symbol| {
                symbol.name == container_name && container_kinds.contains(&symbol.kind.as_str())
            })
            .collect();
        let mut seen_files = HashSet::new();
        for container in &containers {
            if !seen_files.insert(container.path.clone()) {
                continue;
            }
            let Some(project) = self.project_for_path(&container.path) else {
                continue;
            };
            let Ok(symbols) = self.document_symbols(&container.path, Some(&project)) else {
                continue;
            };
            for symbol in symbols {
                if symbol.name != member_name {
                    continue;
                }
                let mut semantic_parts: Vec<_> = symbol
                    .container
                    .split('.')
                    .filter(|part| !part.is_empty())
                    .collect();
                semantic_parts.push(member_name);
                if parts.ends_with(&semantic_parts)
                    && self.module_qualifier_matches(
                        &symbol.path,
                        &parts[..parts.len() - semantic_parts.len()],
                    )
                {
                    matches.push(symbol);
                }
            }
        }
        if matches.is_empty() {
            let reason = if containers.is_empty() {
                format!("qualified target not found: {target}")
            } else {
                format!("qualified member not found in {container_name}: {member_name}")
            };
            let mut candidates = leaf_candidates;
            candidates.sort_by(|left, right| {
                let left_owner = left.container.rsplit('.').next() != Some(container_name);
                let right_owner = right.container.rsplit('.').next() != Some(container_name);
                (
                    left_owner,
                    std::cmp::Reverse(definition_priority(left)),
                    &left.path,
                    left.line,
                )
                    .cmp(&(
                        right_owner,
                        std::cmp::Reverse(definition_priority(right)),
                        &right.path,
                        right.line,
                    ))
            });
            candidates.truncate(4);
            return Resolution::NotFound { reason, candidates };
        }
        matches.sort_by(|left, right| {
            (
                std::cmp::Reverse(definition_priority(left)),
                &left.path,
                left.line,
            )
                .cmp(&(
                    std::cmp::Reverse(definition_priority(right)),
                    &right.path,
                    right.line,
                ))
        });
        matches.dedup_by(|left, right| {
            left.path == right.path && left.line == right.line && left.name == right.name
        });
        let best_priority = definition_priority(&matches[0]);
        let top: Vec<_> = matches
            .iter()
            .filter(|symbol| definition_priority(symbol) == best_priority)
            .cloned()
            .collect();
        let unique: HashSet<_> = top
            .iter()
            .map(|symbol| (&symbol.path, symbol.line))
            .collect();
        if unique.len() > 1 {
            return Resolution::Ambiguous {
                reason: "multiple exact qualified definitions found".to_owned(),
                candidates: top.into_iter().take(8).collect(),
            };
        }
        let symbol = top[0].clone();
        let candidates = matches
            .into_iter()
            .filter(|candidate| {
                candidate.path != symbol.path
                    || candidate.line != symbol.line
                    || candidate.name != symbol.name
            })
            .take(4)
            .collect();
        Resolution::Found {
            symbol: Box::new(symbol),
            candidates,
            requested_location: None,
            cursor_definition: false,
        }
    }

    pub(crate) fn resolve_location(
        &self,
        path: &Path,
        line: u64,
        column: u64,
        explicit_column: bool,
    ) -> Resolution {
        let path = fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
        let requested = Location {
            path: path.clone(),
            line,
            column,
            source: "explicit",
        };
        let Some(project) = self.project_for_path(&path) else {
            return Resolution::Found {
                symbol: Box::new(explicit_location_symbol(&path, line, column)),
                candidates: Vec::new(),
                requested_location: Some(requested),
                cursor_definition: false,
            };
        };
        let symbols = self
            .document_symbols(&path, Some(&project))
            .unwrap_or_default();
        if explicit_column {
            let definitions = self
                .session(&project)
                .and_then(|session| session.definitions(&path, line, column))
                .unwrap_or_default();
            let mut mapped = Vec::new();
            for definition in definitions {
                let Some((definition_path, range)) = lsp_location(&definition) else {
                    continue;
                };
                if !definition_path.starts_with(&self.root) {
                    continue;
                }
                let location = Location {
                    path: definition_path,
                    line: range.start.line + 1,
                    column: range.start.character + 1,
                    source: "lsp",
                };
                if let Some(symbol) = self.symbol_at_location(&location)
                    && !mapped.iter().any(|candidate: &Symbol| {
                        candidate.path == symbol.path
                            && candidate.line == symbol.line
                            && candidate.name == symbol.name
                    })
                {
                    mapped.push(symbol);
                }
            }
            if mapped.len() == 1 {
                return Resolution::Found {
                    symbol: Box::new(mapped.remove(0)),
                    candidates: Vec::new(),
                    requested_location: Some(requested),
                    cursor_definition: true,
                };
            }
            if mapped.len() > 1 {
                return Resolution::Ambiguous {
                    reason: "multiple definitions found at requested cursor position".to_owned(),
                    candidates: mapped.into_iter().take(8).collect(),
                };
            }
            let mut point_matches: Vec<_> = symbols
                .iter()
                .filter(|symbol| contains_point(symbol, line, column))
                .cloned()
                .collect();
            sort_smallest(&mut point_matches);
            if let Some(symbol) = point_matches.into_iter().next() {
                return Resolution::Found {
                    symbol: Box::new(symbol),
                    candidates: Vec::new(),
                    requested_location: Some(requested),
                    cursor_definition: false,
                };
            }
        }
        let semantic_kinds = [
            "Function",
            "Method",
            "Constructor",
            "Class",
            "Interface",
            "Struct",
            "Enum",
        ];
        let mut containing: Vec<_> = symbols
            .into_iter()
            .filter(|symbol| {
                semantic_kinds.contains(&symbol.kind.as_str()) && contains_line(symbol, line)
            })
            .collect();
        sort_smallest(&mut containing);
        Resolution::Found {
            symbol: Box::new(
                containing
                    .into_iter()
                    .next()
                    .unwrap_or_else(|| explicit_location_symbol(&path, line, column)),
            ),
            candidates: Vec::new(),
            requested_location: Some(requested),
            cursor_definition: false,
        }
    }

    pub(crate) fn close(&self) {
        {
            let mut documents = self.lock_documents();
            documents.closed = true;
            documents.cache.clear();
            documents.flights.clear();
            self.document_changed.notify_all();
        }
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

    fn exact_document_candidates(&self, name: &str, limit: usize) -> Vec<Symbol> {
        let mut candidates = Vec::new();
        for path in exact_definition_files(&self.root, name, limit) {
            let Some(project) = self.project_for_path(&path) else {
                continue;
            };
            let Ok(symbols) = self.document_symbols(&path, Some(&project)) else {
                continue;
            };
            candidates.extend(symbols.into_iter().filter(|symbol| symbol.name == name));
        }
        candidates.sort_by(|left, right| {
            (
                &left.path,
                left.line,
                std::cmp::Reverse(definition_priority(left)),
            )
                .cmp(&(
                    &right.path,
                    right.line,
                    std::cmp::Reverse(definition_priority(right)),
                ))
        });
        candidates.dedup_by(|left, right| {
            left.path == right.path && left.line == right.line && left.name == right.name
        });
        candidates
    }

    fn module_qualifier_matches(&self, path: &Path, qualifier: &[&str]) -> bool {
        if qualifier.is_empty() {
            return true;
        }
        let Ok(relative) = path.strip_prefix(&self.root) else {
            return false;
        };
        let mut parts: Vec<_> = relative
            .with_extension("")
            .components()
            .map(|component| component.as_os_str().to_string_lossy().into_owned())
            .collect();
        if parts.last().is_some_and(|part| part == "__init__") {
            parts.pop();
        }
        parts.len() >= qualifier.len()
            && parts[parts.len() - qualifier.len()..]
                .iter()
                .map(String::as_str)
                .eq(qualifier.iter().copied())
    }

    fn symbol_at_location(&self, location: &Location) -> Option<Symbol> {
        let project = self.project_for_path(&location.path)?;
        let symbols = self.document_symbols(&location.path, Some(&project)).ok()?;
        let mut candidates: Vec<_> = symbols
            .iter()
            .filter(|symbol| contains_point(symbol, location.line, location.column))
            .cloned()
            .collect();
        if candidates.is_empty() {
            candidates.extend(
                symbols
                    .into_iter()
                    .filter(|symbol| symbol.line == location.line),
            );
        }
        sort_smallest(&mut candidates);
        candidates.into_iter().next()
    }

    fn lock_sessions(&self) -> MutexGuard<'_, SessionRegistry> {
        self.sessions
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn lock_documents(&self) -> MutexGuard<'_, DocumentRegistry> {
        self.documents
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

fn exact_definition_files(root: &Path, name: &str, limit: usize) -> Vec<PathBuf> {
    if !is_identifier(name) {
        return Vec::new();
    }
    let escaped = regex_escape(name);
    let patterns = [
        format!(r"^\s*(?:async\s+def|def|class)\s+{escaped}\b"),
        format!(
            r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?(?:function|class|interface|type|enum)\s+{escaped}\b"
        ),
        format!(r"^\s*(?:export\s+)?(?:const|let|var)\s+{escaped}\b"),
    ];
    let mut command = Command::new("rg");
    command
        .current_dir(root)
        .args(["--files-with-matches", "--null", "--hidden"])
        .args(["-g", "*.py", "-g", "*.pyi", "-g", "*.ts", "-g", "*.tsx"])
        .args(["-g", "*.js", "-g", "*.jsx", "-g", "!node_modules/**"])
        .args(["-g", "!.git/**", "-g", "!.next/**", "-g", "!dist/**"])
        .args(["-g", "!build/**", "-g", "!Quant-worktrees/**"])
        .args(["-g", "!worktrees/**", "-g", "!.worktrees/**"]);
    for pattern in &patterns {
        command.arg("-e").arg(pattern);
    }
    command.arg(".");
    let Ok(output) = command.output() else {
        return Vec::new();
    };
    if !matches!(output.status.code(), Some(0 | 1)) {
        return Vec::new();
    }
    let mut paths: Vec<_> = output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|raw| !raw.is_empty())
        .map(|raw| root.join(String::from_utf8_lossy(raw).as_ref()))
        .map(|path| fs::canonicalize(&path).unwrap_or(path))
        .collect();
    paths.sort();
    paths.dedup();
    paths.truncate(limit);
    paths
}

fn is_identifier(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphabetic() || matches!(byte, b'_' | b'$'))
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'$'))
}

fn regex_escape(value: &str) -> String {
    value
        .chars()
        .flat_map(|character| {
            if matches!(
                character,
                '.' | '+' | '*' | '?' | '(' | ')' | '|' | '[' | ']' | '{' | '}' | '^' | '$' | '\\'
            ) {
                vec!['\\', character]
            } else {
                vec![character]
            }
        })
        .collect()
}

fn definition_priority(symbol: &Symbol) -> u8 {
    let base = if matches!(
        symbol.kind.as_str(),
        "Function"
            | "Method"
            | "Class"
            | "Interface"
            | "Enum"
            | "Constructor"
            | "Struct"
            | "TypeParameter"
    ) {
        30
    } else if matches!(symbol.kind.as_str(), "Constant" | "Property" | "Field") {
        20
    } else if symbol.kind == "Variable" {
        10
    } else {
        0
    };
    base + u8::from(symbol.origin == "document") * 2
}

fn contains_line(symbol: &Symbol, line: u64) -> bool {
    symbol.range.start.line < line && line <= symbol.range.end.line + 1
}

fn contains_point(symbol: &Symbol, line: u64, column: u64) -> bool {
    if !contains_line(symbol, line) {
        return false;
    }
    let column0 = column.saturating_sub(1);
    let start_line = symbol.range.start.line + 1;
    let end_line = symbol.range.end.line + 1;
    !(line == start_line && column0 < symbol.range.start.character
        || line == end_line && column0 > symbol.range.end.character)
}

fn sort_smallest(symbols: &mut [Symbol]) {
    symbols.sort_by_key(|symbol| {
        let line_span = symbol
            .range
            .end
            .line
            .saturating_sub(symbol.range.start.line);
        let character_span = if line_span == 0 {
            symbol
                .range
                .end
                .character
                .saturating_sub(symbol.range.start.character)
        } else {
            0
        };
        (
            line_span,
            character_span,
            std::cmp::Reverse(definition_priority(symbol)),
        )
    });
}

fn explicit_location_symbol(path: &Path, line: u64, column: u64) -> Symbol {
    let position = Position {
        line: line.saturating_sub(1),
        character: column.saturating_sub(1),
    };
    Symbol {
        name: path
            .file_name()
            .and_then(OsStr::to_str)
            .unwrap_or("")
            .to_owned(),
        kind: "Location".to_owned(),
        container: String::new(),
        path: path.to_owned(),
        line,
        column,
        range: Range {
            start: position.clone(),
            end: position,
        },
        source: "explicit",
        origin: "explicit",
    }
}

fn file_marker(path: &Path) -> Result<FileMarker, LspError> {
    let metadata = fs::metadata(path)
        .map_err(|error| LspError::new(format!("cannot inspect {}: {error}", path.display())))?;
    let modified_ns = metadata
        .modified()
        .unwrap_or(SystemTime::UNIX_EPOCH)
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    Ok(FileMarker {
        modified_ns,
        size: metadata.len(),
    })
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
    use crate::symbol::Resolution;

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
        let source = temporary.path().join("app.py");
        fs::write(
            &source,
            "def format_greeting() -> str:\n    return 'hi'\n\nclass Greeter:\n    def greet(self) -> str:\n        return format_greeting()\n",
        )
        .expect("source file");
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

        let document_barrier = Arc::new(Barrier::new(5));
        let document_workers: Vec<_> = (0..4)
            .map(|_| {
                let workspace = Arc::clone(&workspace);
                let project = project.clone();
                let source = source.clone();
                let barrier = Arc::clone(&document_barrier);
                thread::spawn(move || {
                    barrier.wait();
                    workspace
                        .document_symbols(&source, Some(&project))
                        .expect("shared document symbols")
                })
            })
            .collect();
        document_barrier.wait();
        let documents: Vec<_> = document_workers
            .into_iter()
            .map(|worker| worker.join().expect("document worker"))
            .collect();
        assert!(
            documents
                .iter()
                .all(|symbols| symbols.iter().any(|symbol| symbol.name == "greet"))
        );
        assert_eq!(sessions[0].request_count(), 2);
        let document_metrics = workspace.lock_documents();
        assert_eq!(document_metrics.misses, 1);
        assert_eq!(document_metrics.hits, 3);
        drop(document_metrics);
        fs::write(
            &source,
            "def format_greeting() -> str:\n    return 'hi'\n\nclass Greeter:\n    def greet(self) -> str:\n        return format_greeting()\n\n# changed\n",
        )
        .expect("changed source file");
        workspace
            .document_symbols(&source, Some(&project))
            .expect("invalidated document symbols");
        assert_eq!(sessions[0].request_count(), 3);
        assert_eq!(workspace.lock_documents().misses, 2);
        match workspace.resolve_qualified("app.Greeter.greet") {
            Resolution::Found { symbol, .. } => {
                assert_eq!(symbol.name, "greet");
                assert_eq!(symbol.container, "Greeter");
            }
            resolution => panic!("unexpected qualified resolution: {resolution:?}"),
        }
        match workspace.resolve_location(&source, 6, 16, true) {
            Resolution::Found {
                symbol,
                cursor_definition,
                ..
            } => {
                assert_eq!(symbol.name, "format_greeting");
                assert!(cursor_definition);
            }
            resolution => panic!("unexpected cursor resolution: {resolution:?}"),
        }
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
            let symbols = workspace
                .document_symbols(path, Some(&project))
                .expect("document symbols");
            assert!(
                symbols.iter().any(|symbol| symbol.name == expected),
                "{expected} missing from {symbols:?}"
            );
            let cached = workspace
                .document_symbols(path, Some(&project))
                .expect("cached document symbols");
            assert_eq!(cached, symbols);
            let qualified = if expected == "greet" {
                "app.greet"
            } else {
                "web.renderGreeting"
            };
            match workspace.resolve_qualified(qualified) {
                Resolution::Found { symbol, .. } => assert_eq!(symbol.name, expected),
                resolution => panic!("unexpected real resolution: {resolution:?}"),
            }
        }
        workspace.close();
    }

    fn fake_server() {
        let stdin = std::io::stdin();
        let mut reader = BufReader::new(stdin.lock());
        let mut stdout = std::io::stdout().lock();
        let mut document_uri = None;
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
                Some("textDocument/didOpen") => {
                    document_uri = message
                        .pointer("/params/textDocument/uri")
                        .and_then(Value::as_str)
                        .map(str::to_owned);
                }
                Some("textDocument/documentSymbol") => write_frame(
                    &mut stdout,
                    &json!({
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": [{
                            "name": "format_greeting",
                            "kind": 12,
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 1, "character": 15}
                            },
                            "selectionRange": {
                                "start": {"line": 0, "character": 4},
                                "end": {"line": 0, "character": 19}
                            }
                        }, {
                            "name": "Greeter",
                            "kind": 5,
                            "range": {
                                "start": {"line": 3, "character": 0},
                                "end": {"line": 5, "character": 32}
                            },
                            "selectionRange": {
                                "start": {"line": 3, "character": 6},
                                "end": {"line": 3, "character": 13}
                            },
                            "children": [{
                                "name": "greet",
                                "kind": 6,
                                "range": {
                                    "start": {"line": 4, "character": 4},
                                    "end": {"line": 5, "character": 32}
                                },
                                "selectionRange": {
                                    "start": {"line": 4, "character": 8},
                                    "end": {"line": 4, "character": 13}
                                }
                            }]
                        }]
                    }),
                ),
                Some("textDocument/definition") => write_frame(
                    &mut stdout,
                    &json!({
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": [{
                            "uri": document_uri.clone().expect("opened document URI"),
                            "range": {
                                "start": {"line": 0, "character": 4},
                                "end": {"line": 0, "character": 19}
                            }
                        }]
                    }),
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
