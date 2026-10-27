# Minimum Profile Certification Implementation Plan

> **For agentic workers:** Native inline implementation; tasks follow test-first order.

**Goal:** Let projects own Ruff/Tach/type-checker configs, offer optional Arborescent presets with advisory drift, and certify only revisions that meet Arborescent minimum profile v1.

**Architecture:** External tools run through their normal config discovery and their findings remain visible to Arborescent. A certification service checks Arborescent's native minimum, exact tool versions and behavioral Ruff/Tach canaries. Suppression and `discard()` counts feed the shrink-only baseline.

**Tech Stack:** Python 3.11+, `ast`, `tokenize`, existing `ToolRunner`/`FileSystem` ports, Ruff 0.17.2 and Tach 0.35.3.

**Spec:** `docs/superpowers/specs/2026-10-26-ruff-engine-and-code-quality-design.md`

## Global Constraints

- Generated tool configs are optional; native project configs remain authoritative.
- Drift from an Arborescent-generated preset is informational and never fails a check by itself.
- Certification profile v1 pins Ruff 0.17.2 and Tach 0.35.3.
- Required Ruff rules are `BLE001`, `E722`, `F401`, `B006`, `C901`, `PLR0912`, `PLR0913`, `PLR0915`, `TID252`, `PLW1514`, `DTZ001`, `T201`, `PLC0415`.
- Certification requires Arborescent strict native minimum, Ruff behavior canaries, Tach layer/external canaries, and no new unbaselined findings.
- Suppressions and `discard(value, reason=...)` are counted per module and ratcheted; this does not claim type-aware `Result` enforcement.
- An `arborescent check --require-profile --require-tools` run is the CI certification gate.

## Review Focus

- Existing user configs must not be overwritten or bypassed when `arborescent check` runs.
- Ruff canaries must fail when required rules are excluded, disabled globally, disabled per file, or hidden by nested config.
- Tach canaries must test active project config and reject both forbidden layer edges and undeclared external dependencies.
- Tool version mismatch or missing tools must never print a certification claim.
- Baseline growth from suppressions or discard calls must use the existing reasoned growth flow.

---

### Task 1: Native tool configs and optional presets

**Files:** `src/arborescent/engines.py`, `src/arborescent/typecheckers.py`, `src/arborescent/generate.py`, `src/arborescent/engine.py`, `src/arborescent/cli.py`, `tests/test_ruff_engine.py`, `tests/test_engine.py`.

**Interfaces:** Tool adapters keep `command(files)` and `passed(result)`; generated configs stay available via `generate`. `CheckReport` keeps drift as advisory output, distinct from violations.

- [ ] Add tests proving Ruff, Tach and type checkers run without an Arborescent `--config` override and respect native discovery.
- [ ] Add tests proving preset generation creates missing files and leaves existing configs untouched.
- [ ] Add tests proving only files marked as Arborescent-generated are compared for drift, and drift never changes `CheckReport.ok`.
- [ ] Implement native command discovery and optional/non-destructive generation.
- [ ] Run the focused engine/generator tests and confirm green.

### Task 2: Versioned minimum profile and behavioral canaries

**Files:** create `src/arborescent/certification.py`; modify `src/arborescent/engine.py`, `src/arborescent/cli.py`, `src/arborescent/domain.py`, `src/arborescent/ports.py`, `src/arborescent/adapters.py`; add `tests/test_certification.py` and update engine tests.

**Interfaces:** Add immutable `CertificationReport(status, profile, tools, failures)`; `certify(policy, root, services, scan) -> CertificationReport`; add `--require-profile`. `CheckReport.certification` carries the result. `--require-tools --require-profile` makes missing tools, version mismatch or failed probes fail the command.

- [ ] Write failing tests for strict native minimum, version mismatch, and profile output.
- [ ] Write failing Ruff canary tests for every required code, a project config, nested config, global disable/exclude and per-file ignore.
- [ ] Write failing Tach canary tests for a forbidden layer edge and undeclared external dependency under the active config.
- [ ] Implement tool version checks and fixture-based behavior probes; do not parse TOML to certify rule activation.
- [ ] Run the focused certification tests and confirm green.

### Task 3: Suppression and discard inventory

**Files:** `src/arborescent/facts.py`, `src/arborescent/adapters.py`, `src/arborescent/engine.py`, `src/arborescent/domain.py`, `src/arborescent/baseline.py`, `src/arborescent/cli.py`; add `tests/test_suppressions.py` and extend baseline tests.

**Interfaces:** Extend cached source facts with suppression tokens and discard-call counts; bump the scan cache schema. Add stable baseline rule IDs for suppression counts, discard counts, and `_ = call` violations.

- [ ] Write failing tests for `# noqa` variants, `# type: ignore`, `discard(..., reason=...)` counts per module, and `_ = call` rejection.
- [ ] Verify each fails against the current behavior.
- [ ] Implement tokenization/AST extraction and emit count-based baseline findings with stable per-module keys.
- [ ] Add report totals by module and preserve existing reasoned growth semantics.
- [ ] Run focused facts/baseline tests and confirm green.

### Task 4: Documentation, CI, and final verification

**Files:** `.github/workflows/ci.yml`, `README.md`, `docs/configuration.md`, `docs/architecture.md`, `tests/test_customizations.py`, `tests/test_todo.py`.

- [ ] Add tests for separate informational preset drift and fatal minimum-profile failures.
- [ ] Update the CI gate to use `arborescent check --require-tools --require-profile`.
- [ ] Document optional presets, native config ownership, profile v1, tool versions, suppression/discard ratchets, and the precise claim.
- [ ] Run `python -m pytest`, `lint-imports`, `python -m arborescent check --require-tools --require-profile`, `python -m arborescent selftest`, and `git diff --check`.

No commit or push is part of this plan; preserve the current worktree changes for review.
