# Incremental architecture analysis

`tools/incremental_analysis.py` watches the project that holds an Arborescent policy file. It uses
the full scan's own file discovery, module selection, fact extraction (`arborescent.facts`) and
rule functions, and extracts facts again only for files that changed.

From the repository root, with Arborescent installed in the active Python environment:

```sh
python -m tools.incremental_analysis --policy /path/to/pyproject.toml --watch
```

The policy file can also be named `arborescent.toml`. Watch mode prints a complete JSON result
whenever it changes, including empty lists when findings clear. Omit `--watch` for one full pass
with a nonzero exit code on `violations` or `syntax_errors`.

| Field | Meaning | Fails |
| --- | --- | --- |
| `parsed` | Source modules whose facts were extracted in this pass | no |
| `violations` | Parity findings that `arborescent check` fails on, before its baseline | yes |
| `notes` | Parity findings that `check` only reports | no |
| `syntax_errors` | Discovered files CPython rejects | yes |

## What matches a full scan

`PARITY_RULES` in `tools/incremental_analysis.py` lists the claimed rules.
`tests/test_incremental_parity.py` compares every field of every finding with `arborescent check`'s
own pass over fixtures, edit sequences and this repository.

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

- A full scan stops at the first syntax error. The watcher lists the broken files in
  `syntax_errors` and reports the rest.

## Cost

Every refresh reads every source, as a full scan does without its cache. The first refresh extracts
every file. The rules that read only the scan rerun only when something they read changed; the patch
rules rerun every time because `unresolved-patch-target` also reads the disk.

The tests run with the rest of the suite (some need Git):

```sh
python -m pytest tests/test_incremental_parity.py tests/test_incremental_save.py
```
