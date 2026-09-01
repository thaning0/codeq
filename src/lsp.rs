use std::collections::{HashMap, VecDeque};
use std::ffi::{OsStr, OsString};
use std::fmt;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, SyncSender};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use std::time::{SystemTime, UNIX_EPOCH};

use nix::sys::signal::{Signal, killpg};
use nix::unistd::{Pid, Uid};
use serde_json::{Value, json};

const MAX_MESSAGE_BYTES: usize = 16 * 1024 * 1024;
const STDERR_LINES: usize = 50;

#[derive(Debug, Clone)]
pub struct LspError(String);

impl fmt::Display for LspError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for LspError {}

type Pending = Arc<Mutex<HashMap<u64, SyncSender<Result<Value, LspError>>>>>;

pub struct LspProcess {
    name: String,
    root: PathBuf,
    timeout: Duration,
    child: Mutex<Child>,
    writer: Arc<Mutex<ChildStdin>>,
    pending: Pending,
    next_id: AtomicU64,
    request_count: AtomicU64,
    document_version: AtomicU64,
    open_documents: Mutex<HashMap<String, (u128, u64)>>,
    navigation: Mutex<()>,
    closed: Arc<AtomicBool>,
    reader: Mutex<Option<JoinHandle<()>>>,
    stderr_reader: Mutex<Option<JoinHandle<()>>>,
    stderr_tail: Arc<Mutex<VecDeque<String>>>,
    server_capabilities: Value,
}

