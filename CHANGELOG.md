# Changelog

0.x: a minor release may break the public surface, a patch release may not.

## Unreleased

### Added

- Opt-in `package_catalog = "packages.toml"` (strict preset only): every first-party module and asset must belong to
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
  schema version bump) (#60).

### Changed

- `packages.py` is split into cohesive `package_*` modules; behaviour and the `arborescent.packages` names are unchanged,
  and `packages extract` is untouched (#50).

### Fixed

- With tach disabled, the minimum profile now also requires import-linter to have contracts configured (it does not
  auto-discover the generated `arborescent-importlinter.toml`).
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
