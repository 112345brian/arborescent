use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};
use tree_sitter::{Node, Parser};

#[derive(Default, Serialize)]
pub struct ModuleFacts {
    pub imports: Vec<Import>,
    pub syntax_error: bool,
    #[serde(skip)]
    pub functions: BTreeMap<String, Vec<Call>>,
}

#[derive(Serialize)]
pub struct Import {
    level: usize,
    module: String,
    names: Vec<String>,
    line: usize,
    lazy: bool,
    type_only: bool,
}

pub struct Call {
    pub target: String,
    pub line: usize,
}

fn children(node: Node<'_>) -> Vec<Node<'_>> {
    node.named_children(&mut node.walk()).collect()
}

fn field_nodes<'tree>(node: Node<'tree>, field: &str) -> Vec<Node<'tree>> {
    node.children_by_field_name(field, &mut node.walk())
        .collect()
}

fn text<'a>(node: Node<'_>, source: &'a str) -> &'a str {
    &source[node.byte_range()]
}

fn field_text<'a>(node: Node<'_>, field: &str, source: &'a str) -> Option<&'a str> {
    node.child_by_field_name(field)
        .map(|child| text(child, source))
}

fn import_name<'a>(node: Node<'_>, source: &'a str) -> (&'a str, Option<&'a str>) {
    if node.kind() == "aliased_import" {
        (
            field_text(node, "name", source).expect("valid aliased import"),
            field_text(node, "alias", source),
        )
    } else {
        (text(node, source), None)
    }
}

fn canonical_import(name: &str) -> String {
    name.chars()
        .filter(|character| !character.is_whitespace() && *character != '\\')
        .collect()
}

fn imports(node: Node<'_>, source: &str, lazy: bool, type_only: bool, out: &mut Vec<Import>) {
    let line = node.start_position().row + 1;
    if node.kind() == "import_statement" {
        for name in field_nodes(node, "name") {
            out.push(Import {
                level: 0,
                module: canonical_import(import_name(name, source).0),
                names: vec![],
                line,
                lazy,
                type_only,
            });
        }
        return;
    }
    if node.kind() == "import_from_statement" {
        let raw =
            canonical_import(field_text(node, "module_name", source).expect("valid from import"));
        let module = raw.trim_start_matches('.');
        let mut names: Vec<_> = field_nodes(node, "name")
            .into_iter()
            .map(|name| canonical_import(import_name(name, source).0))
            .collect();
        if names.is_empty() {
            names.push("*".to_owned());
        }
        out.push(Import {
            level: raw.len() - module.len(),
            module: module.to_owned(),
            names,
            line,
            lazy,
            type_only,
        });
        return;
    }
    let type_guard = node.kind() == "if_statement"
        && matches!(
            field_text(node, "condition", source),
            Some("TYPE_CHECKING" | "typing.TYPE_CHECKING")
        );
    let consequence = node.child_by_field_name("consequence");
    for child in children(node) {
        imports(
            child,
            source,
            lazy || node.kind() == "function_definition",
            type_only || (type_guard && Some(child) == consequence),
            out,
        );
    }
}

fn absolute_base(raw: &str, module: &str, is_package: bool) -> Option<String> {
    let tail = raw.trim_start_matches('.');
    let level = raw.len() - tail.len();
    if level == 0 {
        return Some(raw.to_owned());
    }
    let mut parts: Vec<_> = module.split('.').collect();
    let remove = level - usize::from(is_package);
    if remove >= parts.len() {
        return None;
    }
    parts.truncate(parts.len() - remove);
    if !tail.is_empty() {
        parts.push(tail);
    }
    Some(parts.join("."))
}

fn aliases(
    root: Node<'_>,
    source: &str,
    module: &str,
    is_package: bool,
) -> BTreeMap<String, String> {
    let mut aliases = BTreeMap::new();
    for node in children(root) {
        match node.kind() {
            "import_statement" => {
                for item in field_nodes(node, "name") {
                    let (name, alias) = import_name(item, source);
                    let name = canonical_import(name);
                    let top = name.split('.').next().expect("import name");
                    let (local, target) = alias.map_or((top, top), |alias| (alias, name.as_str()));
                    aliases.insert(local.to_owned(), target.to_owned());
                }
            }
            "import_from_statement" => {
                let raw = canonical_import(
                    field_text(node, "module_name", source).expect("valid from import"),
                );
                if let Some(base) = absolute_base(&raw, module, is_package) {
                    for item in field_nodes(node, "name") {
                        let (name, alias) = import_name(item, source);
                        aliases.insert(alias.unwrap_or(name).to_owned(), format!("{base}.{name}"));
                    }
                }
            }
            _ => {}
        }
    }
    aliases
}

