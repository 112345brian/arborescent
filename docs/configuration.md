# Configuration reference

The policy lives in `[tool.arborescent]` in `pyproject.toml`, or as top-level keys in an `arborescent.toml`. Arborescent searches
upwards from the current folder (or use `arborescent --config PATH ...`); the folder holding the policy file is the
repository root. A malformed or self-contradicting policy exits with code 2 and a one-line reason; nothing is repaired
silently.

## Single-scope top-level keys

The keys below define the legacy single-scope form. It remains supported as written. For a repository with independently
governed libraries, use `[tool.arborescent.scopes.<name>]` instead; the root retains only shared tool and repository
settings. The two forms cannot be mixed.

| key | default | meaning |
|---|---|---|
| `rings` | required | Distinct ring names, **innermost first**. |
| `classify` | `{}` | Table of glob -> ring, used when a module has no valid `__arbor__` annotation. Globs match the dotted module name and `*` crosses dots. The first match wins. Every module needs a ring from an annotation or a matching glob (`unclassified`). |
| `sources` | `["."]` | Folders or single files to scan. A single `.py` file is an entry script, not a package. |
| `source_roots` | `["."]` | Folders that module names are relative to (`src` for a `src/app` layout). Also handed to tach. |
| `exclude` | `[]` | Globs to skip while scanning. A bare entry (`client`, `*.gen`, `dir/sub`) is unanchored, like a `.gitignore` line: it matches the path, a file or folder name, or any ancestor folder at **any depth**, so `client` also hides `src/pkg/client/`. A leading `/` anchors the entry to the repository root and matches by full relative path only: `/client` hides the top-level `client/` and nothing deeper, `/src/*/generated` hides one level of generated folders. Excluded Python files are counted by folder on every `check`; a folder hidden only by a bare name below the root is flagged there with the anchored spelling. |
| `children` | `[]` | Explicit nested package roots to inspect for a direct Arborescent policy. In `certify` mode, roots with their own policy are checked recursively; roots without one remain in the parent scan. |
| `child_policy_mode` | `"certify"` | `"certify"` respects direct child policies and includes their results in parent certification. `"parent"` overrides child policies: listed roots stay in the parent scan and are not checked separately. |
| `scan_vendors` | `false` | Include convention-named `vendor`, `vendored`, `third_party` and `site-packages` folders in the parent scan. This lets parent rules govern vendored code; declared child roots with their own policy are still separated in `certify` mode. |
| `vendor_roots` | `[]` | Folders whose child folders are vendored packages. Each child `X` becomes a source root (`<root>/X/src` if it exists, else `<root>/X`) added to `sources` and `source_roots`, and the folder is scanned even though its name is a vendor convention, so a vendored package is not reported as external. Other vendor folders stay excluded. Not allowed with `scopes`. |
| `library_roots` | `[]` | Same expansion for editable in-repo libraries (for example `libs`); the folders are scanned normally. Not allowed with `scopes`. |
| `module_forbid_imports` | `{}` | Module glob -> internal module/package names this module may not import, in addition to ring rules. |
| `importable_by` | `{}` | Module glob -> importer globs: the inverse of `module_forbid_imports` (see below). A listed module, and its submodules, may be imported only by modules matching one of the globs. |
| `preset` | `"strict"` | How much of the external and OS rules applies: `strict`, `standard` or `relaxed` (see below). |
| `middle_forbid` | the preset's | Effect categories middle rings may not use, replacing the preset default (see below). |
| `git_hook` | the preset's | Whether `generate` and `check` manage the `pre-push` hook, replacing the preset default (see below). `true` installs it under any preset; `false` removes an Arborescent-managed one. A hook Arborescent did not write is never touched either way. |
| `io_modules` | built-in set | Stdlib modules that count as I/O, process or OS. Replaces the default set; `[]` means none. |
| `groups` | `{}` | Named lists, used as `"@name"` inside any `may_import` or `external` list. |
| `dependencies` | `{}` | Import name -> distribution name, where they differ (`yaml = "pyyaml"`). Used for entry-script and manifest checks. |
| `tach_external_exclude` | `[]` | Package names written to the generated Tach `[external] exclude`: optional extras imported lazily, or transitive dependencies the project does not declare. Tach reads only the base dependencies and matches distribution names where they differ from import names (`google-genai`, `pyyaml`). |
| `forbid_everywhere` | `[]` | Packages no module may import, in any ring (one global import-linter contract). |
| `forbidden_modules` | `[]` | `classify`-style globs on dotted first-party module names that must not exist under `sources`, for example `["text_claims", "text_claims.*"]` after that package moved to a pinned dependency. Globs match the whole module name and `*` crosses dots, so a package needs its own name and `name.*`. Each matching module fails once (`forbidden-module`), naming the first matching pattern. It looks only at modules that exist, never at imports: use `forbid_everywhere` to ban importing a package. A pattern that matches nothing is fine. |
| `forbid_circular_imports` | `true` | Rejects cycles in the first-party module import graph, including lazy and `TYPE_CHECKING` imports. Set to `false` only to opt out; the Arborescent minimum profile requires `true`. |
| `require_arbor_annotations` | `false` | Makes a module that resolves its ring only through `classify` (no valid `__arbor__` annotation) a hard failure (`unannotated-module`), instead of a silent fallback. See "Requiring annotations" below. |
| `require_arbor_annotations_exclude` | `[]` | `classify`-style module-name globs exempt from `require_arbor_annotations`, for modules that cannot carry an annotation (vendored, read-only, generated). Has no effect unless `require_arbor_annotations` is set. |
| `entry_scripts` | `[]` | Dotted names of PEP 723 single-file scripts. Each must declare, in its `# /// script` block, every third-party distribution the project code imports. |
| `type_checkers` | `["ty"]` | `pyright`, `basedpyright`, `ty` and/or `pyrefly`. The first one runs by default; `check --type-checker` overrides it. A config file is generated for each one listed. |
| `dev_tools` | `[]` | Repository-relative folders or files that are developer tooling (`["devtools", "scripts"]`). Entries are normalised (`./tools/` is `tools`); `.`, `..` and absolute paths are refused. They need no ring or `__arbor__` annotation (never `unclassified`), may import product code, and are skipped by the ring, external-import and effect rules. A product module that imports one fails with `dev-tool-import`. A module annotated `__arbor__ = {"role": "dev_tool"}` outside these paths fails with `dev-tool-misplaced`. Tooling still enters the suppression and discard inventories, and the report lists it under its own `dev tools` heading, apart from `exclude`. Root-level (shared by all scopes). |
| `dev_tools_forbid` | `["process"]` | Effect categories (`clock`, `env`, `process`, `filesystem`, …) that `dev_tools` modules may not use. The default reports raw `subprocess` use in tooling; `[]` allows everything. Reported as the matching `effect-*` rule. |
| `dev_tools_may_import` | `[]` | Dotted-module globs of product modules that `dev_tools` modules may import. Empty means any; otherwise an import of another product module is `dev-tool-product-import`. |
| `effect_defaults` | `"injection"` | How a bare parameter default that names an effect (`now=datetime.now`) counts. `"injection"`: never a use. `"typed"`: not a use only when the parameter is annotated `Callable` or `Mapping` (alone, in `Optional`/`Union`/`\|`, or as a string), so `x: int = datetime.now` still counts. `"count"`: always a use. Calls, aliases and lambda-wrapped defaults always count; applies to `clock`, `env`, `identity`, `randomness`, `sleep`, `threads` and `process`. Settable per scope. |
| `taint_seeds` | every `effect-*` category that existed before 0.8.0 except `patch` and `global`, plus `network` (`monotonic`, `host` and `fs-query` are valid but not seeded unless listed) | Which I/O primitives the function-level taint pass (#98, below `classify-facts`) seeds a function as reaching. `[]` turns the pass into a no-op. Root-level (shared by all scopes). |
| `disabled_tools` | `[]` | Tool names intentionally not run or installed: `import-linter`, `tach`, `ruff`, `pyright`, `basedpyright`, `ty`, or `pyrefly`. Disabled tools are omitted from missing-tool hints and setup prompts. Also accepts `arbor-annotations` to ignore `__arbor__` annotations and their validation findings (see below). |
| `python` | `"3.11"` | Target Python version, a quoted string (`"3.12"`; a patch version such as `"3.12.1"` is read as `3.12`): written into the type-checker configs and selects which modules count as stdlib (`3.11` to `3.14`; anything else is an error). See [stdlib-table.md](stdlib-table.md). |
| `output_dir` | `"."` | Where `generate` writes the config files, relative to the root. |
| `baseline` | `"arborescent-baseline.json"` | The baseline file used by `arborescent baseline` and `check`. |
| `ratchet_files` | `[]` | Extra numeric ratchet JSON files (`{"values": {"name": 3}, "growth": []}`) checked by `check --growth-against`. |
| `suggestion_threshold` | `300` | Logical code lines above which `check` prints a report-only file-size suggestion. |
| `practices` | `{}` | Repository-hygiene checks (below). |
| `modularity` | `"ready"` | Needs `package_catalog` for `"report"`. `"ready"`: every package gap fails `check`. `"report"`: gaps are shown as notes and never fail. Independent of `preset`; `--require-modularity` certifies it (see [Strict linting and strict modularity](#strict-linting-and-strict-modularity)). |
| `package_catalog` | unset | Opt-in, any preset: path of a `packages.toml`. Every module and asset must belong to exactly one package (see [Package assignment](#package-assignment)). |
| `package_parts` | `[]` | Opt-in, needs `package_catalog`: relative paths, inside the repository, of extracted part trees to check against the catalog (see [Annotation preservation](#annotation-preservation-across-a-split)). |

| `package_pyproject` | `false` | Opt-in, needs `package_catalog`: derive each package's `pyproject.toml` fields from the annotations (see [Package pyproject.toml](#package-pyprojecttoml)). |

## Multi-scope configuration

In `[tool.arborescent]` or `arborescent.toml`, add a `[scopes.<name>]` table for each application, library or shared
repository component. Each scope requires non-empty `sources`, `source_roots` and `rings`.

| scope key | default | meaning |
|---|---|---|
| `sources` | required | Literal repository-relative source paths owned by this scope. Source paths cannot overlap. Each Python file has one owner. |
| `source_roots` | required | Paths used to derive dotted module names for this scope. Two scopes may use the same local ring names and module names, but duplicate derived module names are an error. |
| `rings` | required | This scope's distinct rings, innermost first. |
| `classify` | `{}` | This scope's module glob -> local ring table. |
| `ring` | `{}` | Per-ring `may_import`, `external`, `os_branching` and `forbid` tables, local to this scope. |
| `public` | `[]` | Dotted module patterns consumers may import from this scope. |
| `depends_on` | `[]` | Scope names this scope may import. Each cross-scope import must target a declared dependency and match its `public` patterns. |
| `preset`, `middle_forbid`, `io_modules`, `groups` | existing defaults | Local external-import, OS and effect defaults, plus named allow-list groups. |
| `module_forbid_imports`, `importable_by`, `dependencies`, `entry_scripts`, `forbid_everywhere`, `forbidden_modules`, `forbid_circular_imports`, `tach_external_exclude`, `require_arbor_annotations`, `require_arbor_annotations_exclude` | existing defaults | Existing architecture settings, evaluated within this scope. |

Root settings such as `python`, `type_checkers`, `disabled_tools`, `output_dir`, `baseline`, `suggestion_threshold`,
`ruff`, `practices`, `exclude` and `children` apply to the composed repository. Root-level architecture keys (`rings`, `sources`,
`source_roots`, `classify`, `ring`, `preset`, `io_modules`, `groups` and the other architecture settings) are rejected
when `scopes` is present. Scope names and dependencies must be unique and known, and the dependency graph must be
acyclic.

## Nested package arbors

Use `children` to configure package roots that may have their own architecture policy. Each path is relative to the
parent policy root, unique and non-overlapping. In the default `child_policy_mode = "certify"`, a direct child
`arborescent.toml` or `pyproject.toml` with `[tool.arborescent]` gives that package its own policy; Arborescent checks
it recursively and excludes its source from the parent scan. If no direct policy is declared, the package remains
part of the parent scan. Arborescent never searches upward for a child policy.

```toml
[tool.arborescent]
children = ["packages/collection_core"]
child_policy_mode = "certify"
```

To apply the parent policy to a listed package even when it has its own config, set `child_policy_mode = "parent"`.
The parent then scans the entire configured source tree, and no child checks run. To include convention-named vendor
directories in that scan, set `scan_vendors = true`; normal `exclude` rules still apply.

With `child_policy_mode = "certify"`, other packages and source directories stay under the parent's own integrated
policy unless they have their own direct policy and are listed as children. `arborescent check` invokes each such child
from its own root, so it uses its own rings, baseline, and native tool configs; nested children are checked recursively.
The parent's `CONSISTENT` status requires its configured checks and every child's configured checks to pass. With
`--require-profile`, the parent passes that requirement to every child and prints `CERTIFIED` only when the parent and
all children meet the minimum profile. A declared path without its own policy remains parent-owned; a discovered child
with an invalid or failing policy makes the parent check fail.

Arborescent does not rewrite project-owned Tach, Ruff, or type-checker configuration. When those parent tools should
only govern parent source, exclude each independently certified child path in their native config as well. The child invocation runs those tools
from the child root with the child's native configuration.

For example, libraries with different core layers can share one repository-owned binary parser:

```toml
[tool.arborescent]
python = "3.12"

[tool.arborescent.scopes.packet_archive]
sources = ["packages/packet-archive/src/packet_archive"]
source_roots = ["packages/packet-archive/src"]
rings = ["domain", "adapters"]
public = ["packet_archive.api*"]
depends_on = ["binary_parser"]

[tool.arborescent.scopes.packet_archive.classify]
"packet_archive.domain*" = "domain"
"packet_archive.adapters*" = "adapters"

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

[tool.arborescent.scopes.binary_parser]
sources = ["packages/binary-parser/src/binary_parser"]
source_roots = ["packages/binary-parser/src"]
rings = ["domain", "adapters"]
public = ["binary_parser.reader"]

[tool.arborescent.scopes.binary_parser.classify]
"binary_parser*" = "domain"
```

`binary_parser.reader` has one source owner and one local policy; both libraries can import it because they declare the
dependency and the module is public. The libraries keep their independent ring hierarchies. A shared helper is never
duplicated to satisfy scope ownership. Scope metadata stays centralized in the root config; extracting a library later
uses the package metadata and extraction tools below.

## Package metadata and extraction

Repository package ownership and extraction metadata live in a root `packages.toml` file. Arborescent owns the schema, the
validation and the manifest that records an extraction; the repository declares stable package identities and the paths/roles
it owns. The extraction itself is done by a migration library (git filter-repo, with history) that `packages extract` drives.
Each path can carry string metadata that is recorded in the manifest, such as the import package name or the relative path to
an independent `arborescent.toml`.

### What "package" means

In Arborescent a package is a Python distribution-sized ownership unit: a stable identity that owns a set of paths, and
that can be extracted into its own repository. It is not a promise of cross-language enforcement.

- Non-Python files inside a package (templates, SQL, data) are assets. They can carry metadata, but Arborescent does not
  analyse them.
- A repository that mixes languages is several units, each described by its main language. Only the Python unit gets
  import analysis, rings and cycle checks.
- Renaming the concept and polyglot support are deferred; see [#31](https://github.com/112345brian/arborescent/issues/31).

```toml
[[packages]]
id = "collection-core"
name = "Shared Collection Core"
kind = "shared-core"
repository = "org/repo"
root = "packages/collection-core"
summary = "Domain-neutral run lifecycle and source execution."
capabilities = ["source-runner", "health-aggregation"]
depends_on = []
extraction_status = "experimental"

[[packages.paths]]
path = "packages/collection-core/src/collection_core"
role = "source"
metadata = { import = "collection_core", arborescent_config = "arborescent.toml" }

[[packages.paths]]
path = "packages/collection-core/tests/test_collection_core"
role = "tests"
destination = "tests/test_collection_core"
```

The package root and every owned path must stay inside the repository. Package IDs are stable lowercase slugs, path
ownership cannot overlap, and declared dependencies must form an acyclic graph. `packages extract` hands the declared paths to
a migration library, which writes them to a new repository, and then writes `.arborescent-package.toml` (see "Extraction
manifest" below). A package with its own direct Arborescent policy is extracted with that policy when it is listed as a
configuration path; the child policy remains independently usable after extraction.

Arborescent does no copying: which files cross (generated output, symlinks, special files, history) is the migration
library's job, and git filter-repo only carries what is committed. Run it from a clean checkout of the branch to extract.

```sh
arborescent packages validate
arborescent packages list
arborescent packages extract collection-core --output /tmp/collection-core         # dry run: prints the exact arguments
arborescent packages extract collection-core --output /tmp/collection-core --yes   # extract, then write the manifest
```

`extract` is the `migrate` pipeline (see [migration-driver.md](migration-driver.md)) plus the manifest: the library runs, the
import rewriter applies any declared `module_renames`, and then Arborescent writes and re-reads `.arborescent-package.toml`.
`--output` is another name for `--target`; `--repository` sets where the manifest says the tree now lives (default: the
package's home repository, else the repository it came from). The destination must not already exist. A dependency is a
separate package, so extract each one on its own: `--with-dependencies` was removed. Extraction does not invent build
configuration; the package metadata declares the layout and the package owns its build and architecture files.

### Extraction manifest

`.arborescent-package.toml` (`manifest_version = 2`) keeps what a package *is* apart from where it *is* and what
extraction *did*:

| Table | Holds | Changes on extraction? |
|---|---|---|
| `[package]` | `id`, `name`, `kind`, `domain`, `summary`, `capabilities`, `depends_on` | never |
| `[[paths]]` `role`, `metadata` | per-path meaning: role plus custom metadata (import name, boundary, `arborescent_config`, scope) | never |
| `[origin]` | `repository`, `root` and `extraction_status` as declared in the catalog | no; kept for traceability |
| `[[paths]]` `origin_path` | the path in the origin repository | no; kept for traceability |
| `[location]` | the current `repository` (defaults to the package's home repository, else the origin; `--repository` sets another) and `root` (`.`) | yes |
| `[[paths]]` `current_path` | where the path lives inside the extracted tree | yes |
| `[extraction]` | `status = "extracted"` and `included_packages` | yes; describes the operation |
| `[[dependencies]]` | the same stable fields for each included dependency, plus its origin and `current_root` | as above |

Module identity is not checked at extraction any more. The old extractor compared the module names a shipped policy derives
before and after the move and refused a layout that changed one. A migration library moves paths; a path move that changes an
import name needs a declared `module_renames` entry, which the driver's rewriter applies, but **nothing yet checks that the two
agree**. This is a known gap, not a covered case. `arborescent_config` metadata is relative to the package's current root and
must be shipped as a declared path.

The manifest is a provenance snapshot taken at extraction, not an integrity seal: an extracted package is expected to
evolve, so its files are not hashed.

`arborescent packages validate --extracted DIR` re-validates an extracted tree (`packages extract` runs the same structural
check after writing the manifest): the manifest must be well formed (stable lowercase package ids, status `extracted`, normalized origin paths, at
least one path, each path inside its package's current root, no duplicate dependencies); current paths
must resolve inside the tree; no symlink may leave it (generated directories such as `.venv` are ignored);
`included_packages` must match the package and its dependencies; and every shipped policy, whether
`arborescent.toml` or a `pyproject.toml` with `[tool.arborescent]` and wherever it was placed, must load and find its
`sources` and `source_roots`. A parent policy (`children`, `child_policy_mode`) is neither read nor changed by extracting
a child; the child keeps governing itself.

`suggestion_threshold` controls a report-only hint for large files. The metric counts lines containing code tokens and
ignores blank lines, comments, module/class/function docstrings, and multi-line literal-only data tables. Suggestions
appear during `check`; they are not violations, are never added to the baseline, and do not change the exit status.
There is no per-file suppression. Function size and complexity remain Ruff rules.

`module_forbid_imports` adds focused internal import restrictions without changing the module's ring. For example,
`"bootstrap.adapters" = ["bootstrap.launcher"]` blocks that package and its descendants only in the named adapter
module (or matching module glob).

`importable_by` says who may import a module, so a new module cannot escape by not being listed:

```toml
[tool.arborescent.importable_by]
"paths" = ["container", "build", "ingest.*"]
"private_git" = ["*_service", "composition_root*"]
```

A module not listed is unrestricted. A listed one (and its submodules) may be imported only by a module matching one of its globs;
the restricted unit's own modules may always import each other, and every matching restriction must allow the importer. Every
spelling of the import counts (`import`, `from pkg import mod`, relative, lazy, `TYPE_CHECKING`). Each key or importer glob that
matches no module is reported as `restricted-import`, so a rename cannot leave the list restricting nothing; `selftest` injects one
disallowed import per entry.

`owned_literal` gives a string to the modules and rings that may spell it. A protocol shared by a parent adapter and a loader
(an environment variable name, a message key) then cannot leak into an inner ring, and a third module cannot copy it and drift:

```toml
[[tool.arborescent.owned_literal]]
literal = "KNOWLEDGE_CONFIG"
modules = ["paths", "script_runner"]     # module globs; the only modules that may spell it
rings = ["infrastructure"]               # and no module outside these rings (either list may be left out, not both)
reason = "the protocol between the process adapter and the config loader"
```

`literal-outside-owner` names the module, its ring and the line of each other spelling, and repeats the `reason`. Only
identifier-like strings (letters, digits, `_`, `.`, `-`, at most 80 characters) are recorded, and docstrings are not spellings.
A `modules` glob that matches no module is reported, and `selftest` spells each literal in a module that does not own it.

`construct_only` lets only some modules and rings call a class or factory. `importable_by` cannot say "anyone may import the reader's
module for its type, only the composition root may build the unfiltered one"; this can, and it is the checkable half of a wiring rule
("the shell is wired to the unfiltered reader"): the unfiltered reader is its own class, and only the composition root constructs it.

```toml
[[tool.arborescent.construct_only]]
target = "app.queries.UnfilteredQueries"   # dotted name of the class or factory, spelled as callers import it
rings = ["composition"]                    # rings that may call it (and/or `modules`, module globs; not both empty)
reason = "the unfiltered reader must not reach the MCP shell"
```

`construction-outside-site` names the module, its ring and the line of each other call, and repeats the `reason`. It goes into the
baseline like any rule, so existing violations can only shrink. A `modules` glob, or a target whose module does not exist, is reported,
and `selftest` calls each target from a module that may not. What it does not see: a name re-exported by another module (list the
path callers import, or one entry per spelling), `partial(X)` or `make = X; make()`, `getattr`, a subclass, and an argument passed to a
constructor (it checks who builds, not what is passed in). A parameter named like an imported class would be mistaken for it.

`result_discard` is an opt-in check that a caller reads the outcome of a function it calls (a new "refused" or "skipped" outcome
is otherwise silently ignored). It reports `result-discard` for a call written as a whole statement:

```toml
[tool.arborescent.result_discard]
functions = ["app.usecases.place_order"]   # dotted names marked result-bearing, whatever they are annotated with
rings = ["adapters", "composition"]        # only modules in these rings are checked (leave out for every ring)
convention = true                          # default: a module-level function annotated `-> *Result` or `-> *Outcome` is marked
```

A function is result-bearing when it is listed in `functions`, or (with `convention`) when its module-level definition is
annotated `-> FooResult` / `-> FooOutcome` (also `"FooResult"`, `mod.FooResult`, `FooResult | None`, `Optional[FooResult]`; not
`list[FooResult]`, and not a bare `Result`). Binding, returning, passing on or testing the value is a use, and `_ = f()` is a
deliberate discard; `await f()` as a statement is reported. Calls are resolved purely by syntax: `f()`, `module.f()`, an
aliased or relative import, and a call inside the defining module. Dynamic calls are out of scope and never reported: methods
(`obj.f()`), callbacks and variables, `getattr`, `functools.partial`, `*` imports, re-exports and shadowed names. Use a type
checker's unused-result check for those. This is separate from the `discard(value, reason=...)` inventory above.

`[tool.arborescent.docs]` makes a module say what it is, and says it in the ring the policy enforces:

```toml
[tool.arborescent.docs]
module_docstring = true    # missing-module-docstring: every module has a non-blank docstring
module_names_ring = true   # docstring-ring-mismatch: the docstring names the module's effective ring
exclude = ["app.generated*"]   # module globs both rules skip
```

The ring is the one `classify` or `__arbor__` gives the module, matched as a whole word, case-insensitively (`Ring: domain`,
`the domain ring`; `subdomain` does not count), so a docstring cannot claim a different ring than the one enforced. A docstring that
names only other rings says which. Both rules are off by default, and both join the baseline ratchet: record today's gaps, and new
ones fail while fixed ones must leave the file. A module without a docstring gets `missing-module-docstring` only, and a module
with no ring (unclassified, or developer tooling) is never asked to name one. `selftest` adds a module with no docstring and one
whose docstring names another ring. Not covered: docstrings on public classes, functions and methods; use Ruff's `D1xx` rules for those.

Tool configs remain project-owned and are discovered natively by their tools. `arborescent generate` creates missing optional presets
and skips every existing file. Drift is shown only for files still marked as Arborescent-generated,
and is informational. `check --require-tools --require-profile` requires Arborescent minimum profile v1 (verified with Ruff 0.17.2,
Tach 0.35.3) and behaviorally probes the required Ruff rules. A different installed Ruff or Tach version prints
`PROFILE WARN` and does not fail the check; a missing tool or a rule whose probe no longer fires still fails.

Use `disabled_tools` for tools this project deliberately does not use, so their absence does not keep appearing as a
setup hint or prompt. Disabled tools are skipped even with `--require-tools`; certification still fails if its required
Ruff or Tach checks are disabled. For example, `disabled_tools = ["import-linter", "tach", "ruff"]` leaves only the
configured type checker active.

`CONSISTENT` means the enabled checks ran successfully, including intentional tool exclusions. Missing or command-line
skipped checks do not receive this label. `CERTIFIED` is printed only when the minimum profile also passes. A project
can therefore be consistent with its own policy while remaining uncertified because it disabled required profile tools.

`disabled_tools = ["arbor-annotations"]` opts out of annotation handling: all ring assignments come from `classify`,
and `invalid-arbor-annotation` findings are not emitted. Other architecture checks still apply. This opt-out prevents
minimum-profile certification, even when the remaining enabled checks pass and the project is `CONSISTENT`.

`[tool.arborescent.ruff]` composes overrides over the optional generated Ruff preset; it never replaces that preset. It accepts
`extend-select`, `ignore`, `per-file-ignores`, `max-complexity`, `max-args`, `max-branches`, `max-returns`, and `max-statements`.
Under the `strict` preset the generated `ruff.toml` already sets them tighter than Ruff's defaults (complexity 8, branches 10,
statements 40, returns 6, args 5) and selects `PLR0911`; `standard` and `relaxed` keep Ruff's defaults. A key you set here wins
over the preset's value for that key only. `generate` never overwrites an existing `ruff.toml`, so an existing project picks
the new limits up by deleting its generated `ruff.toml` and running `arborescent generate` again.
The base includes correctness, typing, security, modernization, path, and performance rule families in addition to
complexity and architecture-aware rules. Ruff's broad test-assertion and subprocess-wrapper warnings are scoped so
tests remain idiomatic and real process adapters can declare their deliberate subprocess boundary. `PIE807` is ignored
because it conflicts with strict type inference for container factories. `check` prints `standard rules (baseline vN)` when no rule
customization is active, or `customised rules: ...` with the changes that apply.

### Package assignment

Set `package_catalog = "packages.toml"` (any lint preset) and `arborescent check`
reports, by rule id, every first-party module and every asset that does not belong to exactly one package. It reuses the
`packages.toml` above: a package claims a file by listing it, or a folder above it, in `paths`. The catalog is only
parsed for this check, so a double claim that `packages validate` refuses outright is reported as a finding naming the
module and every claiming package.

Non-Python files (templates, static files, vocabulary data, SQL) below the policy's `sources` are assets. Beyond the
package `paths`, a catalog can claim assets with ordered globs; `*` and `?` stay inside one folder, `**` spans folders,
and **the first matching rule wins**, so globs never claim a file twice. A glob that gives a file to a package other than
the one whose path contains it is a double claim. The policy's `exclude` patterns and generated output (`__pycache__`,
`build`, `dist`, `*.pyc`) are not assets; modules under a declared child arbor are not scanned and so not assigned.

```toml
[[assets]]
pattern = "src/**/templates/*.html"
package = "web"
```

Repository-level files (`LICENSE`, `README.md`, CI workflows, the lock file) belong to no package. Give them to the
workspace with `package = "@root"`: they are then not unowned, and are never carried into a package on extraction. `@root`
is only an `[[assets]]` owner, not a package id; a root-owned file that a package path also contains is a double claim.
The catalog file itself (`package_catalog`) is never an asset.

```toml
[[assets]]
pattern = ".github/**"
package = "@root"
```

Findings that name a package pair (`package-multiply-assigned-*`, `package-undeclared-dependency`) are failures like any
other rule and can be baselined. `package-spans-rings` is printed as a `NOTE` and neither fails the check nor enters
the baseline. `selftest` probes each rule when `package_catalog` is set: a new unowned module or asset, a second
package claiming an owned file, a glob contradicting a path owner, a package spanning two rings, and a new import across
packages with no declared dependency. A probe that cannot be built for the catalog is listed with the reason.
This adds no behaviour to `packages extract`, which is unchanged.

**Two layers.** The *annotation layer* is everything on this page that describes the code: the `__arbor__` ring
annotations ([below](#module-ring-annotations-__arbor__)), the package catalog and its keys above, and the checks that
report a missing or contradictory annotation by rule id. From it Arborescent builds the *migration arguments*: typed,
tool-neutral objects (`arborescent.migration_arguments`: `MigrationArguments`, `PackageArguments`, `OwnedModule`,
`OwnedAsset`, `PathRename`, `Bypass`, `Edge`, `Gap`) that carry, per package, its identity and `home`, the modules and
assets it owns with their target paths, the module-name map, its public surface and the modules that bypass it, its
declared dependencies and the open gaps. `packages export` is those objects serialised as JSON. Any tool can consume the
arguments; none is named in them.

The *auxiliary driver* is a separate layer on top. Its only job is to supply a dedicated migration library with the
arguments it needs, through a port; it does not migrate, and the annotation layer never depends on it. The command is
`arborescent packages migrate` ([migration-driver.md](migration-driver.md)).
[migration-recipes.md](migration-recipes.md) describes it and shows, with a worked example and the caveats found by running
each tool, how to supply the same arguments by hand to git filter-repo, `git subtree split`, rope, deptry, Copybara and
Josh. Those recipes are documentation, not commands Arborescent ships.

### Migration annotations and export

(The [migration driver](migration-driver.md), `arborescent packages migrate`, hands one package's arguments to a migration library.)

A package entry can also say where it will live and what it exposes, and `arborescent packages export` publishes all of
it as one versioned JSON document for external migration tools (it converts nothing and migrates nothing):

```toml
[[packages]]
id = "core"
# ... id, name, kind, repository, root, summary, depends_on, paths as above
public = ["app.core.api", "app.core.models.*"]   # the public surface (module-name globs)
externals = ["pydantic"]                          # third-party import names it may use
[packages.home]                                   # kind = "repository", repository = "org/core"
kind = "workspace-member"                         # ... or a member of a workspace in the same repository
path = "packages/core"
[[packages.module_renames]]                       # module names that change in the target
from = "app.core"
to = "core"
```

**Before the move (a flat layout).** `root` is where the package will live, so it may be a folder that does not exist yet.
When no owned path sits inside `root`, every path must set `destination`, which says where that path lands; the catalog is then
accepted and the assignment checks (`package-undeclared-dependency`, `package-spans-rings`, private imports) run on the
layout as it is today, which is how the move can be guarded before it is made:

```toml
[[packages]]
id = "core"
root = "core/src/core"                  # the target folder, not yet created
# ... name, kind, repository, summary
[[packages.paths]]
path = "privacy.py"                     # where the module sits today, beside other packages' modules
role = "source"
destination = "core/src/core/privacy.py"
```

All four keys are optional; a missing one is a named gap in the export when it matters (`public` only when another package imports the package), never a guess. See
[migration-export.md](migration-export.md) for the document, the schema, the gap codes, a `git filter-repo` recipe and the
comparison with blinkered's `blinkered.toml`.

#### Package contracts for the engines: the cut list

With `package_catalog` set, `arborescent generate` also compiles the assignment into the engines' own config, so what
import-linter and tach report for a package **is** its cut list; Arborescent walks no import graph for it. A package's
interface is its `public` list (see "Annotation preservation across a split"); a package without one is open.

- **import-linter**: one `forbidden` contract per package, `package <id> imports only declared packages' public modules`.
  Its sources are the package's modules, its forbidden modules every module of another package that the package does
  not list in `depends_on`, plus every non-public module of a package it does list. Contracts use
  `allow_indirect_imports = true`, so the output is the list of direct edges to cut.
- **tach**: a `[[modules]]` entry per module as before, now with an explicit `depends_on` that is the ring rule
  intersected with the same package rule (own package, a declared package's public modules). Setting `package_catalog`
  therefore switches tach to `layers_explicit_depends_on`, whose lists grow with the number of modules.
- **third-party names**: one `package <id> imports no I/O or third-party package its rings do not list` contract per
  package, forbidding the names that none of its modules' rings list. The ring contracts already imply this for a
  classified module; it names the package and also covers a module that has no ring. A package with a ring that allows
  every external package (`"*"`) gets no such contract.

A module claimed by no package or by several is left out of these contracts (those are the gap findings above). A package
that is allowed to import everything has no boundary contract. A package whose rings and edges produce no violation, and
that has no gap, can leave the monorepo. `check` reports a contract failure as the engine's own failure and flags a
generated file that is out of date as drift. The engines' output is not ratcheted through the baseline: only Ruff's is.
The same edges are also reported by Arborescent's own `package-undeclared-dependency` (#52) and `package-private-import`
(#55) rules, which need no engine installed; a test keeps the contracts and those rules from disagreeing.

### Strict linting and strict modularity

Two independent questions with two independent keys. `preset` (`strict`, `standard`, `relaxed`) is how strictly the
code is linted. `modularity` is whether the repository is ready to be split: with `package_catalog` set, `ready` (the
default) fails `check` on every open package gap, `report` shows them as `NOTE` lines and never fails. A `relaxed`
repository can be modularity-ready and a `strict` one can be not.

`arborescent check --require-modularity` certifies it: it fails (`MODULARITY FAIL ...`) when no `package_catalog` is
set, when `modularity` is `report`, when an error-severity `package-*` gap is open, or under `--changed` (gaps are a
whole-repository fact). A non-empty cut list (`package-spans-rings`) is information and is allowed. One package owning every module
(`package-owns-everything`) is information too: a package's size says nothing about its quality. On success it
prints `CERTIFIED modularity ready`. `--require-profile` is unrelated: it certifies lint and tool versions only.

### Annotation preservation across a split

After a split by other tools, the question for Arborescent is whether the annotations survived, not whether the graph
or the tests did (grimp, import-linter, tach and each part's own suite judge those). Two opt-in inputs, both needing
`package_catalog` (any lint preset):

- A package may declare `public = ["app.api", "app.api.*"]` in `packages.toml`: the dotted module globs other packages
  may import (the same matching as a scope's `public`). Without `public`, a package has no declared surface and nothing
  of it is private. `public` is stable semantic metadata: extraction carries it into `.arborescent-package.toml`, and
  only when set, so manifests of packages without one are unchanged.
- `package_parts = ["split/core", "split/shell"]` lists extracted trees (#27) that live inside the repository, for
  example a staging folder the splitting tool writes to. Keep them outside the policy's `sources` or in `exclude`, or
  the origin scan would see their modules twice. Each part is read with its manifest and scanned under the policy it
  ships; a part that is not a valid extraction or ships no policy is an error, never a pass. Parts outside the
  repository are not supported: `selftest` mutates a copy of the repository and must carry the parts with it.

With parts, `check` compares each part with the catalog and the origin scan: semantic metadata (`id`, `name`, `kind`,
`domain`, `summary`, `capabilities`, `depends_on`, `public`, and each path's role and metadata, keyed by origin path)
must be equal; where a path now lives is a coordinate and is free to change. Module identity is assumed stable (#27): a
renamed module reads as one missing and one extra, since there is no explicit module mapping yet. Only classes and
functions count as symbols; a dropped constant is not seen.

`package-private-import` also runs on the origin without parts, for every package that declares `public`, and on each
part's modules, resolving their imports by origin module identity, so a part importing a private module of another part
is found. The ring comparison uses the effective ring, so a changed `classify` entry and a moved `__arbor__` annotation
both show. Rings that disagree between `__arbor__` and `classify` inside one tree remain `arbor-ring-mismatch`, and an
import across packages without a declared dependency remains `package-undeclared-dependency`; neither is repeated here.
`selftest` probes each rule that applies (one needs parts) and lists a probe it cannot build with the reason.

### Release, pin and history policy

Some inputs to a split cannot be derived from the code: how dependents pin a package, whether its history is kept, which
merge style keeps pinned SHAs reachable. They are decisions, so a package entry in `packages.toml` records them.
Arborescent stores and validates them and carries them in the `policy` field of the migration export; it releases, tags,
pins and rewrites nothing. The tools that do receive these values as arguments.

```toml
[[packages]]
id = "core"
# ... name, kind, repository, root, summary, paths as above
[packages.home]
kind = "repository"
repository = "https://github.com/org/core.git"
history = "preserve"               # "preserve" or "fresh": keep the package's history, or start a new one
[packages.release]
pin = "tag"                        # "tag" or "sha": how dependents pin this package
merge = "merge-commit"             # only "merge-commit": squash or rebase would orphan pinned SHAs
tag_before_branch_delete = true    # true or false: tag the release before its branch is deleted

[[packages]]
id = "scratch"
# ...
unmanaged = true                   # explicitly outside release, pin and history policy

[[packages]]
id = "text-claims"
# ...
vendored = true                    # a read-only copy of code maintained elsewhere
```

Every field is optional. Values are checked when the catalog is read: a value outside the lists above, a wrong type, an
unknown key under `[packages.release]`, `unmanaged = true` together with a policy, or a policy on a workspace member is an
error naming the package (a hard error like any other malformed catalog, not a baselinable finding). Any key a
`[[packages]]` entry does not know is rejected the same way, so a typo such as `histroy` cannot pass silently. `unmanaged`
can only be `true`; omit it to declare a policy.

`vendored = true` marks a package whose code is a read-only copy maintained elsewhere (for example under a `vendor_roots`
folder). It is never edited here, so it declares no release, history or `unmanaged` policy (a catalog error if it does),
is exempt from `package-release-policy-missing`, and gets no derived `pyproject.toml`. Ownership, `depends_on` and the
public-surface checks still apply, since other packages must import it correctly. The export carries `vendored: true`, a
`null` `policy` and no derived `pyproject`, so migration tools leave it alone.

A package that declares neither a policy (`history` or a `[packages.release]` table, even an empty one) nor
`unmanaged = true` is a named gap, reported as `package-release-policy-missing`; it fails the check like the other
assignment gaps, can be baselined, and appears in the export's `gaps`. A workspace member is exempt: it stays in the
repository, so it has no history or pin of its own, and its exported `policy` is always `null`. `selftest` probes the
rule by adding a package with no policy.

In the export, `policy` is `null` when nothing is declared, and otherwise
`{ history, release: { pin, merge, tag_before_branch_delete } | null, unmanaged }`, with `null` for each field that is
not set. An unmanaged package exports `{ history: null, release: null, unmanaged: true }`.


### Package `pyproject.toml`

Set `package_pyproject = true` (needs `package_catalog`) and `arborescent generate` creates, for every package in the
catalog, the `pyproject.toml` at `<root>/pyproject.toml` when it does not exist. **An existing file is never rewritten.**
Each package therefore needs its own `root`; two packages sharing one is an error that names both.

Derived from the annotations (the only fields `check` compares):

| field | derived from |
|---|---|
| `[project].dependencies` | the third-party names in the `external` allow-lists of the rings of the package's modules, mapped to distributions through the policy's `dependencies` table (`bs4 = "beautifulsoup4"`), plus the `id` of every package in `depends_on` (a package's distribution name is its `id`). Version specifiers are not compared. |
| `[tool.uv.sources]` | one entry per sibling in `depends_on`, from the homes of the two packages: `{ workspace = true }` only when both are workspace members; `{ git = "<the sibling's catalog repository>", subdirectory = "<its home.path>" }` for a workspace member that a package outside the workspace needs; `{ git = "<home.repository>" }` for a package in its own repository; no entry when the sibling has no `home` (the export reports that gap). The pin (`rev`, `tag`, an `index`) is yours and is not compared. An extra `workspace = true` entry for a package that is not a dependency is drift. A git value that is not a URL gets no entry and is reported (below); Arborescent never guesses a URL or a host. |
| `[tool.uv.workspace].members` | the `home.path` of every workspace-member package. Written into the root `pyproject.toml` (the file of the package rooted at `.`, or a new one when there is none). |
| `[tool.arborescent]` and `[tool.tach]` `source_roots` | the policy's source roots that contain the package's modules, relative to the package root |
| `[tool.importlinter].root_packages` | the top-level packages of the package's modules |

A new file also gets a template: `name` (the package `id`), `description` (its `summary`), `version = "0.0.0"` and
`requires-python` from the policy's `python`. The template adds no build backend, classifiers or other packaging
configuration. A `[project]` that lists `dependencies` as `dynamic` is not compared; the three `source_roots` and
`root_packages` tables are compared only when the file has them.

`check` reports a differing derived field as `package-pyproject-drift`, naming the file, the field, both values and the
annotation it disagrees with. Like generated-preset drift it is advisory: a `NOTE` line that neither fails the check nor
enters the baseline, and the file is left as it is. A missing file is not drift (`generate` creates it). The same rule also reports, whether or not the file exists, a git source that would be built from a value that is not a URL (`org/name`, for instance, which uv does not accept).
Changing one package's `home` changes only the `sources` entries that depend on it (its consumers', and its own when it has dependencies) and the member list. The files are written at
each package's current `root`, while `members` lists `home.path`, the post-migration location; they are the same in a
repository that already has the target layout. With `package_pyproject` set, `packages export` fills each package's
`pyproject` object (path, `requires_python`, dependencies, sources, workspace members, source roots, root packages) with
the same derived values; it is `null` otherwise. `selftest` probes the rule by appending a workspace source for
a non-dependency to a derived file; with no derived file on disk the probe is listed as skipped with the reason.

### Package constraints: `[package.constraints]`

A package can state what it promises *not* to do, as constraints rather than position (the host owns the rings; see
`docs/superpowers/specs/2026-11-10-host-owned-rings-and-admission-design.md`). The table lives in the package's own policy
file, `arborescent.toml` or `[tool.arborescent.package.constraints]` in `pyproject.toml`, and is **separate from**
`__arbor__` annotations and from `packages.toml` (a `[[packages]]` entry carrying `constraints` is still an unknown key).

```toml
[package.constraints]
must_be_pure = true              # bool, default false: no effect use of any category
forbid_effects = ["network"]     # effect categories this package never uses, default []
```

`forbid_effects` takes the scanner's effect categories (`clock`, `env`, `identity`, `randomness`, `sleep`, `threads`,
`process`, `database`, `network`, `filesystem`, `dynamic-import`, `patch`, `console`, `global`); duplicates collapse. They
apply to paths with role `source`; test, docs and asset paths are exempt.

There is no blind spot in the schema: an unknown key under `package` or `package.constraints`, an unknown effect name
(`netwrok`), a non-boolean `must_be_pure`, or a `forbid_effects` that is not a list of names is a policy error. With no
`[package]` table nothing changes.

Two independent rules enforce the table, each module judged against **its own scope's** ladder (the table sits at the policy
root, so in multi-scope mode one promise covers every scope and each module is checked against the ring it is placed in):

- `package-constraint-facts`: the module **uses** a forbidden effect, in whatever ring it sits, so a host cannot loosen the
  promise by placement. `forbid_effects` covers the module's own effect uses (the scanner's per-function analysis and
  category taxonomy; no second detector). `must_be_pure` forbids every category and also: any non-type-only import of
  something that is neither first-party nor on the pure allow-list (the structural stdlib `__future__`, `typing`,
  `dataclasses`, `collections`, `enum`, `abc`, plus `math`, `re`, `functools`, `itertools`, `operator`, `json` and similar
  computation-only modules; `arborescent.package_constraint_rules.PURE_STDLIB` is the list), and any import of a first-party
  module from which a forbidden effect is reachable (the existing `effect-via-import` graph: first-party, same scope,
  type-only imports carry nothing).
- `package-constraint-placement`: the ring the host put the module in **permits** an effect the package forbids (a ring's
  `forbid` list, or the preset default, leaves it open). Failing here, before any module misbehaves, is the point: tighten the
  ring or drop the constraint. For `must_be_pure` every effect category counts bar the test-only `patch`.

Limits: reachability is the file-level import graph, not a call graph, and it does not look inside third-party packages (the
import itself is refused instead); `must_be_pure` placement compares effects and does not read a ring's `external` list;
`forbid_effects` alone does not follow imports or restrict them; a module without a ring is left to `unclassified`. Paths
below a `tests`, `test` or `docs` folder, `conftest.py` and `dev_tools` modules are exempt. Both rules are blocking whatever
`modularity` says. `selftest` probes each with a new module (a clock call; an `__arbor__` annotation into a ring that permits
the effect) and lists a probe it cannot build, with the reason, as skipped.

## Module ring annotations: `__arbor__`

A module can declare its own ring with a module-level assignment to a literal dictionary:

```python
__arbor__ = {"ring": "domain"}
```

The `ring` value must be a string naming one of the policy's configured `rings` (the owning scope's rings in
multi-scope mode). The legal keys are `ring`, `ring_reason` and `role`; any other key reports `invalid-arbor-annotation`. `role` accepts only
`"dev_tool"` (`__arbor__ = {"role": "dev_tool"}`, no ring needed) and is valid only under a `dev_tools` path (`dev-tool-misplaced`).
Arborescent reads the assignment statically, without importing or executing the module. Only direct module-level
assignments count; assignments inside functions or classes are ignored (they are not module-level annotations at
all). An assignment inside conditional code at module level (`if`, `try`, `for`, `while` or `with`) is not ignored:
it reports `invalid-arbor-annotation`, because a ring that depends on control flow cannot be resolved statically. If
there are multiple unconditional direct assignments, the last one wins.

A module whose valid `__arbor__` ring disagrees with the ring its first matching `classify` glob would have assigned
reports `arbor-ring-mismatch`; the annotation still wins for enforcement (see resolution order below), but the
disagreement is surfaced so a stale `classify` entry or annotation gets noticed and reconciled.

Ring resolution follows this order while annotations are enabled (the default):

1. A valid `__arbor__` annotation takes precedence over every `classify` glob, and can classify a module even when no
   glob matches it.
2. If the annotation is absent, the first matching `classify` glob supplies the ring.
3. If the annotation is malformed or names an unknown ring, Arborescent reports `invalid-arbor-annotation` and still
   uses the first matching `classify` glob for enforcement. If no glob matches, it also reports `unclassified`.

For example, if `"app.domain" = "domain"` appears in `classify`, adding `__arbor__ = {"ring": 42}` to that module
reports `invalid-arbor-annotation`. Its imports, effects and OS branching are still checked under the `domain` ring;
the invalid annotation does not create a gap in those checks. A valid `__arbor__ = {"ring": "adapters"}` instead
assigns that module to `adapters`, regardless of the glob. Ring annotations are validated against the owning scope,
so scopes may reuse ring names without sharing a hierarchy.

Annotations apply to individual Python modules. An annotation in `__init__.py` assigns the package module's own
ring; it does not assign rings to its descendants. Scope boundaries are configured separately through `sources`,
`public` and `depends_on`; package extraction is separately invoked through `arborescent packages extract`.

### `ring_reason`: why a looser ring than the tightest one

```python
__arbor__ = {"ring": "adapters", "ring_reason": "wraps the OS client; no I/O yet"}
```

`ring_reason` is a human justification, never a computed fact. It must be a non-blank string without NUL characters
(otherwise `invalid-arbor-annotation`) and is only meaningful next to a `ring`. `arborescent admit` (below) reads it: it excuses
a ring looser than the tightest one that fits, and it is the only way to admit a module with a dynamic import whose target is
not a string literal. `check` itself does not judge it.

**Minimum version.** A tool older than the release that added `ring_reason` (the first release after 0.6.0) reports
`invalid-arbor-annotation` on a file that uses it, because unknown keys are errors. Upgrade every checkout and CI image
before `admit --reason` writes the key.

### Requiring annotations

`classify` remains supported indefinitely: resolution order 2 above is not deprecated, and most projects never need
to change it. `require_arbor_annotations = true` is an opt-in ratchet for a project (or one scope of a multi-scope
project) that wants to finish its migration to annotations and catch regressions afterward: once set, a module that
resolves its ring only through `classify` — no annotation at all, or an invalid one that falls back — reports
`unannotated-module` and fails the check, instead of silently relying on resolution order 2 or 3. This changes
nothing about *which* source wins; it only removes the option to rely on the fallback.

The flag is a per-scope architecture setting, like `forbid_circular_imports`: a repository can require annotations
in one newly-written scope while a legacy scope keeps classifying as before. `require_arbor_annotations_exclude`
takes `classify`-style globs on the dotted module name for modules that cannot carry an annotation at all — a
vendored, read-only or generated subtree — without forcing them into a separate scope; an excluded module still
resolves its ring normally (by `classify`, or by an annotation if it happens to have a valid one) and is simply not
required to have one. Setting `require_arbor_annotations = true` together with
`disabled_tools = ["arbor-annotations"]` is a contradiction — you cannot require what you have turned off — and is
rejected as a policy error.

Migrating an existing classify-only project:

1. Run `arborescent annotate --legacy --against REF` (dry-run by default) to see what it would insert; add `--yes` to
   write `__arbor__ = {"ring": ...}` into every module `classify` currently resolves but that has no existing
   annotation. This records the policy's placement, not a validated one: `classify` is human-written and nothing
   checks it against the module's facts. `--legacy` is required, and so is `--against REF` (any git reference, such
   as `origin/main`): a module added since the merge base of `REF` and HEAD is never annotated here; the command
   lists it and names `arborescent admit MODULE --ring R`, which scans its imports and effects before it writes. An
   unreadable `REF` (unknown reference, shallow clone, no git) fails with exit 1 and writes nothing. A module git
   sees as renamed is not new. The command is idempotent: a second run reports already-annotated files and writes
   nothing.
2. Run `arborescent discover` and `arborescent check` and confirm nothing is left relying on `classify` alone (add
   `require_arbor_annotations_exclude` entries for anything that genuinely cannot be annotated).
3. Set `require_arbor_annotations = true`. From then on, a new module that lands without an annotation fails
   `check` immediately rather than silently inheriting a `classify` glob.

## Rings: `[tool.arborescent.ring.<name>]` or `[tool.arborescent.scopes.<scope>.ring.<name>]`

| key | default | meaning |
|---|---|---|
| `may_import` | `[]` | Inner rings this ring may import; `["*"]` means every inner ring. A ring may always import itself. An outer ring in this list is a policy error (the onion rule). |
| `external` | `[]` | Top-level packages (third-party and stdlib) this ring may import; `["*"]` means unrestricted. Which imports actually need an entry depends on the preset. |
| `os_branching` | `false` | Whether this ring may read `os.name` or `sys.platform`. Only checked when the preset limits OS branching. |
| `forbid` | positional preset default | Effect categories this ring may not use: `clock`, `env`, `identity`, `randomness`, `sleep`, `threads`, `process`, `database`, `network`, `filesystem`, `dynamic-import`, `patch`, `console`, `global`, and, since 0.8.0, `state`, `monotonic`, `host` and `fs-query` (see *Effect categories*). The positional and preset defaults never include the four new ones: name them to opt in. |
| `max_reexport_ratio` | unset | A number from 0 to 1: the share of a module's public surface (a static `__all__`, else public top-level bindings; modules with fewer than 5 names are not judged) that may be first-party imports before `reexport-ratio` fires. Leave it unset on rings that legitimately aggregate (a composition root); the exemption is then visible in config. A dynamic `__all__` is ignored in favour of the bindings. Wrapping each name in a forwarding function evades it; that is the forwarded-import rule's job. |
| `require_abstract` | `false` | Every public (non-`_`-prefixed) top-level class or function in this ring must be an abstract shape: a class inheriting `abc.ABC`/`typing.Protocol` (or declaring `metaclass=ABCMeta`), or a function whose body is a stub (`...`, `pass`, or `raise NotImplementedError`, past an optional docstring). A concrete member fires `non-abstract-port`. |
| `require_typed_ports` | `false` | Every method of a `typing.Protocol` class in this ring must name its return type. A missing return annotation, `Any`, `object`, a bare `dict`/`list`/`tuple`/`set`/`frozenset` or `Mapping[str, Any]` (also inside `Optional`, `\|` unions and string annotations) fires `weakly-typed-port`; `None`, named classes and parameterised containers pass. `__init__` is skipped. Parameters are not checked. |

A ring that is not given a table has no inner imports, no external imports and no OS branching. If `forbid` is omitted,
the innermost ring forbids every effect, the outermost allows every effect, and middle rings use the preset default.
Custom ring names follow the same positional defaults. An explicit `forbid = []` allows every effect in that ring; if a
ring forbids any category, `dynamic-import` is also forbidden automatically.

## Presets

A preset changes how much of the external-import, OS and middle-ring effect rules apply. Ring layering, classification,
lazy-import detection and the entry-script check are the same in all of them.

| preset | imports needing a ring `external` entry | OS branching limited to named rings |
|---|---|---|
| `strict` (default) | every external import, stdlib included | yes |
| `standard` | third-party packages and the `io_modules` set | yes |
| `relaxed` | third-party packages only | no |

For middle rings, `strict` forbids every effect category, `standard` forbids `env`, `sleep`, `threads` and `process`,
and `relaxed` forbids none. The innermost and outermost positional defaults apply under every preset.

`middle_forbid = ["clock", "env"]` replaces that middle-ring default under any preset, so a `relaxed` policy can enforce
effect categories without the allow-lists `standard` and `strict` also require. A ring's own `forbid` still wins, and
`dynamic-import` is added whenever a ring forbids anything. In a scoped policy the key is set per scope.

`io_modules` also drives the generated import-linter contracts, which ban the I/O modules a ring does not list. Under
`relaxed` the contracts ban only declared third-party packages.

Only `strict` installs the `pre-push` hook by default; `git_hook = true` replaces that default under any preset, and
`git_hook = false` removes an Arborescent-managed hook (never a foreign one) the next time `generate` or `check` runs. In
a scoped policy the key is set per scope, like `middle_forbid`. `ARBORESCENT_SKIP_HOOK=1 git push` skips the hook for one
push without touching other hooks or the `git_hook` setting; `git push --no-verify` is git's own way to skip every hook.

## Rules Arborescent checks itself

The ids below are `arborescent.domain.Rule`. They appear in baseline entries (`rule|subject|detail`), so they never change.

| rule | fires when |
|---|---|
| `unclassified` | a module has neither a valid enabled `__arbor__` annotation nor a matching `classify` pattern |
| `dev-tool-import` | a module outside every `dev_tools` path imports a module below one (top-level, lazy and `TYPE_CHECKING` imports all count) |
| `dev-tool-misplaced` | a module annotated `__arbor__ = {"role": "dev_tool"}` lies outside every `dev_tools` path, so the annotation cannot grant the exemption by itself |
| `dev-tool-product-import` | a `dev_tools` module imports a product module that `dev_tools_may_import` does not allow |
| `forbidden-module` | a first-party module's dotted name matches a `forbidden_modules` pattern. Reported even if the module is also `unclassified`; per scope, only for modules that scope owns |
| `noise-dir-code` | a tracked `.py` file appears below a skipped directory such as `.venv`, `.tox` or `node_modules` |
| `classify-facts` | information only (never fails, never baselined, not part of `--require-modularity`): a module's facts contradict its ring. Three signals, described in "Checking `classify` against a module's facts" below |
| `tach-invisible-import` | information, never baselined: a scanned module (outside `tests`, `.venv` and other folders tach skips) calls `importlib` or `__import__`, which tach's static graph does not follow. Not reported when `tach` is in `disabled_tools`; `check --require-visible` turns each into `VISIBILITY FAIL` |
| `vendor-folder` | a convention-named vendor folder is excluded and fingerprinted in the baseline |
| `wildcard-ring` | a non-outermost ring uses `*` in `external` or `may_import` (one finding per ring and axis) |
| `suppression` | a `# noqa`, `# ruff: noqa` or `# type: ignore` directive is inventoried by module and kind; each occurrence is ratcheted |
| `discard` | a `discard(value, reason=...)` call is inventoried by module; each call is ratcheted without claiming type-aware `Result` detection |
| `lazy-import` | a function-level `import` with no `# deferred: <reason>` on its line or alone on the line above it; keyed by what it imports and its ordinal in the module, so moving it does not change the key. See "Function-level imports and import cycles" below |
| `effect-clock` | a ring that forbids `clock` reads the ambient clock (`time.time`, `datetime.now` and related calls) |
| `effect-env` | a ring that forbids `env` reads environment variables through `os.environ` or `os.getenv` |
| `effect-identity` | a ring that forbids `identity` generates UUID identities |
| `effect-randomness` | a ring that forbids `randomness` uses `random` or `secrets` |
| `effect-sleep` | a ring that forbids `sleep` calls `time.sleep` or `asyncio.sleep` |
| `effect-threads` | a ring that forbids `threads` starts a `threading.Thread`/`Timer` or constructs a `ThreadPoolExecutor` (locks and events are `state`) |
| `effect-process` | a ring that forbids `process` starts a process or shell command through `subprocess`, `multiprocessing`, `ProcessPoolExecutor`, or direct `os` spawn/exec APIs |
| `effect-network` | a ring that forbids `network` calls `urllib.request`, `http.client`, `http.server`, `socket`, `requests`, `httpx`, `aiohttp` or `urllib3` (a call probe, not an import ban) |
| `effect-database` | a ring that forbids `database` uses `sqlite3`, `psycopg`, `psycopg2`, `asyncpg`, `pymysql`, `mysql.connector`, `sqlalchemy`, `pymongo`, `redis` or `aiosqlite` |
| `effect-filesystem` | a ring that forbids `filesystem` calls `open`, `os` file operations (`os.remove`, `os.listdir`, `os.walk`, ...), `shutil`, `tempfile`, `glob`, or reads/writes through `Path(...)` or through a name bound to a path (`read_text`, `write_text`, `mkdir`, `glob`, ...). A call routed through a wrapper, or a path stored on `self`, is not seen |
| `effect-dynamic-import` | a ring that forbids any effect loads a module at run time (`importlib.import_module`, `__import__`, `spec_from_file_location`/`module_from_spec`, `exec_module`); `find_spec`, `importlib.resources` and `importlib.metadata` do not |
| `effect-patch` | a configured tests folder patches a project target whose ring forbids `patch` |
| `effect-console` | a ring that forbids `console` calls `print`, `input`, `sys.stdout.write` or `sys.stderr.write` |
| `effect-global` | a ring that forbids `global` has a function that declares `global x` and then assigns `x` (a read-only `global` is not a use) |
| `effect-state` | a ring that forbids `state` creates a `threading.Lock`/`RLock`/`Semaphore`/`Event`/`Condition`/`local` (0.8.0) |
| `effect-monotonic` | a ring that forbids `monotonic` calls `time.monotonic`, `perf_counter` or `process_time` (0.8.0) |
| `effect-host` | a ring that forbids `host` reads `sys.argv`, `sys.stdin`, `locale`, `platform`, the process id, the CPU count or the host name (0.8.0) |
| `effect-fs-query` | a ring that forbids `fs-query` asks the filesystem a question: `os.path.exists`, `os.getcwd`, `Path.cwd`, `p.exists()`, `shutil.which`... (0.8.0) |
| `effect-unsanctioned-import` | with `[effects] sanctioned` set, a ring that forbids effects imports a module of an `effects.rings` ring that is not listed as a sanctioned seam and has effects of its own (0.8.0) |
| `effect-via-import` | a ring forbids an effect that a module it imports (directly or through that module's own imports, same scope, not an `if TYPE_CHECKING` import) uses directly; the message names the import chain |
| `internal-import` | a ring imports a ring it may not, or a module imports a target prohibited by `module_forbid_imports` (top-level, lazy and `TYPE_CHECKING` imports all count) |
| `forwarded-import` | a module imports a pass-through function (`def f(a): return other.f(a)`: one call to an imported name, arguments exactly the parameters, no default, decorator or other statement) whose target `module_forbid_imports` forbids it: the dependency one hop away. Chains of forwarders are followed. Only `from B import f` is seen, not `B.f` attribute use; wrappers that add logic are real boundaries and are not flagged. Counts against the importing module and takes its own baseline key |
| `restricted-import` | a module outside a restriction's `importable_by` globs imports the restricted module; also a key or importer glob that matches no module |
| `result-discard` | a call used as a whole statement drops the value of a result-bearing function (`result_discard`); `_ = f()` is a deliberate discard and is not reported |
| `missing-module-docstring` | `docs.module_docstring` is on and a module has no, or a blank, docstring |
| `docstring-ring-mismatch` | `docs.module_names_ring` is on and a module's docstring does not name the ring that `classify` or `__arbor__` gives it (it says which other rings it names) |
| `literal-outside-owner` | a module or ring spells an `owned_literal` string that belongs to other modules or rings; also an owner glob that matches no module |
| `reexport-ratio` | a ring with `max_reexport_ratio` has a module whose public surface (a static `__all__`, else its public top-level bindings; at least 5 names) is mostly first-party imports. Moving an import into such a pass-through module clears the `internal-import` edge without changing the dependency. Third-party and stdlib re-exports do not count, and the detail carries no counts so the baseline key stays stable |
| `mixed-purity` | a module holds at least `min_pure_units` (default 2) substantial pure top-level functions or classes (a body of at least `min_body_lines`, default 4, lines) and at least one effectful one, or an effect that runs at import time. Enforced in every ring; see [Mixed purity](#mixed-purity-toolarborescentmixed_purity). The detail names only the ring, so the baseline key survives renaming functions; the hint lists the effect kinds, the effectful and pure definitions and the importer count. Also an `allow` entry that matches no module or no mixed module |
| `untokenizable-source` | a file Python's `ast` accepts but `tokenize` rejects (a doubled carriage return `\r\r\n` is the usual cause, #181). Its `# noqa`, `# type: ignore` and `# deferred:` comments cannot be read, so the file gets no comment facts and this finding instead of aborting the run. A violation, so it can be baselined; the detail is fixed text (the tokenizer's line and message ride in the hint), and the fix is to remove the stray byte |
| `mixed-purity-excluded` | informational: a file under `exclude` (not a vendor folder, a noise folder or a child package) would be reported by `mixed-purity`. Never fails a check and never enters the baseline, but the exclusion no longer hides it |

| `construction-outside-site` | a module or ring calls a `construct_only` class or factory that only other modules or rings may build; also an owner glob or target module that does not exist |
| `non-abstract-port` | a ring with `require_abstract = true` has a public top-level class or function that is not a recognized abstract shape (the Dependency Inversion Principle: a "ports" ring must hold abstractions, not concrete implementations) |
| `weakly-typed-port` | a ring with `require_typed_ports = true` has a `typing.Protocol` method whose return type is missing, `Any`, `object`, a bare container or `Mapping[str, Any]`, so callers cannot be type-checked against the port; allow a deliberate case with a baseline entry |
| `scope-import` | a module imports another scope without declaring that dependency, or imports a target outside the dependency's `public` patterns |
| `scope-effect-leak` | a module imports another scope's declared-`public` module, and that module uses an effect the importing ring forbids (one hop, not transitive) |
| `circular-import` | when `forbid_circular_imports` is enabled, first-party modules form a circular import dependency, including cycles made only of lazy or `TYPE_CHECKING` imports |
| `external-import` | a ring imports an external package missing from its `external` list (scope set by the preset) |
| `os-branching` | a ring not marked `os_branching` reads `os.name` or `sys.platform` |
| `bad-import` | a relative import that points above the package root |
| `undeclared-entry-dependency` | an entry script lacks a `# /// script` block, or omits a third-party distribution the project imports |
| `invalid-arbor-annotation` | a module's `__arbor__` annotation is malformed or names an unknown ring |
| `arbor-ring-mismatch` | a valid `__arbor__` ring disagrees with the ring the first matching `classify` glob would assign |
| `unannotated-module` | `require_arbor_annotations` is enabled and a module resolves its ring only through `classify`, with no valid `__arbor__` annotation |
| `package-unassigned-module` | `package_catalog` is set and a first-party module lies under no package's owned `paths` |
| `package-multiply-assigned-module` | a module lies under the paths of two or more packages (the message names the module and every claiming package) |
| `package-release-policy-missing` | `package_catalog` is set and a package declares no `history`, no `[packages.release]` table and not `unmanaged = true` |
| `package-unowned-asset` | a non-Python file below the policy's `sources` is under no package path and matches no `[[assets]]` glob |
| `package-multiply-assigned-asset` | an asset is under one package's path and also matched by another package's `[[assets]]` glob |
| `package-spans-rings` | information only (never fails, never baselined): a package has modules in several rings, so a split must follow module sets, not folders |
| `package-undeclared-dependency` | a module imports a module of another package that its own package does not list in `depends_on` |
| `package-dependency-cycle` | packages list each other (directly or through others) in `depends_on`, so none can be split or released first; the same check `packages validate` makes |
| `package-unused-dependency` | information only: a package lists a package in `depends_on` that none of its modules imports, which widens what it may import unnoticed (a plugin or runtime-loaded dependency can be real without a static import) |
| `package-never-depended-on` | information only: a package that depends on nothing and that no other package lists in `depends_on` or imports from (a vendored package nothing uses). A package with its own `depends_on` is treated as an entrypoint and never flagged |
| `package-owns-everything` | information only: one package owns every module (two or more), so the ownership and dependency checks have no boundary between packages to check (rings and imports are still enforced). Never fails, here or in `--require-modularity` |
| `package-semantics-drift` | a part's manifest names a package the catalog lacks, or a semantic field or a path's role or metadata differs from the catalog |
| `package-membership-drift` | a module of an included package is missing from its part, or a part holds a module the catalog gives elsewhere or nowhere |
| `package-ring-drift` | a module's effective ring in its part differs from the origin's |
| `package-public-symbol-dropped` | a module on a package's declared public surface lost a public class or function in its part |
| `package-private-import` | a module imports another package's module outside the `public` surface that package declares (checked in the origin and in each part) |

| `package-constraint-facts` | a module uses an effect `[package.constraints]` forbids (`forbid_effects`, or any effect under `must_be_pure`), or under `must_be_pure` imports a non-pure external package or a first-party module that reaches one, whatever its ring |
| `package-constraint-placement` | the ring a module is placed in permits an effect `[package.constraints]` forbids; tighten the ring or drop the constraint |
| `package-pyproject-drift` | information only (never fails, never baselined): `package_pyproject` is set and a derived field of an existing package `pyproject.toml` disagrees with the annotations |

Effect rules detect direct imports and attribute chains, including aliases. They do not detect uses routed through
`getattr`, variables, or wrappers. A test patch is checked against the target module's ring and is considered only when
the first argument is a static project-package string or attribute under the tests folder the `effect-patch` practice
configures (falling back to the `secrets-guard` tests folder when `effect-patch` is unset, for a policy that configured
only `secrets-guard`; set `effect-patch` directly to enable this check without also enabling the environment-scrubbing
check `secrets-guard` makes).
`noqa` comments do not suppress native effect findings. Suppression and reasoned `discard()` counts are always reported;
increases need `baseline --allow-growth --reason` (or one-time family adoption), while removals can shrink the baseline.

`effect-via-import` propagates at module granularity: a module that mixes a forbidden effect with other code taints
every importer, even one that only uses the clean part. It also stops at calls on an object of unknown type (it
cannot tell what `helper.do_it()` does when `helper`'s type is not known statically) and does not cross scope
boundaries (that is `scope-effect-leak`, one hop, checked separately) or an `if TYPE_CHECKING` import (nothing runs
there to inherit from). The function-level taint pass below (#98) is the sharper, same-granularity complement: it
names which function actually calls the effectful code, instead of flagging every importer of the module that holds it.

### Function-level imports and import cycles

A function-level `import` is the usual workaround for an import cycle or a heavy dependency: it stops the
`ImportError` and leaves the coupling in the graph. Each one is its own finding, `lazy-import`, so a baseline of them is
a visible, shrink-only list per module. `check` prints `function-level imports by module:` with the total and how many
carry a reason. An import under `if TYPE_CHECKING` never runs and is not counted. A decision is recorded on the line
or alone on the line above (comment lines that continue it are fine, a blank line ends it):

```python
def load() -> None:
    import heavy  # deferred: 300 ms of startup the CLI help path should not pay
    # deferred: the registry imports this module at import time
    import registry
```

The reason must not be empty. A justified import is counted but is not a finding. It still counts as an edge in the import
graph, so `circular-import` and `arborescent cycles` see it. (Excluding these imports from a closure size is not done
here; the closure used by `internal-import` move suggestions still follows them.)

`arborescent cycles` reports the cycles themselves. For each strongly connected component of the first-party import
graph it lists every edge with its kind (`import-time`, `function-level` or `type-checking`; an edge that several
imports make takes the strongest kind) and the import lines, then:

- **cut**: an approximate minimum feedback arc set, the edges whose removal leaves the component acyclic (an
  Eades-Lin-Smyth ordering, thinned until no edge can be put back). Approximate, not guaranteed minimum.
- **function-level imports that exist to break the cycle**: a function-level edge `a -> b` where `b` already reaches `a`
  through import-time edges alone, so moving the import to module level would close an import-time cycle. A
  function-level edge on a cycle without that property is only on cycles made of other deferred or type-only edges, and
  one outside every cycle is deferred for another reason (startup cost, an optional dependency).
- **Protocol owner**: the innermost-ring module of the component (then the one the others import most). Define the
  `Protocol` there, make the importing side depend on it, and have the outer module register its implementation.

The last line gives the function-level edge totals: how many are on a cycle, how many of those exist to break one, and
how many are outside every cycle. A cycle fixed by a forwarder or a re-export is still a cycle; run `cycles` after
those checks. The command only reports (exit 0); `circular-import` under `check` is what fails.

### Function-level taint: which functions actually reach I/O

A whole-repository pass, informational only (feeds `classify-facts` below; never fails `check`, never baselined, and
skipped under `--changed` since it compares a module's functions with the ones its calls resolve to). For every
module-level function and method, it resolves the names the function calls — including `self.method()`, a
same-function `x = C(); x.method()` constructor pattern, an import alias, and a name re-exported through a
package's `__init__.py` — and marks a function tainted when it calls an I/O primitive directly or calls a function
that is itself tainted, iterated to a fixed point. The seed list is `taint_seeds` (default: every `effect-*`
category except `patch` (test-only) and `global` (state mutation, not I/O), plus `network` — `urllib.request`,
`http.client`, `http.server` and `socket`, which `effect-*` does not cover since they are specific submodules, not
a whole stdlib package match).

What it does not see, by design:

- **No type inference.** A call through an object of unknown static type (`self._fs.exists()`, a parameter, a
  return value) is not followed — the same limit `effect-via-import` documents for `helper.do_it()`. `Path(...).read_text()`
  and similar are real I/O but a method call on a call's *result*, not an import alias; recognising them needs a
  type this pass does not have, so they are a known false negative.
- **No `getattr`, decorators, or callbacks.** A function only ever passed as a value and invoked through another
  name is not tainted by that alone (a non-goal, not an oversight: proving purity is out of scope).
- **No inheritance.** `self.method()` resolves only to a method defined directly on the enclosing class, never one
  inherited from a base class.
- **Module-level only.** A nested `def` inside a function, or a class defined inside a function, is invisible to
  this pass, and so is anything it calls.
- **Constructor tracking is a same-function approximation.** It follows a plain `name = Call(...)` left to right
  until `name` is rebound; it does not follow the variable across branches, loop bodies, or into another function.
- **Lazy imports are not read.** Only module-level `import`/`from ... import` lines bind a name this pass follows;
  a function-local import is invisible to it (as it is to `_aliases`-based effect detection generally).

The result names the tainted functions on the `classify-facts` notes it already produces: "could sit in an inner
ring" and "program and library" each append `; reaches I/O through a call from ...` when the pass found one, so the
note names the function to look at instead of leaving the module-level claim unqualified.

### Checking `classify` against a module's facts

`classify` and `__arbor__` are trusted: a glob such as `training* -> adapters` puts every match in that ring, and the
ring rules only complain when a module does *more* than its ring allows. `classify-facts` notes the other direction and
a few mixed cases. It uses only facts the scan already has (imports, effect uses, top-level definitions, and a
`__main__` guard), never a module's name or what it seems to mean. It is information: shown as `NOTE`, never fails `check`,
never enters the baseline, and is not a reason to fail `--require-modularity`. It is skipped under `--changed`, because each
signal compares a module with its importers and peers. Three signals:

| signal | stated precisely | limits |
|---|---|---|
| could sit in an inner ring | the module defines at least one top-level class or function, has no `__main__` guard, defines no abstract class or stub function, is not an `entry_scripts` module, and passes every per-module rule of a more inner ring (internal imports, external packages, OS branching, effects, `require_abstract`). The note names the innermost such ring, and the function-level taint pass below appends which of the module's own functions it found reaching I/O, if any. | The scanner does not see `open()`, `print()`, `input()`, calls through objects that do I/O, or what a function does with its arguments, so "passes the rules" is weaker than "is pure" (the taint pass narrows this, it does not close it — see its own limits below). If your rings share one external allow-list, nearly everything fits the inner ring and the note carries no information. A ring can be right for its role (an adapter that only drives a port) even though its facts fit an inner one. |
| composition-shaped | the module is not in the outermost ring, and the outermost ring may import every ring (`may_import = ["*"]`); the module defines functions and no class, no other module imports it, and it imports two or more distinct modules of its own ring plus at least one module of an inner ring. | It shows the shape of an entrypoint that wires adapters to a use case, not that it constructs or calls anything. Nothing is said about what the wiring does. A leaf script that is meant to be an adapter will match. |
| program and library | the module has a top-level `if __name__ == "__main__":` and at least one other module imports it, and it is not in the outermost ring when that ring may import every ring (where programs belong). The function-level taint pass below appends which of the module's own functions reach I/O, if any. | It cannot tell which functions are pure and which read a database beyond what the taint pass resolves, so it reports the mix, not every part that should move. A `__main__` guard that only calls `main()` still counts. |

None of these is a verdict. Each is a prompt to open the file and decide.

## Practices: `[tool.arborescent.practices]`

Each practice is off until it has a value.

| key | value | checks |
|---|---|---|
| `gitignore-shadow` | `true`, or a list of file globs to allow | a `.py` file is git-ignored (an unanchored pattern hid a package) |
| `sibling-versions` | glob of `pyproject.toml` files, e.g. `"*/pyproject.toml"` | a project pins a sibling project to a version the sibling does not have |
| `secrets-guard` | tests folder, e.g. `"tests"` | `conftest.py` has no autouse fixture that scrubs the environment |
| `effect-patch` | tests folder, e.g. `"tests"` | not a check of its own: the tests folder the `effect-patch` ring rule reads a `mock.patch` test from (falls back to `secrets-guard`'s tests folder when unset) |
| `docs-sync` | path of the architecture doc | the doc's `\| module \| ring \|` table equals the policy (see `docs/architecture.md`) |
| `crlf` | list of file globs, e.g. `["*.sh"]` | a matching file has CRLF line endings |
| `undeclared-imports` | `true`, or a manifest path (default `pyproject.toml`) | a third-party import is absent from the manifest's dependencies |
| `stale-config-path` | list of config file globs, e.g. `["Makefile", ".github/workflows/*.yml"]`; `stale-config-path-allow` lists path globs to ignore | a glob or `.py` path in the file matches nothing (a suite silently runs zero tests after a move), or a pyproject's hatch `packages` entry does not exist |
| `source-root-drift` | `true`, or a list of files (default `["tach.toml", "pyproject.toml"]`) | a `sources` entry is below no `source_roots` entry, or the root list copied into `tach.toml` `source_roots`, hatch `sources`/`packages` or pytest `pythonpath` has drifted from the policy |
| `test-reference` | tests folder, e.g. `"tests"`; `test-reference-exempt` lists `module.function` names or module globs to skip | information only: a public module-level function in an I/O-free ring (one that forbids `filesystem`, `network`, `database` and `process`; the innermost ring by default) that no function under the tests folder imports and calls. Resolved through import aliases and package re-exports; not through an object of unknown type, a fixture or a parametrized argument, and a function only a tested caller reaches is reported. Private, abstract and class members are skipped. A name that is only imported or mentioned does not count (#104) |
| `adapter-equivalence` | list of groups of full module names, e.g. `[["pkg.fact_queries", "pkg.modes_store"]]`; `adapter-equivalence-tests` is the tests folder (default `"tests"`); `adapter-equivalence-report = true` makes the missing-test finding information only | fails when no single test function calls a function of every module in a group, so two adapters of one port cannot both ship without a differential test (#113). Group errors always fail, even with `adapter-equivalence-report`: an unknown module (not a scanned module; a unique suffix match is suggested), a group with fewer than two distinct modules, a module in two groups, a value that is not a list of lists of strings. See "Adapter equivalence" below |
| `adapter-equivalence-report` | `true` | not a check of its own: makes `adapter-equivalence`'s missing-test finding information only (rule id `adapter-equivalence-report`, never fails, never baselined); group errors still fail |

### Adapter equivalence

`adapter-equivalence` forces a differential test to exist; it does not check that the test compares anything. The behaviour
of a port is the project's contract test to write (see "Two adapters of one port can still disagree" in `docs/architecture.md`). A test
function counts only through direct calls it makes (the machinery of `test-reference`, so aliases and package re-exports
resolve to the defining module, which is the name to list). It does **not** count:

- a call in a helper the test delegates to, or a module-level call (call one function of each module in the test body);
- a fixture or a parametrized argument (`store.list_facts()` on an unknown-type object);
- a bare import or a mention such as `assert a is not b`.

Members may sit in different rings and scopes; no ring filter applies and the check adds no import edge. A module outside
the scan (excluded, or misspelled) is an error rather than a skipped member.

`stale-config-path` is a heuristic: it reads path-like tokens (containing `/`, and either a glob or ending in `.py`) and skips
comments, URLs, variables, absolute and `..` paths, generated folders and `.pyc`/`.log`/`.tmp` globs. A path it should not
check goes in `stale-config-path-allow`. `source-root-drift` reports a missing entry in a pyproject list or in tach's
`source_roots`, and an extra entry only for tach; a list that is absent is not drift.

## Baseline ratchet

`arborescent baseline` creates a baseline the first time. On later runs it removes stale entries, but refuses to add new
ones. To adopt one previously unrecorded rule family, use `arborescent baseline --adopt FAMILY`; adoption is allowed only
when that family has no existing baseline entries. Other growth requires
`arborescent baseline --allow-growth --reason "why this debt is accepted" --approved-by "name or issue URL"`. The version-2 JSON baseline records each
growth reason and the exact keys it added; version-1 baselines still load and are upgraded when written.

`arborescent check --growth-against REF` compares the baseline at `REF` with the working baseline and requires every new
key to match the keys in growth-log entries appended since that ref. This checks consistency; it does not prove the
command wrote the file. A person could edit both the baseline and its log by hand, so review of baseline changes
remains part of the protection (for example, require review through `CODEOWNERS`). Each new growth entry must carry a
non-blank `reason` and `approved-by`; the tool cannot verify the claim, but a false one is now an explicit, attributable
string. Files listed in `ratchet_files` get the same check per name: an added or raised value needs a `growth` entry
`{"key": NAME, "reason": ..., "approved-by": ...}` appended since the ref. To catch two branches that each raise a
ratchet, run `check --growth-against <target-branch>` on the merge result in CI, not only on each branch.

### `baseline report`

`arborescent baseline report [FILE ...] [--json]` is read-only: it changes no file and no gate. It reads each file (default: the
policy's `baseline`, paths relative to the policy root) in any of three shapes, a list of `rule|subject|detail` keys (also under
`violations`), a `{module: count}` counter, or a `{name: {rule, module, target, count}}` registry, and prints:

- the top clusters (`--top`, default 10) by rule, by module family (`--depth` leading segments of a dotted name or `/` path,
  default 2), by module, and by target (the first backticked name in a key's detail, or a record's `target`), each with its share;
- the concentration: any family, module or target holding more than `--threshold` percent (default 25) of the total, and at
  least 3 entries, is flagged as a likely missing abstraction (a rule is never flagged);
- the burn-down: entries added, removed and net per `--period` (`day`, `month`, `year`; default month) from the git history of
  the file, reading the newest `--history-limit` revisions (default 200), with a warning when the baseline has only ever grown.
  A counter entry that rises counts as added by the difference. `--no-history` skips it; a file git does not track prints a note.


A shrink-only cap on imports (`module_forbid_imports` with the baseline) is sized in baseline entries, and `from pkg import mod`
records two entries (`pkg` and `pkg.mod`). After `check` and `baseline`, one line sizes the baselined `internal-import` entries
in modules: `baselined internal-import: 232 entries, 41 importing modules, 156 distinct targets`. A target is the dotted module
as the entry records it, so the package and the submodule of one `from` import count separately.

## Coupling metrics: `[tool.arborescent.coupling]`

Import rules say what may import what; they do not say which modules everything depends on. `arborescent coupling` measures that
from the same import facts the ring checks use (`from pkg import sub` is one edge to `pkg.sub`). Every import has one kind, and a
pair imported in several ways takes the strongest: module-level, then function-level only, then `TYPE_CHECKING` only.

```toml
[tool.arborescent.coupling]
entrypoints = ["app.__main__"]   # modules whose import closure is measured
hub_fan_in = 8                   # importers from which a module is a hub (default 8)
baseline = "arborescent-coupling.json"
[tool.arborescent.coupling.fan_in_max]
domain = 40                      # report-only cap on distinct importers per ring
```

- **Fan-in** per module: distinct importing modules (any kind), those that import it at module level, and the modules that reach
  it through module-level imports (transitive). A module never counts as its own importer. A ring over its `fan_in_max` is flagged.
- **Closure** per entrypoint: the project modules loaded by following module-level imports, the entrypoint included. The modules
  only its function-level imports reach, and those only `TYPE_CHECKING` imports reach, are counted separately and not in the size.
  Like the ring checks, this does not add the `__init__` of each parent package.
- **Ambient hub candidates**: a module with at least `hub_fan_in` importers whose public definitions mostly (more than half) use an
  effect directly (environment, file system, clock, process, ...). A definition owns the lines up to the next one; a parameter
  default that names an effect is a seam and does not count. Pass such a module in through a port instead of importing it.

`arborescent coupling report [--top N] [--json]` prints all three; `arborescent coupling cost IMPORTER [--imports MODULE...]
[--modules N]` prints, per entrypoint that loads `IMPORTER`, how many modules its closure gains when `N` new modules (default 3:
a port, a service and an adapter) are added behind it and import the given modules, so the price of a new module is visible
before the commit.

The optional ratchet is `arborescent coupling check`, with the counts in `arborescent-coupling.json`: `{"fan-in": {module: n},
"closure": {entrypoint: n}, "growth": [...]}`. `fan-in` lists the modules at or above `hub_fan_in`; a module not listed must stay
below it, and every declared entrypoint needs a `closure` entry. A count above its baseline, or an unlisted hub, fails.
`arborescent coupling baseline` writes the file the first time and afterwards only lowers counts (a smaller or gone entry prints as
`STALE` in `check`); a rise or a new entry needs `--allow-growth --reason TEXT` and is logged in `growth`. The file is separate
from the check baseline and is not read by `arborescent check`.

## Effect categories and the `[effects]` table

Every direct use of an effectful API is classified into a category; a ring's `forbid` list decides which categories fail there
(`effect-<category>`), and `effect-via-import` carries the same categories across imports. What each category matches:

| category | matches |
|---|---|
| `clock` | `time.time`, `time.time_ns`, `time.gmtime`, `time.localtime`, `time.strftime`, `datetime.now`/`utcnow`/`today`, `date.today` |
| `monotonic` | `time.monotonic`, `perf_counter`, `process_time`, `thread_time` and their `_ns` forms (0.8.0; deadline arithmetic is not a wall-clock read, so it is its own kind) |
| `env` | `os.environ`, `os.getenv` |
| `host` | `sys.argv`, `sys.stdin`, `locale.*`, `platform.*` (bar the OS-branching `platform.system`), `os.getpid`/`getppid`/`cpu_count`/`getlogin`/`uname`, `socket.gethostname`/`getfqdn`, `getpass.getuser` (0.8.0) |
| `identity` | `uuid.uuid1`, `uuid.uuid4` |
| `randomness` | `random.*`, `secrets.*`, `os.urandom`. **`random.Random(<seed>)` is deterministic and does not count**; `random.Random()`, `Random(None)`, `SystemRandom` and the module-level functions do |
| `sleep` | `time.sleep`, `asyncio.sleep` |
| `threads` | `threading.Thread`/`Timer`, `ThreadPoolExecutor`, `multiprocessing.pool.ThreadPool` and the rest of `threading` that starts or inspects threads |
| `state` | `threading.Lock`/`RLock`/`Semaphore`/`BoundedSemaphore`/`Event`/`Condition`/`Barrier`/`local` (0.8.0; they guard shared state, they create no thread, so "inject a port" is the wrong remedy and the category is not in any default `forbid`) |
| `process` | `subprocess`, `multiprocessing`, `ProcessPoolExecutor`, `os.system`/`spawn*`/`exec*` |
| `filesystem` | `open`, `os` file operations, `shutil`, `tempfile`, `glob`, and read/write/`mkdir`/`glob`/`stat`... methods on `Path(...)` **or on a name bound to a path** (0.8.0: a parameter or variable annotated `Path`, or assigned `Path(...)`, `a / b`, `p.parent`, `p.resolve()`; resolved per function, so a `str` of the same name elsewhere is not a path; needs `import pathlib`) |
| `fs-query` | questions asked of the filesystem: `exists`/`is_file`/`is_dir`/`resolve`/`expanduser`/`samefile` on a path, `Path.cwd`/`home`, `os.path.exists`/`isfile`/`getsize`/`getmtime`/`abspath`/`realpath`/`expanduser`, `os.getcwd`, `os.access`, `shutil.which`, `tempfile.gettempdir` (0.8.0) |
| `network` | `urllib.request`, `http.client`, `http.server`, `socket` (bar `gethostname`/`getfqdn`), and `requests`/`httpx`/`aiohttp`/`urllib3` calls |
| `database` | `sqlite3`, `psycopg`, `sqlalchemy`, ... |
| `dynamic-import` | **only** run-time module loading: `importlib.import_module`, `importlib.__import__`, `importlib.reload`, `importlib.util.spec_from_file_location`/`spec_from_loader`/`module_from_spec`, `*.exec_module`, `__import__`. `importlib.resources`, `importlib.metadata` and `importlib.util.find_spec` import nothing and do not count (0.8.0) |
| `console` | `print`, `input`, `sys.stdout.write`, `sys.stderr.write` |
| `global` | a function that declares `global x` and assigns it |
| `patch` | a test patching a project target |

A reference inside a type annotation (a parameter, return or variable annotation) is never a use: a hint names a type, it does not
call it. A bare parameter default naming an effect, and since 0.8.0 a `lambda` default whose body is a call
(`clock=lambda: datetime.now(UTC)`), is an injection seam under the default `effect_defaults`; arguments passed *into* that
call are still uses.

Four categories were added in 0.8.0 (`state`, `monotonic`, `host`, `fs-query`). They are valid in every `forbid`, `forbid_effects`,
`dev_tools_forbid` and `mixed_purity.kinds` list but are in **no default**: a policy that does not name them keeps its findings,
except that the extended `Path` detection (a path held in a variable) and the narrower `dynamic-import`, `threads`
and `randomness` definitions change which modules the pre-existing categories report. `must_be_pure` keeps meaning "every
pre-0.8.0 category"; list the new ones under `forbid_effects` to add them.

### `[tool.arborescent.effects]`: sanctioned seams

A *seam* is a module that owns an effect on purpose (a file-read port, a process-environment reader, the logging setup) so that
everything else can take it as a port. Without help, `effect-via-import` reports every importer of such a module, which is the
sanctioned path itself. The `effects` table names the seams:

```toml
[tool.arborescent.effects]
sanctioned = ["runtime_seams.*", "app_logging.logging_setup"]   # dotted names, package prefixes or globs
rings = ["effects"]                                              # default: the rings that hold such modules
```

- A sanctioned module's **own direct uses** do not propagate to its importers. What it imports still does: a seam that reaches a
  raw-effect module is not a seam.
- The guard that makes this safe, `effect-unsanctioned-import`: once the table is present (even `sanctioned = []`), a module in a
  ring that forbids effects may import a module of a `rings` ring only if that module is `sanctioned` or has no effects of its own (a pure module is not a
  seam and needs no listing). Otherwise it is a finding, so a domain module importing a raw environment reader cannot hide behind the stopped propagation. `TYPE_CHECKING`
  imports and rings that forbid nothing are not checked; modules in another scope are not checked.
- Without the table nothing changes. Never stop propagation for a whole ring: list the seams.

## Mixed purity: `[tool.arborescent.mixed_purity]`

A ring label says where a module sits; it does not say the module is one thing. A module in a permissive ring can hold a
large block of pure logic next to a function that reads the clock, walks the filesystem and logs, and every importer inherits
all of it. `mixed-purity` flags that module in **every ring**: it is two modules.

A module is *mixed* when it has at least `min_pure_units` **substantial pure** top-level definitions and at least one
**effectful** one (or an effect that runs when the module is imported).

- A definition is *substantial* when its body (past the docstring) spans `min_body_lines` source lines. A class counts only
  when it has a method that is not a stub; a field-only dataclass, a `Protocol` or an ABC is a model, not logic. A pass-through
  wrapper (the `forwarded-import` shape) does not count.
- A definition is *effectful* when it directly uses an effect of a kind in `kinds`, **or calls an effectful function of the
  same module by name**: a function that calls a logging helper is not pure. It is also effectful when it calls a **function of
  another scanned module** (by `from m import f`, `m.f()`, an alias, or a function-level import) that is itself effectful by
  these rules: one hop, so `save_text_atomic` in an `effects` module makes its caller effectful, but the caller's callers are
  not. A class is not followed (building it is not calling its methods) and a callee outside the scan is unknown, not pure.
  A method call on a module-level `Path` constant (`REGISTRY = Path(...)`, then `REGISTRY.read_text()`) is filesystem use.
- Effects come from the facts the `effect-*` rules already extract (same table, same `effect_defaults` seam handling), so there is
  no second detector. A use outside every top-level definition is an import-time effect, except inside an
  `if __name__ == "__main__":` block, which runs as a program. `os-branching` and `patch` are not effects here.
  `TYPE_CHECKING` imports create no use, so they never make a module effectful.
- A module where every definition is effectful (an adapter, a port wrapper) or none is (a pure module, a pure model) is not mixed.

```toml
[tool.arborescent.mixed_purity]
enabled = true            # default; false turns the rule off
report_excluded = true    # default; false skips parsing files under `exclude` (one extra cold parse per excluded file)
min_pure_units = 2        # default: pure definitions needed before a module counts as mixed
min_body_lines = 4        # default: lines a definition needs to be substantial
kinds = ["clock", "env", "filesystem", "logging", "memo", "state"]   # default: every kind below except ambient
ambient = ["myapp.state.state_root", "myapp.env.*"]                    # globs on the absolute dotted name of an ambient lookup
exclude_rings = ["entrypoints", "effects"]                             # default []: rings whose modules are never reported (they are meant to be effectful)

[[tool.arborescent.mixed_purity.allow]]
module = "myapp.bundle_schema.hydration"   # a dotted name, a package prefix or a glob
reason = "split tracked in #212; until then the cache slot and the validation ship together"
```

`kinds` takes the existing categories (`clock`, `env`, `identity`, `randomness`, `sleep`, `threads`, `process`, `database`,
`network`, `filesystem`, `dynamic-import`, `console` for `print`/`input`, `global` for a function that rebinds a module global),
the 0.8.0 categories `monotonic`, `host` and `fs-query` (valid, but not in the default list), and
and the kinds this rule adds, which never feed the `effect-*` rules:

| kind | what it is |
|---|---|
| `logging` | `logging.info(...)` and friends, a call on a module-level `logging.getLogger(...)` / `structlog.get_logger(...)` result, `loguru.logger`. `getLogger` itself is not an effect |
| `memo` | `functools.lru_cache` / `cache` / `cached_property` decorators, or a write to a module-level dict, list or set whose name contains `cache` or `memo` |
| `state` | a write (item assignment, `del`, `append`/`update`/`setdefault`/... ) to any other module-level dict, list or set from inside a function, unless that function binds the name locally; and, since 0.8.0, a `threading.Lock`/`RLock`/`Semaphore`/`Event`/`Condition`/`local`. Reads are not effects |
| `ambient` | a call to something matching `ambient`, resolved through absolute and relative imports (`state_root()`, `os.getcwd`). Off until `ambient` lists a pattern |

**Why is this function listed as pure?** `arborescent purity --explain MODULE` prints, for each top-level definition of one
module (mixed or not), the effect kind, the line and the call behind an "effectful" label, or `no effect found`. Attach its
output to a report of a wrong label.

**Choosing what counts.** The default `kinds` count everything above, so a CLI `main` that prints, a module that only logs, and an `lru_cache` on a regex compiler all make a module "mixed". To report only the splits that separate real effects, restrict `kinds` (for example drop `console`, `logging`, `memo`, `state`, `global`) and list the rings that are meant to be effectful under `exclude_rings`. `exclude_rings` also applies to `arborescent purity`. Both default to today's behaviour.

**Allowlist.** An entry needs a non-blank `reason`; without one the policy fails to load. An entry that matches no module, or
only modules that are not mixed (any more), is itself a `mixed-purity` finding, so an exemption cannot outlive the code it excused.
With `--changed` only a missing module is judged, since the other modules were not parsed.

**Adopting it.** Run `arborescent baseline` to record today's findings and let the ratchet shrink them; a fixed module leaves a
stale entry you delete. `arborescent purity` lists every mixed module, including allowed ones, ranked by
how many modules import it (ties: more pure units first, then module name), which is the order to split them in. The
listing is grouped by ring with per-ring counts, and each line carries the effect kinds, the effectful and pure definitions
and the importer count. `--top N` (N >= 1; 0 or a negative number is an error) keeps only the N highest-ranked modules;
`--json` prints the same rows for tooling. Ranking is presentation only: `check`, the baseline keys and the exit code are
unaffected, and the importer counts are the ones `check` reports.

**Excluded paths.** With the rule on, files under `exclude` are parsed (through the same content-hash cache) and the mixed ones
are reported as `mixed-purity-excluded` notes. Tooling, tests and fixtures stay excluded; they just stop being invisible. Turn
`report_excluded = false` to skip that parse, which costs one extra cold parse per excluded file (a large excluded `tests` tree can double the cold scan; this repository turns it off for that reason).

**Limits.** Only top-level functions and classes are units, and a class is one unit. A call through an object of unknown type
is not followed, so a method called as `self.helper()` does not carry its effect to the caller. Developer tooling
(`dev_tools`) has no ring and is not judged.

## Named ratchets: `[tool.arborescent.ratchet.<name>]`

A named ratchet is a per-module counter that may only go down: function-level imports, bare prints, calls to a banned
reader, findings of one rule, the size of an allow-list. Every ratchet uses one baseline format and one way to be lowered
or raised, so a project does not need a baseline file and shrink-only script per scanner.

```toml
[tool.arborescent.ratchet.bare-prints]
ast_call = "print"            # calls of this dotted name, per module
scope = ["myapp.*"]           # optional module globs; default: every scanned module

[tool.arborescent.ratchet.nested-imports]
regex = '^[ ]+(import|from) '  # matches per module (Python regex, multi-line)

[tool.arborescent.ratchet.suppressions]
rule = "suppression"          # findings of one of Arborescent's own rule ids, per module

[tool.arborescent.ratchet.legacy-scan]
command = ["python", "tools/scan.py"]   # prints one `module count` line per module
baseline = "ratchets/legacy.json"       # optional; default arborescent-ratchets.json
```

Each table sets exactly one measure (`rule`, `regex`, `ast_call` or `command`). `rule` takes an id from the rules above and
counts findings by subject; the engines' findings (`ruff-*`) are not Arborescent's own rules, so measure those with a
`command`. A `command` runs from the repository root, must exit 0 and print `module count` lines (blank lines are ignored; a
repeated module, a line that is not `module count`, or a missing executable fails the ratchet). A file that cannot be read or
parsed fails an `ast_call`/`regex` ratchet; it is never counted as zero.

The baseline file holds every ratchet that names it:

```json
{"version": 1, "ratchets": {"bare-prints": {"myapp.cli": 4}}, "growth": []}
```

A module at zero is not listed, and a ratchet recorded at zero is `{}`. `arborescent check` compares each ratchet with its
baseline and fails on a count above it (a regression) or below it (stale: run `arborescent ratchet` so the gain cannot be
spent again), and on a ratchet that has no baseline yet. In `--changed` runs only regressions are reported.

`arborescent ratchet [--name NAME]` records the baselines: the first recording of a ratchet is an adoption and logs itself,
a later one only lowers counts. A count that would rise is refused unless
`--allow-growth --reason "why" --approved-by "name or issue URL"` is given; that appends a growth entry
`{"key": "NAME|MODULE", "reason": ..., "approved-by": ..., "from": 2, "to": 3}`. This is the same `key` / `reason` /
`approved-by` record `ratchet_files` use. `arborescent check --growth-against REF` also reads each ratchet baseline at `REF`
and fails when a module was added or raised since without an attributed entry appended since (a baseline file that is new since
`REF` must log every count), or when existing log entries were rewritten. As with the main baseline, this checks consistency
and not authorship: review baseline changes (for example through `CODEOWNERS`).

## Commands and exit codes

All seven commands are documented here. Run `arborescent COMMAND --help` for the concise option list.

### `arborescent generate`

Create missing optional presets for import-linter, Tach and configured type checkers. Existing files are left untouched;
generation is optional, and drift from generated presets is informational.

### `arborescent check`

Run Arborescent's native checks and the configured external engines, report generated-preset drift and ratchet findings
against the baseline. Options:

- `--changed REF`: limit Arborescent, Ruff and the selected type checker to files changed since a Git ref. Graph-wide
  engines are skipped because they cannot check a partial module graph.
- `--type-checker NAME`: override the first checker listed in `type_checkers` (`basedpyright`, `pyrefly`, `pyright`
  or `ty`).
- `--list-dev-tools`: print each `dev_tools` path with the modules below it, plus modules annotated as dev tooling outside every path (exit 1 if any), then exit without checking.
- `--no-tools` / `--engines core`: core mode. Run only Arborescent's own checks (rings, effects, suppressions, the baseline
  ratchet); no import-linter, Tach, type checker or installer is invoked, and each is listed as `SKIPPED`, never `OK`. It
  needs only Arborescent itself (`uvx arborescent check --engines core`), does not import the project and uses no network.
  It starts no engine, but it is not subprocess-free: it uses `git ls-files` to list files (without git it walks the
  directory), `practices` and `--changed`/`--growth-against` call git, and a child policy is checked by a nested
  `arborescent check --no-tools`. It rejects
  `--require-tools`, `--require-profile` and `--install-tools` (exit 2), since those demand an engine.
- `--require-tools`: fail when a configured checker is missing instead of skipping it.
- `--require-profile`: require the versioned Arborescent minimum profile; this also requires its tools.
- `--install-tools`: offer to install missing configured tools before checking. The prompt defaults to No.
- `--yes`: install without prompting; valid with `--install-tools`.
- `--no-baseline`: report every finding without applying the baseline.
- `--rule FAMILY` (repeatable): run and exit on the named rule families only, so a migration can show one family going
  to zero while others are still open. `FAMILY` means what it does for `baseline --adopt` (a rule id, a `ruff`-style
  prefix, or `coverage`, `effects`, `suppressions`, `discard`); an unknown name is an error rather than an empty
  selection. Findings, baseline entries (so none outside the selection are reported stale) and failing engines
  outside the selection are ignored; an engine that fails without any finding still fails the run. Profile and
  modularity gates you request explicitly stay in force. Every `check` also prints `by rule: <rule> N (M new)`, where
  M counts the findings not covered by the baseline.
- `--growth-against REF`: verify baseline growth entries against the baseline at a Git ref.
- `--admitted-against REF`: fail a module that is new since `REF` and was not validly admitted (see "Checking admission of new modules" below).
- `--no-cache`: bypass the Arborescent AST fact cache for this run.

By default, `check` is read-only with respect to the environment and prints install instructions for missing tools.
Installation targets the active interpreter (or uses uv when pip is unavailable); Poetry projects use the `arborescent`
dependency group and update the lockfile. See the installation behavior above for details.

### `arborescent install-tools`

Install missing check tools without running checks. `--type-checker NAME` selects a checker other than the policy's
default, and `--yes` skips the default-No confirmation. This command changes the project environment and, in Poetry
projects, its dependency group and lockfile.

### `arborescent baseline`

Create the initial baseline or shrink it by removing findings that no longer occur. New findings are rejected by
default. `--adopt FAMILY` adopts a previously unrecorded family once; `--allow-growth --reason TEXT --approved-by WHO` records other
intentional baseline growth, its reason and its approver.

### `arborescent coupling`

`report`, `check`, `baseline` and `cost`, described under [Coupling metrics](#coupling-metrics-toolarborescentcoupling).
`check` exits 1 on a regression; a policy or baseline error exits 2.

### `arborescent purity`

Lists every module `mixed-purity` finds (allowed ones marked), most imported first, with its effect kinds, effectful and pure
definitions and importer count. `--json` prints the same as JSON. Report only: `check` is what fails.

### `arborescent ratchet`

Record or lower the `[ratchet.<name>]` baselines (`--name NAME` for one). A rise needs
`--allow-growth --reason TEXT --approved-by WHO`; see [Named ratchets](#named-ratchets-toolarborescentratchetname).

### `arborescent discover`

List Python files that still lack a ring classification, grouped by scope and folder with that scope's outermost ring suggested.
Add valid `__arbor__` ring annotations, matching `classify` patterns, or explicitly exclude files that should not be
scanned. Git-tracked Python files under noise directories are reported separately by `check`.

### `arborescent admit`

`arborescent admit MODULE --ring RING [--reason TEXT] [--yes]` decides whether one module may enter a ring and writes its
`__arbor__` annotation. Dry-run by default; `--yes` writes. It exits 1 when it refuses and never writes then.

The scan is strict and function-level: every import counts, including function-local ones, imports under `TYPE_CHECKING`, and
`importlib.import_module("pkg.x")` / `__import__("pkg.x")` with a literal target (treated as an edge). It refuses when:

- the module's facts violate the ring (the same per-module rules as `check`, applied to all of those imports);
- a dynamic import has a target that is not a plain absolute string literal and no `ring_reason` is recorded (a gate that cannot
  see an edge must not certify its absence);
- a tighter ring also fits and no `ring_reason` is recorded (`--reason TEXT`, or already in the file);
- the ring is unknown or the module has no owning scope.

It prints the evidence it used, reports importers whose ring cannot import the chosen ring as warnings (`check` fails on those
later), and notes a `ring_reason` that excuses nothing (possibly stale). A module that is already annotated with another ring
is left alone. "Admitted" means this module's own facts fit the ring, not that its contribution to the architecture is sound
(re-export and forwarding laundering have their own rules). Existing modules are not forced through admission, and
`discover --suggest` remains the read-only view.

#### Checking admission of new modules: `check --admitted-against REF`

A CI diff check, stateless apart from the human `ring_reason`. New modules are the `.py` files that
`git diff --name-status -M --diff-filter=A <merge-base of REF and HEAD> HEAD` reports added, mapped to module names with the
current source roots (git is called through the `runner` port). For each one the check **recomputes admission at HEAD** with the
same strict test as `arborescent admit`, so a hand-written `__arbor__` is accepted only if it passes. A new module fails when:

- it has no explicit `__arbor__` ring: a ring that comes only from a `classify` glob does not count as admitted
  (`admission-refused`);
- admission refuses it: its facts violate the ring, a dynamic import cannot be read and no `ring_reason` names it, or the
  annotation is invalid (`admission-refused`);
- a tighter ring also fits and no `ring_reason` is recorded (`admission-looser-than-needed`).

Existing modules are never judged, so adopting the flag creates no backlog; the shrink-only baseline still carries the legacy
debt. A rename (content similarity above git's `-M` threshold) is not new; a move that falls below the threshold is new and is
admitted afresh. A deleted module is not new. An empty diff passes. If the diff cannot be read (unknown ref, shallow clone with no
shared history, a ref that looks like an option) the check **fails** with `could not verify admission: ...`; it never passes. In CI
use `fetch-depth: 0` or deepen the clone. The check runs on the full module set (never `--changed`), and nothing is cached
between runs, so the ref is part of every run. `classify-facts` "could sit in an inner ring" stays an information-tier note for
existing modules; it skips modules with no top-level definitions, a `__main__` guard, an abstract class or an entry-script
entry. Admission shares the underlying test (`classify_audit.tighter_ring`) but applies none of those exemptions, because a
bare-import shim that passes every inner ring is exactly what a new module must not be admitted as. The check is not a native
mutation-tested rule (its findings are not `Violation`s and never enter the baseline); `selftest` probes the admission gate
itself (`admission_probes`).

### `arborescent selftest`

Mutation-test Arborescent's native rules by adding forbidden constructs to a temporary copy and checking that each
mutation produces a finding. A surviving mutation fails the command; rules that cannot be exercised for the current
policy are reported as skipped.

### `arborescent cycles`

Print the first-party import cycles, the edges to cut and which function-level imports only break a cycle (see
"Function-level imports and import cycles"). Read-only; exits 0 even when it finds cycles.

### `arborescent bench DIR...`

Measure cold and warm timings for native checks, each available engine and the whole check on prepared projects. Each
directory must contain an `arborescent.toml`. `--checkers NAME...` chooses which type checkers to benchmark.

### Exit codes

Commands succeed with exit code 0. A failed `check`, `selftest` or installation exits 1. A malformed policy or refused
baseline change exits 2. `discover` exits 0 whether or not it finds unclassified files.

## Releasing to PyPI

Releases use the `Publish to PyPI` GitHub Actions workflow, triggered when a GitHub Release is published. Before the
first release, configure a **pending trusted publisher** in PyPI for owner `112345brian`, repository `arborescent`, workflow
`publish.yml`, and GitHub environment `pypi`. The workflow reruns the full test matrix, builds the wheel and source
distribution, then publishes through PyPI Trusted Publishing; no long-lived PyPI token is stored in GitHub. Create a
GitHub Release for the matching version tag (for example, `v0.1.0`) only after the release commit is on `main`.

When `type_checkers` is omitted, Arborescent uses Ty. Set `type_checkers = ["pyright"]` to select Pyright explicitly. During `--changed`, graph-wide import-linter and Tach checks are skipped; Ruff and the selected type checker receive only changed files.

## Running a ring's tests with real I/O blocked (`arborescent.pytest_io_block`)

An opt-in pytest plugin (#105). It never auto-loads: there is no `pytest11` entry point and arborescent still has no
runtime dependency. Install `arborescent[pytest]` (or have pytest 8+) and load it with `-p arborescent.pytest_io_block`
or `addopts`. Select the tests of modules in rings you declared I/O-free:

```ini
[pytest]
addopts = -p arborescent.pytest_io_block
arbor_io_free =
    tests/domain/*
    tests/test_rules.py
```

Globs are matched against the test file's path relative to the rootdir, with `/` separators on every platform;
`--arbor-io-block` selects every test, and `@pytest.mark.arbor_allow_io` opts one test out. During the call phase of a
selected test (fixtures are not blocked) these raise `IOBlockedError`, an `AssertionError`: `open` of a non-source file,
`socket.connect`, `subprocess.Popen`, `os.system`, `sqlite3.connect`, `os.remove`/`rmdir`/`rename`, `shutil.rmtree`,
`time.time`/`monotonic`/`perf_counter`/`sleep`, `datetime.datetime.now`/`today`, the `random` module functions and
`os.environ`. Only calls whose first non-standard-library frame is neither pytest nor the plugin raise.

A passing blocked test shows the code ran on plain values or in-memory fakes **for the inputs that test chose**; it does
not prove a branch the test did not take. Known gaps: names bound before the patch (`from time import time` at import),
a test that patches `builtins.open`, `ctypes` and C extensions that do I/O without an audit event. The static
`effect-*` rules and `effect-patch` remain the guards for those. For network blocking with allow-lists, `pytest-socket`
is the maintained tool; this plugin only raises. Deriving the glob list from the policy is not done yet.
