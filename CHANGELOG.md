# Changelog

0.x: a minor release may break the public surface, a patch release may not.

## Unreleased

### Added
- `arborescent check --rule FAMILY` (repeatable) runs and exits on one rule family, and `check` prints a
  `by rule: … N (M new)` line splitting findings by rule with how many the baseline does not cover (#137). Selecting by
  ring and a `type_checking` option for `internal-import` are not part of this change.
- Baseline growth entries now carry `approved-by`: `baseline --allow-growth` requires `--approved-by WHO` (adoption
  records itself), and `check --growth-against` fails an entry without a non-blank `reason` and `approved-by`.
- `ratchet_files`: numeric ratchet JSON files (`{"values": {...}, "growth": [...]}`) checked by `--growth-against`; an
  added or raised value needs an attributed growth entry appended since the ref (#139).

### Changed
- Growth entries recorded before this release have no `approved-by`, so they fail `--growth-against` only while they
  are newer than the ref.
- `forwarded-import`: a module forbidden by `module_forbid_imports` from importing C can no longer satisfy the rule by
  importing a pass-through wrapper of C; the wrapper import is reported against the importer (#138).
- `weakly-typed-port`: a ring with `require_typed_ports = true` flags `typing.Protocol` methods whose return type is
  missing, `Any`, `object`, a bare `dict`/`list`/`tuple`/`set` or `Mapping[str, Any]` (#140).

## 0.6.0 — 2026-11-07

### Added
- `arborescent baseline --prune` removes stale baseline entries without recording anything new, so it works while the
  tree has unrelated new violations; the growth log is untouched (#131).
- `network` effect category (`effect-network`): `forbid = ["network"]` now catches `urllib.request`, `http.client`,
  `http.server`, `socket` and `requests`/`httpx` verb calls, independent of preset (#129).
- `strict` no longer requires allow-listing `__future__`, `typing`, `dataclasses`, `collections`, `enum` or `abc`;
  every other stdlib and third-party import is still allow-listed (#128).
- `package-never-depended-on` (information): flags a package that depends on nothing and that no other package declares
  or imports (#130).
- A ring-mismatched `internal-import` now ends with the whole closure a move would drag along (the project modules to
  move too, and the externals to allow), instead of one edge per run. The baseline key is unchanged (#133).
- `arborescent discover --suggest` prints, per module with no ring, the innermost ring its imports and effects already
  satisfy and the facts behind it (project imports, externals, effect calls). Rings looser than a module's facts need
  are already reported by `classify-facts` (#132). Splitting a module that mixes pure functions and an effect wrapper is
  not covered.

## 0.5.1 — 2026-11-06

### Changed
- `selftest`'s mutation loop (54 full, cold, uncached checks of this package's own 72 modules) now runs across a
  process pool instead of sequentially, cutting `test_arborescent_probes_its_own_package_rules` from ~76s to ~13s
  locally. The pool sits behind a new `Parallel` port (`ProcessPoolParallel`), since the application ring forbids
  the process effect directly; `Services.parallel` defaults to `None`, so every other `selftest` caller keeps the
  original sequential, same-process behavior (several rely on `monkeypatch` to simulate a removed rule, which only
  works in-process). Only the CLI's `selftest` command and this package's own self-test opt in.

## 0.5.0 — 2026-11-06

### Fixed
- `_dev_tool_findings` checked `dev_tools_forbid` effects against the root policy instead of the tool module's own scope,
  so a scope-level `effect_defaults` override was silently ignored there; `selftest`'s scope-effect-leak mutation probe
  had the same bug the other way around, checking the importing scope's `effect_defaults` instead of the scope that
  owns the leaking module. `check --list-dev-tools` also returned before validating a core-mode flag conflict, instead
  of after.
- `from pkg import submodule` is now an import of `pkg.submodule` only, not also of the package's `__init__`; the package edge
  remains when any imported name is not a submodule (or is `*`). Both spellings of one import now agree (#109).
  This includes the relative `from . import sibling, Hints`, which now also depends on the package (it reads `Hints` off its
  `__init__`) where it used to resolve to the sibling alone. Package splitting (`resolve_runtime_targets`) still carries
  `pkg/__init__` with every importer of a submodule, since importing it runs that file.
- `classify_from` now classifies a package that tach never names as a module (only its submodules) with the one ring all its
  classified descendants share, so its `__init__.py` is no longer `unclassified`; when the descendants disagree it stays
  unclassified, and an explicit `classify` entry or a tach module for the package still wins (#107). A package that holds a tach
  module without a `layer`, or an explicitly classified descendant in another ring, is not inferred either.
- The strict preset's pre-push hook runs the project's `.venv` interpreter when there is one, and when the interpreter it finds
  has no `arborescent` installed it prints a warning and lets the push through instead of failing every push with "No module
  named arborescent". An installed but broken `arborescent`, and every real `check` failure, still block the push.

### Changed
- The `strict` preset now generates tighter Ruff function limits: complexity 8 (Ruff: 10), branches 10 (12), statements 40 (50),
  returns 6 and args 5 stated explicitly, and selects `PLR0911` (too many returns). `standard` and `relaxed` are unchanged.
  A new `max-returns` key joins the `[tool.arborescent.ruff]` overrides, and several pylint limits now share one `[lint.pylint]`
  table (each used to open its own, which is invalid TOML). An existing `ruff.toml` is not overwritten: delete it and run
  `arborescent generate` to adopt the limits. Arborescent's own 30 functions over them are recorded in the baseline to be
  worked down.
- `packages migrate` now rewrites `from <old> import <module>` (also with `as`, several names, `from app import core` for a
  renamed `app.core`, and `from <old> import *`) instead of refusing it: a small LibCST transformer in the adapter rewrites the
  statement before `RenameCommand` runs, keeping every name bound to the same module under the same name (#71). It still
  refuses, with the statements listed and nothing modified, a `from <old> import a, b` that mixes a renamed module with a name
  that is not one from a renamed package, `import <new> as x` for a module renamed to a top-level name (LibCST's rename corrupts
  it), and files that do not parse. `docs/migration-driver.md`.
- Effect rules no longer count a bare parameter default that names an effect (`clock_now=datetime.now`, `new_id=uuid.uuid4`,
  `environ=os.environ`) as a use: it is the injection seam the message asks for. Calls, module-level aliases and a default
  wrapped in a lambda still count. Applies to `clock`, `env`, `identity`, `randomness`, `sleep`, `threads` and `process`; the
  `effect_defaults` (`injection` default, `typed` to require a `Callable`/`Mapping` annotation, `count` for the old behavior) controls it. The scan cache is rebuilt once.

### Added
- Three new effect categories and rules, closing a gap where I/O reaching an inner ring through an otherwise-allowed
  helper import went undetected (#103): `console` (`print`, `input`, `sys.stdout.write`, `sys.stderr.write`;
  `effect-console`), `global` (a function that declares `global x` and then assigns `x`; a read-only `global` is not a
  use; `effect-global`), and `effect-via-import` — a module that imports another module (same scope, not an
  `if TYPE_CHECKING` import) that itself uses, or itself imports something that uses, an effect this module's ring
  forbids, naming the import chain. Clock reads through `time.gmtime`, `time.localtime` and `time.strftime` are now
  also detected. Propagation is module-granularity (a mixed helper taints every importer) and stops at calls on an
  object of unknown type; see docs/configuration.md for the documented limits.
- Function-level call-graph taint (#98), the sharper complement to `effect-via-import`'s module granularity: a
  whole-repository pass resolves, for every function and method, the names it calls (`self.method()`, a same-function
  `x = C(); x.method()` constructor pattern, import aliases, and names re-exported through a package's `__init__.py`),
  then marks a function tainted when it calls an I/O primitive directly or calls another tainted function, iterated to
  a fixed point. The seed list is `taint_seeds` (default: every `effect-*` category except `patch` and `global`, plus a
  `network` primitive list `effect-*` does not cover: `urllib.request`, `http.client`, `http.server`, `socket`). Feeds
  `classify-facts`' "could sit in an inner ring" and "program and library" notes with the specific tainted function
  instead of just the module; informational only, never baselined, skipped under `--changed`. Documented limits (no
  type inference, no `getattr`/decorators/callbacks, no inheritance, module-level only, same-function-only constructor
  tracking, no lazy imports) are in docs/configuration.md.
- `git_hook = true` / `git_hook = false` in `[tool.arborescent]` chooses the `pre-push` hook independently of the preset:
  `true` makes `generate` and `check` install it under any preset, `false` makes them remove an Arborescent-managed one
  (a hook Arborescent did not write is still never touched, in either direction). Previously only the `strict` preset
  installed it, and switching away from `strict` left a stale hook behind. Settable per scope, like `middle_forbid`.
  `ARBORESCENT_SKIP_HOOK=1 git push` skips the hook for one push only, leaving other hooks and the `git_hook` setting
  alone; `git push --no-verify` remains git's own way to skip every hook.
- `owned_literal`: `[[tool.arborescent.owned_literal]] literal = "APP_CONFIG", modules = [...], rings = [...], reason = "..."` lets only
  those modules (and rings) spell an identifier-like string, such as an environment variable name; the new `literal-outside-owner` rule
  names the module, ring and line, reports an owner glob that matches no module, and is mutation-tested by `selftest`. The scan cache is
  rebuilt once (#112). Not done: putting the variable name into `effect-env` messages.
- A package whose modules still sit beside other packages' modules (a flat layout before a move into a workspace) can be declared:
  `root` may be the target folder when every owned path sets `destination`; the root-coverage error now says so (#114).
- `[[assets]] package = "@root"` gives repository-level files (LICENSE, CI, lock file) to the workspace instead of a package, so they are
  not `package-unowned-asset` and are never extracted. The catalog file is no longer an asset itself (#115).
- `importable_by`: the inverse of `module_forbid_imports`. `"paths" = ["container", "ingest.*"]` lets only those modules import `paths`
  (and its submodules); a module not listed is unrestricted, a stale key or glob is reported, and the new `restricted-import`
  rule is mutation-tested by `selftest` (#111).
- `classify_from` also derives `rings` (innermost first) from the tach.toml's `layers`, and a default `may_import` of every inner ring for
  each, when the policy has no `rings`; explicit `[ring.<name>]` settings still win and an explicit `rings` list is left alone
  (#106).
- The `strict` preset (the default) installs a `pre-push` git hook when you run `generate` or `check`. It runs `arborescent check
  --require-tools --require-profile`, so a push that would fail CI is blocked locally. A `pre-push` hook Arborescent did not write is
  left alone with a warning; `standard` and `relaxed` install nothing. `FileSystem` gained `make_executable`.
- `check --engines core` (same as `--no-tools`) is the documented core mode for a pre-commit hook or a plain test run: only
  Arborescent's own checks run, every skipped engine is listed as `SKIPPED`, and combining it with `--require-tools`,
  `--require-profile` or `--install-tools` exits 2 instead of passing silently. It starts no engine, but still uses git (file list,
  `practices`, `--changed`, `--growth-against`) and checks child policies in a nested core-mode run (#100).
- `dev_tools` policy key and the `dev-tool-import` and `dev-tool-misplaced` rules: folders of developer tooling need no ring,
  may import product code, and are skipped by the ring, external-import and effect rules (unresolvable imports, forbidden
  module names and import cycles are still checked), but product code importing them
  fails. `__arbor__ = {"role": "dev_tool"}` is accepted only below a `dev_tools` path. Tooling keeps its suppression and
  discard inventory and gets its own heading in the report. Tooling is also checked for raw process use (`dev_tools_forbid`, default `["process"]`) and can be limited to chosen product modules (`dev_tools_may_import`, rule `dev-tool-product-import`); `check --list-dev-tools` shows what is covered. The scan cache is rebuilt once (#99).
- `database` and `filesystem` effect categories with `effect-database` and `effect-filesystem` rules, forbiddable per ring with
  `forbid` / `middle_forbid` like the others. Direct references only (`open`, `os`/`glob`
  file calls, `shutil` copy/move/remove, `tempfile` file creators, `Path(...).read_text()` and similar). The innermost ring (and
  `strict` middle rings) forbid them by default like every other category, so an inner-ring module that calls `open(`,
  `shutil` or `sqlite3` is now reported; set `forbid` on the ring to allow it. The scan cache is rebuilt once (#92).
- Test pinning #93: with `classify_from`, a module missing from tach.toml's layers is reported as `unclassified` (it was never
  exempt); no new rule was needed.
- `classify-facts` (information, never baselined, not part of `--require-modularity`): checks a module's `[classify]` ring against
  its facts with three signals: it passes every rule of a more inner ring, it is a class-free leaf that only wires peers to
  inner-ring code, or it has a `__main__` guard and is also imported. It uses imports, effect uses, top-level definitions and a
  new `__main__` guard fact, never names; the scan cache is rebuilt once. Limits are in `docs/configuration.md` (#90).
- `forbidden_modules` policy key (root or per scope) and the `forbidden-module` rule: `check` fails when a first-party module whose
  dotted name matches a `classify`-style glob exists, for a generic package that moved to a pinned dependency and must not
  come back. It never looks at imports, so it does not overlap `forbid_everywhere`, and it fires even for a module that is
  classified or `unclassified` (#78).
- `package-file-relative-path` (information): a module that moves in a migration and reads `__file__` may now resolve a `.env`,
  data folder or sibling-script path from the wrong place. It is exported as a gap and listed by `packages migrate` under
  "review before --yes"; it never blocks. The scan cache is rebuilt once (#83).
- After `check` and `baseline`, one line sizes the baselined `internal-import` entries as importing modules and distinct
  targets, so a shrink-only import cap can be read in modules and not only baseline entries (#75).

## 0.4.0 — 2026-11-01

`packages migrate` and `packages extract` are experimental in this release: they have run on a synthetic monorepo, on
Arborescent itself and, since the first draft, against real repositories (which produced several fixes below), and their
arguments may still change.

### Added
- `middle_forbid = [...]` (top level or per scope) replaces the preset's default of effect categories that middle rings may not
  use, so a `relaxed` policy can enforce effects without the I/O allow-lists `standard` and `strict` also require. A ring's own
  `forbid` still wins (#86).
- Practice `stale-config-path`: a glob or `.py` path in a configured file (a Makefile, CI yaml, pyproject) that matches nothing
  after a move, so a suite silently ran zero tests; also a hatch `packages` entry that does not exist (#89).
- Practice `source-root-drift`: a `sources` entry below no `source_roots` entry, and drift between the policy and the copies in
  `tach.toml` `source_roots`, hatch `sources`/`packages` and pytest `pythonpath` (#91).
- Migration export gap `package-module-rename-mismatch` (error): a module's `target_module` is not the import name its target
  path gives, so imports would break in the new repository. A declared rename that does not match the move is the same gap (#77).
- `tach-invisible-import` (information): a module that imports by a run-time name (`importlib.import_module`, `__import__`) creates
  an edge `tach check` cannot follow, so its pass said nothing about it. It is now listed per module as a `NOTE`, and
  `check --require-visible` fails on each one (`VISIBILITY FAIL`) for repositories that want tach's verdict to be complete (#59).
- `vendored = true` on a package: a read-only copy of code maintained elsewhere. It is exempt from the release-policy gap and
  from derived `pyproject.toml` files, may not combine with a release, history or `unmanaged` policy, and the migration
  export gains a required boolean `vendored` per package (schema v1 updated; `policy` is `null` for such a package) (#59).
- `vendor_roots` and `library_roots`: declare a folder such as `vendor` or `libs` once and every child package becomes a
  source root (`sources`, `source_roots`, and so the generated tach and import-linter files). A `vendor_roots` folder is
  scanned despite its vendor-convention name, and the `[vendor-folder]` failure now names `vendor_roots` and `scan_vendors` (#59).
- Three package rules found by mutation-testing Arborescent's own boundaries: `package-dependency-cycle` (error: packages
  that list each other in `depends_on`; a catalog edit like this used to be certified by `--require-modularity`, because
  `check` only parsed the catalog and the cycle search ran only in `packages validate`, which now shares it),
  `package-unused-dependency` (information: a declared dependency no module imports) and `package-owns-everything`
  (information: one package owns every module; never fails, since size says nothing about quality; #73). `selftest` probes the first two.
- `modularity = "ready" | "report"` and `check --require-modularity`: strict modularity is now separate from strict linting.
  `package_catalog` no longer requires the `strict` preset (a `standard` or `relaxed` repository can be modularity-ready).
  `report` shows package gaps as notes instead of failing; `--require-modularity` fails without a catalog, in `report`
  mode, under `--changed`, or while an error-severity package gap is open (`MODULARITY FAIL`, `CERTIFIED modularity ready`).
  `--require-profile` stays lint and tool-version certification only. Behavior change: a policy that was rejected for
  `package_catalog` with a non-strict preset is now accepted.
- `packages migrate` takes its target directory from `--target` or from a git-ignored local config (`[targets]` in `.arborescent/local.toml`, found through `ARBORESCENT_LOCAL_CONFIG`, a `.env`, or that default) and prints which source supplied it. Where a repository is checked out is machine-specific, so the config is read by this command only and never reaches the catalog, the export, derived files or contracts; `home.repository` stays the committed remote identity.

- `arborescent packages migrate PACKAGE_ID --target DIR`: the migration driver (#67). It supplies a dedicated migration
  library with one package's migration arguments through a port and does not migrate itself. The library is git
  filter-repo (optional extra `arborescent[filterrepo]`); a missing one prints a `SETUP` line with the install command.
  A dry run (default) prints the exact arguments; `--yes` supplies them. It refuses, with no override, while the package
  has an open error gap, an undeclared first-party dependency, or private modules that other packages still import, and
  does nothing for a workspace member. Module renames are reported as not applied (filter-repo does not rewrite
  imports), unless a second port rewrites them: a LibCST adapter (`arborescent[libcst]`) applies the module-name map to the new
  repository only, one `RenameCommand` per module, as an uncommitted change. It refuses (exit 1, statements listed, nothing
  touched) when a `from <old> import <module>` statement is present, because LibCST's output could then still parse while
  naming an unbound name, and fails loudly on any import still naming an old module. The annotation layer cannot import the driver: `module_forbid_imports` in Arborescent's own policy, now
  probed by `selftest` for every configured pattern. `docs/migration-driver.md`.
- Opt-in `package_catalog = "packages.toml"` (any preset): every first-party module and asset must belong to
  exactly one package. New rule ids `package-unassigned-module`, `package-multiply-assigned-module`,
  `package-unowned-asset`, `package-multiply-assigned-asset` and `package-undeclared-dependency` fail the check;
  `package-spans-rings` is information (a `NOTE` line, never failing, never baselined). Assets are claimed by package
  `paths` or ordered `[[assets]]` globs, first match wins. `selftest` probes each rule when the key is set (#52).
- `arborescent packages export`: one read-only, versioned JSON export of the migration arguments of every package
  (identity and `home`, owned modules and assets with target paths and target module names, public surface and bypassing
  modules, declared dependencies, open annotation gaps as named entries) with a published JSON Schema
  (`docs/schemas/migration-export-v1.schema.json`, `arborescent packages schema`). New optional catalog keys: `home`,
  `public`, `externals`, `module_renames`. `release`/`pin`/`history` (#56) and `pyproject` (#60) are reserved slots. No
  tool-specific emitter ships; `docs/migration-export.md` has a `git filter-repo` recipe and the comparison with
  blinkered's `blinkered.toml` (#54).

- Annotation preservation across a split: a package may declare `public` module globs, and the opt-in
  `package_parts` lists extracted part trees (#27) to compare with the catalog. New rule ids
  `package-semantics-drift`, `package-membership-drift`, `package-ring-drift`, `package-public-symbol-dropped` and
  `package-private-import` (the last also runs on the origin without parts). No graph comparison and no test runs;
  `selftest` probes each rule (#55).
- With `package_catalog` set, `arborescent generate` compiles the package assignment into import-linter (one `forbidden`
  contract per package for undeclared packages and for modules outside a declared package's `public` list, and one for
  third-party names its rings do not list) and into tach's per-module `depends_on`, so the engines' own violation output
  is a package's cut list. Tach switches to explicit `depends_on` when the catalog is set (#53).

- Per-package release, pin and history policy in `packages.toml`: `release.pin` (`tag` or `sha`), `release.merge`
  (`merge-commit` only), `release.tag_before_branch_delete`, `history` (`preserve` or `fresh`) and an explicit
  `unmanaged = true`. Values are validated and unknown `release` keys rejected. New rule
  `package-release-policy-missing` names a package with neither a policy nor the marker (workspace members are exempt
  and cannot declare one); `selftest` probes it. The policy fills the `policy` slot of the migration export and the gap
  appears in its `gaps`. Any unknown key on a `[[packages]]` entry is now rejected. Data and checks only: nothing is
  released, pinned or rewritten (#56).
- Opt-in `package_pyproject = true` (needs `package_catalog`): `arborescent generate` creates each missing package
  `pyproject.toml` with the fields the annotations determine (dependencies from the rings' `external` allow-lists through
  the `dependencies` mapping, plus `depends_on`; `[tool.uv.sources]` per sibling; the root `[tool.uv.workspace]
  members`; `source_roots` and `root_packages` tables) and a minimal template; an existing file is never rewritten.
  New information rule `package-pyproject-drift` (a `NOTE`, never failing, never baselined) names a derived field that
  disagrees with the annotations. `selftest` probes it. Where a sibling comes from is its `[packages.home]` (#54), and
  `packages export` fills each package's `pyproject` object with the same derived fields (still `object | null`, no
  schema version bump). A source is `{ workspace = true }` only between two workspace members; otherwise a git source
  (with `subdirectory` for a workspace member), and a git value that is not a URL gets no entry and is reported, never guessed (#60).
- `arborescent packages export`: one read-only, versioned JSON export of the migration arguments of every package
  (identity and `home`, owned modules and assets with target paths and target module names, public surface and bypassing
  modules, declared dependencies, open annotation gaps as named entries) with a published JSON Schema
  (`docs/schemas/migration-export-v1.schema.json`, `arborescent packages schema`). New optional catalog keys: `home`,
  `public`, `externals`, `module_renames`. `release`/`pin`/`history` (#56) and `pyproject` (#60) are reserved slots. No
  tool-specific emitter ships; `docs/migration-export.md` has a `git filter-repo` recipe and the comparison with
  blinkered's `blinkered.toml` (#54).
- Acceptance test for the migration annotations (`tests/test_split_acceptance.py`, fixture `tests/split_fixture.py`): a
  synthetic four-package monorepo (one own-repository package, one workspace member, one package spanning three rings,
  one with an assets folder, one deliberate blocking edge) is split with `git filter-repo` driven only by the JSON export;
  the cut list is the generated import-linter and tach output, and the parts are checked with the preservation rules. It
  skips with a reason where `git filter-repo`, import-linter or tach is not installed; Arborescent does not migrate (#57).

### Changed
- The `external-import` detail now names the first-party module a bare sibling import resembles and says it only resolves when the
  file runs as a script. The rule id is unchanged (#88).
- `packages extract` is a wrapper: it runs the `migrate` pipeline (a migration library such as git filter-repo, then the import
  rewriter) and writes `.arborescent-package.toml` afterwards. It takes `--output` as another name for `--target`, is a dry run
  unless `--yes`, and needs `package_catalog` in the policy. Arborescent no longer copies files itself: `PackageCatalog.extract`,
  the symlink and generated-output handling, `package_copy` and the extraction-time module-identity comparison are removed, and
  `--with-dependencies` is refused (a dependency is a separate package). A path move that changes an import name without a
  declared `module_renames` entry is therefore no longer caught at extraction; nothing checks it yet.
- `check` now runs import-linter with the generated `arborescent-importlinter.toml` (it was written and drift-checked but never
  read, so a hand-written copy of the contracts had to be kept beside it). The file is used only when it carries the
  generated header and the project has no import-linter configuration of its own; a project's own config always wins and
  `generate` still never changes an existing file. Arborescent's own hand-written contract is removed: the generated ones
  (10 here, including the package contract) are a strict superset of it. New optional port extension `RootAwareEngine`.

- `packages.py` is split into cohesive `package_*` modules; behaviour and the `arborescent.packages` names are unchanged,
  and `packages extract` is untouched (#50).

### Fixed

- With tach disabled, the minimum profile now also requires import-linter to have contracts configured: the project's own
  config, or the generated `arborescent-importlinter.toml`.
- Scope-path normalization no longer caches every scanned file path.

## 0.3.0 — 2026-10-31

### Added

- `classify_from = "tach.toml"` (top level or per scope) derives `classify` from tach layers; explicit entries win.
- tach is optional for the minimum profile: `disabled_tools = ["tach"]` passes when import-linter stays enabled and
  installed. `check` warns (advisory) when tach is enabled over several distinct `source_roots`, where tach cannot
  enforce cross-root edges, and the minimum profile then skips tach's layer canary if import-linter is enabled (#41).
- Module-level `__arbor__ = {"ring": "domain"}` annotations take precedence over `classify` globs. Malformed
  annotations and unknown rings report `invalid-arbor-annotation` while falling back to `classify` for enforcement;
  modules without a fallback ring also report `unclassified`.
- `disabled_tools = ["arbor-annotations"]` ignores annotations and their validation findings, uses `classify` alone,
  and prevents minimum-profile certification. `__arbor__` remains a module-level declaration; `package` and
  `prepackage` declarations do not establish scope ownership.
- Named architecture scopes let one root policy govern independently structured libraries and application cores.
  Each scope has its own sources, roots, rings, classifications and public import patterns; cross-scope imports require
  an explicit dependency and a public target. Shared repository code has one source owner, and single-scope policies
  keep their existing meaning.
- `require_arbor_annotations`: an opt-in, per-scope policy flag that turns an unannotated, classify-only module into
  a hard failure (`unannotated-module`) instead of the silent, permanent `classify` fallback. `require_arbor_annotations_exclude`
  exempts module-name globs (vendored or read-only code) without needing a separate scope. `classify` itself is
  unchanged and stays the default, indefinitely supported path; `arborescent annotate` remains the migration tool.

### Fixed

- Generated `tach.toml` now writes `[external] exclude` from `tach_external_exclude` (lazily-imported optional
  extras tach can't see from base dependencies) in both the single-scope and multi-scope generators; the multi-scope
  path previously always wrote an empty exclude list.
- The `unclassified` finding now hints `pkg*` when a `classify` glob names only a parent package (`classify` matches
  whole module names, so `pkg` does not cover `pkg.sub`).
- Scope-path normalization is memoized, removing redundant work in multi-scope scans.

## 0.2.0 — 2026-10-26

### Changed

- Renamed the distribution, CLI, Python import package, policy table, generated baseline and configuration files, and GitHub repository from ArchLint to Arborescent.
- Windows install hints now use valid PowerShell quoting, including interpreter paths with spaces.

## 0.1.0 — 2026-10-26

### Changed

- Fixes after merging the cleanup branches: `python = "3.12.1"` is read as `3.12`; `type_checkers` must be a
  non-empty list of names (an empty list used to crash `generate`); `archlint bench` reports a bad policy as exit 2
  like every other command, applies `--checkers` to the policy and validates them before timing; `Services.engines` has
  no default any more (a forgotten list used to skip import-linter and tach silently); `ports.TypeChecker` extends
  `Engine`; the scanner, practices and selftest share `domain.NOISE_DIRS`; the in-memory file system sorts glob results
  like the real one.
- The config file's shape no longer lives in the domain: `Policy.from_mapping` is now `config.policy_from_mapping`
  (`@group` expansion, `preset`, `io_modules`, defaults). `Policy` checks its own invariants on construction (distinct
  rings, onion rule, `classify` naming known rings), so a hand-built policy cannot be inconsistent. Messages and
  `PolicyError` are unchanged.
- Rule ids live in one place, `archlint.domain.Rule` (a `StrEnum`; the string values, and so baseline keys, are
  unchanged). `selftest.NO_MUTATION` records why a rule has no mutation. `engine.write_generated` now writes the
  generated files (`cli` only prints), and `rules.check_modules` is split into one function per rule. The unused
  `rules.RULES` tuple is gone.

### Added

- `FileSystem` port (`ports`), `LocalFileSystem` (`adapters`) and `FakeFileSystem` (`testing`). Baseline, config, the
  practices, drift, selftest and bench no longer touch the disk themselves; `Services` gains a required `fs` field and
  the functions take it as their first argument. The `application` ring's external list drops `shutil` and `tempfile`
  (`pathlib` stays for `Path` values only; JSON and timing go through `FileSystem.read_json`/`write_json` and a new `Clock` port, `Services.clock`), and a test fails if application code bypasses the port.
- Engine hardening: import-linter and tach are `Engine` adapters (`archlint.engines`) behind a new port, so `run_check`
  no longer hardcodes their commands; an unknown type checker (policy, `--type-checker`, `bench`) is a `PolicyError`
  (exit 2) instead of a traceback; `run_check` always shuts its worker pool down, even if the scan raises.
- Stdlib detection follows the policy's `python` (`3.11` to `3.14`) instead of the interpreter running ArchLint, from a
  data table in the new `archlint.stdlib` (domain ring; refresh steps in `docs/stdlib-table.md`). An unsupported
  `python` is now a policy error. `facts.STDLIB` is gone; use `Policy.stdlib`.
- Documentation: `docs/configuration.md` (every key, rule and practice) and `docs/architecture.md` (ArchLint's rings
  and module table, kept in sync with the policy by `docs-sync`, which is now enabled for this repository).
- Presets: `preset = "strict" | "standard" | "relaxed"` in `[tool.archlint]` (default `strict`, identical to the
  behaviour before presets). They set how much of the external-import allow-list applies (all imports / third-party plus
  I/O stdlib / third-party only) and whether OS branching is limited. Layering and classification are never relaxed.
  `io_modules = [...]` replaces the stdlib set treated as I/O. The I/O set moved from `generate` into the new
  `archlint.presets` (domain ring).
- One `[tool.archlint]` policy (rings, glob classification, per-ring internal and external allow-lists, OS-branching
  rings, entry-script dependencies, practices and circular-import enforcement) used by ArchLint's native checks.
  `archlint generate` optionally scaffolds import-linter, Tach, Ruff and type-checker presets without overwriting
  existing files. `check` reports preset drift as informational, runs the selected tools in parallel, runs one-pass AST
  checks and applies the shrink-only baseline (`archlint baseline`). `selftest` mutation-tests the rules; `bench` times
  every engine.
- Ruff integration as a first-class engine using project-owned Ruff configuration, optional preset generation and
  minimum-profile canaries. Profile v1 pins Ruff and Tach versions and verifies that required Ruff rules are effective.
- Optional ArchLint minimum-profile certification. Certification requires the minimum native architecture policy and
  verified Ruff and Tach checks; users can keep a project consistent with a weaker custom policy without receiving
  certification. `forbid_circular_imports` defaults to true and is required by the minimum profile.
- Effect facts and per-ring effect rules for ambient clock, environment, identities, randomness, sleeping, threads,
  processes, dynamic imports and test patching. Alias-aware AST findings remain scoped to the ring policy.
- Strict first-party coverage and a `discover` command for unclassified files, noise-directory checks, fingerprinted
  vendor-folder exclusions, ratcheted wildcard-ring findings and shrink-only baselines with explicit adoption or
  reasoned growth logs.
- Report-only logical-line size suggestions, plus inventories that ratchet suppression directives and reasoned
  `discard()` calls.
- `install-tools` and `check --install-tools` for explicit missing-checker setup, including interpreter-aware installs,
  version-constrained requirements and Poetry's `archlint` development dependency group.
  Repository practices: gitignore-shadow, sibling-versions, secrets-guard, docs-sync, crlf, undeclared-imports.
- Measured on Windows (cold/warm seconds; small = 13 modules, medium = 314, large = 1344; type errors are inflated
  because the copies have no virtualenv): ArchLint own checks 0.02/0.01, 0.80/0.10, 2.69/0.35; import-linter
  0.4/0.3, 0.4/0.4, 0.6/0.6; tach 0.5/0.5, 0.7/0.7, 1.1/1.1; pyright 2.5/2.1, 12.2/11.6, 36.9/36.2; basedpyright
  1.4/1.3, 10.8/10.9, 35.7/35.9; ty 0.12/0.14, 0.25/0.22, 0.78/0.68; pyrefly 0.31/0.29, 0.53/0.51, 0.90/0.80; whole
  `check` (parallel, pyright) 2.0/2.1, 11.4/11.3, 36.2/36.4. The type checker dominates; ty and pyrefly are 15-50x
  faster.

### Fixed

- The generated pyright config no longer sets `reportImportCycles` (measured 100x slowdown).