impl LspProcess {
    pub fn start(
        program: &OsStr,
        arguments: &[OsString],
        root: &Path,
        scratch: &Path,
        environment: &[(OsString, OsString)],
        name: &str,
        timeout: Duration,
    ) -> Result<Self, LspError> {
        let root = fs::canonicalize(root)
            .map_err(|error| LspError(format!("cannot resolve LSP root: {error}")))?;
        let temp = prepare_temp_dir(scratch)?;
        let mut command = Command::new(program);
        command
            .args(arguments)
            .current_dir(&root)
            .env("TMPDIR", &temp)
            .env("TEMP", &temp)
            .env("TMP", &temp)
            .envs(environment.iter().cloned())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        let mut child = command
            .spawn()
            .map_err(|error| LspError(format!("cannot start {name}: {error}")))?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| LspError(format!("{name} has no stdin pipe")))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| LspError(format!("{name} has no stdout pipe")))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| LspError(format!("{name} has no stderr pipe")))?;

        let writer = Arc::new(Mutex::new(stdin));
        let pending = Arc::new(Mutex::new(HashMap::new()));
        let closed = Arc::new(AtomicBool::new(false));
        let stderr_tail = Arc::new(Mutex::new(VecDeque::new()));
        let settings = settings_for_server(name);
        let reader = spawn_reader(
            stdout,
            Arc::clone(&writer),
            Arc::clone(&pending),
            Arc::clone(&closed),
            root.clone(),
            settings.clone(),
            name.to_owned(),
        );
        let stderr_reader = spawn_stderr_reader(stderr, Arc::clone(&stderr_tail));

        let mut process = Self {
            name: name.to_owned(),
            root,
            timeout,
            child: Mutex::new(child),
            writer,
            pending,
            next_id: AtomicU64::new(1),
            request_count: AtomicU64::new(0),
            document_version: AtomicU64::new(2),
            open_documents: Mutex::new(HashMap::new()),
            navigation: Mutex::new(()),
            closed,
            reader: Mutex::new(Some(reader)),
            stderr_reader: Mutex::new(Some(stderr_reader)),
            stderr_tail,
            server_capabilities: Value::Null,
        };
        let initialized = process.request_with_timeout(
            "initialize",
            initialize_params(&process.root),
            timeout.max(Duration::from_secs(20)),
        )?;
        process.server_capabilities = initialized
            .get("capabilities")
            .cloned()
            .unwrap_or(Value::Null);
        process.notify("initialized", json!({}))?;
        if settings != json!({}) {
            process.notify(
                "workspace/didChangeConfiguration",
                json!({"settings": settings}),
            )?;
        }
        Ok(process)
    }

    pub fn request(&self, method: &str, params: Value) -> Result<Value, LspError> {
        self.request_with_timeout(method, params, self.timeout)
    }

    pub fn request_with_timeout(
        &self,
        method: &str,
        params: Value,
        timeout: Duration,
    ) -> Result<Value, LspError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(LspError(format!("{} language server is closed", self.name)));
        }
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        self.request_count.fetch_add(1, Ordering::Relaxed);
        let (sender, receiver) = mpsc::sync_channel(1);
        lock(&self.pending).insert(id, sender);
        if let Err(error) = send_message(
            &self.writer,
            &json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}),
        ) {
            lock(&self.pending).remove(&id);
            return Err(error);
        }
        match receiver.recv_timeout(timeout) {
            Ok(result) => {
                result.map_err(|error| LspError(format!("{} {method}: {error}", self.name)))
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                lock(&self.pending).remove(&id);
                Err(LspError(format!("{} timed out on {method}", self.name)))
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(LspError(format!(
                "{} language server exited during {method}",
                self.name
            ))),
        }
    }

    pub fn notify(&self, method: &str, params: Value) -> Result<(), LspError> {
        send_message(
            &self.writer,
            &json!({"jsonrpc": "2.0", "method": method, "params": params}),
        )
    }

    pub fn pid(&self) -> u32 {
        lock(&self.child).id()
    }

    pub fn is_alive(&self) -> bool {
        lock(&self.child)
            .try_wait()
            .map(|status| status.is_none())
            .unwrap_or(false)
    }

    pub fn capabilities(&self) -> &Value {
        &self.server_capabilities
    }

    pub fn stderr_tail(&self) -> Vec<String> {
        lock(&self.stderr_tail).iter().cloned().collect()
    }

    pub fn request_count(&self) -> u64 {
        self.request_count.load(Ordering::Relaxed)
    }

    pub fn ensure_open(&self, path: &Path) -> Result<(), LspError> {
        let path = fs::canonicalize(path)
            .map_err(|error| LspError(format!("cannot open {}: {error}", path.display())))?;
        let language = language_id(&path)
            .ok_or_else(|| LspError(format!("unsupported source language: {}", path.display())))?;
        let metadata = fs::metadata(&path)
            .map_err(|error| LspError(format!("cannot inspect {}: {error}", path.display())))?;
        let modified = metadata
            .modified()
            .unwrap_or(SystemTime::UNIX_EPOCH)
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let marker = (modified, metadata.len());
        let uri = file_uri(&path);
        let previous = lock(&self.open_documents).get(&uri).copied();
        if previous == Some(marker) {
            return Ok(());
        }
        let text = String::from_utf8_lossy(
            &fs::read(&path)
                .map_err(|error| LspError(format!("cannot read {}: {error}", path.display())))?,
        )
        .into_owned();
        if previous.is_none() {
            self.notify(
                "textDocument/didOpen",
                json!({
                    "textDocument": {
                        "uri": uri,
                        "languageId": language,
                        "version": 1,
                        "text": text,
                    }
                }),
            )?;
        } else {
            let version = self.document_version.fetch_add(1, Ordering::Relaxed);
            self.notify(
                "textDocument/didChange",
                json!({
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                }),
            )?;
        }
        lock(&self.open_documents).insert(uri, marker);
        Ok(())
    }

    pub fn position_params(&self, path: &Path, line: u64, column: u64) -> Result<Value, LspError> {
        self.ensure_open(path)?;
        Ok(json!({
            "textDocument": {"uri": file_uri(path)},
            "position": {
                "line": line.saturating_sub(1),
                "character": column.saturating_sub(1),
            },
        }))
    }

    pub fn workspace_symbols(&self, query: &str) -> Result<Vec<Value>, LspError> {
        let _navigation = lock(&self.navigation);
        self.request_array("workspace/symbol", json!({"query": query}))
    }

    pub fn document_symbols(&self, path: &Path) -> Result<Vec<Value>, LspError> {
        self.ensure_open(path)?;
        self.request_array(
            "textDocument/documentSymbol",
            json!({"textDocument": {"uri": file_uri(path)}}),
        )
    }

    pub fn hover(&self, path: &Path, line: u64, column: u64) -> Result<Value, LspError> {
        self.request(
            "textDocument/hover",
            self.position_params(path, line, column)?,
        )
    }

    pub fn definitions(&self, path: &Path, line: u64, column: u64) -> Result<Vec<Value>, LspError> {
        let result = self.request(
            "textDocument/definition",
            self.position_params(path, line, column)?,
        )?;
        Ok(value_array(result))
    }

    pub fn references(&self, path: &Path, line: u64, column: u64) -> Result<Vec<Value>, LspError> {
        let _navigation = lock(&self.navigation);
        let mut params = self.position_params(path, line, column)?;
        params["context"] = json!({"includeDeclaration": false});
        self.request_array("textDocument/references", params)
    }

    pub fn implementations(
        &self,
        path: &Path,
        line: u64,
        column: u64,
    ) -> Result<Vec<Value>, LspError> {
        let params = self.position_params(path, line, column)?;
        self.request_array("textDocument/implementation", params)
    }

    pub fn prepare_call_hierarchy(
        &self,
        path: &Path,
        line: u64,
        column: u64,
    ) -> Result<Vec<Value>, LspError> {
        let params = self.position_params(path, line, column)?;
        self.request_array("textDocument/prepareCallHierarchy", params)
    }

    pub fn incoming_calls(&self, item: Value) -> Result<Vec<Value>, LspError> {
        let _navigation = lock(&self.navigation);
        self.request_array("callHierarchy/incomingCalls", json!({"item": item}))
    }

    pub fn outgoing_calls(&self, item: Value) -> Result<Vec<Value>, LspError> {
        self.request_array("callHierarchy/outgoingCalls", json!({"item": item}))
    }

    fn request_array(&self, method: &str, params: Value) -> Result<Vec<Value>, LspError> {
        self.request(method, params).map(value_array)
    }

    pub fn close(&self) {
        if !self.closed.load(Ordering::Acquire) {
            let _ = self.request_with_timeout("shutdown", Value::Null, Duration::from_secs(1));
            let _ = self.notify("exit", json!({}));
        }
        self.closed.store(true, Ordering::Release);
        terminate_child(&self.child);
        drain_pending(
            &self.pending,
            LspError(format!("{} language server closed", self.name)),
        );
        join_thread(&self.reader);
        join_thread(&self.stderr_reader);
    }
}

