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
| `source_roots` | `["."]` | Folders that module names are relative to (`src` for a `src/app` layout). Also handed to tach (see [How tach reads `source_roots`](#how-tach-reads-source_roots)). |
| `exclude` | `[]` | Globs to skip while scanning. A bare entry (`client`, `*.gen`, `dir/sub`) is unanchored, like a `.gitignore` line: it matches the path, a file or folder name, or any ancestor folder at **any depth**, so `client` also hides `src/pkg/client/`. A leading `/` anchors the entry to the repository root and matches by full relative path only: `/client` hides the top-level `client/` and nothing deeper, `/src/*/generated` hides one level of generated folders. Excluded Python files are counted by folder on every `check`; a folder hidden only by a bare name below the root is flagged there with the anchored spelling. |
| `children` | `[]` | Explicit nested package roots to inspect for a direct Arborescent policy. In `certify` mode, roots with their own policy are checked recursively; roots without one remain in the parent scan. |
| `child_policy_mode` | `"certify"` | `"certify"` respects direct child policies and includes their results in parent certification. `"parent"` overrides child policies: listed roots stay in the parent scan and are not checked separately. |
| `scan_vendors` | `false` | Include convention-named `vendor`, `vendored`, `third_party` and `site-packages` folders in the parent scan. This lets parent rules govern vendored code; declared child roots with their own policy are still separated in `certify` mode. |
| `vendor_roots` | `[]` | Folders whose child folders are vendored packages. Each child `X` becomes a source root (`<root>/X/src` if it exists, else `<root>/X`) added to `sources` and `source_roots`, and the folder is scanned even though its name is a vendor convention, so a vendored package is not reported as external. Other vendor folders stay excluded. Not allowed with `scopes`. |
| `library_roots` | `[]` | Same expansion for editable in-repo libraries (for example `libs`); the folders are scanned normally. Not allowed with `scopes`. |
| `module_forbid_imports` | `{}` | Module glob -> internal module/package names this module may not import, in addition to ring rules. |
| `importable_by` | `{}` | Module glob -> importer globs: the inverse of `module_forbid_imports` (see below). A listed module, and its submodules, may be imported only by modules matching one of the globs. |
| `construction` | `[]` | Array of tables: what a caller may not or must pass to a class, and which modules may construct it (see below). |
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
| `taint_seeds` | every `effect-*` category that existed before 0.8.0 except `patch` and `global`, plus `network` (`monotonic`, `host` and `fs-query` are valid but not seeded unless listed; `state`, `registry` and `cache` mutate memory rather than do I/O and are not valid seeds) | Which I/O primitives the function-level taint pass (#98, below `classify-facts`) seeds a function as reaching. `[]` turns the pass into a no-op. Root-level (shared by all scopes). |
| `disabled_tools` | `[]` | Tool names intentionally not run or installed: `import-linter`, `tach`, `ruff`, `pyright`, `basedpyright`, `ty`, or `pyrefly`. Disabled tools are omitted from missing-tool hints and setup prompts. Also accepts `arbor-annotations` to ignore `__arbor__` annotations and their validation findings (see below). |
| `python` | `"3.11"` | Target Python version, a quoted string (`"3.12"`; a patch version such as `"3.12.1"` is read as `3.12`): written into the type-checker configs and selects which modules count as stdlib (`3.11` to `3.14`; anything else is an error). See [stdlib-table.md](stdlib-table.md). |
| `output_dir` | `"."` | Where `generate` writes the config files, relative to the root. `tach.toml` always goes to the root (see [How tach reads `source_roots`](#how-tach-reads-source_roots)). |
| `baseline` | `"arborescent-baseline.json"` | The baseline file used by `arborescent baseline` and `check`. |
| `ratchet_files` | `[]` | Extra numeric ratchet JSON files (`{"values": {"name": 3}, "growth": []}`) checked by `check --growth-against`. |
| `suggestion_threshold` | `300` | Logical code lines above which `check` prints a report-only file-size suggestion. |
| `class_field_threshold` | `20` | Fields above which `check` prints a report-only suggestion for a class; `0` switches it off. |
| `class_method_threshold` | `20` | Methods above which `check` prints a report-only suggestion for a class; `0` switches it off. |
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
| `module_forbid_imports`, `importable_by`, `construction`, `dependencies`, `entry_scripts`, `forbid_everywhere`, `forbidden_modules`, `forbid_circular_imports`, `tach_external_exclude`, `require_arbor_annotations`, `require_arbor_annotations_exclude` | existing defaults | Existing architecture settings, evaluated within this scope. |

Root settings such as `python`, `type_checkers`, `disabled_tools`, `output_dir`, `baseline`, `suggestion_threshold`,
`class_field_threshold`, `class_method_threshold`, `ruff`, `practices`, `exclude` and `children` apply to the composed repository. Root-level architecture keys (`rings`, `sources`,
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

`class_field_threshold` and `class_method_threshold` control the same kind of hint for a single class (a "god class"), which a
file's line count does not show: `SUGGEST app/model.py:12: class Settings has 34 fields (suggestion threshold: 20)`. A count must
exceed its threshold. It applies to every scanned class in every ring, nested classes counted on their own; it is never a
violation, never baselined and never changes the exit status. There is no per-class suppression: raise a threshold, or set it to
`0`, where wide data holders are normal.

- A field is a distinct name declared in the class body or assigned on `self` in a method. `ClassVar` annotations, dunders,
  `ALL_CAPS` constants and enum members do not count (attributes set through `setattr` are not seen).
- A method is a distinct non-dunder `def`, so a property and its setter count once.
- Only what the class itself declares counts, not what it inherits.

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

`call_allowlist` says which modules may make a particular call, where an import rule cannot: `os`, `tempfile` and `subprocess` are
imported everywhere for good reasons, but only the wrapper that carries the timeout or the atomic-write discipline should call
`os.replace` or `subprocess.run`. It is off until an entry is declared:

```toml
[[tool.arborescent.call_allowlist]]
calls = ["subprocess.*"]                   # dotted call targets; `*` is a glob and also matches dots
modules = ["app.command_port"]             # module globs that may call them (and/or `rings`; not both empty)
reason = "the command port sets the timeout and kills the whole process tree"

[[tool.arborescent.call_allowlist]]        # an idiom: either call alone is fine, both in one function is not
calls = ["tempfile.mkstemp", "tempfile.NamedTemporaryFile"]
and_calls = ["os.replace", "os.rename", "pathlib.Path.replace", "pathlib.Path.rename"]
modules = ["app.fsutil"]
reason = "use save_text_atomic"
```

Without `and_calls`, every other call of a listed target is `call-outside-allowlist`, one finding per call site, numbered per
callee so that a further call in a baselined module is new and a moved one is not. With `and_calls`, a function or method that
makes one call matching `calls` and another matching `and_calls` is `hand-rolled-idiom`, and neither call is restricted by itself.
Either message names the module, its ring, who may do it and the `reason`. A `modules` glob that matches no module is reported,
and `selftest` makes each entry's call (or pair) in a module that may not. `dev_tools` modules are checked too: list them in
`modules` if scripts may make the call. A first-party function can be a target as well (`app.queries.build`).

Targets are resolved by syntax, through `import os as o`, `from os import replace as swap`, function-level and relative imports.

**`pathlib` methods** are targets of their own, `pathlib.Path.<method>`; listing `os.replace` does not cover them. A method call
counts when its receiver can only be a `Path`: `Path(tmp).replace(dest)`, `(root / name).rename(dest)`,
`dest.with_suffix(".tmp").write_text(text)`, or a name or class attribute whose every binding says so (`tmp_path: Path`,
`p: Path | None`, `tmp = Path(name)`, a dataclass field `tmp: Path` used as `self.tmp`). `str.replace` and `datetime.replace`
are never taken for it. Only the methods that read or write the filesystem are recorded: `chmod`, `hardlink_to`, `lchmod`,
`mkdir`, `open`, `read_bytes`, `read_text`, `rename`, `replace` (with its one argument), `rmdir`, `symlink_to`, `touch`,
`unlink`, `write_bytes`, `write_text`. A glob such as `pathlib.*` matches them too.

What it does not see:

- a call on a value of any other type: `proc.communicate()` is not `subprocess.*`;
- a `Path` the module does not show to be one: an unannotated parameter or fixture (`def f(tmp_path):`), a `str | Path` value
  even after `p = Path(p)`, a loop, `with` or unpacking target, what a function of yours returns, an imported constant,
  `self.tmp = Path(...)` with no class-level annotation, a subclass of `Path`, a re-exported `Path`, or any method not listed
  above;
- a call that is never spelled: `swap = os.replace; swap(a, b)`, `getattr(os, "replace")(a, b)`, `functools.partial(os.replace, a)`,
  `asyncio.to_thread(subprocess.run, argv)`, `map(os.remove, paths)`;
- `from os import *`, a re-export through another module (`from app.compat import replace`: list that name too), and a builtin
  such as `open` or `eval` (a target must be dotted);
- a first-party `Class.method` called inside the module that defines the class;
- an idiom split across functions (one makes the temporary file, a helper renames it), or written at module level. A nested
  function or `lambda` counts as part of the function around it;
- names are resolved per module, not per scope, so a local variable or parameter named like an imported module is taken for it,
  and a name imported twice resolves to one of the two;
- a target is only text: a mistyped `os.repalce` restricts nothing, and `selftest` cannot tell, because it writes the same text.

`companion` says that doing one thing obliges a function to do another: submit and record, delete and audit. Types and rings
cannot see a skipped record, because each call is allowed on its own. It is off until an entry is declared:

```toml
[[tool.arborescent.companion]]
when_calls = "app.forms.submit"                      # the action: a dotted target (`*` is a glob), `*.name`, or a list
must_also_call = ["*.record_outcome", "app.audit.write"]  # any one of these will do
rings = ["adapters", "composition"]                  # where it applies (and/or `modules` globs; leave both out for everywhere)
reason = "an unrecorded submission is lost"
```

`missing-companion-call` reports a function or method that itself calls a `when_calls` target and has a path that finishes
with no record: no `must_also_call` target is called on it, and no function that does is called on it. "A function it calls" is
one hop: a helper in the module, an imported function or static method of this repository, or `self.name()` / `cls.name()` in
the same class. A dotted target is resolved as for `call_allowlist` (`pathlib.Path.unlink` included); `*.name` matches a call of
anything named `name` on any receiver (`self.recorder.record_outcome()`, `make().record_outcome()`, `record_outcome()` imported
from anywhere), which is how an injected recorder is named. The finding is on the line of the action and says where the
unrecorded path finishes. A record before the action on the same path counts.

**Paths** are followed through `if`/`elif`/`else`, early `return`, `try`/`except`/`else`/`finally`, `match`, `for`, `while`,
`break` and `continue`. To keep false findings low, these are assumptions, not proofs:

- a path that leaves by an exception (`raise`, `sys.exit()`, `assert_never()`) need not record: the caller hears of it. A
  function that records nowhere at all is still reported;
- a loop runs its body at least once, so `for result in results: record(result)` records;
- an `except` handler is entered by an exception from before the action: an action that raised is taken as not done. A record
  the `try` body makes unconditionally counts there too (best-effort recording). An explicit `raise` in the body carries its
  path to every handler, whatever the exception type;
- a `match` covers its subject when it has a `case _` (or a bare name), or at least two unguarded cases that are all dotted
  names, class patterns, `None`, `True` or `False`; otherwise there is a path that matches no case;
- the same test twice gives the same answer (`if live: submit()` ... `if live: record()`), until a name in it is assigned.
  Only names, attributes, constants, one `is`/`==` comparison, `not`, and a flat `and`/`or` of those are compared, spelled alike;
- where the recorder is known to be missing there is nothing to record: `if self.recorder: self.recorder.record_outcome()`.

To exempt a function, say why in a comment: `# companion-exempt: <reason>`, anywhere in it or on the line above it (a blank line
ends it, and a comment without a reason exempts nothing). It exempts the function from every `companion` entry.

What it does not see, or gets wrong:

- a record skipped in one iteration of a loop is not reported if a later iteration or the code after the loop records, and a
  loop that never ends (`while True` with no `break` or `return`) has no finishing path. A loop that runs zero times is not
  considered;
- a record skipped because an exception was swallowed after the action (`submit()` and `notify()` inside the same `try`, then
  `record()`), or because a context manager suppressed it;
- `x and record()`, `record() if x else None`, a comprehension, and a nested function or `lambda` count as always running, where
  they are written; a helper reached in one hop counts as recording even when only some of its own paths do;
- conditions linked in any other way are taken as independent, so these can be reported though no real path skips the record:
  a flag set beside the action (`sent = True` ... `if sent:`), a test with a call or `isinstance` in it, and `if result:
  record(result)` where a false result means nothing happened. Use the exemption comment there;
- a very large or deeply branching function whose paths cannot be enumerated within a fixed limit is judged as a whole: any
  record in it will do;
- order, count and arguments: recording before the action, once for ten actions, or recording something else all pass;
- a record two or more calls away, or behind a call that cannot be named: a method on an injected or built object
  (`self.service.finish()`, `Helper().finish()`), an inherited method, `__init__`, a decorator, a context manager's exit, a
  callback, `getattr`, `functools.partial` or a re-export. A record by one of these routes is reported although it happens;
- an action that is not spelled as a call: a re-bound name (`send = submit; send()`), a function passed on
  (`asyncio.to_thread(submit)`), or a dotted target called on a value (name it `*.submit` instead);
- an action at module level, and callers of a wrapper: only the function that calls the action directly is checked, so a
  wrapper that leaves recording to its callers is reported (exempt it, and add its name to `when_calls`);
- a nested function or `lambda` counts as part of the function around it, whether or not it ever runs;
- `*.name` matches every method of that name, so a common name (`*.save`, `*.write`) reports, or accepts, unrelated calls;
- a mistyped or renamed `when_calls` target matches nothing, and `selftest` cannot tell, because it writes the same text.

A `modules` glob that matches no module is reported. `selftest` adds, in a module each entry watches, a function that does the
action and records nothing. `check --changed` reads only changed files, so there it does not report a function that calls anything
else of this repository in a file it did not read (that callee might record); only a full run decides.

`[[tool.arborescent.construction]]` goes one step further and says what a caller may pass (#234), for a field that is derived from the
others and must never be supplied, which no type annotation can say. Off unless an entry or a field marker exists.

```toml
[[tool.arborescent.construction]]
type = "app.models.OnboardingProgress"   # dotted name of a class
forbid_kwargs = ["tier"]                 # no caller may supply it, by keyword or by position
require_kwargs = ["user_id"]             # every constructing call must pass it by name
only_from = ["app.onboarding.*"]         # module globs that may construct the class at all
reason = "tier is derived from the answers"
```

At least one of the three lists is needed, and a written list must not be empty. A field can carry the first restriction itself, so
that it travels with the type and needs no policy entry:

```python
tier: Tier = field(default=Tier.NEW, metadata={"arbor": "derived"})
```

- `construction-forbidden-argument`: a call passes a forbidden or derived field by keyword, in a literal `**{...}`, by a positional
  argument that lands on it, or through `dataclasses.replace` / `copy.replace`.
- `construction-missing-argument`: a call does not pass a `require_kwargs` name by keyword (a positional argument does not count;
  `replace` is not asked).
- `construction-outside-site`: a module outside `only_from` calls the class, or `replace`s an instance of it. This includes the
  module that defines the class, so list it if its own factories or methods build one.

The class is resolved through imports, aliases and re-exports (a package `__init__` importing it). Positional arguments are mapped for
dataclasses (inheritance, `kw_only`, `KW_ONLY`, `init=False`, `ClassVar`), `NamedTuple`, an explicit `__init__` and subclasses of
those. A marker is inherited by subclasses; a policy entry names one class and does not cover its subclasses. Entries are checked
against the call sites in their own scope; markers apply everywhere. A class, field or `only_from` glob that no longer exists is
reported, findings go into the baseline like any rule, and `selftest` breaks each restriction on purpose.

For `replace`, the type of the first argument is read from the same module only: a parameter or variable annotation, a constructor
call, `self` / `cls`, `self.attribute` annotated in the class body, or a name assigned from one of these. It is flow-insensitive.

What it does not see (a clean run is not proof):

- `replace` on a value whose type comes from elsewhere: a function's return value, an unannotated parameter, a loop or
  tuple-unpacking target, a container element, `a.b.c`.
- A `**value` that is not a literal dict; a `*value` of unknown length, or anything positional after it.
- `partial(X, ...)`, `make = X; make(...)`, `getattr`, `X.__new__`, `__replace__`, `cls(...)` and `super().__init__(...)` inside the
  class, deserialisers that build the object from data, and assigning the attribute after construction.
- Positional arguments of a class whose constructor cannot be read: an `attrs` or other unknown decorator, a metaclass, an own
  `__new__`, a base
  outside the scanned sources, `pydantic.BaseModel`.
- A class made by a call (`namedtuple(...)`) or nested in another class, a star re-export, or a re-export left out of `__all__`.
- A marker whose `metadata` is not a literal dict or whose `field` is not `dataclasses.field`.
- In a check of selected paths, a class defined in a file that was not selected (keyword checks from policy entries still work).

`port_binding` is an opt-in check on how many production classes bind a port. A port with one adapter (a clock, a file
system) degrades silently when a second one appears; a port with none is dead, or the scan does not reach its adapter.
Plugin points with many implementors are simply not listed, or given the `max` they are meant to have:

```toml
[[tool.arborescent.port_binding]]
port = "app.ports.Clock"            # a Protocol or abstract class
# max = 1                           # the default; at least one implementor is always required
# exclude = ["app.simulation"]      # module globs or `module.Class` names that are not production bindings
# reason = "one source of time"     # appended to the finding
# shadow = ["monotonic"]            # effect categories this port owns (needs max = 1); see below
# shadow_allow = ["app.bench"]      # module globs or `module.function` names that may use them directly
```

A class implements the port when it subclasses it, or, for a Protocol, when it defines every member the Protocol (and the
Protocols it extends) names: methods, class attributes and `self.x` assignments, its own or inherited from scanned bases.
Only names are compared, not signatures (that is the type checker's job), so a Protocol with one common method name such
as `run` can match an unrelated class: name it in `exclude`. A memberless Protocol and an abstract class count
subclasses only. A class does not count when it is itself a Protocol, lists `ABC`/`ABCMeta` or declares an
`@abstractmethod`; when it is a test double (in a `tests`, `testing`, `conftest`, `fakes` or `test_*` module, or named
`Fake*`, `Stub*` or `Mock*`); or when it lies in a `dev_tools` path or an excluded file. A member or base the scan cannot
see (a third-party base class, `setattr`, `__getattr__`) is not followed. The table is per scope; implementors are counted
across all scopes. `check --changed` skips the rule.

`shadow` (opt-in per port) looks for a function reaching the port's resource by another route. The resource is an effect
category you name, as in a ring's `forbid` (`monotonic`, `clock`, `process`, `network`, `database`, `filesystem`, ...;
not `state`, `registry`, `cache`, `global` or `patch`). Any function or module-level code that uses an effect of that
category directly, outside the one implementation, gets a `port-shadow` note: a `NOTE` line that never fails `check` and
never enters the baseline. `time.monotonic` is noted although the adapter calls `time.perf_counter`.

The implementation is the class, its scanned base classes, the functions and classes of the same module that they call
(transitively), and that module's top-level code; a function beside the adapter that none of them calls is noted. Not
noted: a parameter default (`clock=time.monotonic`, the injection seam), a constant or exception class
(`subprocess.DEVNULL`, `subprocess.TimeoutExpired`), test doubles, `exclude`, `dev_tools` and `shadow_allow`. If the
implementation uses no effect of a listed category, a note says there is nothing to compare. The check waits for a clean
binding (exactly one implementation).

What it does not see: a resource with no effect category (a third-party SDK the scan does not know), a route through
another module's helper or an injected object (only the direct user is noted), a method on a value whose type the scan
cannot tell, and a same-module helper the adapter only passes as a callback (it is noted). Name those calls in a
`call_allowlist`. A broad category such as `filesystem` notes every other adapter that touches files: list them in
`shadow_allow`, or leave the port without `shadow`.

`name_collisions` is an opt-in observation (a `NOTE` line; it never fails `check` and never enters the baseline) for one
word used for two concepts: a field or local called `ratchet` that is not a `Ratchet`, or, once `scope` is a declared word,
a helper called `_in_scope` that has nothing to do with `ArchitectureScope`.

```toml
[tool.arborescent.name_collisions]  # the table alone turns it on
# min_length = 5                    # shorter names are too generic
# words = ["scope"]                 # words that mean one thing: `scope` matches a type ending in `...Scope`; also turns on
#                                   # parameters and function names for that word

[[tool.arborescent.name_collisions.allow]]
name = "baseline"
reason = "the file and the object are the same concept here"
```

The definition is deliberately narrow, to stay quiet:

- Vocabulary: public module-level classes of the same scope. Names are compared ignoring case and underscores
  (`architecture_scope` matches `ArchitectureScope`); `scope` matches `ArchitectureScope` only when listed in `words`.
- Looked at: annotated class fields, `self.x` assignments and function locals. For declared `words` only: annotated
  parameters (the name is the word) and function names (the name, or its last `_` part, is the word: `_in_scope`). Not
  module-level variables, unannotated class attributes (enum members), `UPPER_CASE` names, dunder methods, docstrings or
  comments.
- Reported only with evidence that the value is something else: an annotation that does not name a type ending in that
  word (`ratchet: tuple[str, ...]`, but not `ratchet: NamedRatchet` or `ratchet: Any`), or, unannotated, a literal or a
  comprehension. A function is judged by all its annotations together. `ratchet = load()` says nothing and is left alone.
- A `str` value (`ratchet: str`, `str | None`, a string literal, a function returning `str`) is taken to be a reference
  to the thing by its name or path, and is not noted. A container of strings is.
- Not automatic: `scope` is matched to `ArchitectureScope` only when you list it in `words`. Matching the last word of
  every type by itself produced over a hundred notes on this project.
- A module that defines the type itself is left alone, and `check --changed` skips the observation.

The table is a whole-run setting: with `scopes`, set it at the root.

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

`private_access` is an opt-in check that Python's leading underscore is respected across modules. Rings and
`importable_by` cannot see it: two modules of one ring may import each other's `_name` freely. It reports
`private-cross-module-access`:

```toml
[tool.arborescent.private_access]
rings = ["application", "adapters"]   # only modules in these rings are checked (leave out for every module)
exempt = ["tests", "*.test_*"]        # accessing modules not checked; replaces the default (test modules and conftest)
allow = ["app.engine._registry"]      # private targets anyone may use
```

An empty table turns it on. A module may not `from m import _x`, nor reach `m._x` through an import (`import m`, `from p import m`,
an alias or a relative import), unless it is `m` itself or below it. A private submodule (`pkg._impl`) belongs to its package, so
anything inside `pkg` may import it and nothing outside may. Only first-party modules are checked, lazy and `TYPE_CHECKING`
imports count, and there is one finding per module and private name, at its first line. By default `tests`, `*.tests`,
`*.tests.*`, `test_*`, `*.test_*`, `conftest` and `*.conftest` modules are exempt, since unit-testing a helper through its private
name is ordinary. Never reported: dunders and a bare `_`, attributes of classes and objects (`self._x`, `Class._x`, a
namedtuple's `_replace`), `getattr`, `*` imports, and an attribute chain whose first name the module also assigns or takes as a
parameter. Findings join the baseline ratchet, and `selftest` reaches into a private name when the table is present.

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

## How tach reads `source_roots`

Tach 0.35.x silently reads **no files** under some spellings of a `source_roots` entry and passes. Arborescent does not
let that happen:

- **macOS and Linux:** the `tach` and `tach-external` engines (and the tach probes of `--require-profile`) fail without
  running tach, naming the spelling to write instead. Refused: a `.` or doubled `/` with two or more folder names after it
  (`./a/b`, `a//b/c`; anywhere in a glob such as `./libs/*/src` or `libs/*/src/`), and `..` after a folder name
  (`a/c/../b`). `./src`, `src/` and `a//b` are read by tach and accepted. The roots come from `tach.toml`, else from
  `[tool.tach]` in `pyproject.toml`.
- **Windows:** forward-slash roots are not read either, so a `tach.toml` that has one runs on a temporary copy of the
  project with normalised backslash roots (#214). The project is not written to.

Not judged: absolute roots and roots outside the project (`../other/src`). Generated `tach.toml` files carry no `./` or
doubled separators.

Tach 0.35.3 does not walk the files under a root outside the folder that holds `tach.toml`. An import from a file inside
that folder into such a root is checked; the files out there are not, and no failure says so. For that reason `generate`
writes `tach.toml` at the project root whatever `output_dir` says, which is also where `arborescent check` runs tach. With
a nested `output_dir` it writes none while `tach` is in `disabled_tools` or `pyproject.toml` has a `[tool.tach]` table,
and `check` notes a generated `tach.toml` left in that folder by an earlier version, which checks nothing.

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
| `forbid` | positional preset default | Effect categories this ring may not use: `clock`, `env`, `identity`, `randomness`, `sleep`, `threads`, `process`, `database`, `network`, `filesystem`, `dynamic-import`, `patch`, `console`, `global`, and, since 0.8.0, `state`, `monotonic`, `host` and `fs-query`, and in the next release `registry` and `cache` (see *Effect categories*). That is every detected category. The positional and preset defaults never include the six newer ones: name them to opt in. |
| `forbid_at_import` | `[]` | Effect categories (the same names as `forbid`) that may not run while a module of this ring is imported; fires `effect-at-import`. Opt-in, independent of `forbid`: a ring may use an effect inside functions and still ban it at import. See "Effects at import time". |
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
| `lazy-import` | a function-level `import` with no `# deferred: <reason>` on any line of the statement or alone on the line above it; keyed by what it imports and its ordinal in the module, so moving it does not change the key. See "Function-level imports and import cycles" below |
| `shadowed-callee` | opt-in (`practices.shadowed-callee`): a local assigned from a call to the same-named module-level name; see "Shadowed callees" |
| `unresolved-patch-target` | opt-in (`practices.unresolved-patch-target`): a test patches a name its module does not have; see "Unresolved patch targets" |
| `entrypoint-eager-commands` | informational, opt-in (`[coupling.eager_commands]`): a module whose function-level imports mostly sit inside CLI command bodies; see "Eager command modules" below |
| `effect-clock` | a ring that forbids `clock` reads the ambient clock (`time.time`, `datetime.now` and related calls) |
| `effect-env` | a ring that forbids `env` reads environment variables through `os.environ` or `os.getenv` |
| `effect-identity` | a ring that forbids `identity` generates UUID identities |
| `effect-randomness` | a ring that forbids `randomness` uses `random` or `secrets` |
| `effect-sleep` | a ring that forbids `sleep` calls `time.sleep` or `asyncio.sleep` |
| `effect-threads` | a ring that forbids `threads` starts a `threading.Thread`/`Timer` or constructs a `ThreadPoolExecutor` (locks and events are `state`) |
| `effect-process` | a ring that forbids `process` starts a process or shell command through `subprocess`, `multiprocessing`, `ProcessPoolExecutor`, or direct `os` spawn/exec APIs |
| `effect-network` | a ring that forbids `network` calls `urllib.request` (not just constructing a `Request`), `http.client`, `http.server`, `socket`, `requests`, `httpx`, `aiohttp` or `urllib3` (a call probe, not an import ban) |
| `effect-database` | a ring that forbids `database` uses `sqlite3`, `psycopg`, `psycopg2`, `asyncpg`, `pymysql`, `mysql.connector`, `sqlalchemy`, `pymongo`, `redis` or `aiosqlite` |
| `effect-filesystem` | a ring that forbids `filesystem` calls `open`, `os` file operations (`os.remove`, `os.listdir`, `os.walk`, ...), `shutil`, `tempfile`, `glob`, or reads/writes through `Path(...)` or through a name bound to a path (`read_text`, `write_text`, `mkdir`, `glob`, ...). A call routed through a wrapper, or a path stored on `self`, is not seen |
| `effect-dynamic-import` | a ring that forbids any effect loads a module at run time (`importlib.import_module`, `__import__`, `spec_from_file_location`/`module_from_spec`, `exec_module`); `find_spec`, `importlib.resources` and `importlib.metadata` do not |
| `effect-patch` | a configured tests folder patches a project target whose ring forbids `patch` |
| `effect-console` | a ring that forbids `console` calls `print`, `input`, `sys.stdout.write` or `sys.stderr.write` |
| `effect-global` | a ring that forbids `global` has a function that declares `global x` and then assigns `x` (a read-only `global` is not a use) |
| `effect-state` | a ring that forbids `state` creates a `threading.Lock`/`RLock`/`Semaphore`/`Event`/`Condition`/`local` (0.8.0); a ring that forbids `registry` writes a module-level dict/list/set from a function, and one that forbids `cache` uses `functools.cache`/`lru_cache` or writes a cache-named module container (unreleased, #194) |
| `effect-monotonic` | a ring that forbids `monotonic` calls `time.monotonic`, `perf_counter` or `process_time` (0.8.0) |
| `effect-host` | a ring that forbids `host` reads `sys.argv`, `sys.stdin`, `locale`, `platform`, the process id, the CPU count or the host name (0.8.0) |
| `effect-fs-query` | a ring that forbids `fs-query` asks the filesystem a question: `os.path.exists`, `os.getcwd`, `Path.cwd`, `p.exists()`, `shutil.which`... (0.8.0) |
| `effect-unsanctioned-import` | with `[effects] sanctioned` set, a ring that forbids effects imports a module of an `effects.rings` ring that is not listed as a sanctioned seam and has effects of its own (0.8.0) |
| `effect-at-import` | a category in a ring's `forbid_at_import` runs while a module of the ring is imported, directly or through one call into a function of a scanned module. See "Effects at import time" |
| `effect-unlisted-category` | informational, whole-repository runs only: one line per `console`, `network`, `global`, `registry`, `cache`, `state`, `monotonic`, `host` or `fs-query` that no ring of the policy (or scope) lists in `forbid` or `forbid_at_import`, with how many modules of restricted rings (those that forbid anything) use it directly. Listing the category in any ring ends the line (unreleased, #194) |
| `effect-seam-bypassed` | with `effect_seam_bypassed` set, a function (or a method of a class) that was given an injected clock, environment or generator also uses the real one. See "Bypassed effect seams" |
| `effect-via-import` | a ring forbids an effect that a module it imports (directly or through that module's own imports, same scope, not an `if TYPE_CHECKING` import) uses directly; the message names the import chain |
| `internal-import` | a ring imports a ring it may not, or a module imports a target prohibited by `module_forbid_imports` (top-level, lazy and `TYPE_CHECKING` imports all count) |
| `forwarded-import` | a module imports a pass-through function (`def f(a): return other.f(a)`: one call to an imported name, arguments exactly the parameters, no default, decorator or other statement) whose target `module_forbid_imports` forbids it: the dependency one hop away. Chains of forwarders are followed. Only `from B import f` is seen, not `B.f` attribute use; wrappers that add logic are real boundaries and are not flagged. Counts against the importing module and takes its own baseline key |
| `restricted-import` | a module outside a restriction's `importable_by` globs imports the restricted module; also a key or importer glob that matches no module |
| `result-discard` | a call used as a whole statement drops the value of a result-bearing function (`result_discard`); `_ = f()` is a deliberate discard and is not reported |
| `private-cross-module-access` | with `private_access` set, a module imports or reaches another module's `_name`, or a `_submodule` of a package it is not in |
| `missing-module-docstring` | `docs.module_docstring` is on and a module has no, or a blank, docstring |
| `docstring-ring-mismatch` | `docs.module_names_ring` is on and a module's docstring does not name the ring that `classify` or `__arbor__` gives it (it says which other rings it names) |
| `literal-outside-owner` | a module or ring spells an `owned_literal` string that belongs to other modules or rings; also an owner glob that matches no module |
| `reexport-ratio` | a ring with `max_reexport_ratio` has a module whose public surface (a static `__all__`, else its public top-level bindings; at least 5 names) is mostly first-party imports. Moving an import into such a pass-through module clears the `internal-import` edge without changing the dependency. Third-party and stdlib re-exports do not count, and the detail carries no counts so the baseline key stays stable |
| `mixed-purity` | a module holds at least `min_pure_units` (default 2) substantial pure top-level functions or classes (a body of at least `min_body_lines`, default 4, lines) and at least one effectful one, or an effect that runs at import time. Enforced in every ring; see [Mixed purity](#mixed-purity-toolarborescentmixed_purity). The detail names only the ring, so the baseline key survives renaming functions; the hint lists the effect kinds, the effectful and pure definitions and the importer count. Also an `allow` entry that matches no module or no mixed module |
| `duplicate-logic` | opt-in (`[tool.arborescent.duplicate_logic]`): a function body repeats another one, or a reference-path one, after local names are erased (with `near_misses`, up to one literal or statement apart); with `branch_skeletons`, a function repeats the same two-way branch as others, and with `branch_conditions`, branches on the same decision with different calls. See "Duplicate logic" below |
| `untokenizable-source` | a file Python's `ast` accepts but `tokenize` rejects (a doubled carriage return `\r\r\n` is the usual cause, #181). Its `# noqa`, `# type: ignore` and `# deferred:` comments cannot be read, so the file gets no comment facts and this finding instead of aborting the run. A violation, so it can be baselined; the detail is fixed text (the tokenizer's line and message ride in the hint), and the fix is to remove the stray byte |
| `unguarded-module-state` | with `[unguarded_module_state]` set, a function writes a module-level container or `global` name outside a module-level lock, in a ring the table covers; also an `allow` entry that matches nothing. See [Unguarded module state](#unguarded-module-state-toolarborescentunguarded_module_state-232) |
| `unguarded-module-state-note` | informational: the same write in a ring that `unguarded_module_state.rings` does not list. Never fails a check and never enters the baseline |
| `mixed-purity-weak` | informational: a module that is mixed only because of `mixed_purity.weak_kinds` (for example it prints or logs). Never fails a check and never enters the baseline. Off until `weak_kinds` is set |
| `mixed-purity-excluded` | informational: a file under `exclude` (not a vendor folder, a noise folder or a child package) would be reported by `mixed-purity`. Never fails a check and never enters the baseline, but the exclusion no longer hides it |

| `construction-outside-site` | a module or ring calls a `construct_only` class or factory that only other modules or rings may build, or builds a `construction` class outside its `only_from`; also an owner glob or target module that does not exist |
| `call-outside-allowlist` | a module or ring calls a `call_allowlist` target that only other modules or rings may call; also an owner glob that matches no module |
| `hand-rolled-idiom` | a function makes both halves of a `call_allowlist` idiom (`calls` and `and_calls`) outside the sanctioned modules and rings; also an owner glob that matches no module |
| `missing-companion-call` | a function calls a `companion` action and has a finishing path on which neither it nor a function it calls records it (`must_also_call`), with no `# companion-exempt:` reason; also a scope glob that matches no module |
| `construction-forbidden-argument` | a call or `replace` supplies a field that a `construction` entry forbids or a `metadata={"arbor": "derived"}` marker marks; also an entry whose class or field does not exist |
| `construction-missing-argument` | a call constructs a `construction` class without passing a `require_kwargs` name by keyword; also an entry whose class or field does not exist |
| `port-binding` | a `port_binding` port has no production implementor, or more than its `max` (default 1); also a port that does not exist or is not a Protocol or abstract class. Whole-repository runs only |
| `port-shadow` | information only (never fails, never baselined): a `port_binding` lists `shadow` categories and code outside the one implementation uses one directly. Whole-repository runs only |
| `name-collision` | information only (never fails, never baselined): `name_collisions` is set and a field or local (for declared `words`, also a parameter or function name) reuses the name of a type defined in another module. Whole-repository runs only |
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

### Effects at import time

An effect that runs at import time (reading a file, the environment or the clock into a module-level value) makes importing
the module order-dependent and hard to test, and bakes in whatever was configured when the process started. The `effect-*`
rules count a use at module level and one inside a function the same way; `effect-at-import` tells them apart:

```toml
[tool.arborescent.ring.application]
forbid = []                                                    # functions may use effects
forbid_at_import = ["filesystem", "env", "network", "process", "clock", "randomness"]   # importing may not
```

**What counts as import time:** module-level statements (including under `if`, `try` and `with`), class bodies, parameter
defaults, decorator expressions and comprehensions, and the body of a lambda called on the spot. **What does not:** function
and lambda bodies, annotations, `if TYPE_CHECKING:` and `if __name__ == "__main__":` blocks (their `else` branches do).

**What counts as running:** a call, a decorator, or reading `os.environ`, `sys.argv` or `sys.stdin`. Naming a function
without calling it (`NOW = time.time`, `now=time.time`, `field(default_factory=time.time)`) runs nothing.

**One hop:** a call made at import into a module-level function or a method of a scanned module (imported, re-exported by a
package, or defined in the same module; a class call runs `__new__`, `__init__` and `__post_init__`; a decorator is a call)
is reported when that function itself runs a forbidden effect. The fix is to move the call into a function.

**Module state at import.** `global`, `registry` and `cache` writes happen inside functions, so they are reported only
through such a call (`register("a")`, `@register`, `Plugin()`), as is calling a `functools.cache`d loader (defining one
runs nothing). Filling a container at module level is not reported: it is the same on every import.

**Not seen:** an `__init_subclass__` hook run by a `class` statement; a second hop; a method on a value (`loader.read()` where `loader = Loader()`); a function passed as an argument
and called by the receiver (`map(os.getenv, names)`); the function a decorator factory returns; an inherited constructor.
Calling an `async` or generator function counts as running its body. `arborescent admit` and the `classify-facts` signals
judge only direct and same-module effects. `check --changed` follows calls only into the files it checks.

The finding is reported per module, category and target (or callee), so several lines share one baseline key. Record
existing ones with `arborescent baseline --adopt effect-at-import`.

### Bypassed effect seams

A function that takes `now` and also calls `time.time()` defeats its own injection: a test that passes a fake still gets
the real clock. The ring may well allow the effect, so no `effect-*` rule sees it. The check is off until its table exists
(in a scoped policy, set it per scope):

```toml
[tool.arborescent.effect_seam_bypassed]
rings = []   # default: every ring
# Both tables replace the defaults when given.
parameters = { now = "clock", clock = "clock", clock_now = "clock", environ = "env", rng = "randomness", uuid_factory = "identity" }
annotations = { "random.Random" = "randomness", "Clock" = "clock" }   # a key with no dot matches any class of that name
```

A parameter is a seam when its default is an effect function (`new_id=uuid.uuid4`, `clock=lambda: datetime.now()`), when
its annotation is listed, or when its name is, in that order; the first that applies decides the category, so
`clock=time.monotonic` stands for `monotonic` and not for the wall clock. The categories are the injectable ones (`clock`,
`env`, `identity`, `randomness`, `sleep`, `threads`, `process`, `monotonic`, `host`). The parameters of a class are those
of its `__init__` (its fields, for a dataclass without one) and cover its instance methods, not its static or class methods.

Reported: a call of the real effect, or a read of `os.environ`, `sys.argv` or `sys.stdin`, of the seam's category, in the
body of the function or in closures and lambdas inside it. One finding per function and category, so each function is
baselined on its own. A seeded `random.Random(seed)` is not a use.

**Not reported**, because a caller who passed a fake still gets only the fake: the fallback for a missing seam, written as
`now or datetime.now()`, `datetime.now() if now is None else now`, `if now is None: ...`, or the lines after
`if now is not None: return now()`. **Not seen:** other spellings of that fallback are reported (baseline them or use one
above); the real function passed on without being called (`g(clock=time.time)`); a seam kept under a different attribute
name than `self.now`/`self._now` inside such a fallback; inherited constructors. Developer tooling is not checked.

Record existing findings with `arborescent baseline --adopt effect-seam-bypassed`.

### Function-level imports and import cycles

A function-level `import` is the usual workaround for an import cycle or a heavy dependency: it stops the
`ImportError` and leaves the coupling in the graph. Each one is its own finding, `lazy-import`, so a baseline of them is
a visible, shrink-only list per module. `check` prints `function-level imports by module:` with the total and how many
carry a reason. An import under `if TYPE_CHECKING` never runs and is not counted. A decision is recorded on any line of
the import statement (so a formatter that wraps it keeps it justified) or alone on the line above (comment lines that
continue it are fine, a blank line ends it):

```python
def load() -> None:
    import heavy  # deferred: 300 ms of startup the CLI help path should not pay
    # deferred: the registry imports this module at import time
    import registry
    from composition import (
        Container,  # deferred: only the callback builds it
    )
```

The reason must not be empty. A justified import is counted but is not a finding.

To start with the rings where a lazy import is a smell and leave the rest (a CLI package or composition root defers imports to keep
startup fast), list them:

```toml
[tool.arborescent.lazy_import]
rings = ["domain", "services", "workflows"]   # default: every ring, so nothing changes until this is set
```

A module in any other ring (or with no ring, such as a dev tool) has no `lazy-import` findings and is left out of the
`function-level imports by module:` list. A ring name that is not defined is an error, not a silent no-op, and so is an empty list;
with `[scopes]` a name may belong to any one scope. `check` prints one line per module, `module: [lazy-import] total/deferred`
with the unjustified imports named, not one line per import (baseline keys are unchanged). While no `lazy-import` finding is
baselined yet it adds `lazy-import: N new, 0 baselined; run 'arborescent baseline --adopt lazy-import' to start a ratchet`.
Adoption works once and fails if other new findings exist; fix or baseline them first. It still counts as an edge in the import
graph, so `circular-import` and `arborescent cycles` see it. (Excluding these imports from a closure size is not done
here; the closure used by `internal-import` move suggestions still follows them.)

#### Eager command modules

In a CLI, imports inside command bodies usually exist to keep `--help` and the other commands fast. `# deferred:` records
that coupling and fixes nothing: the fix is structural. Opt in with a table (every key is optional):

```toml
[tool.arborescent.coupling.eager_commands]
decorators = ["command", "callback", "group"]  # a plain name matches the last segment, "app.command" matches exactly
min_imports = 3     # function-level imports that must sit in command bodies
min_share = 0.5     # and the share of the module's function-level imports they must be
min_commands = 2    # across at least this many commands
```

A command is a function with one of those decorators, one registered by calling it (`app.command("x")(run)`), or one
passed to argparse as `set_defaults(func=run)`. The registration may sit in another module that imports the function
(`set_defaults(func=handlers.run)`); a re-export through a third module is not followed, and under `check --changed` a
registration in an unchanged file is not seen. A module over every threshold gets one
`entrypoint-eager-commands` note with the number of imports and commands, and how many project modules importing it would
load if those imports were hoisted (left out under `check --changed`). Imports with a `# deferred:` reason count toward
the share. The note never fails a check and is never baselined. The `lazy-import` findings and their baseline keys do not
change, so enabling this needs no new baseline and a new import still fails; the failing ones in command bodies print as
one line per module that names each import.

The structure it asks for is one module per command, imports at the top, mounted lazily by the composition root. Declare
the mounts in [`[coupling.lazy_mounts]`](#startup-budgets-and-lazy-mounts) so the one remaining deferral is a reviewed,
countable list.

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
| `crlf` | list of file globs, e.g. `["*.sh"]` | a matching file has CRLF line endings; noise folders and nested checkouts (clones, linked worktrees, submodules) are not entered |
| `undeclared-imports` | `true`, or a manifest path (default `pyproject.toml`) | a third-party import is absent from the manifest's dependencies |
| `stale-config-path` | list of config file globs, e.g. `["Makefile", ".github/workflows/*.yml"]`; `stale-config-path-allow` lists path globs to ignore | a glob or `.py` path in the file matches nothing (a suite silently runs zero tests after a move), or a pyproject's hatch `packages` entry does not exist |
| `source-root-drift` | `true`, or a list of files (default `["tach.toml", "pyproject.toml"]`) | a `sources` entry is below no `source_roots` entry, or the root list copied into `tach.toml` `source_roots`, hatch `sources`/`packages` or pytest `pythonpath` has drifted from the policy |
| `test-reference` | tests folder, e.g. `"tests"`; `test-reference-exempt` lists `module.function` names or module globs to skip | information only: a public module-level function in an I/O-free ring (one that forbids `filesystem`, `network`, `database` and `process`; the innermost ring by default) that no function under the tests folder imports and calls. Resolved through import aliases and package re-exports; not through an object of unknown type, a fixture or a parametrized argument, and a function only a tested caller reaches is reported. Private, abstract and class members are skipped. A name that is only imported or mentioned does not count (#104) |
| `adapter-equivalence` | list of groups of full module names, e.g. `[["pkg.fact_queries", "pkg.modes_store"]]`; `adapter-equivalence-tests` is the tests folder (default `"tests"`); `adapter-equivalence-report = true` makes the missing-test finding information only | fails when no single test function calls a function of every module in a group, so two adapters of one port cannot both ship without a differential test (#113). Group errors always fail, even with `adapter-equivalence-report`: an unknown module (not a scanned module; a unique suffix match is suggested), a group with fewer than two distinct modules, a module in two groups, a value that is not a list of lists of strings. See "Adapter equivalence" below |
| `unreachable-patch` | tests folder, e.g. `"tests"`; `unreachable-patch-exempt` lists `module.name` bindings or module globs to skip | a test patches `X.name` while a module it reaches calls `name` through its own `from X import name`, which the patch never reaches. See "Unreachable patches" below (#226) |
| `unresolved-patch-target` | tests folder, e.g. `"tests"`; `unresolved-patch-target-exempt` lists `module.name` targets or module globs to skip | a test patches `module.name` and the first-party `module` has no `name`. See "Unresolved patch targets" below (#215) |
| `shadowed-callee` | `true` | a function assigns a local from a call to the module-level name it shadows (`rows = rows()`), which raises `UnboundLocalError`. See "Shadowed callees" below (#215) |
| `adapter-equivalence-report` | `true` | not a check of its own: makes `adapter-equivalence`'s missing-test finding information only (rule id `adapter-equivalence-report`, never fails, never baselined); group errors still fail |

### Unreachable patches

`from X import name` gives the importing module its own reference to the function. `monkeypatch.setattr(X, "name", fake)`
replaces `X`'s attribute only, so that module keeps calling the real one, and the test passes or fails for the wrong reason.
`unreachable-patch` joins the two halves and reports the production module and the line of the call. Importing the style alone
is never reported; fix a finding with `import X` and `X.name()`, or patch the caller's name (`monkeypatch.setattr(caller, "name", fake)`).

A finding needs all of the following:

- a file under the tests folder patches `X.name` with `monkeypatch.setattr` (object and name, or a dotted string; also through
  `monkeypatch.context()`), `mock.patch("X.name")`, `patch.object(X, "name")` or the same through pytest-mock's `mocker`;
- a first-party module binds `name` with a `from X import` that runs at import time (aliases and relative forms count) and calls
  it bare inside a function or lambda;
- that test file reaches the module: it imports it directly or transitively (function-level imports count, `TYPE_CHECKING` ones
  do not), itself, through a `conftest.py` above it or through a helper module in the tests folder;
- the same test file does not also patch the module's own binding.

Not seen, so never reported: a call at import time or in a default argument, passing the function on without calling it, a
function-level `from X import name` (the patch does reach it), a name the module rebinds or takes as a parameter anywhere, a
re-export chain (`from package import name` patched at `package.impl.name`), third-party and stdlib targets,
`patch.multiple`, `delattr`, and a module reached only through a fixture from elsewhere, `importlib` or a subprocess. The test
files are parsed once and cached like source files, whether or not they are in `sources`; the check is skipped under
`--changed`. A configured folder with no Python files is itself reported. Findings join the baseline ratchet, and `selftest`
adds a test that patches an unpatched binding.

### Unresolved patch targets

After a name moves to another module, the tests that still patch the old place need retargeting. `unresolved-patch-target`
reports one finding per test file and target, so the output is the whole list. It reads the same patch forms and test files
as `unreachable-patch` (the two may name different folders) and works without it.

A target is reported when its longest known module is first-party and has no attribute or submodule of the next name:
`app.repo.gone`, `app.gone.load` (the module `app.gone` is gone) or `app.repo.Gone.get`. Not reported:

- anything after that first name (`app.repo.Store.missing` only checks `Store`);
- a module whose namespace its source does not settle: a `*` import, a module `__getattr__`, `globals`, a bare `vars()`,
  `exec` without a namespace argument or `sys.modules[__name__]`;
- builtins (`mock.patch` creates them), dunder names, and a patch allowed to create the name (`create=True`,
  `raising=False`);
- third-party and stdlib targets.

A name that only another module assigns onto this one is not seen, so it is reported: exempt it. A name that is only
annotated (`x: int`) or only imported under `TYPE_CHECKING` does not exist at run time and is reported too.
The check is skipped under `--changed`, a folder with no Python files is itself reported, and findings join the baseline.

### Shadowed callees

Renaming `_rows` to `rows` for cross-module use turns `rows = _rows()` into `rows = rows()`. The assignment makes `rows`
local to the whole function, so the call raises `UnboundLocalError`. `shadowed-callee` reports it once per function and name,
for a name the module imports or defines at top level. Module and class bodies are left out (the call finds the global
there), as are parameters, `global`/`nonlocal` names, and a name the function binds before the assignment. A call through a
lambda or generator is not counted, since it may never run. It also runs under `--changed` and findings join the baseline.

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

### Root causes (#201)

Some rules emit one finding per symptom: `lazy-import` one per importing module, `effect-unsanctioned-import` one per
importer of an unlisted effect module, `effect-via-import` one per module downstream of an effectful seam. Such a finding carries a `root_cause` (`<rule>|<what they share>`, for example
`lazy-import|psutil`) beside its identity key. The key still decides baseline matching; the root cause only groups.

- `check` ends with `N violation(s) / M root cause(s)` and `by rule: lazy-import 83 / 2 root causes (0 new)` when findings
  share causes; with no fan-out the lines are unchanged. `check --by-root-cause` lists each cause once, widest fan-out
  first, with a sample of the modules it reaches and, where the rule has one, a fix that clears the whole cause; findings that
  name no cause keep their own line.
- `effect-via-import`'s cause is the effect kind plus the last edge of the import chain: `effect-via-import|env|client.bundle->state.app_root`
  (`importer->module that uses the effect`), or `effect-via-import|env|state.app_root` for a direct importer. 623 modules that reach
  `state.app_root` through `client.bundle` are one cause with one fix: inject a port at `client.bundle`, or list `state.app_root`
  under `effects.sanctioned`.
- `baseline --by-root-cause` (also with `--adopt FAMILY`) writes one entry per cause under `root_causes` in the baseline file,
  `{cause: [modules...]}`, instead of one key per finding. The entry is shrink-only: a finding under a recorded cause is
  tolerated while its module is listed, a module the cause gained is new (growth, needing `--allow-growth` like any other),
  and members that stop occurring make the entry stale until `baseline --prune`. The growth log names added members as
  `<cause>|<module>`, and `--growth-against` checks them. A finding already recorded under its own key stays there; delete
  those entries first to regroup them. Baselines without `root_causes` work unchanged.

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

### Startup budgets and lazy mounts

```toml
[tool.arborescent.coupling]
entrypoints = ["app.cli"]
[tool.arborescent.coupling.closure_max]   # hard cap on an entrypoint's closure; each key must be an entrypoint
"app.cli" = 40
[tool.arborescent.coupling.lazy_mounts]   # command name -> "module:attr", loaded only when the command runs
backfill = "app.commands.backfill:app"
"backfill conditions" = "app.commands.conditions:app"
```

Both are static: they count project modules reached by module-level imports, not seconds. Only `startup --measure`
(below) runs the project.

- `arborescent coupling check` fails when a closure is over its `closure_max`, naming the imports to blame. The baseline
  does not excuse it, unlike growth, which `check` already reports against the `closure` counts.
- It also fails when an entrypoint loads a mounted module at import time, and prints the import chain. This check also
  follows parent packages (`import a.b.c` runs `a` and `a.b`). A `TYPE_CHECKING` or function-level import passes.
- `arborescent coupling startup [--top N]` is the report. Per entrypoint it prints the closure size and budget, the `N`
  heaviest imports (`a -> b pulls 12 module(s)` is what removing that import saves; several importers means every one of
  those edges must go), and per lazy mount the modules running it adds, or `EAGER`.

A mount whose module does not exist is a policy error (exit 2); `attr` is checked for syntax only. How the mount loads the
module (a string table with `importlib`) is the project's code. Add a mounted command module to `entrypoints` to ratchet
or cap its own closure.

#### Measured startup and heavy third-party imports

```
arborescent coupling startup --measure [--threshold-ms 50] [--python PATH] [--timeout 120]
```

**This runs your code**: each entrypoint is imported (not executed as `__main__`) by `python -X importtime` in a
subprocess, with the project root as the working directory and the source roots first on `sys.path`. Module-level code
runs, so an entrypoint without a `__main__` guard starts its program. Nothing else in arborescent does this; `check` and
`coupling check` never run it, and no budget or baseline holds the times. `--python` names the interpreter that has the
project's dependencies (default: the one running arborescent).

Per entrypoint it adds the measured import time, the number of project modules actually loaded, the time of each blamed
import, and the third-party packages that the startup path imports at module level and that took at least
`--threshold-ms` (cumulative, so a package includes what it was first to import). Each is listed with the modules that
import it and the commands that use it:

```
app.cli: measured 412.0 ms, 38 project modules loaded
  heavy third-party imports (>= 50 ms):
    pydantic 180.0 ms: imported by app.models; only used by app.cli:export: import it where that command runs
```

"Used by" is a static approximation. A command is a lazy mount (it uses what the modules it loads import) or a command
function on the startup path, found as for `entrypoint-eager-commands` (it uses what its body imports or reads: the package,
or a project module that loads it at module level). A use through a helper in the command's own module, a method of an
object or a dynamic import is not seen, so "used by no command found" means unknown, not unused. A failed import, a
timeout or a missing interpreter is printed and exits 1.

One run per entrypoint: the first after a change also pays for compiling `.pyc` files, so run it twice before comparing.
Not measured: `argv` paths such as `backfill --help`, cold against warm starts, and process start-up before the import.

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
| `network` | `urllib.request` (bar `Request`, which only builds an object), `http.client`, `http.server`, `socket` (bar `gethostname`/`getfqdn`), and `requests`/`httpx`/`aiohttp`/`urllib3` calls |
| `database` | `sqlite3`, `psycopg`, `sqlalchemy`, ... |
| `dynamic-import` | **only** run-time module loading: `importlib.import_module`, `importlib.__import__`, `importlib.reload`, `importlib.util.spec_from_file_location`/`spec_from_loader`/`module_from_spec`, `*.exec_module`, `__import__`. `importlib.resources`, `importlib.metadata` and `importlib.util.find_spec` import nothing and do not count (0.8.0) |
| `console` | `print`, `input`, `sys.stdout.write`, `sys.stderr.write` |
| `global` | a function that declares `global x` and assigns it |
| `registry` | a function writes a module-level `dict`/`list`/`set` (item assignment, `del`, or `append`/`add`/`update`/`setdefault`/`pop`/`clear`... on it) that it does not bind locally; reported as `effect-state` (unreleased, #194). See *Module-level state* below |
| `cache` | a `functools.cache`/`lru_cache` decorator, or a write to a module-level container whose name contains `cache` or `memo`; reported as `effect-state` (unreleased, #194). `cached_property` is per instance and does not count |
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

### Module-level state: `registry` and `cache` (#194)

`global x` rebinding was already the `global` category. Two further ways of keeping shared state in a module are detected (unreleased), each in
its own category so a ring opts in to exactly what it means to ban. Both report as `effect-state` (the same rule id as locks, `state`), are in
no default `forbid`, are not taint seeds and are not part of `must_be_pure`; `coupling` accessor counts and `discover --suggest` evidence ignore
them. A policy that does not list them in a `forbid` keeps every finding and baseline entry it had.

- **Registry.** A module-level name bound (at the top of the module) to a `dict`/`list`/`set` literal, comprehension or constructor
  (`dict()`, `defaultdict(...)`, `OrderedDict`, `Counter`, `deque`) is a registry when a function or method writes it: `REG[k] = v`, `REG[k] += 1`,
  `del REG[k]`, or a mutating call (`append`, `extend`, `insert`, `add`, `update`, `pop`, `popitem`, `remove`, `discard`, `clear`, `setdefault`,
  `appendleft`, `popleft`). A name the function, or a function around it, binds itself (a parameter or an assignment) is a local, not
  the module's container. The same writes count on:
  - a container of **another scanned module**, however it is imported (`from m import REG`, `m.REG`, `m.Plugins.items`, a package
    re-export). The finding is on the writing module, under the writing module's ring.
  - a **class-attribute container** of a class defined at module level: through `cls`, the class name, `type(self)` or
    `self.__class__`, and through `self` when no method of the class (or of its bases in the same module) assigns `self.<name>`,
    which would make it per instance. An annotated attribute of a class that has a base or a decorator may be a per-instance
    field (pydantic, attrs), so through `self` it needs `ClassVar` to count.
  - an **alias** bound once to any of these (`r = REG`, `items = self.items`, a module-level `ALIAS = REG`).

  Not seen: reads, a fill at import time (deterministic), an attribute inherited from a class of another module, an object's
  attributes (`SINGLETON.items[k] = v`), an alias of an alias, a name bound more than once, a container passed as an argument or
  returned by a call, a container built by anything but the forms above, and `REG = REG | {...}` style rebinding (that is
  `global` when declared). With `disabled_tools = ["arbor-annotations"]`, `check --changed` reads only the changed files and
  does not see a write to a container of an unchanged one.
- **Cache.** `@functools.cache` and `@functools.lru_cache` (aliases and `from functools import ...` resolved, on functions and methods) make a
  loader whose result depends on call order, so they are a `cache` use at the decorator. A write to a module-level container whose name contains
  `cache` or `memo` is also `cache`, not `registry`. A hand-rolled `global _x; if _x is None: _x = load()` slot is the `global` category.

`mixed-purity` is unchanged: it keeps its own `state` and `memo` kinds for the same writes and decorators, and a `mixed_purity.kinds` list cannot
name `registry` or `cache`. Every detected category, for a `forbid` list: `clock`, `monotonic`, `env`, `host`, `identity`, `randomness`, `sleep`,
`threads`, `state`, `process`, `filesystem`, `fs-query`, `network`, `database`, `dynamic-import`, `console`, `global`, `registry`, `cache`, `patch`.

### Unguarded module state: `[tool.arborescent.unguarded_module_state]` (#232)

`registry`, `cache` and `global` say whether a ring may hold shared module state at all. This opt-in rule asks the narrower question: where such
state exists, is every write guarded by a lock? Without the table it is off; an empty table turns it on for every ring.

```toml
[tool.arborescent.unguarded_module_state]
rings = ["adapters"]                            # fail here; a note in other rings. Omit to fail in every ring
locks = ["app.sync.registry_lock"]              # extra `with` contexts that guard (dotted names or globs)
guard_decorators = ["app.sync.synchronized"]    # decorators that guard the whole function

[[tool.arborescent.unguarded_module_state.allow]]
module = "app.cli"                              # module, package prefix or glob
name = "_handlers"                              # the module-level name, or a glob
reason = "filled before any thread starts"      # required
```

**What is a write.** Inside a function or method: `REG[k] = v`, `REG[k] += 1`, `del REG[k]` and the mutating calls listed under
[Module-level state](#module-level-state-registry-and-cache-194) on a module-level `dict`/`list`/`set` (cache-named ones included), and any
assignment, augmented assignment or `del` of a name the function declares `global`. Import-time fills, reads and locals of the same name are not.

**What guards it.** The write is lexically inside a `with` block, within the same function, naming a module-level `threading.Lock`, `RLock` or
`Condition` (or `multiprocessing.Lock`/`RLock`), defined in this module or imported from another scanned one; or naming an entry of `locks` (for
`with factory(key):`, the factory); or the function, or one around it, has a decorator listed in `guard_decorators`. A `with` around a nested
`def` does not guard the nested function. There is one finding per module, name and writing function.

`arborescent baseline --adopt unguarded-module-state` records the current findings once. A stale `allow` entry is reported under the same rule.
`--changed` reads only the changed files, so it trusts a `with` on a name from a module it did not read.

**Blind spots (a clean run is not proof of thread safety).** This rule looks only at the module's own containers and `global` names
(narrower than `registry`/`cache`). Not reported although they may race: containers imported from another module, writes
through an alias, `self.attr` or a class attribute, nested containers (`REG[k].append(v)`), object attributes (`STATE.x = 1`), functions that
mutate an argument (`heapq.heappush(REG, v)`, `|=` on an alias), containers bound inside `if`/`try`, and containers built by anything other than
the listed literals and constructors. Reads are never checked, so check-then-act outside the lock passes, and so do two writers that hold
*different* locks. Reported although they may be safe (use `locks`, `guard_decorators` or `allow`): `lock.acquire()`/`release()` pairs, a helper
documented as "caller holds the lock", an instance or class lock (`with self._lock:`), semaphores, a lock re-exported through a third module or
created lazily, a registry filled only by import-time decorators, `deque.append` and other single operations that CPython happens to make atomic,
and `asyncio` code whose state only one event loop touches.

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
rings = ["domain", "application"]                                      # default []: when set, the only rings the rule looks at
skip_unrestricted_rings = true                                         # default false: skip every ring whose effective `forbid` is empty
weak_kinds = ["console", "logging", "memo"]                            # default []: effects that alone do not make a module worth splitting

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
| `memo` | a write to a module-level dict, list or set whose name contains `cache` or `memo`. A `functools.lru_cache` / `cache` / `cached_property` decorator is no longer a memo: a memoised pure function is pure |
| `state` | a write (item assignment, `del`, `append`/`update`/`setdefault`/... ) to any other module-level dict, list or set from inside a function, unless that function binds the name locally; and, since 0.8.0, a `threading.Lock`/`RLock`/`Semaphore`/`Event`/`Condition`/`local`. Reads are not effects |
| `ambient` | a call to something matching `ambient`, resolved through absolute and relative imports (`state_root()`, `os.getcwd`), or a method call on a parameter annotated with a matching class (`reader: Reader` then `reader.read()` matches `myapp.ports.Reader.read`; also `Reader | None`, `Optional[Reader]` and `"Reader"`; a parameter the function reassigns is skipped). Off until `ambient` lists a pattern; `ambient = ["myapp.ports.*"]` makes a loader that calls a port effectful |

**Why is this function listed as pure?** `arborescent purity --explain MODULE` prints, for each top-level definition of one
module (mixed or not), the effect kind, the line and the call behind an "effectful" label, or `no effect found`. Attach its
output to a report of a wrong label.

**Rings.** `rings` limits the rule to the listed rings (a name that is not a declared ring is an error); `exclude_rings` removes rings;
`skip_unrestricted_rings = true` skips every ring whose effective `forbid` is empty, since effects are allowed there. The three
combine and all apply to `arborescent purity` and to `selftest`.

**Weak kinds.** `weak_kinds` names effects that alone should not ask for a split. A module whose effects are all weak is
reported as an informational `mixed-purity-weak` note instead of a `mixed-purity` finding (and as `weak` in
`arborescent purity --json`); one strong effect keeps it a finding. Existing baselines are unaffected until the key is set.

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

## Duplicate logic: `[tool.arborescent.duplicate_logic]`

`duplicate-logic` (#228) finds the same narrow rule implemented a second time. It is **off** unless the table is present.

```toml
[tool.arborescent.duplicate_logic]
min_statements = 5                      # smallest body compared (statements at any depth, docstring excluded)
reference_paths = ["vendor", "libs/shared/helpers.py"]  # compared against, never flagged
branch_skeletons = false                # also match a repeated two-way branch
min_branch_repeats = 3                  # functions that make a branch a repeat (at least 2)
near_misses = false                     # also match bodies one literal or one statement apart
min_near_statements = 8                 # smallest body compared that way
branch_conditions = false               # also match one decision repeated with different calls
```

| key | default | meaning |
|---|---|---|
| `enabled` | `true` when the table is present | `false` keeps the table and turns the rule off |
| `min_statements` | `5` | positive integer |
| `reference_paths` | `[]` | repository-relative folders or `.py` files (no `..`, `.` or absolute path) |
| `branch_skeletons` | `false` | see "Repeated branches" |
| `min_branch_repeats` | `3` | integer of at least 2; also the threshold of `branch_conditions` |
| `near_misses` | `false` | see "Near copies" |
| `min_near_statements` | `8` | integer of at least 2; both bodies must be this long |
| `branch_conditions` | `false` | see "One decision, different calls" |

The table is whole-run configuration: one per repository, not per scope, and copies are found across scopes.

**What counts as the same body.** Parameter and local names, the function's own name where it calls itself, docstrings,
comments and annotations are erased. Literals, operators, attribute and keyword-argument names and every name the function
does not bind (so the called names) are kept; `async` differs from plain `def`. Parameter order, defaults and decorators are
not compared. The match is exact after that, unless `near_misses` is on.

Module-level functions and class methods are compared; a nested `def` is part of its parent. Stubs (`...`, `pass`, a
docstring, `raise NotImplementedError`, `return NotImplemented`), such as `Protocol` and abstract members, are never compared.

**Reference paths.** Their `.py` files are read whether or not the scan covers them (an auto-excluded `vendor/` folder, an
`exclude`d one). Their functions are targets only. A first-party function with the same body is reported with the reference
function to use instead, even when it is the only copy. A scanned module below a reference path or a vendor-named folder
(`vendor_roots`, `scan_vendors`) is treated the same way. A reference path that holds no readable Python file, and a
reference file that cannot be parsed, are findings: a check that compares against nothing does not pass silently. A vendor
folder that is not listed in `reference_paths` and not scanned is not compared.

**Near copies** (`near_misses = true`). Two bodies of at least `min_near_statements` also match when they differ in exactly
one of these ways, after the same erasure:

- one literal value (of the same type: `"-"` for `"_"`, `0` for `1`, but not `1` for `1.0`);
- one extra statement anywhere in the body, including inside a loop or `try`. A one-line guard (`if x: return None`, no
  `else`) counts as one; a longer block does not.

Both together, two literals, a changed called name, operator or attribute, or the same statements wired to different locals
do not match. A near copy of a `reference_paths` function is reported against it. A near finding has the same key as an exact
one, so a baselined copy that drifts by one literal stays one known entry; the hint says which kind it is.

**Repeated branches** (`branch_skeletons = true`). Matches an `if`/`else`, a conditional expression, or a guard (`if c: return a`
followed by the block's final `return b`) whose condition is the same (local names erased) and whose two sides call the same
set of names, in at least `min_branch_repeats` functions. Both sides must call something. Branches of more than 200 syntax
nodes are skipped. Sides that call differently named functions do not match here (see below). Copies already reported as
whole duplicates count as one function.

**One decision, different calls** (`branch_conditions = true`). Finds the dispatch copied around many functions, where each
copy calls its own pair of functions:

```python
def load_users():
    if backend.hosted_enabled():
        return backend.client().get_users()
    return filter_users(load_local())
```

A function is reported when at least `min_branch_repeats` functions branch on the same condition and:

- the condition calls something other than a builtin and reads no parameter or local of the function (a leading `not` is
  ignored). A method may call on `self`/`cls`; those are compared only within the same class of one module;
- each side calls something the other does not, and neither side ends in `raise` (a precondition is not a choice);
- the branch is an `if`/`else`, a conditional expression, or a guard that returns a value, followed by at most 20 statements
  of the same block that end by returning a value.

So `isinstance(value, str)`, `if flag:`, `sys.platform == "win32"` and `if value.ready():` are never reported. The key holds
the condition text and a short digest of it. With `branch_skeletons` also on, a branch reported here is not reported twice.

**Findings and the ratchet.** One finding per function, `duplicate-logic|module|function `Class.name` duplicates logic
defined elsewhere`; the partners are in the rendered hint, not in the key. So the baseline only shrinks: removing one of three
copies leaves one stale entry, removing one of two leaves two, editing all copies alike changes nothing, and only a function
that was not a duplicate before is new. A function name repeated within a module is numbered `#2`. A repeated branch adds the
condition and called names (and a short digest) to the key. Moving or renaming a duplicated function changes its key.
Turning `near_misses` or `branch_conditions` on adds findings; `branch_conditions` also replaces the `branch_skeletons`
entries of the branches it covers, so re-record the baseline after changing either.

Adopt on an existing repository with `arborescent baseline --adopt duplicate-logic`. A named ratchet
(`rule = "duplicate-logic"`) counts findings per module.

**Limits.** This is a clone detector, not a proof of equivalence: two functions that call same-named functions imported from
different modules match, and two that compute the same thing differently do not. Parameter order is not compared, so two
bodies that use their arguments in swapped roles can match. Near matching reports deliberate families too (an `http` and an
`https` variant of one method). `branch_conditions` compares condition text, so it does not see that two differently written
conditions ask the same question. `check --changed` does not evaluate it, since
the original may be in an unchanged file. Each file is parsed once more (cached by content in `.arborescent` beside the scan
cache) and functions are grouped by digest, never compared pair by pair; near copies are found through hashes of the body with
one literal or one statement left out.

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
intentional baseline growth, its reason and its approver. `--by-root-cause` records fan-out findings once per root cause (see [Root causes](#root-causes-201)).

### `arborescent coupling`

`report`, `check`, `baseline`, `startup` and `cost`, described under [Coupling metrics](#coupling-metrics-toolarborescentcoupling).
`check` exits 1 on a regression, an exceeded `closure_max` or an eagerly loaded lazy mount; a policy or baseline error exits 2.

### `arborescent purity`

Lists every module `mixed-purity` finds (allowed ones marked), most imported first, with its effect kinds, effectful and pure
definitions and importer count. `--json` prints the same as JSON. Report only: `check` is what fails.

`--path PATH` and `--module MODULE` list only a subset, for reviewing a diff or checking changed files. Both take several values
and can be repeated; everything named is one union. Only the listed rows change: importer counts, classification and ordering still
come from the whole scan, and `--top` applies to what is left.

- `--path` takes a file or a folder, relative to the working directory or absolute.
- `--module` takes a dotted name (its submodules included) or a glob such as `app.*_service`.
- A path that does not exist or lies outside the policy root, a blank value, or a `--module` that matches no scanned module
  exits 2, so a typo never reads as a clean result. An existing path with no scanned module in it (a README, an excluded test) is
  noted on stderr and skipped. A deleted file does not exist, so filter it out: `git diff --name-only --diff-filter=d main`.
- Neither combines with `--explain`, which already names one module.

```console
arborescent purity --path $(git diff --name-only --relative --diff-filter=d main -- '*.py')
```

When that file list is empty, `--path` has no value and the command exits 2, so guard the call.

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
