mod facts;

use facts::{extract, ModuleFacts};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::error::Error;
use std::io::{self, BufRead, Write};
use tree_sitter::Parser;

#[derive(Deserialize)]
struct Update {
    upsert: BTreeMap<String, Source>,
    remove: Vec<String>,
    effect_targets: Vec<String>,
}

#[derive(Deserialize)]
struct Source {
    text: String,
    is_package: bool,
}

#[derive(Serialize)]
struct Effect {
    function: String,
    module: String,
    target: String,
    line: usize,
    chain: Vec<String>,
}

#[derive(Default)]
struct Workspace {
    modules: BTreeMap<String, ModuleFacts>,
    // Keep edges to unresolved names so introducing a function activates its callers.
    callers: BTreeMap<String, BTreeMap<String, usize>>,
}

impl Workspace {
    fn remove(&mut self, module: &str) {
        if let Some(facts) = self.modules.remove(module) {
            for (function, calls) in facts.functions {
                for call in calls {
                    if let Some(callers) = self.callers.get_mut(&call.target) {
                        callers.remove(&function);
                        if callers.is_empty() {
                            self.callers.remove(&call.target);
                        }
                    }
                }
            }
        }
    }

    fn insert(&mut self, module: String, facts: ModuleFacts) {
        self.remove(&module);
        for (function, calls) in &facts.functions {
            for call in calls {
                self.callers
                    .entry(call.target.clone())
                    .or_default()
                    .entry(function.clone())
                    .or_insert(call.line);
            }
        }
        self.modules.insert(module, facts);
    }

    fn effects(&self, targets: &[String]) -> Vec<Effect> {
        let mut effects = Vec::new();
        for target in targets {
            // An in-workspace module can shadow an external effect target.
            if target
                .rsplit_once('.')
                .is_some_and(|(module, _)| self.modules.contains_key(module))
            {
                continue;
            }
            let mut seen = BTreeSet::from([target.as_str()]);
            let mut queue = VecDeque::from([(target.as_str(), vec![target.clone()])]);
            while let Some((callee, chain)) = queue.pop_front() {
                for (caller, line) in self.callers.get(callee).into_iter().flatten() {
                    if seen.insert(caller.as_str()) {
                        let mut path = vec![caller.clone()];
                        path.extend(chain.iter().cloned());
                        let (module, _) = caller.rsplit_once('.').expect("qualified function");
                        effects.push(Effect {
                            function: caller.clone(),
                            module: module.to_owned(),
                            target: target.clone(),
                            line: *line,
                            chain: path.clone(),
                        });
                        queue.push_back((caller, path));
                    }
                }
            }
        }
        effects
    }
}

#[derive(Serialize)]
struct Snapshot<'a> {
    parsed: usize,
    modules: &'a BTreeMap<String, ModuleFacts>,
    effects: Vec<Effect>,
}

fn main() -> Result<(), Box<dyn Error>> {
    let mut parser = Parser::new();
    parser.set_language(&tree_sitter_python::LANGUAGE.into())?;
    let mut workspace = Workspace::default();
    let mut output = io::stdout().lock();
    for line in io::stdin().lock().lines() {
        let update: Update = serde_json::from_str(&line?)?;
        let parsed = update.upsert.len();
        for module in update.remove {
            workspace.remove(&module);
        }
        for (module, source) in update.upsert {
            let facts = extract(&mut parser, &source.text, &module, source.is_package);
            workspace.insert(module, facts);
        }
        let snapshot = Snapshot {
            parsed,
            modules: &workspace.modules,
            effects: workspace.effects(&update.effect_targets),
        };
        serde_json::to_writer(&mut output, &snapshot)?;
        writeln!(output)?;
        output.flush()?;
    }
    Ok(())
}