impl Drop for LspProcess {
    fn drop(&mut self) {
        self.close();
    }
}

fn spawn_reader<R: Read + Send + 'static>(
    reader: R,
    writer: Arc<Mutex<ChildStdin>>,
    pending: Pending,
    closed: Arc<AtomicBool>,
    root: PathBuf,
    settings: Value,
    name: String,
) -> JoinHandle<()> {
    thread::spawn(move || {
        let mut reader = BufReader::new(reader);
        loop {
            let message = match read_message(&mut reader) {
                Ok(Some(message)) => message,
                Ok(None) => break,
                Err(_) => break,
            };
            if let Some(id) = message.get("id").and_then(Value::as_u64)
                && message.get("method").is_none()
            {
                let result = if let Some(error) = message.get("error") {
                    Err(LspError(error_message(error)))
                } else {
                    Ok(message.get("result").cloned().unwrap_or(Value::Null))
                };
                if let Some(waiter) = lock(&pending).remove(&id) {
                    let _ = waiter.send(result);
                }
                continue;
            }
            if message.get("id").is_some() && message.get("method").is_some() {
                let response = server_request_response(&message, &root, &settings);
                let _ = send_message(&writer, &response);
            }
        }
        closed.store(true, Ordering::Release);
        drain_pending(&pending, LspError(format!("{name} language server exited")));
    })
}

fn spawn_stderr_reader<R: Read + Send + 'static>(
    reader: R,
    tail: Arc<Mutex<VecDeque<String>>>,
) -> JoinHandle<()> {
    thread::spawn(move || {
        for line in BufReader::new(reader).lines().map_while(Result::ok) {
            if line.is_empty() {
                continue;
            }
            let mut tail = lock(&tail);
            tail.push_back(line);
            while tail.len() > STDERR_LINES {
                tail.pop_front();
            }
        }
    })
}

fn read_message(reader: &mut impl BufRead) -> Result<Option<Value>, LspError> {
    let length = loop {
        let mut length = None;
        loop {
            let mut header = String::new();
            let bytes = reader
                .read_line(&mut header)
                .map_err(|error| LspError(format!("cannot read LSP header: {error}")))?;
            if bytes == 0 {
                return Ok(None);
            }
            if header == "\r\n" || header == "\n" {
                break;
            }
            if let Some((name, value)) = header.split_once(':')
                && name.eq_ignore_ascii_case("content-length")
            {
                length = value.trim().parse::<usize>().ok();
            }
        }
        if let Some(length) = length {
            break length;
        }
    };
    if length == 0 || length > MAX_MESSAGE_BYTES {
        return Err(LspError(format!("invalid LSP Content-Length: {length}")));
    }
    let mut body = vec![0; length];
    reader
        .read_exact(&mut body)
        .map_err(|error| LspError(format!("cannot read LSP body: {error}")))?;
    serde_json::from_slice(&body)
        .map(Some)
        .map_err(|error| LspError(format!("language server returned invalid JSON: {error}")))
}

