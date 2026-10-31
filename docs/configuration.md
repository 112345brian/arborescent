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
| `exclude` | `[]` | Globs matched against a path or a file/folder name to skip while scanning. Excluded Python files are counted by folder on every `check`. |
| `children` | `[]` | Explicit nested package roots to inspect for a direct Arborescent policy. In `certify` mode, roots with their own policy are checked recursively; roots without one remain in the parent scan. |
| `child_policy_mode` | `"certify"` | `"certify"` respects direct child policies and includes their results in parent certification. `"parent"` overrides child policies: listed roots stay in the parent scan and are not checked separately. |
| `scan_vendors` | `false` | Include convention-named `vendor`, `vendored`, `third_party` and `site-packages` folders in the parent scan. This lets parent rules govern vendored code; declared child roots with their own policy are still separated in `certify` mode. |
| `module_forbid_imports` | `{}` | Module glob -> internal module/package names this module may not import, in addition to ring rules. |
| `preset` | `"strict"` | How much of the external and OS rules applies: `strict`, `standard` or `relaxed` (see below). |
| `io_modules` | built-in set | Stdlib modules that count as I/O, process or OS. Replaces the default set; `[]` means none. |
| `groups` | `{}` | Named lists, used as `"@name"` inside any `may_import` or `external` list. |
| `dependencies` | `{}` | Import name -> distribution name, where they differ (`yaml = "pyyaml"`). Used for entry-script and manifest checks. |
| `tach_external_exclude` | `[]` | Package names written to the generated Tach `[external] exclude`: optional extras imported lazily, or transitive dependencies the project does not declare. Tach reads only the base dependencies and matches distribution names where they differ from import names (`google-genai`, `pyyaml`). |
| `forbid_everywhere` | `[]` | Packages no module may import, in any ring (one global import-linter contract). |
| `forbid_circular_imports` | `true` | Rejects cycles in the first-party module import graph, including lazy and `TYPE_CHECKING` imports. Set to `false` only to opt out; the Arborescent minimum profile requires `true`. |
| `require_arbor_annotations` | `false` | Makes a module that resolves its ring only through `classify` (no valid `__arbor__` annotation) a hard failure (`unannotated-module`), instead of a silent fallback. See "Requiring annotations" below. |
| `require_arbor_annotations_exclude` | `[]` | `classify`-style module-name globs exempt from `require_arbor_annotations`, for modules that cannot carry an annotation (vendored, read-only, generated). Has no effect unless `require_arbor_annotations` is set. |
| `entry_scripts` | `[]` | Dotted names of PEP 723 single-file scripts. Each must declare, in its `# /// script` block, every third-party distribution the project code imports. |
| `type_checkers` | `["ty"]` | `pyright`, `basedpyright`, `ty` and/or `pyrefly`. The first one runs by default; `check --type-checker` overrides it. A config file is generated for each one listed. |
| `disabled_tools` | `[]` | Tool names intentionally not run or installed: `import-linter`, `tach`, `ruff`, `pyright`, `basedpyright`, `ty`, or `pyrefly`. Disabled tools are omitted from missing-tool hints and setup prompts. Also accepts `arbor-annotations` to ignore `__arbor__` annotations and their validation findings (see below). |
| `python` | `"3.11"` | Target Python version, a quoted string (`"3.12"`; a patch version such as `"3.12.1"` is read as `3.12`): written into the type-checker configs and selects which modules count as stdlib (`3.11` to `3.14`; anything else is an error). See [stdlib-table.md](stdlib-table.md). |
| `output_dir` | `"."` | Where `generate` writes the config files, relative to the root. |
| `baseline` | `"arborescent-baseline.json"` | The baseline file used by `arborescent baseline` and `check`. |
| `suggestion_threshold` | `300` | Logical code lines above which `check` prints a report-only file-size suggestion. |
| `practices` | `{}` | Repository-hygiene checks (below). |
| `package_catalog` | unset | Opt-in, strict preset only: path of a `packages.toml`. Every module and asset must belong to exactly one package (see [Package assignment](#package-assignment)). |

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
| `preset`, `io_modules`, `groups` | existing defaults | Local external-import, OS and effect defaults, plus named allow-list groups. |
| `module_forbid_imports`, `dependencies`, `entry_scripts`, `forbid_everywhere`, `forbid_circular_imports`, `tach_external_exclude`, `require_arbor_annotations`, `require_arbor_annotations_exclude` | existing defaults | Existing architecture settings, evaluated within this scope. |

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

Repository package ownership and extraction metadata live in a root `packages.toml` file. Arborescent owns the schema,
validation, and extraction command; the repository declares stable package identities and the paths/roles it owns. Each
path can carry string metadata that follows it when copied, such as the import package name or the relative path to an
independent `arborescent.toml`.

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
ownership cannot overlap, and declared dependencies must form an acyclic graph. Extraction copies declared paths into a
new destination and writes `.arborescent-package.toml` (see "Extraction manifest" below). A package with its own
direct Arborescent policy is copied with that policy when it is listed as a configuration path; the child policy remains
independently usable after extraction.

Generated local output below a declared path is not copied: `__pycache__`, `*.pyc`, `.venv`, `venv`, `node_modules`,
`.git` (directory or worktree pointer file), `.arborescent`, `.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `.tox`,
`build`, `dist` and `.DS_Store` (the noise directories Arborescent already ignores, plus build output). A directory with
one of those names is treated as generated output unless it is a real Python package (it contains `__init__.py`), so a
package such as `pkg/build/__init__.py` is copied. A namespace package without `__init__.py` that is named like build
output is skipped; give it an `__init__.py` or rename it. A declared path itself is always copied, even when it is named
`build`, and symlinks inside skipped directories do not abort extraction. Special files (named pipes, devices) and
symlinks elsewhere are rejected.

```sh
arborescent packages validate
arborescent packages list
arborescent packages extract collection-core --output /tmp/collection-core
arborescent packages extract collection-core --output /tmp/collection-core --with-dependencies
```

Dependencies are included only when explicitly requested and are placed under `dependencies/<package-id>/`. The
destination must not already exist. Extraction does not rewrite imports or invent build configuration; the package
metadata declares the layout and the package owns its build and architecture files.

### Extraction manifest

`.arborescent-package.toml` (`manifest_version = 2`) keeps what a package *is* apart from where it *is* and what
extraction *did*:

| Table | Holds | Changes on extraction? |
|---|---|---|
| `[package]` | `id`, `name`, `kind`, `domain`, `summary`, `capabilities`, `depends_on` | never |
| `[[paths]]` `role`, `metadata` | per-path meaning: role plus custom metadata (import name, boundary, `arborescent_config`, scope) | never |
| `[origin]` | `repository`, `root` and `extraction_status` as declared in the catalog | no; kept for traceability |
| `[[paths]]` `origin_path` | the path in the origin repository | no; kept for traceability |
| `[location]` | the current `repository` (defaults to the origin; `--repository` sets another) and `root` (`.`) | yes |
| `[[paths]]` `current_path` | where the path lives inside the extracted tree | yes |
| `[extraction]` | `status = "extracted"` and `included_packages` | yes; describes the operation |
| `[[dependencies]]` | the same stable fields for each included dependency, plus its origin and `current_root` | as above |

Module identity is never rewritten. Extraction copies `arborescent.toml`, `classify` globs and in-file `__arbor__`
annotations byte for byte. Extraction then compares, for every shipped `arborescent.toml`, the module name the policy
derives for each copied file (from its `sources` and `source_roots`) before and after the move. A `destination` that
renames a module, or moves it out of the policy's `sources`, fails with `would change module identity` (for example
`core -> renamed`) and the partial extraction is removed. Files that merely become newly visible to a policy, such as
tests moved under a `sources = "."` policy, add modules and are allowed. `arborescent_config` metadata is relative to
the package's current root and must be shipped as a declared path.

The manifest is a provenance snapshot taken at extraction, not an integrity seal: an extracted package is expected to
evolve, so its files are not hashed.

`arborescent packages validate --extracted DIR` re-validates an extracted tree (extraction runs the same structural
check): the manifest must be well formed (stable lowercase package ids, status `extracted`, normalized origin paths, at
least one path, each path inside its package's current root, no duplicate dependencies); current paths
must resolve inside the tree; no symlink may leave it (generated directories such as `.venv` are ignored);
`included_packages` must match the package and its dependencies; and every shipped policy, whether
`arborescent.toml` or a `pyproject.toml` with `[tool.arborescent]` and wherever it was placed, must load and find its
`sources` and `source_roots`. The module-identity comparison (also applied to `pyproject.toml` policies) needs the origin tree, so it
runs only during extraction. Files outside a policy's `sources` are not part of that comparison. A parent policy (`children`, `child_policy_mode`) is neither read nor changed by extracting
a child; the child keeps governing itself.

`suggestion_threshold` controls a report-only hint for large files. The metric counts lines containing code tokens and
ignores blank lines, comments, module/class/function docstrings, and multi-line literal-only data tables. Suggestions
appear during `check`; they are not violations, are never added to the baseline, and do not change the exit status.
There is no per-file suppression. Function size and complexity remain Ruff rules.

`module_forbid_imports` adds focused internal import restrictions without changing the module's ring. For example,
`"bootstrap.adapters" = ["bootstrap.launcher"]` blocks that package and its descendants only in the named adapter
module (or matching module glob).

Tool configs remain project-owned and are discovered natively by their tools. `arborescent generate` creates missing optional presets
and skips every existing file. Drift is shown only for files still marked as Arborescent-generated,
and is informational. `check --require-tools --require-profile` requires Arborescent minimum profile v1 (Ruff 0.17.2,
Tach 0.35.3) and behaviorally probes the required Ruff rules.

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
`extend-select`, `ignore`, `per-file-ignores`, `max-complexity`, `max-args`, `max-branches`, and `max-statements`.
The base includes correctness, typing, security, modernization, path, and performance rule families in addition to
complexity and architecture-aware rules. Ruff's broad test-assertion and subprocess-wrapper warnings are scoped so
tests remain idiomatic and real process adapters can declare their deliberate subprocess boundary. `PIE807` is ignored
because it conflicts with strict type inference for container factories. `check` prints `standard rules (baseline vN)` when no rule
customization is active, or `customised rules: ...` with the changes that apply.

### Package assignment

Set `package_catalog = "packages.toml"` (strict preset only; any other preset is a policy error) and `arborescent check`
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

Findings that name a package pair (`package-multiply-assigned-*`, `package-undeclared-dependency`) are failures like any
other rule and can be baselined. `package-spans-rings` is printed as a `NOTE` and neither fails the check nor enters
the baseline. `selftest` probes each rule when `package_catalog` is set: a new unowned module or asset, a second
package claiming an owned file, a glob contradicting a path owner, a package spanning two rings, and a new import across
packages with no declared dependency. A probe that cannot be built for the catalog is listed with the reason.
This adds no behaviour to `packages extract`, which is unchanged.

### Migration annotations and export

A package entry can also say where it will live and what it exposes, and `arborescent packages export` publishes all of
it as one versioned JSON document for external migration tools (it converts nothing and migrates nothing):

```toml
[[packages]]
id = "core"
# ... id, name, kind, repository, root, summary, depends_on, paths as above
public = ["app.core.api", "app.core.models.*"]   # the public surface (module-name globs)
externals = ["pydantic"]                          # third-party import names it may use
[packages.home]                                   # kind = "repository", repository = "https://github.com/org/core.git"
kind = "workspace-member"                         # ... or a member of a workspace in the same repository
path = "packages/core"
[[packages.module_renames]]                       # module names that change in the target
from = "app.core"
to = "core"
```

All four keys are optional; a missing one is a named gap in the export when it matters (`public` only when another package imports the package), never a guess. See
[migration-export.md](migration-export.md) for the document, the schema, the gap codes, a `git filter-repo` recipe and the
comparison with blinkered's `blinkered.toml`.

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
`arborescent packages migrate` (draft PR #69; its page, `migration-driver.md`, arrives with it).
[migration-recipes.md](migration-recipes.md) describes it and shows, with a worked example and the caveats found by running
each tool, how to supply the same arguments by hand to git filter-repo, `git subtree split`, rope, deptry, Copybara and
Josh. Those recipes are documentation, not commands Arborescent ships.

## Module ring annotations: `__arbor__`

A module can declare its own ring with a module-level assignment to a literal dictionary:

```python
__arbor__ = {"ring": "domain"}
```

The `ring` value must be a string naming one of the policy's configured `rings` (the owning scope's rings in
multi-scope mode); `ring` is the only legal key today, and any other key reports `invalid-arbor-annotation`.
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

1. Run `arborescent annotate` (dry-run by default) to see what it would insert; `arborescent annotate --yes` writes
   `__arbor__ = {"ring": ...}` into every module `classify` currently resolves but that has no existing annotation.
   It is idempotent: a second run reports already-annotated files and writes nothing.
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
| `forbid` | positional preset default | Effect categories this ring may not use: `clock`, `env`, `identity`, `randomness`, `sleep`, `threads`, `process`, `dynamic-import`, or `patch`. |
| `require_abstract` | `false` | Every public (non-`_`-prefixed) top-level class or function in this ring must be an abstract shape: a class inheriting `abc.ABC`/`typing.Protocol` (or declaring `metaclass=ABCMeta`), or a function whose body is a stub (`...`, `pass`, or `raise NotImplementedError`, past an optional docstring). A concrete member fires `non-abstract-port`. |

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

`io_modules` also drives the generated import-linter contracts, which ban the I/O modules a ring does not list. Under
`relaxed` the contracts ban only declared third-party packages.

## Rules Arborescent checks itself

The ids below are `arborescent.domain.Rule`. They appear in baseline entries (`rule|subject|detail`), so they never change.

| rule | fires when |
|---|---|
| `unclassified` | a module has neither a valid enabled `__arbor__` annotation nor a matching `classify` pattern |
| `noise-dir-code` | a tracked `.py` file appears below a skipped directory such as `.venv`, `.tox` or `node_modules` |
| `vendor-folder` | a convention-named vendor folder is excluded and fingerprinted in the baseline |
| `wildcard-ring` | a non-outermost ring uses `*` in `external` or `may_import` (one finding per ring and axis) |
| `suppression` | a `# noqa`, `# ruff: noqa` or `# type: ignore` directive is inventoried by module and kind; each occurrence is ratcheted |
| `discard` | a `discard(value, reason=...)` call is inventoried by module; each call is ratcheted without claiming type-aware `Result` detection |
| `effect-clock` | a ring that forbids `clock` reads the ambient clock (`time.time`, `datetime.now` and related calls) |
| `effect-env` | a ring that forbids `env` reads environment variables through `os.environ` or `os.getenv` |
| `effect-identity` | a ring that forbids `identity` generates UUID identities |
| `effect-randomness` | a ring that forbids `randomness` uses `random` or `secrets` |
| `effect-sleep` | a ring that forbids `sleep` calls `time.sleep` or `asyncio.sleep` |
| `effect-threads` | a ring that forbids `threads` uses `threading` or constructs a `ThreadPoolExecutor` |
| `effect-process` | a ring that forbids `process` starts a process or shell command through `subprocess`, `multiprocessing`, `ProcessPoolExecutor`, or direct `os` spawn/exec APIs |
| `effect-dynamic-import` | a ring that forbids any effect uses `importlib` or `__import__` |
| `effect-patch` | a configured tests folder patches a project target whose ring forbids `patch` |
| `internal-import` | a ring imports a ring it may not, or a module imports a target prohibited by `module_forbid_imports` (top-level, lazy and `TYPE_CHECKING` imports all count) |
| `non-abstract-port` | a ring with `require_abstract = true` has a public top-level class or function that is not a recognized abstract shape (the Dependency Inversion Principle: a "ports" ring must hold abstractions, not concrete implementations) |
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
| `package-unowned-asset` | a non-Python file below the policy's `sources` is under no package path and matches no `[[assets]]` glob |
| `package-multiply-assigned-asset` | an asset is under one package's path and also matched by another package's `[[assets]]` glob |
| `package-spans-rings` | information only (never fails, never baselined): a package has modules in several rings, so a split must follow module sets, not folders |
| `package-undeclared-dependency` | a module imports a module of another package that its own package does not list in `depends_on` |

Effect rules detect direct imports and attribute chains, including aliases. They do not detect uses routed through
`getattr`, variables, or wrappers. A test patch is checked against the target module's ring and is considered only when
the first argument is a static project-package string or attribute under the configured `secrets-guard` tests folder.
`noqa` comments do not suppress native effect findings. Suppression and reasoned `discard()` counts are always reported;
increases need `baseline --allow-growth --reason` (or one-time family adoption), while removals can shrink the baseline.

## Practices: `[tool.arborescent.practices]`

Each practice is off until it has a value.

| key | value | checks |
|---|---|---|
| `gitignore-shadow` | `true`, or a list of file globs to allow | a `.py` file is git-ignored (an unanchored pattern hid a package) |
| `sibling-versions` | glob of `pyproject.toml` files, e.g. `"*/pyproject.toml"` | a project pins a sibling project to a version the sibling does not have |
| `secrets-guard` | tests folder, e.g. `"tests"` | `conftest.py` has no autouse fixture that scrubs the environment |
| `docs-sync` | path of the architecture doc | the doc's `\| module \| ring \|` table equals the policy (see `docs/architecture.md`) |
| `crlf` | list of file globs, e.g. `["*.sh"]` | a matching file has CRLF line endings |
| `undeclared-imports` | `true`, or a manifest path (default `pyproject.toml`) | a third-party import is absent from the manifest's dependencies |

## Baseline ratchet

`arborescent baseline` creates a baseline the first time. On later runs it removes stale entries, but refuses to add new
ones. To adopt one previously unrecorded rule family, use `arborescent baseline --adopt FAMILY`; adoption is allowed only
when that family has no existing baseline entries. Other growth requires
`arborescent baseline --allow-growth --reason "why this debt is accepted"`. The version-2 JSON baseline records each
growth reason and the exact keys it added; version-1 baselines still load and are upgraded when written.

`arborescent check --growth-against REF` compares the baseline at `REF` with the working baseline and requires every new
key to match the keys in growth-log entries appended since that ref. This checks consistency; it does not prove the
command wrote the file. A person could edit both the baseline and its log by hand, so review of baseline changes
remains part of the protection (for example, require review through `CODEOWNERS`).

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
- `--no-tools`: skip import-linter, Tach and the type checker.
- `--require-tools`: fail when a configured checker is missing instead of skipping it.
- `--require-profile`: require the versioned Arborescent minimum profile; this also requires its tools.
- `--install-tools`: offer to install missing configured tools before checking. The prompt defaults to No.
- `--yes`: install without prompting; valid with `--install-tools`.
- `--no-baseline`: report every finding without applying the baseline.
- `--growth-against REF`: verify baseline growth entries against the baseline at a Git ref.
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
default. `--adopt FAMILY` adopts a previously unrecorded family once; `--allow-growth --reason TEXT` records other
intentional baseline growth and its reason.

### `arborescent discover`

List Python files that still lack a ring classification, grouped by scope and folder with that scope's outermost ring suggested.
Add valid `__arbor__` ring annotations, matching `classify` patterns, or explicitly exclude files that should not be
scanned. Git-tracked Python files under noise directories are reported separately by `check`.

### `arborescent selftest`

Mutation-test Arborescent's native rules by adding forbidden constructs to a temporary copy and checking that each
mutation produces a finding. A surviving mutation fails the command; rules that cannot be exercised for the current
policy are reported as skipped.

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
