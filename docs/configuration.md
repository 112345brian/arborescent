# Configuration reference

The policy lives in `[tool.archlint]` in `pyproject.toml`, or as top-level keys in an `archlint.toml`. ArchLint searches
upwards from the current folder (or use `archlint --config PATH ...`); the folder holding the policy file is the
repository root. A malformed or self-contradicting policy exits with code 2 and a one-line reason; nothing is repaired
silently.

## Top-level keys

| key | default | meaning |
|---|---|---|
| `rings` | required | Distinct ring names, **innermost first**. |
| `classify` | required in practice | Table of glob -> ring. Globs match the dotted module name and `*` crosses dots. The first match wins. Every module must match one (`unclassified`). |
| `sources` | `["."]` | Folders or single files to scan. A single `.py` file is an entry script, not a package. |
| `source_roots` | `["."]` | Folders that module names are relative to (`src` for a `src/app` layout). Also handed to tach. |
| `exclude` | `[]` | Globs matched against a path or a file/folder name to skip while scanning. |
| `preset` | `"strict"` | How much of the external and OS rules applies: `strict`, `standard` or `relaxed` (see below). |
| `io_modules` | built-in set | Stdlib modules that count as I/O, process or OS. Replaces the default set; `[]` means none. |
| `groups` | `{}` | Named lists, used as `"@name"` inside any `may_import` or `external` list. |
| `dependencies` | `{}` | Import name -> distribution name, where they differ (`yaml = "pyyaml"`). Used for entry-script and manifest checks. |
| `forbid_everywhere` | `[]` | Packages no module may import, in any ring (one global import-linter contract). |
| `entry_scripts` | `[]` | Dotted names of PEP 723 single-file scripts. Each must declare, in its `# /// script` block, every third-party distribution the project code imports. |
| `type_checkers` | `["pyright"]` | `pyright`, `basedpyright`, `ty` and/or `pyrefly`. The first one runs by default; `check --type-checker` overrides it. A config file is generated for each one listed. |
| `python` | `"3.11"` | Python version written into the type-checker configs. |
| `output_dir` | `"."` | Where `generate` writes the config files, relative to the root. |
| `baseline` | `"archlint-baseline.json"` | The baseline file used by `archlint baseline` and `check`. |
| `practices` | `{}` | Repository-hygiene checks (below). |

## Rings: `[tool.archlint.ring.<name>]`

| key | default | meaning |
|---|---|---|
| `may_import` | `[]` | Inner rings this ring may import; `["*"]` means every inner ring. A ring may always import itself. An outer ring in this list is a policy error (the onion rule). |
| `external` | `[]` | Top-level packages (third-party and stdlib) this ring may import; `["*"]` means unrestricted. Which imports actually need an entry depends on the preset. |
| `os_branching` | `false` | Whether this ring may read `os.name` or `sys.platform`. Only checked when the preset limits OS branching. |

A ring that is not given a table has no inner imports, no external imports and no OS branching.

## Presets

A preset changes how much of the external-import and OS rules applies. Ring layering, classification, lazy-import
detection and the entry-script check are the same in all of them.

| preset | imports needing a ring `external` entry | OS branching limited to named rings |
|---|---|---|
| `strict` (default) | every external import, stdlib included | yes |
| `standard` | third-party packages and the `io_modules` set | yes |
| `relaxed` | third-party packages only | no |

`io_modules` also drives the generated import-linter contracts, which ban the I/O modules a ring does not list. Under
`relaxed` the contracts ban only declared third-party packages.

## Rules ArchLint checks itself

The ids below are `archlint.domain.Rule`. They appear in baseline entries (`rule|subject|detail`), so they never change.

| rule | fires when |
|---|---|
| `unclassified` | a module matches no `classify` pattern |
| `internal-import` | a ring imports a ring it may not (top-level, lazy and `TYPE_CHECKING` imports all count) |
| `external-import` | a ring imports an external package missing from its `external` list (scope set by the preset) |
| `os-branching` | a ring not marked `os_branching` reads `os.name` or `sys.platform` |
| `bad-import` | a relative import that points above the package root |
| `undeclared-entry-dependency` | an entry script lacks a `# /// script` block, or omits a third-party distribution the project imports |

## Practices: `[tool.archlint.practices]`

Each practice is off until it has a value.

| key | value | checks |
|---|---|---|
| `gitignore-shadow` | `true`, or a list of file globs to allow | a `.py` file is git-ignored (an unanchored pattern hid a package) |
| `sibling-versions` | glob of `pyproject.toml` files, e.g. `"*/pyproject.toml"` | a project pins a sibling project to a version the sibling does not have |
| `secrets-guard` | tests folder, e.g. `"tests"` | `conftest.py` has no autouse fixture that scrubs the environment |
| `docs-sync` | path of the architecture doc | the doc's `\| module \| ring \|` table equals the policy (see `docs/architecture.md`) |
| `crlf` | list of file globs, e.g. `["*.sh"]` | a matching file has CRLF line endings |
| `undeclared-imports` | `true`, or a manifest path (default `pyproject.toml`) | a third-party import is absent from the manifest's dependencies |

## Commands and exit codes

`archlint generate | check | baseline | selftest | bench`. `check` and `selftest` exit 1 on failure, `generate` and
`baseline` exit 0, and a bad policy exits 2. `check` options: `--changed REF`, `--type-checker NAME`, `--no-tools`,
`--require-tools`, `--no-baseline`, `--no-cache`.