fn send_message(writer: &Mutex<ChildStdin>, message: &Value) -> Result<(), LspError> {
    let body = serde_json::to_vec(message)
        .map_err(|error| LspError(format!("cannot encode LSP message: {error}")))?;
    let mut writer = lock(writer);
    write!(writer, "Content-Length: {}\r\n\r\n", body.len())
        .and_then(|()| writer.write_all(&body))
        .and_then(|()| writer.flush())
        .map_err(|error| LspError(format!("cannot write LSP message: {error}")))
}

fn server_request_response(message: &Value, root: &Path, settings: &Value) -> Value {
    let method = message.get("method").and_then(Value::as_str).unwrap_or("");
    let params = message.get("params").cloned().unwrap_or_else(|| json!({}));
    let result = match method {
        "workspace/configuration" => Value::Array(
            params
                .get("items")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .map(|item| configuration_value(settings, item.get("section")))
                .collect(),
        ),
        "workspace/workspaceFolders" => json!([{
            "uri": file_uri(root),
            "name": root.file_name().and_then(OsStr::to_str).unwrap_or("workspace"),
        }]),
        "workspace/applyEdit" => {
            json!({"applied": false, "failureReason": "codeq is read-only"})
        }
        _ => Value::Null,
    };
    json!({"jsonrpc": "2.0", "id": message["id"], "result": result})
}

fn configuration_value(settings: &Value, section: Option<&Value>) -> Value {
    let Some(section) = section
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    else {
        return settings.clone();
    };
    let mut value = settings;
    for part in section.split('.') {
        let Some(nested) = value.get(part) else {
            return Value::Null;
        };
        value = nested;
    }
    value.clone()
}

fn initialize_params(root: &Path) -> Value {
    json!({
        "processId": std::process::id(),
        "clientInfo": {"name": "codeq", "version": env!("CARGO_PKG_VERSION")},
        "rootUri": file_uri(root),
        "rootPath": root,
        "workspaceFolders": [{
            "uri": file_uri(root),
            "name": root.file_name().and_then(OsStr::to_str).unwrap_or("workspace"),
        }],
        "capabilities": {
            "workspace": {
                "workspaceFolders": true,
                "configuration": true,
                "symbol": {"dynamicRegistration": false},
            },
            "textDocument": {
                "documentSymbol": {"hierarchicalDocumentSymbolSupport": true},
                "definition": {"dynamicRegistration": false},
                "references": {"dynamicRegistration": false},
                "implementation": {"dynamicRegistration": false},
                "hover": {"contentFormat": ["plaintext", "markdown"]},
                "callHierarchy": {"dynamicRegistration": false},
            },
        },
    })
}

fn settings_for_server(name: &str) -> Value {
    let analysis = json!({"analysis": {"diagnosticMode": "openFilesOnly"}});
    let lower = name.to_ascii_lowercase();
    if lower.contains("basedpyright") {
        json!({"basedpyright": analysis})
    } else if lower.contains("pyright") {
        json!({"python": analysis})
    } else {
        json!({})
    }
}

fn language_id(path: &Path) -> Option<&'static str> {
    match path
        .extension()
        .and_then(OsStr::to_str)
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("py" | "pyi") => Some("python"),
        Some("ts") => Some("typescript"),
        Some("tsx") => Some("typescriptreact"),
        Some("js" | "mjs" | "cjs") => Some("javascript"),
        Some("jsx") => Some("javascriptreact"),
        _ => None,
    }
}

fn value_array(value: Value) -> Vec<Value> {
    match value {
        Value::Array(items) => items,
        Value::Object(_) => vec![value],
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => Vec::new(),
    }
}

fn prepare_temp_dir(scratch: &Path) -> Result<PathBuf, LspError> {
    let temp = scratch.join("lsp-tmp");
    fs::create_dir_all(&temp)
        .map_err(|error| LspError(format!("cannot create LSP temp directory: {error}")))?;
    let metadata = fs::metadata(&temp)
        .map_err(|error| LspError(format!("cannot inspect LSP temp directory: {error}")))?;
    if metadata.uid() != Uid::current().as_raw() {
        return Err(LspError(format!(
            "LSP temp directory is not owned by current user: {}",
            temp.display()
        )));
    }
    fs::set_permissions(&temp, fs::Permissions::from_mode(0o700))
        .map_err(|error| LspError(format!("cannot protect LSP temp directory: {error}")))?;
    Ok(temp)
}