fn dotted(node: Node<'_>, source: &str) -> Option<String> {
    match node.kind() {
        "identifier" => Some(text(node, source).to_owned()),
        "attribute" => {
            let object = dotted(node.child_by_field_name("object")?, source)?;
            Some(format!(
                "{object}.{}",
                field_text(node, "attribute", source)?
            ))
        }
        _ => None,
    }
}

fn identifiers<'a>(node: Node<'_>, source: &'a str, names: &mut BTreeSet<&'a str>) {
    if node.kind() == "identifier" {
        names.insert(text(node, source));
    }
    for child in children(node) {
        identifiers(child, source, names);
    }
}

fn walk_body(node: Node<'_>, visit: &mut impl FnMut(Node<'_>)) {
    visit(node);
    if matches!(
        node.kind(),
        "function_definition" | "decorated_definition" | "lambda" | "class_definition"
    ) {
        return;
    }
    for child in children(node) {
        walk_body(child, visit);
    }
}

fn definition_name<'a>(node: Node<'_>, source: &'a str) -> Option<&'a str> {
    let definition = node.child_by_field_name("definition").unwrap_or(node);
    match definition.kind() {
        "function_definition" | "class_definition" => field_text(definition, "name", source),
        _ => None,
    }
}

fn binding_names<'a>(node: Node<'_>, source: &'a str, names: &mut BTreeSet<&'a str>) {
    match node.kind() {
        "assignment" | "augmented_assignment" | "for_statement" | "for_in_clause" => {
            if let Some(left) = node.child_by_field_name("left") {
                identifiers(left, source, names);
            }
        }
        "named_expression" => {
            if let Some(name) = field_text(node, "name", source) {
                names.insert(name);
            }
        }
        _ => {}
    }
}

fn function_calls(
    function: Node<'_>,
    source: &str,
    module: &str,
    aliases: &BTreeMap<String, String>,
) -> Vec<Call> {
    let mut shadowed = BTreeSet::new();
    let parameters = function
        .child_by_field_name("parameters")
        .expect("function parameters");
    identifiers(parameters, source, &mut shadowed);
    let body = function.child_by_field_name("body").expect("function body");
    let mut incomplete = function.child(0).is_some_and(|node| node.kind() == "async");
    walk_body(body, &mut |node| {
        binding_names(node, source, &mut shadowed);
        if let Some(name) = definition_name(node, source) {
            shadowed.insert(name);
        }
        // Skip scopes whose call execution or local imports are outside this model.
        if matches!(
            node.kind(),
            "import_statement"
                | "import_from_statement"
                | "nonlocal_statement"
                | "global_statement"
                | "yield"
        ) {
            incomplete = true;
        }
    });
    if incomplete {
        return vec![];
    }
    let mut calls = Vec::new();
    walk_body(body, &mut |node| {
        if node.kind() != "call" {
            return;
        }
        let Some(name) = node
            .child_by_field_name("function")
            .and_then(|callee| dotted(callee, source))
        else {
            return;
        };
        let (head, rest) = name.split_once('.').unwrap_or((&name, ""));
        if shadowed.contains(head) {
            return;
        }
        let target = match aliases.get(head) {
            Some(target) if rest.is_empty() => target.clone(),
            Some(target) => format!("{target}.{rest}"),
            None if rest.is_empty() => format!("{module}.{head}"),
            None => return,
        };
        calls.push(Call {
            target,
            line: node.start_position().row + 1,
        });
    });
    calls
}

pub fn extract(parser: &mut Parser, source: &str, module: &str, is_package: bool) -> ModuleFacts {
    let tree = parser.parse(source, None).expect("unbounded parse");
    let root = tree.root_node();
    if root.has_error() {
        return ModuleFacts {
            syntax_error: true,
            ..ModuleFacts::default()
        };
    }
    let mut result = ModuleFacts::default();
    imports(root, source, false, false, &mut result.imports);
    let mut aliases = aliases(root, source, module, is_package);
    let mut rebound = BTreeSet::new();
    walk_body(root, &mut |node| binding_names(node, source, &mut rebound));
    for name in &rebound {
        aliases.remove(*name);
    }
    let top_level = children(root);
    for definition in &top_level {
        if let Some(name) = definition_name(*definition, source) {
            aliases.remove(name);
        }
    }
    for function in top_level
        .into_iter()
        .filter(|node| node.kind() == "function_definition")
    {
        let name = field_text(function, "name", source).expect("function name");
        if !rebound.contains(name) {
            result.functions.insert(
                format!("{module}.{name}"),
                function_calls(function, source, module, &aliases),
            );
        }
    }
    result
}
