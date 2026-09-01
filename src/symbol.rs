use std::ffi::OsString;
use std::fs;
use std::os::unix::ffi::OsStringExt;
use std::path::{Path, PathBuf};

use serde::Serialize;
use serde_json::Value;

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
pub(crate) struct Position {
    pub(crate) line: u64,
    pub(crate) character: u64,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
pub(crate) struct Range {
    pub(crate) start: Position,
    pub(crate) end: Position,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub(crate) struct Symbol {
    pub(crate) name: String,
    pub(crate) kind: String,
    pub(crate) container: String,
    pub(crate) path: PathBuf,
    pub(crate) line: u64,
    pub(crate) column: u64,
    pub(crate) range: Range,
    pub(crate) source: &'static str,
    pub(crate) origin: &'static str,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub(crate) struct Location {
    pub(crate) path: PathBuf,
    pub(crate) line: u64,
    pub(crate) column: u64,
    pub(crate) source: &'static str,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum Resolution {
    Found {
        symbol: Box<Symbol>,
        candidates: Vec<Symbol>,
        requested_location: Option<Location>,
        cursor_definition: bool,
    },
    NotFound {
        reason: String,
        candidates: Vec<Symbol>,
    },
    Ambiguous {
        reason: String,
        candidates: Vec<Symbol>,
    },
}

pub(crate) fn flatten_document_symbols(raw: &[Value], path: &Path) -> Vec<Symbol> {
    let path = fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
    let mut symbols = Vec::new();
    for item in raw {
        visit_document_symbol(item, &path, "", &mut symbols);
    }
    symbols
}

fn visit_document_symbol(item: &Value, path: &Path, container: &str, out: &mut Vec<Symbol>) {
    if let Some(location) = item.get("location") {
        if let Some((location_path, range)) = lsp_location(location) {
            out.push(Symbol {
                name: string(item, "name"),
                kind: symbol_kind(item.get("kind")),
                container: item
                    .get("containerName")
                    .and_then(Value::as_str)
                    .unwrap_or(container)
                    .to_owned(),
                line: range.start.line + 1,
                column: range.start.character + 1,
                path: location_path,
                range,
                source: "lsp",
                origin: "document",
            });
        }
        return;
    }
    let range = item
        .get("selectionRange")
        .or_else(|| item.get("range"))
        .and_then(parse_range)
        .unwrap_or_default();
    let full_range = item
        .get("range")
        .and_then(parse_range)
        .unwrap_or_else(|| range.clone());
    let name = string(item, "name");
    out.push(Symbol {
        name: name.clone(),
        kind: symbol_kind(item.get("kind")),
        container: container.to_owned(),
        path: path.to_owned(),
        line: range.start.line + 1,
        column: range.start.character + 1,
        range: full_range,
        source: "lsp",
        origin: "document",
    });
    let semantic_name = if path.extension().and_then(|extension| extension.to_str()) == Some("rs") {
        rust_impl_target(&name).unwrap_or_else(|| name.clone())
    } else {
        name.clone()
    };
    let child_container = if container.is_empty() {
        semantic_name
    } else if name.is_empty() {
        container.to_owned()
    } else {
        format!("{container}.{semantic_name}")
    };
    if let Some(children) = item.get("children").and_then(Value::as_array) {
        for child in children {
            visit_document_symbol(child, path, &child_container, out);
        }
    }
}

fn rust_impl_target(name: &str) -> Option<String> {
    let mut rest = name.strip_prefix("impl")?.trim_start();
    if rest.starts_with('<') {
        let mut depth = 0usize;
        let mut end = None;
        for (index, character) in rest.char_indices() {
            match character {
                '<' => depth += 1,
                '>' => {
                    depth = depth.saturating_sub(1);
                    if depth == 0 {
                        end = Some(index + character.len_utf8());
                        break;
                    }
                }
                _ => {}
            }
        }
        rest = rest.get(end?..)?.trim_start();
    }
    let target = rest.rsplit_once(" for ").map_or(rest, |(_, target)| target);
    let target = target
        .split_once(" where ")
        .map_or(target, |(target, _)| target)
        .trim();
    let leaf = target.rsplit("::").next()?.trim();
    let identifier: String = leaf
        .chars()
        .take_while(|character| character.is_alphanumeric() || *character == '_')
        .collect();
    (!identifier.is_empty()).then_some(identifier)
}

pub(crate) fn lsp_location(raw: &Value) -> Option<(PathBuf, Range)> {
    if let Some(location) = raw.get("location") {
        return lsp_location(location);
    }
    let uri = raw
        .get("uri")
        .or_else(|| raw.get("targetUri"))
        .and_then(Value::as_str)?;
    let range = raw
        .get("range")
        .or_else(|| raw.get("targetSelectionRange"))
        .or_else(|| raw.get("targetRange"))
        .and_then(parse_range)
        .unwrap_or_default();
    Some((file_uri_path(uri)?, range))
}

fn parse_range(raw: &Value) -> Option<Range> {
    Some(Range {
        start: parse_position(raw.get("start")?)?,
        end: parse_position(raw.get("end")?)?,
    })
}

fn parse_position(raw: &Value) -> Option<Position> {
    Some(Position {
        line: raw.get("line")?.as_u64()?,
        character: raw.get("character")?.as_u64()?,
    })
}

fn file_uri_path(uri: &str) -> Option<PathBuf> {
    let encoded = uri.strip_prefix("file://")?;
    let bytes = encoded.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            let high = *bytes.get(index + 1)?;
            let low = *bytes.get(index + 2)?;
            decoded.push(hex(high)? << 4 | hex(low)?);
            index += 3;
        } else {
            decoded.push(bytes[index]);
            index += 1;
        }
    }
    let path = PathBuf::from(OsString::from_vec(decoded));
    Some(fs::canonicalize(&path).unwrap_or(path))
}

