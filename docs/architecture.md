# Arborescent's architecture

Arborescent is checked by Arborescent: the policy is the `[tool.arborescent]` table in `pyproject.toml`, and `arborescent check`
fails when this document's module table disagrees with it (practice `docs-sync`).

This project is one library, so its existing root-level policy is the implicit single scope. The module inventory includes
a scope column; repositories with multiple libraries use the same `module | scope | ring` shape to document ownership.

## Rings

Five rings, innermost first. A ring may import itself and the rings it lists; nothing may import an outer ring. The domain
uses `pathlib.PurePosixPath` only for lexical path validation; it does not access the filesystem.

| ring | may import | external packages | role |
|---|---|---|---|
| domain | nothing | stdlib basics, no I/O | vocabulary and pure rules: the validated policy value types (including declared child-arbor paths), rule ids (`Rule`), violations, facts about a parsed module, presets |
| ports | domain | `pathlib`, `contextlib` | the protocols the world must satisfy |
| application | domain, ports | `pathlib`, `concurrent`, `tomllib`, `json`, `re` | use cases: generate, check, baseline, annotate, selftest, suggestions, practices, package metadata and extraction, package assignment gaps, the driver's translation of migration arguments into a library request |
| adapters | domain, ports | `pathlib`, `contextlib`, `json`, `hashlib`, `os`, `shutil`, `subprocess`, `tempfile`, `time`, `sysconfig`, `tomllib` | focused file-system, scanner, clock, subprocess, engine and type-checker adapters |
| composition | everything | `argparse`, `pathlib`, `os`, `shlex` | the command line: wires adapters to use cases and presents reports |

The application ring does no file I/O of its own: it reads, writes, lists, copies and creates temporary folders through
the `FileSystem` port (`Services.fs`), which is why `shutil` and `tempfile` are not on its list. What remains there is
computation, not I/O: `pathlib` for `Path` values (joining and `relative_to`; never `read_text`, `exists` and the like).
JSON goes through `FileSystem.read_json`/`write_json` and the stopwatch through the `Clock` port (`Services.clock`).
The application explicitly allows the `threads` effect because `engine.run_check` runs independent engines in a thread
pool; it still forbids every other effect category under the strict preset.
`tests/test_filesystem.py` scans
`application` and `composition` for any file access that bypasses the port.

Adapters do **not** import `application`. Use cases receive adapters through the ports (`Services` in `engine.py`), and
only `cli.py` knows both sides. Only `adapters` may branch on the OS.

## Migration: supply the arguments, do not reimplement the tools

Arborescent supplies annotations, the checks that they are complete and consistent, engine contracts compiled from
them, and a stable export, including the typed arguments a monorepo-to-polyrepo migration needs. An auxiliary driver on top supplies those arguments to a dedicated migration library
(history extraction, import rewriting) through ports with adapters; it does not migrate, as it drives import-linter and tach. It does not
reimplement what those tools do. The boundary, the wrapper rules, the tool landscape and the status of the existing
`packages extract` are in `docs/superpowers/specs/2026-10-31-monorepo-split-annotations-design.md`.

The driver is five modules, one per ring, and the annotation layer never imports any of them:

