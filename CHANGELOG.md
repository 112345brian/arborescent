# Changelog

0.x: a minor release may break the public surface, a patch release may not.

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