fn file_uri(path: &Path) -> String {
    let resolved = fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
    let bytes = resolved.as_os_str().as_encoded_bytes();
    let mut uri = String::from("file://");
    for byte in bytes {
        if byte.is_ascii_alphanumeric() || matches!(*byte, b'/' | b':' | b'-' | b'_' | b'.' | b'~')
        {
            uri.push(char::from(*byte));
        } else {
            use std::fmt::Write as _;
            let _ = write!(uri, "%{byte:02X}");
        }
    }
    uri
}

fn error_message(error: &Value) -> String {
    error
        .get("message")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| error.to_string())
}

fn drain_pending(pending: &Pending, error: LspError) {
    for (_, waiter) in lock(pending).drain() {
        let _ = waiter.send(Err(error.clone()));
    }
}

fn terminate_child(child: &Mutex<Child>) {
    let mut child = lock(child);
    if child.try_wait().ok().flatten().is_some() {
        return;
    }
    let pid = Pid::from_raw(child.id() as i32);
    let _ = killpg(pid, Signal::SIGTERM);
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        if child.try_wait().ok().flatten().is_some() {
            return;
        }
        thread::sleep(Duration::from_millis(20));
    }
    let _ = killpg(pid, Signal::SIGKILL);
    let _ = child.wait();
}

fn join_thread(handle: &Mutex<Option<JoinHandle<()>>>) {
    if let Some(handle) = lock(handle).take() {
        let _ = handle.join();
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[cfg(test)]
mod tests {
    use std::env;
    use std::ffi::OsString;
    use std::io::{BufReader, Write};
    use std::time::Duration;

    use serde_json::{Value, json};
    use tempfile::TempDir;

    use super::{LspProcess, read_message};

    #[test]
    fn round_trip_timeout_and_shutdown() {
        if env::var_os("CODEQ_LSP_TEST_SERVER").is_some() {
            fake_server();
            return;
        }
        let temporary = TempDir::new().expect("temporary directory");
        let executable = env::current_exe().expect("test executable");
        let arguments = [
            OsString::from("--exact"),
            OsString::from("lsp::tests::round_trip_timeout_and_shutdown"),
            OsString::from("--nocapture"),
            OsString::from("--quiet"),
        ];
        let environment = [(OsString::from("CODEQ_LSP_TEST_SERVER"), OsString::from("1"))];
        let process = LspProcess::start(
            executable.as_os_str(),
            &arguments,
            temporary.path(),
            temporary.path(),
            &environment,
            "basedpyright-test",
            Duration::from_secs(2),
        )
        .expect("start fake language server");
        assert!(process.is_alive());
        assert!(process.pid() > 1);
        assert_eq!(process.capabilities()["hoverProvider"], Value::Bool(true));
        assert!(process.stderr_tail().is_empty());
        let echoed = process
            .request("test/echo", json!({"text": "你好, LSP"}))
            .expect("echo response");
        assert_eq!(echoed, json!({"text": "你好, LSP"}));
        let timeout =
            process.request_with_timeout("test/never", json!({}), Duration::from_millis(30));
        assert!(
            timeout
                .expect_err("request should time out")
                .to_string()
                .contains("timed out")
        );
        process.close();
        assert!(!process.is_alive());
    }

    fn fake_server() {
        let stdin = std::io::stdin();
        let mut reader = BufReader::new(stdin.lock());
        let mut stdout = std::io::stdout().lock();
        let mut configuration_seen = false;
        while let Some(message) = read_message(&mut reader).expect("read client message") {
            let method = message.get("method").and_then(Value::as_str);
            if message.get("id").and_then(Value::as_u64) == Some(900) && method.is_none() {
                assert_eq!(
                    message["result"],
                    json!([{"diagnosticMode": "openFilesOnly"}])
                );
                configuration_seen = true;
                continue;
            }
            let response = match method {
                Some("initialize") => {
                    write_frame(
                        &mut stdout,
                        &json!({
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "result": {"capabilities": {"hoverProvider": true}},
                        }),
                    );
                    write_frame(
                        &mut stdout,
                        &json!({
                            "jsonrpc": "2.0",
                            "id": 900,
                            "method": "workspace/configuration",
                            "params": {"items": [{"section": "basedpyright.analysis"}]},
                        }),
                    );
                    None
                }
                Some("test/echo") => Some(json!({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": message["params"],
                })),
                Some("test/never") => None,
                Some("shutdown") => Some(json!({
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": null,
                })),
                Some("exit") => {
                    assert!(configuration_seen);
                    break;
                }
                _ => None,
            };
            if let Some(response) = response {
                write_frame(&mut stdout, &response);
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