- domain, `driver_request`: `MigrationRequest` (one package's path renames and module renames, plus where from and to) and `MigrationOutcome`.
- ports, `driver_port`: the `MigrationLibrary` protocol (availability, the arguments it cannot apply, the exact arguments, `supply`).
- application, `driver_plan`: `plan_supply` turns a `PackageArguments` into a request, refusing while an error gap of the package is open and for a workspace member (it keeps its history where it is); `supply` calls the port.
- adapters, `driver_filter_repo`: git filter-repo behind the port, reached through `ToolRunner`.
- composition, `cli_migrate`: `arborescent packages migrate`; `cli` binds the adapter.

The constraint "the annotation layer must not import the driver" is `module_forbid_imports` in Arborescent's own policy,
so `check` enforces it and `selftest` injects the forbidden import (`module_forbid_probes`). Adapters do not import the
application ring (the ring rules and their probes), and the application imports no adapter.

git filter-repo is the first library because it keeps history, takes explicit path selection and renames, reads one
repository and writes another (the monorepo is never modified) and installs from PyPI (`arborescent[filterrepo]`). It
does not rewrite imports, so the module renames of a request are reported as not applied; an import-rewriting library
would be a second adapter over the same port.

## Modules

| module | scope | ring |
|---|---|---|
| `arborescent` | `single` | domain |
| `arborescent.domain` | `single` | domain |
| `arborescent.facts` | `single` | domain |
| `arborescent.stdlib` | `single` | domain |
| `arborescent.presets` | `single` | domain |
| `arborescent.rules` | `single` | domain |
| `arborescent.package_model` | `single` | domain |
| `arborescent.package_fields` | `single` | domain |
| `arborescent.package_parse` | `single` | domain |
| `arborescent.package_render` | `single` | domain |
| `arborescent.path_exclusion` | `single` | domain |
| `arborescent.package_assignment` | `single` | domain |
| `arborescent.migration_arguments` | `single` | domain |
| `arborescent.migration_json` | `single` | domain |
| `arborescent.driver_request` | `single` | domain |
| `arborescent.ports` | `single` | ports |
| `arborescent.driver_port` | `single` | ports |
| `arborescent.annotate` | `single` | application |
| `arborescent.baseline` | `single` | application |
| `arborescent.bench` | `single` | application |
| `arborescent.config` | `single` | application |
| `arborescent.certification` | `single` | application |
| `arborescent.install_tools` | `single` | application |
| `arborescent.engine` | `single` | application |
| `arborescent.generate` | `single` | application |
| `arborescent.practices` | `single` | application |
| `arborescent.selftest` | `single` | application |
| `arborescent.suggestions` | `single` | application |
| `arborescent.packages` | `single` | application |
| `arborescent.package_tree` | `single` | application |
| `arborescent.package_manifest` | `single` | application |
| `arborescent.package_validation` | `single` | application |
| `arborescent.package_copy` | `single` | application |
| `arborescent.mutation` | `single` | application |
| `arborescent.package_gaps` | `single` | application |
| `arborescent.package_probes` | `single` | application |
| `arborescent.migration_export` | `single` | application |
| `arborescent.driver_plan` | `single` | application |
| `arborescent.module_forbid_probes` | `single` | application |
| `arborescent.adapters` | `single` | adapters |
| `arborescent.installer_adapter` | `single` | adapters |
| `arborescent.clock_adapter` | `single` | adapters |
| `arborescent.filesystem_adapter` | `single` | adapters |
| `arborescent.scanner` | `single` | adapters |
| `arborescent.tool_runner` | `single` | adapters |
| `arborescent.engines` | `single` | adapters |
| `arborescent.driver_filter_repo` | `single` | adapters |
| `arborescent.testing` | `single` | adapters |
| `arborescent.typecheckers` | `single` | adapters |
| `arborescent.__main__` | `single` | composition |
| `arborescent.cli` | `single` | composition |
| `arborescent.cli_install` | `single` | composition |
| `arborescent.cli_report` | `single` | composition |
| `arborescent.cli_migrate` | `single` | composition |

## Data flow of `arborescent check`

1. `cli` loads the policy (`config.policy_from_mapping` -> `domain.Policy`, which validates itself; with its `presets.Preset`) and builds `Services`: a scanner, a tool
   runner, the file system (`LocalFileSystem`; tests use `testing.FakeFileSystem`), a clock, the engine adapters (`engines`: import-linter, tach) and the type-checker adapters.
2. `engine.run_check` starts every `Engine` in `Services` (import-linter, tach) and the chosen type checker in parallel. They run as
   subprocesses behind the `ToolRunner` port; a new engine is an adapter, not an edit to `run_check`. An unknown type
   checker name is a `PolicyError`, and the worker pool is always shut down. It then runs each declared child arbor from
   the child's direct policy root through the same `ToolRunner`; child status contributes to the parent result.
3. Meanwhile the scanner finds every module and parses each file once (content-hash cached); `rules.check_modules`,
   suppression and `discard()` inventories, and the practices run over that one parse.
4. `generate.generate` renders the expected tool configs in memory; `engine.drift` diffs them against the committed files.
5. `baseline.apply` compares the violations with the baseline: new ones fail, entries that vanished are reported stale.

The parent scanner excludes explicitly listed child roots only when they have a direct policy and
`child_policy_mode = "certify"`. Paths without their own policy remain assigned to the parent's single policy or named
scopes. `child_policy_mode = "parent"` overrides listed child policies and keeps those roots in the parent scan.
`scan_vendors` configures whether convention-named vendor folders participate in that scan. Parent certification
recursively requires each independently governed child to pass its requested check profile.

## Rules of the house

- The domain and ports rings do no I/O. A second import-linter contract in `pyproject.toml` enforces it directly, as a
  check that does not depend on the generated configs.
- A new behaviour switch goes into the policy (a preset field or a key), never into a check as a constant, so
  `generate`, `rules` and `selftest` agree on it.
- Every applicable native rule has a mutation in `selftest`; `scope-import` is probed when the policy has multiple scopes.
- Any additional clean-architecture constraint must be added to Arborescent's policy and enforced by a self-test mutation;
  keep Arborescent's own minimum-profile certification passing with `arborescent check --require-profile`. Update the
  standard/customized certification label when the added constraint changes whether the policy is standard.
- The policy defaults `forbid_circular_imports` to true. Arborescent's minimum profile requires it, and `selftest` injects
  a self-import to prove that the cycle rule remains active.
