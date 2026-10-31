# Nested Arborescent Certification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let a parent Arborescent policy certify child-owned packages, override their policies, or integrate policy-less and vendored source into the parent tree.

**Architecture:** Add immutable child-root metadata and configurable ownership/vendor-scan settings to `Policy`. Respect direct child policies by default, allow the parent to override them, integrate policy-less roots into the parent scan, and recursively invoke the same CLI for independently governed children.

**Tech Stack:** Python 3.11+, dataclasses, `tomllib`, existing `ToolRunner` and scanner ports, pytest.

**Spec:** `docs/superpowers/specs/2026-10-29-nested-arbors-design.md`

## Global Constraints

- `child_policy_mode = "certify"` respects direct child policies; `"parent"` overrides them.
- A listed root without a direct policy remains in the parent scan.
- Vendored folders remain excluded unless `scan_vendors = true`.
- Legacy and multi-scope policies retain existing behavior when `children` is absent.
- Parent native tool configs must exclude child source when they should not govern it.

## Review Focus

- A child without its own direct config must fail instead of inheriting the parent's config; test config resolution.
- A child path that escapes the parent, equals `.`, or overlaps another child must be rejected; test policy validation.
- Parent scans must not classify or report child Python modules; test scanner exclusion/counts.
- Child check failures and missing profile certification must fail parent aggregation; test command/result behavior.
- Nested children must run once through recursive child ownership and not be double-declared by the parent; test configuration overlap rules.

---

### Task 1: Child policy model and direct config resolution

**Files:**
- Modify `src/arborescent/domain.py` for `ChildArbor` and `Policy.children`.
- Modify `src/arborescent/config.py` to parse `children` in single and multi-scope forms, validate relative normalized paths, and resolve direct child config files.
- Test `tests/test_policy.py`.

- [x] Add tests for accepted child list, duplicate/overlapping paths, absolute and parent-traversing paths, and direct-only child config resolution.
- [ ] Run those focused tests and observe expected failures.
- [x] Implement immutable child metadata and validation.
- [ ] Run `uv run pytest -p no:tach tests/test_policy.py -q`.

### Task 2: Parent scan ownership

**Files:**
- Modify `src/arborescent/scanner.py` to exclude declared child roots from parent discovery.
- Test `tests/test_scanner.py`.

- [x] Add fixtures for independent child ownership, policy-less parent integration, and explicit parent override.
- [ ] Run the test and observe expected failure.
- [x] Implement conditional child exclusion and configurable vendor scanning.
- [ ] Run `uv run pytest -p no:tach tests/test_scanner.py -q`.

### Task 3: Recursive child checks and aggregation

**Files:**
- Modify `src/arborescent/engine.py` or a focused `children.py` use case to invoke child CLI checks through `ToolRunner` and add `EngineRun` results.
- Modify `src/arborescent/cli_report.py` only if child summaries need a distinct presentation.
- Test `tests/test_engine.py` and CLI output coverage.

- [x] Add tests that child checks run from their own root/config, ordinary child failure fails the parent, and `--require-profile` propagates to children.
- [ ] Run the focused tests and observe expected failures.
- [x] Implement recursive child invocation and aggregate exit status without sharing baselines.
- [ ] Run `uv run pytest -p no:tach tests/test_engine.py -q`.

### Task 4: Documentation and end-to-end verification

**Files:**
- Modify `docs/configuration.md` and `docs/architecture.md`.
- Modify `tests/test_policy.py`, `tests/test_scanner.py`, and `tests/test_engine.py` as needed.

- [x] Document configurable ownership, recursive certification semantics, own baselines, and native-tool exclusions.
- [ ] Add an end-to-end fixture with a parent and a separately configured child, showing parent failure when child fails and parent certification when both certify.
- [ ] Run the full test suite and the repository's architecture self-check.
- [x] Review diff for unchanged behavior when no `children` key is configured.
