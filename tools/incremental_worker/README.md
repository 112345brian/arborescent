# Incremental architecture analysis prototype

The Python driver watches the project that holds an Arborescent policy file. It uses the
full scan's own file discovery, module selection, fact extraction (`arborescent.facts`) and rule
functions, and extracts facts again only for files that changed. The Rust worker keeps reverse
call edges between saves and walks backwards from `os.getenv` to provide call evidence.

CPython decides what is a syntax error, because tree-sitter accepts much that Python rejects.

From the repository root, with Arborescent installed in the active Python environment:

```sh
cargo build --release --locked --manifest-path tools/incremental_worker/Cargo.toml
python -m tools.incremental_analysis \
  --worker tools/incremental_worker/target/release/arborescent-incremental-worker \
  --policy /path/to/pyproject.toml --watch
```

The policy file can also be named `arborescent.toml`. On Windows, add `.exe` to the worker
path. Watch mode prints a complete JSON result whenever it changes, including empty lists when
findings clear. Omit `--watch` for one full pass with a nonzero exit code on `violations` or
`syntax_errors`.

| Field | Meaning | Fails |
| --- | --- | --- |
| `violations` | Parity findings that `arborescent check` fails on, before its baseline | yes |
| `notes` | Parity findings that `check` only reports | no |
| `call_paths` | `effect-via-call`, advisory (below) | no |
| `unresolved_calls` | Module -> calls `call_paths` did not follow | no |
| `syntax_errors` | Discovered files CPython rejects | yes |
| `incomplete` | Valid files the worker's grammar rejects, so `call_paths` misses their calls | no |

## What matches a full scan

`PARITY_RULES` in `tools/incremental_analysis.py` lists the claimed rules. `test_parity.py`
compares every field of every finding with `arborescent check`'s own pass over fixtures, edit
sequences and this repository.

- Discovery, names, imports and metadata: `arbor-ring-mismatch`, `bad-import`, `circular-import`,
  `dev-tool-import`, `dev-tool-misplaced`, `dev-tool-product-import`, `external-import`,
  `forbidden-module`, `internal-import`, `invalid-arbor-annotation`, `noise-dir-code`,
  `restricted-import`, `scope-import`, `unannotated-module`, `unclassified`,
  `undeclared-entry-dependency`, `vendor-folder`, `wildcard-ring`.
- Effects: every `effect-<category>` rule (`effect-state` covers `registry` and `cache`,
  `effect-patch` is the patch rule), `os-branching`, `effect-via-import`, `effect-at-import`,
  `effect-unsanctioned-import`, `effect-unlisted-category`, `effect-seam-bypassed`,
  `scope-effect-leak`, `unguarded-module-state` and its note, `unreachable-patch` and
  `unresolved-patch-target`.

**A clean result does not mean `arborescent check` passes.** No other rule is evaluated (among
them `mixed-purity`, `port-shadow`, `forwarded-import`, the construction, call and companion
rules, package rules and taint), and no baseline, child policy, external engine or other practice
runs.

- A full scan stops at the first syntax error. The driver lists the broken files in
  `syntax_errors` and reports the rest.

## Call paths

`effect-via-call` has no full-scan equivalent, so it is never mixed into `violations` and never
fails a run. `check` reports a forbidden effect at the module that uses it and, through
`effect-via-import`, at the modules that import it; a call path adds which function of an importer
reaches it. It follows statically resolved calls between synchronous, non-generator module-level
functions to `os.getenv` only. This gives no purity guarantee: methods, dynamic dispatch and
arbitrary Python binding are not followed, and `unresolved_calls` counts such calls in modules
whose ring forbids the effect.

Its identity for a baseline (`CallPath.key`) is the rule, module, ring, function and effect
category. Lines and the chain are not part of it. (`effect-via-import` keys do include the module
chain, as in a full scan.)

## Cost

Every refresh reads every source, as a full scan does without its cache, and the worker
returns a full evidence snapshot. The first refresh extracts every file. The rules that read only
the scan rerun only when something they read changed; the patch rules rerun every time because
`unresolved-patch-target` also reads the disk.

Run the tests from the repository root (they need Cargo and Git):

```sh
python -m pytest -p no:tach tools/incremental_worker/test_parity.py tools/incremental_worker/test_save.py
```

Without `ARBORESCENT_WORKER` the tests build a debug worker themselves, and skip when Cargo is
missing. Set it to a built binary (relative paths start at the repository root) to test that one;
a missing file then fails the run. CI does this with the release build.
