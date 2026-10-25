# archlint

One architecture policy. It **generates** the configuration of [import-linter](https://import-linter.readthedocs.io),
[tach](https://docs.gauge.sh) and a strict type checker (pyright, basedpyright, [ty](https://github.com/astral-sh/ty)
or [pyrefly](https://pyrefly.org)), then **enforces more than any of them**: every module must be classified, each ring
has an allow-list of external packages, OS branching is limited to named rings, lazy imports count, entry scripts must
declare what the code imports, and a set of repository-hygiene practices that each come from a bug that really happened.

Prototype, 0.1.0. Core has no dependencies; the engines are extras run as subprocesses behind a port.

## Quickstart

```toml
# pyproject.toml
[tool.archlint]
sources = ["src/app"]
source_roots = ["src"]
rings = ["domain", "ports", "adapters", "composition"]      # innermost first
type_checkers = ["pyright"]                                 # or ["ty"], ["pyrefly"], ["basedpyright"]

[tool.archlint.groups]
pure = ["__future__", "dataclasses", "typing"]

[tool.archlint.ring.domain]
external = ["@pure"]
[tool.archlint.ring.ports]
may_import = ["domain"]
external = ["@pure"]
[tool.archlint.ring.adapters]
may_import = ["domain", "ports"]
external = ["@pure", "os", "psutil"]
os_branching = true                                         # only these rings may read os.name / sys.platform
[tool.archlint.ring.composition]
may_import = ["*"]
external = ["@pure", "sys"]

[tool.archlint.classify]                                    # first matching glob wins; every module needs one
"app" = "domain"
"app.domain" = "domain"
"app.ports" = "ports"
"app.adapters" = "adapters"
"app.main" = "composition"
```

```
archlint generate     # writes archlint-importlinter.toml, tach.toml, pyrightconfig.json (commit them)
archlint check        # drift diff, engines in parallel, own checks, baseline ratchet
archlint check --changed origin/main      # only files changed since a git ref
archlint baseline     # record today's violations; check then fails only on NEW ones, and on entries that vanished
archlint selftest     # mutation-test the rules: inject a forbidden import, require the check to fail
archlint bench DIR…   # cold/warm timings of every engine on codebases you prepared
```

Documentation: [configuration reference](docs/configuration.md) (every key, rule and practice) and
[ArchLint's own architecture](docs/architecture.md).

## Presets

`preset = "strict"` is the default. A preset only changes how much of the external-import and OS rules applies; ring
layering, classification (every module needs a ring) and lazy-import detection are the same in all of them.

| preset | imports that need a ring `external` entry | OS branching limited to named rings |
|---|---|---|
| `strict` (default) | every external import, stdlib included | yes |
| `standard` | third-party packages and the I/O stdlib set; other stdlib is free | yes |
| `relaxed` | third-party packages only | no |

The I/O stdlib set (`os`, `subprocess`, `socket`, `sqlite3`, `pathlib`, ...: see `archlint.presets.IO_MODULES`) also drives
the generated import-linter bans. Replace it per project with `io_modules = ["os", "subprocess", ...]` (an empty list means
no stdlib module counts as I/O).

## Ports

- `ToolRunner` (`available`, `run`) -- how engines and git are executed; `archlint.testing.FakeToolRunner` fakes it.
- `TypeChecker` (`render_config`, `command`, `passed`) -- pyright, basedpyright, ty and pyrefly adapters in
  `archlint.typecheckers`. Every strict-mode setting was verified by running the tool (see the module docstring).
- `FileSystem` -- every file read, write, listing, copy and temporary folder the use cases need; `LocalFileSystem` is the
  real one and `archlint.testing.FakeFileSystem` an in-memory twin (both run the same contract tests).
- `ModuleScanner` -- finds modules and parses each file once; `FsScanner` adds a content-hash cache.

## What the policy compiles to

| engine | what is generated | what it cannot say (ArchLint's own check says it) |
|---|---|---|
| import-linter | one `forbidden` contract per ring for the rings it may not import, one per ring banning I/O stdlib and declared third-party packages the ring does not list, one global `forbid_everywhere` | an external allow-list (it only has deny-lists); single-file entry scripts (not packages) |
| tach | `layers` (outermost first), one `[[modules]]` per module; `layers_explicit_depends_on` + per-module `depends_on` when a ring skips an inner ring | tach's circular-dependency check is switched off in that mode (same-ring peers must depend on each other) |
| pyright / basedpyright / ty / pyrefly | strict config, `# type: ignore` ignored, `match` exhaustiveness, no Unknown | -- |

## Practices (`[tool.archlint.practices]`)

`gitignore-shadow` (an unanchored `local_voice/` ignored a real package: empty wheel), `sibling-versions` (a meta-package
pinned `==0.1.1` while the component was `0.2.0`), `secrets-guard` (tests need an autouse conftest fixture that scrubs
the environment), `docs-sync` (the architecture doc's `| module | ring |` table must equal the policy), `crlf`
(scripts with CRLF), `undeclared-imports` (third-party import absent from the manifest). Each has a reproduction test in
`tests/test_practices.py` that builds the broken repository in a temp directory.

## Speed

Engines run concurrently; ArchLint parses each file once (AST), caches facts by content hash in `.archlint/`, and
`--changed` limits its own checks and the type checker to changed files. import-linter and tach are graph-wide and
still run in full. See the repository CHANGELOG for measured numbers; `archlint bench` reproduces them.

## Limits (honest)

Windows only was exercised. Ring granularity only: a module cannot be given an exception finer than its ring
(`tests/test_todo.py` lists this and the other open items as strict xfails). `os` aliases (`import os as o`) are not seen
by the OS-branching rule.