fn hex(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        b'A'..=b'F' => Some(value - b'A' + 10),
        _ => None,
    }
}

fn symbol_kind(value: Option<&Value>) -> String {
    match value.and_then(Value::as_u64) {
        Some(1) => "File",
        Some(2) => "Module",
        Some(3) => "Namespace",
        Some(4) => "Package",
        Some(5) => "Class",
        Some(6) => "Method",
        Some(7) => "Property",
        Some(8) => "Field",
        Some(9) => "Constructor",
        Some(10) => "Enum",
        Some(11) => "Interface",
        Some(12) => "Function",
        Some(13) => "Variable",
        Some(14) => "Constant",
        Some(15) => "String",
        Some(16) => "Number",
        Some(17) => "Boolean",
        Some(18) => "Array",
        Some(19) => "Object",
        Some(20) => "Key",
        Some(21) => "Null",
        Some(22) => "EnumMember",
        Some(23) => "Struct",
        Some(24) => "Event",
        Some(25) => "Operator",
        Some(26) => "TypeParameter",
        Some(kind) => return format!("Kind{kind}"),
        None => return "Unknown".to_owned(),
    }
    .to_owned()
}

fn string(value: &Value, key: &str) -> String {
    value
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned()
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use serde_json::json;

    use super::flatten_document_symbols;

    #[test]
    fn flattens_hierarchical_symbols_with_semantic_containers() {
        let raw = json!([{
            "name": "Greeter",
            "kind": 5,
            "range": {"start": {"line": 2, "character": 0}, "end": {"line": 5, "character": 0}},
            "selectionRange": {"start": {"line": 2, "character": 6}, "end": {"line": 2, "character": 13}},
            "children": [{
                "name": "greet",
                "kind": 6,
                "range": {"start": {"line": 3, "character": 4}, "end": {"line": 4, "character": 20}},
                "selectionRange": {"start": {"line": 3, "character": 8}, "end": {"line": 3, "character": 13}}
            }]
        }]);
        let symbols = flatten_document_symbols(raw.as_array().expect("array"), Path::new("app.py"));
        assert_eq!(symbols.len(), 2);
        assert_eq!(symbols[0].name, "Greeter");
        assert_eq!(symbols[0].line, 3);
        assert_eq!(symbols[1].name, "greet");
        assert_eq!(symbols[1].kind, "Method");
        assert_eq!(symbols[1].container, "Greeter");
        assert_eq!(symbols[1].line, 4);
        assert_eq!(symbols[1].column, 9);
    }

    #[test]
    fn normalizes_rust_impl_blocks_to_type_containers() {
        let raw = json!([{
            "name": "impl<T> Display for crate::Greeter<T>",
            "kind": 19,
            "range": {"start": {"line": 2, "character": 0}, "end": {"line": 5, "character": 0}},
            "selectionRange": {"start": {"line": 2, "character": 0}, "end": {"line": 2, "character": 38}},
            "children": [{
                "name": "fmt",
                "kind": 6,
                "range": {"start": {"line": 3, "character": 4}, "end": {"line": 4, "character": 20}},
                "selectionRange": {"start": {"line": 3, "character": 7}, "end": {"line": 3, "character": 10}}
            }]
        }]);
        let symbols =
            flatten_document_symbols(raw.as_array().expect("array"), Path::new("src/lib.rs"));
        assert_eq!(symbols[1].container, "Greeter");
    }
}
