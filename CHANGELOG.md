# Changelog

0.x: a minor release may break the public surface, a patch release may not.

## Unreleased -- 0.1.0 (prototype)

### Added

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
  rings, entry-script dependencies, practices) that `archlint generate` compiles into import-linter contracts,
  `tach.toml` and a strict config for pyright, basedpyright, ty or pyrefly. `check` diffs the generated files against
  the committed ones, runs the engines in parallel, runs its own one-pass AST checks, and applies a shrink-only baseline
  (`archlint baseline`). `selftest` mutation-tests the rules; `bench` times every engine.
  Repository practices: gitignore-shadow, sibling-versions, secrets-guard, docs-sync, crlf, undeclared-imports.
- Measured on Windows (cold/warm seconds; small = 13 modules, medium = 314, large = 1344; type errors are inflated
  because the copies have no virtualenv): ArchLint own checks 0.02/0.01, 0.80/0.10, 2.69/0.35; import-linter
  0.4/0.3, 0.4/0.4, 0.6/0.6; tach 0.5/0.5, 0.7/0.7, 1.1/1.1; pyright 2.5/2.1, 12.2/11.6, 36.9/36.2; basedpyright
  1.4/1.3, 10.8/10.9, 35.7/35.9; ty 0.12/0.14, 0.25/0.22, 0.78/0.68; pyrefly 0.31/0.29, 0.53/0.51, 0.90/0.80; whole
  `check` (parallel, pyright) 2.0/2.1, 11.4/11.3, 36.2/36.4. The type checker dominates; ty and pyrefly are 15-50x
  faster.

### Fixed

- The generated pyright config no longer sets `reportImportCycles` (measured 100x slowdown).
