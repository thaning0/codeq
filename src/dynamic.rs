use std::collections::HashSet;
use std::fs;
use std::path::Path;

use serde_json::Value;

pub(crate) fn classify_references(
    references: &[Value],
    symbol_name: &str,
    probe_limit: usize,
) -> Vec<Value> {
    let mut classified = Vec::new();
    let mut seen = HashSet::new();
    for reference in references {
        let Some(path) = reference.get("path").and_then(Value::as_str).map(Path::new) else {
            continue;
        };
        let extension = path
            .extension()
            .and_then(|extension| extension.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        if !matches!(
            extension.as_str(),
            "py" | "pyi" | "ts" | "tsx" | "js" | "jsx" | "mjs" | "cjs"
        ) {
            continue;
        }
        let line = reference.get("line").and_then(Value::as_u64).unwrap_or(0);
        let column = reference.get("column").and_then(Value::as_u64).unwrap_or(1);
        let Some(text) = source_line(path, line) else {
            continue;
        };
        let Some(reason) = classify_line(&text, column, symbol_name, &extension) else {
            continue;
        };
        if !seen.insert((path.to_owned(), line, column, reason)) {
            continue;
        }
        let mut item = reference.clone();
        item["reason"] = Value::String(reason.to_owned());
        item["confidence"] = Value::String("possible".to_owned());
        item["evidence"] = Value::String("possible_dynamic".to_owned());
        item["text"] = Value::String(text.trim().to_owned());
        classified.push(item);
        if classified.len() >= probe_limit {
            break;
        }
    }
    classified
}

pub(crate) fn classify_python_call_reference(reference: &Value, symbol_name: &str) -> Option<bool> {
    let path = reference
        .get("path")
        .and_then(Value::as_str)
        .map(Path::new)?;
    if !matches!(
        path.extension().and_then(|extension| extension.to_str()),
        Some("py" | "pyi")
    ) {
        return None;
    }
    let line_number = reference.get("line").and_then(Value::as_u64)?;
    let column = reference.get("column").and_then(Value::as_u64).unwrap_or(1);
    let line = source_line(path, line_number)?;
    let start = usize::try_from(column.saturating_sub(1)).ok()?;
    let occurrence = line
        .get(start..)
        .and_then(|tail| tail.find(symbol_name).map(|offset| start + offset))?;
    let before = &line[..occurrence];
    let after = &line[occurrence + symbol_name.len()..];
    let trimmed = line.trim_start();
    if trimmed.starts_with("import ") || trimmed.starts_with("from ") {
        return Some(false);
    }
    if (trimmed.starts_with("def ") || trimmed.starts_with("async def ")) && before.contains('=') {
        return None;
    }
    if before.contains("lambda ") {
        return None;
    }
    if after.trim_start().starts_with('(') {
        return Some(true);
    }
    if classify_line(&line, column, symbol_name, "py").is_some() {
        return None;
    }
    Some(false)
}

pub(crate) fn is_python_property(path: &Path, line: u64) -> bool {
    let Ok(source) = fs::read_to_string(path) else {
        return false;
    };
    let lines: Vec<_> = source.lines().collect();
    let Some(mut index) = usize::try_from(line.saturating_sub(1)).ok() else {
        return false;
    };
    while index > 0 {
        index -= 1;
        let previous = lines.get(index).map_or("", |line| line.trim());
        if previous.is_empty() {
            continue;
        }
        return previous.starts_with("@property")
            || previous.starts_with("@cached_property")
            || previous.contains(".setter")
            || previous.contains(".getter")
            || previous.contains(".deleter");
    }
    false
}

fn source_line(path: &Path, requested: u64) -> Option<String> {
    let source = fs::read_to_string(path).ok()?;
    source
        .lines()
        .nth(requested.checked_sub(1)? as usize)
        .map(str::to_owned)
}

fn classify_line(
    line: &str,
    column: u64,
    symbol_name: &str,
    extension: &str,
) -> Option<&'static str> {
    let start = usize::try_from(column.saturating_sub(1)).ok()?;
    let occurrence = line
        .get(start..)
        .and_then(|tail| tail.find(symbol_name).map(|offset| start + offset))
        .or_else(|| line.find(symbol_name))?;
    let before = &line[..occurrence];
    let after = &line[occurrence + symbol_name.len()..];
    let trimmed = line.trim_start();

    if definition_or_import(trimmed, symbol_name, extension) {
        return None;
    }
    if after.trim_start().starts_with('(') {
        return None;
    }
    let open_brace = before.rfind('{');
    let close_brace = before.rfind('}');
    if open_brace.is_some_and(|open| close_brace.is_none_or(|close| close < open))
        && before[open_brace.unwrap_or(0)..].contains(':')
    {
        return Some("mapping_value");
    }
    if type_position(before) {
        return None;
    }
    let open_parenthesis = before.rfind('(');
    let close_parenthesis = before.rfind(')');
    if open_parenthesis.is_some_and(|open| close_parenthesis.is_none_or(|close| close < open)) {
        return Some("callback_argument");
    }
    let assignment = before.trim_end();
    if assignment.ends_with('=') {
        if assignment.ends_with("]=") || assignment.contains("[") && assignment.ends_with("] =") {
            return Some("registry_assignment");
        }
        return Some("assigned_callable");
    }
    if assignment.ends_with("return") {
        return Some("returned_callable");
    }
    if trimmed.starts_with('@') {
        return Some("decorator_reference");
    }
    if open_brace.is_some() || before.rfind('[').is_some() {
        return Some("collection_member");
    }
    None
}

fn definition_or_import(line: &str, symbol_name: &str, extension: &str) -> bool {
    if line.starts_with("import ") || line.starts_with("from ") {
        return true;
    }
    if matches!(extension, "py" | "pyi") {
        return line.starts_with(&format!("def {symbol_name}"))
            || line.starts_with(&format!("async def {symbol_name}"))
            || line.starts_with(&format!("class {symbol_name}"));
    }
    let declaration = ["function", "class", "interface", "type", "enum"]
        .iter()
        .any(|kind| line.contains(&format!("{kind} {symbol_name}")));
    declaration || line.starts_with("import ") || line.starts_with("export {")
}

fn type_position(before: &str) -> bool {
    let before = before.trim_end();
    before.ends_with(':')
        || before.ends_with(" as")
        || before.ends_with(" extends")
        || before.ends_with(" implements")
}

#[cfg(test)]
mod tests {
    use super::classify_line;

    #[test]
    fn separates_dynamic_positions_from_direct_calls_and_types() {
        assert_eq!(
            classify_line("CALLBACKS = {\"order\": dispatch}", 24, "dispatch", "py"),
            Some("mapping_value")
        );
        assert_eq!(
            classify_line("result = invoke(dispatch)", 17, "dispatch", "py"),
            Some("callback_argument")
        );
        assert_eq!(
            classify_line("result = dispatch()", 10, "dispatch", "py"),
            None
        );
        assert_eq!(classify_line("value: dispatch", 8, "dispatch", "ts"), None);
    }
}
