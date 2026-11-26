# Incremental architecture analysis prototype

The Python driver watches the project that holds an Arborescent policy file. It uses the
full scan's own file discovery, module selection and `check_modules`. The Rust worker keeps
each module's imports and reverse call edges between saves, parses only changed modules,
and walks backwards from the requested effect targets to provide call evidence.

CPython also parses each changed file in the driver. It decides what is a syntax error and
reads `__arbor__` and PEP 723 metadata, because tree-sitter accepts much that Python rejects.

From the repository root, with Arborescent installed in the active Python environment:

```sh
cargo build --release --locked --manifest-path tools/incremental_worker/Cargo.toml
python -m tools.incremental_analysis \
  --worker tools/incremental_worker/target/release/arborescent-incremental-worker \
  --policy /path/to/pyproject.toml --watch
```

The policy file can also be named `arborescent.toml`. On Windows, add `.exe` to the worker
path. Watch mode prints a complete JSON diagnostic list whenever the findings change,
including an empty list when findings clear. Omit `--watch` for one full pass with a nonzero
exit code on violations or syntax errors.

## What matches a full scan

`PARITY_RULES` in `tools/incremental_analysis.py` lists the rules decided by discovery,
module names, imports, `__arbor__` and PEP 723 metadata. For those, `test_parity.py` compares
every field of every finding with `arborescent check`'s own pass over fixtures, edit sequences
and this repository.

**A clean result does not mean `arborescent check` passes.** No other rule is evaluated,
including every effect rule (module state, at import, registry and cache among them), and no
baseline, child policy, engine or practice runs.

- A full scan stops at the first syntax error. The driver lists the broken files in
  `syntax_errors` and reports the rest.
- Some valid Python is rejected by the tree-sitter grammar. Those files appear in
  `incomplete`: their imports are read by Python, but their calls are missing.

## Worker-only evidence

`effect-via-call` has no full-scan equivalent. It follows statically resolved calls between
synchronous, non-generator module-level functions to `os.getenv`. This limited function
analysis does not provide purity guarantees or coverage for dynamic dispatch and arbitrary
Python binding.

## Cost

Every refresh reads every source, as a full scan does without its cache, and the worker
returns a full evidence snapshot. The import rules rerun only when something they read changed.

Run the tests from the repository root (they need Cargo and Git):

```sh
python -m pytest -p no:tach tools/incremental_worker/test_parity.py tools/incremental_worker/test_save.py
```

Without `ARBORESCENT_WORKER` the tests build a debug worker themselves, and skip when Cargo is
missing. Set it to a built binary (relative paths start at the repository root) to test that one;
a missing file then fails the run. CI does this with the release build.
