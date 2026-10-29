# Arborescent

One repository policy can enforce Clean Architecture across an application and independently structured libraries. It runs [import-linter](https://import-linter.readthedocs.io),
[tach](https://docs.gauge.sh), [Ruff](https://docs.astral.sh/ruff/) and a type checker ([ty](https://github.com/astral-sh/ty) by default, with pyright and basedpyright
or [pyrefly](https://pyrefly.org)) using their native project configuration. `arborescent generate` can scaffold optional preset files and never overwrites an existing file.
Arborescent also enforces: every module must be classified, each ring
has an allow-list of external packages, OS branching is limited to named rings, lazy imports count, entry scripts must
declare what the code imports, and a set of repository-hygiene practices that each come from a bug that really happened.

Arborescent's core has no runtime dependencies; the engines are optional extras run as subprocesses.

Install the checks you use with the package, or install every engine for development:

```sh
python -m pip install '.[all]'
python -m pip install '.[dev]'  # tests and all checker adapters
```

Ruff is included in `all` and `dev`, and is also available as the `ruff` extra. Ty is the default type checker; list
`type_checkers = ["ty", "pyright"]` to keep both configs generated and use `--type-checker pyright` when needed.

## Quickstart

```toml
# pyproject.toml
[tool.arborescent]
sources = ["src/app"]
source_roots = ["src"]
rings = ["domain", "ports", "adapters", "composition"]      # innermost first
type_checkers = ["ty", "pyright"]                          # first is the default; --type-checker pyright selects strict Pyright

[tool.arborescent.groups]
pure = ["__future__", "dataclasses", "typing"]

[tool.arborescent.ring.domain]
external = ["@pure"]
[tool.arborescent.ring.ports]
may_import = ["domain"]
external = ["@pure"]
[tool.arborescent.ring.adapters]
may_import = ["domain", "ports"]
external = ["@pure", "os", "psutil"]
os_branching = true                                         # only these rings may read os.name / sys.platform
[tool.arborescent.ring.composition]
may_import = ["*"]
external = ["@pure", "sys"]

[tool.arborescent.classify]                                    # fallback for modules without a valid __arbor__; first glob wins
"app" = "domain"
"app.domain" = "domain"
"app.ports" = "ports"
"app.adapters" = "adapters"
"app.main" = "composition"
```

Alternatively, a module can declare its ring in Python:

```python
# src/app/adapters.py
__arbor__ = {"ring": "adapters"}
```

A valid module-level `__arbor__` takes precedence over `classify`; the ring must be declared in the policy.
An absent annotation uses the first matching `classify` glob. In a multi-scope config, the ring must belong to the module's owning scope. A malformed annotation or unknown ring reports
`invalid-arbor-annotation` and falls back to `classify`, so an already-classified module keeps all its ring checks.
Annotations are module-level only: package and distribution boundaries (what survives extraction into a standalone
package) are not covered.
Without a fallback match it also reports `unclassified`. These annotations classify individual modules only.

### Independently governed libraries

Use named scopes when a repository contains libraries with different ring hierarchies. Each scope owns its source paths,
module roots, rings, classifications, public module patterns and declared scope dependencies. The application can have
its own scope. Existing root-level architecture settings remain the single-scope shorthand.

```toml
# pyproject.toml
[tool.arborescent]
python = "3.12"
type_checkers = ["ty"]

[tool.arborescent.scopes.packet_archive]
sources = ["packages/packet-archive/src/packet_archive"]
source_roots = ["packages/packet-archive/src"]
rings = ["domain", "adapters"]
public = ["packet_archive.api*"]
depends_on = ["binary_parser"]

[tool.arborescent.scopes.packet_archive.classify]
"packet_archive.domain*" = "domain"
"packet_archive.adapters*" = "adapters"

[tool.arborescent.scopes.binary_parser]
sources = ["packages/binary-parser/src/binary_parser"]
source_roots = ["packages/binary-parser/src"]
rings = ["domain", "adapters"]
public = ["binary_parser.reader"]

[tool.arborescent.scopes.binary_parser.classify]
"binary_parser*" = "domain"

[tool.arborescent.scopes.catalog]
sources = ["packages/catalog/src/catalog"]
source_roots = ["packages/catalog/src"]
rings = ["core", "use_cases", "adapters"]
public = ["catalog.api*"]
depends_on = ["binary_parser"]

[tool.arborescent.scopes.catalog.classify]
"catalog.core*" = "core"
"catalog.use_cases*" = "use_cases"
"catalog.adapters*" = "adapters"
```

The parser has one owner and its own rings. Both libraries consume its public module through an explicit dependency;
they keep their own core and ring rules. Scope boundaries are checked by Arborescent and composed into the generated
root tool configs. Shared source files are not copied between scopes, and extracting a library into a separate
repository remains a separate packaging step.

```
arborescent generate     # optionally create missing recommended config files; existing files stay untouched
arborescent check        # engines in parallel, own checks, baseline ratchet; generated-preset drift is advisory
arborescent check --install-tools # prompt to install missing tools, then run the checks
arborescent install-tools # prompt to install missing tools without running checks
arborescent check --install-tools --yes # install without prompting, then check
arborescent check --require-tools --require-profile  # require profile v1 and its verified tool versions/rules
arborescent check --changed origin/main      # only files changed since a git ref
arborescent discover     # list Python files that still need ring classification
arborescent baseline     # record existing violations; future checks fail on new findings
arborescent selftest     # mutation-test native rules and fail if a rule stops firing
arborescent bench DIR…   # cold/warm timings of every engine on prepared codebases
```

Documentation: [CLI commands, configuration and rules](docs/configuration.md) and
[Arborescent's own architecture](docs/architecture.md).

`check` never installs packages unless you pass `--install-tools`. By default, missing selected tools produce an exact
install command and the usual skip/fail behavior. `check --install-tools` prints the command, prompts with No as the
default, installs on confirmation, then runs checks; `--yes` skips the prompt. `arborescent install-tools` uses the same
setup flow without running checks. Installation targets the active Python environment (or uv targeting that
interpreter when pip is unavailable). In Poetry projects it uses `poetry add` to place checkers in an `arborescent` group
included by `dev`, then verifies the lock. Set `disabled_tools = ["import-linter", "tach", "ruff"]` in
`[tool.arborescent]` (or the top-level `arborescent.toml`) to deliberately omit those tools from checks and setup hints;
minimum-profile certification still requires Ruff and Tach.
The same setting accepts `"arbor-annotations"`: `disabled_tools = ["arbor-annotations"]` ignores `__arbor__`
annotations and their validation findings, uses `classify` alone, and prevents minimum-profile certification.

## Presets

`preset = "strict"` is the default. A preset only changes how much of the external-import and OS rules applies; ring
layering, classification (every module needs a ring) and lazy-import detection are the same in all of them.

| preset | imports that need a ring `external` entry | OS branching limited to named rings |
|---|---|---|
| `strict` (default) | every external import, stdlib included | yes |
| `standard` | third-party packages and the I/O stdlib set; other stdlib is free | yes |
| `relaxed` | third-party packages only | no |

The I/O stdlib set (`os`, `subprocess`, `socket`, `sqlite3`, `pathlib`, ...: see `arborescent.presets.IO_MODULES`) also drives
the generated import-linter bans. Replace it per project with `io_modules = ["os", "subprocess", ...]` (an empty list means
no stdlib module counts as I/O).

## Ports

- `ToolRunner` (`available`, `run`) -- how engines and git are executed; `arborescent.testing.FakeToolRunner` fakes it.
- `TypeChecker` (`render_config`, `command`, `passed`) -- pyright, basedpyright, ty and pyrefly adapters in
  `arborescent.typecheckers`. Every strict-mode setting was verified by running the tool (see the module docstring).
- `FileSystem` -- every file read, write, listing, copy and temporary folder the use cases need; `LocalFileSystem` is the
  real one and `arborescent.testing.FakeFileSystem` an in-memory twin (both run the same contract tests).
- `ModuleScanner` -- finds modules and parses each file once; `FsScanner` adds a content-hash cache.

## What the policy compiles to

| engine | optional generated preset | what it cannot say (Arborescent's own check says it) |
|---|---|---|
| import-linter | one `forbidden` contract per ring for the rings it may not import, one per ring banning I/O stdlib and declared third-party packages the ring does not list, one global `forbid_everywhere` | an external allow-list (it only has deny-lists); single-file entry scripts (not packages) |
| tach | `layers` (outermost first), one `[[modules]]` per module; `layers_explicit_depends_on` + per-module `depends_on` when a ring skips an inner ring | tach's circular-dependency check is switched off in that mode; Arborescent independently enforces circular-import checks across all first-party modules |
| Ruff | recommended `ruff.toml`; violations join the baseline, and suppression comments are ignored | ring-dependent effects (Arborescent's native rules) |
| pyright / basedpyright / ty / pyrefly | strict config, `# type: ignore` ignored, `match` exhaustiveness, no Unknown | -- |

## Practices (`[tool.arborescent.practices]`)

`gitignore-shadow` (an unanchored `local_voice/` ignored a real package: empty wheel), `sibling-versions` (a meta-package
pinned `==0.1.1` while the component was `0.2.0`), `secrets-guard` (tests need an autouse conftest fixture that scrubs
the environment), `docs-sync` (the architecture doc's `| module | ring |` table must equal the policy), `crlf`
(scripts with CRLF), `undeclared-imports` (third-party import absent from the manifest). Each has a reproduction test in
`tests/test_practices.py` that builds the broken repository in a temp directory.

## Speed

Engines run concurrently; Arborescent parses each file once (AST), caches facts by content hash in `.arborescent/`, and
`--changed` limits Arborescent, Ruff, and the type checker to changed files. import-linter, `tach check`, and `tach check-external`
are graph-wide, so Arborescent skips them and reports that reason in the engine results. See the repository CHANGELOG for measured numbers; `arborescent bench` reproduces them.

## Limits (honest)

Effect checks detect direct imports, aliases and attribute chains. They intentionally do not catch calls routed through
`getattr`, variables or wrapper functions. `module_forbid_imports` can add finer-grained internal-import restrictions
for selected modules without changing their ring. CI is configured for Windows, macOS and Linux; the account's
GitHub billing block has prevented hosted runs so far.
