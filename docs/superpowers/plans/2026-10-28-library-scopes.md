# Independently Scoped Libraries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one repository config and one root invocation enforce a separate Clean Architecture and public import boundary for each library and the application core.

**Architecture:** Keep the current `Policy` as the compatibility form for one architecture. Add named scopes, each backed by its own ordinary `Policy`, plus `public` module patterns and explicit `depends_on` scope names. The scanner assigns every discovered module to one source-owning scope; native rules resolve rings within that scope and check cross-scope imports against the dependency graph and target public patterns. Generated engine configs compose all scopes from the same root policy.

**Tech Stack:** Python 3.11+, stdlib `tomllib`, existing dataclasses, AST scanner, Ruff, Tach, import-linter, and pytest.

**Spec:** `docs/superpowers/specs/2026-10-28-library-scopes-design.md`

## Global Constraints

- One root-level Arborescent invocation can check the complete repository.
- Each named library has its own sources, module roots, ring hierarchy, and classifications.
- Every source module has one owning scope; scopes do not duplicate files to apply multiple policies.
- Each library declares the modules that form its supported boundary for consumers.
- Cross-library dependencies are explicit and may target only a dependency's declared boundary.
- Shared helpers belong to a named shared scope (or to the library that owns their capability) and are consumed through that scope's declared boundary.
- Preserve the existing single-policy configuration as a compatible shorthand for one scope.
- The helper is never duplicated just to make it fit two scopes.

## Review Focus

- Overlapping source paths must fail clearly; unowned Python files must remain visible as unclassified. Pin in Task 1 and Task 2.
- Two scopes with the same dotted module name must fail instead of silently replacing one module. Pin in Task 2.
- Scopes may reuse local ring names such as `domain`, and annotations must validate against the owning scope's rings. Pin in Task 1 and Task 2.
- A source may import a target scope only when it lists that scope in `depends_on`, and only modules matching that target's `public` patterns may cross the boundary. Pin in Task 3.
- `--changed`, warm caches, and `arbor-annotations` opt-out must not leak a ring or scope assignment from one scope into another. Pin in Task 2 and Task 3.

---

### Task 1: Scope policy model and configuration

**Files:**
- Modify: `src/arborescent/domain.py`
- Modify: `src/arborescent/config.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Add `ArchitectureScope(name: str, policy: Policy, public: tuple[str, ...], depends_on: tuple[str, ...])`.
- Add `Policy.scopes: tuple[ArchitectureScope, ...] = ()`, `Policy.scope(name: str) -> ArchitectureScope`, and `Policy.scope_for_path(path: str) -> ArchitectureScope | None`.
- In multi-scope mode, each child `Policy` owns `sources`, `source_roots`, `rings`, `classify`, `[ring.*]`, `preset`, `io_modules`, `groups`, `module_forbid_imports`, `entry_scripts`, `dependencies`, `forbid_everywhere`, and `forbid_circular_imports`.
- Keep `python`, `type_checkers`, `disabled_tools`, `output_dir`, `baseline`, `suggestion_threshold`, Ruff overrides, `practices`, and `exclude` at the root.
- Read scopes from `[tool.arborescent.scopes.<name>]` (or top-level `scopes` in `arborescent.toml`). Each scope table requires non-empty `sources`, `source_roots`, and `rings`; `public` and `depends_on` are string lists and may be empty. `classify` and `[ring.<name>]` retain their current shapes inside the scope table.
- Reject mixing root-level legacy architecture keys with `scopes`; preserve the legacy single-policy form unchanged.
- Scope source paths must not overlap, scope names and dependency names must be unique/known, and the scope dependency graph must be acyclic.

- [ ] **Step 1: Write tests** named `test_scope_policy_parses_local_rings_and_public_boundary`, `test_scopes_can_reuse_ring_names`, `test_scope_config_rejects_legacy_architecture_keys`, `test_scope_config_rejects_overlapping_sources`, `test_scope_config_rejects_unknown_or_cyclic_dependencies`, and `test_legacy_policy_parses_unchanged`.
- [ ] **Step 2: Run the tests and confirm the new scope cases fail while the legacy case passes.**
- [ ] **Step 3: Implement `ArchitectureScope`, scope validation, and scope-table parsing with the exact ownership split above.**
- [ ] **Step 4: Run `uv run pytest -p no:tach tests/test_policy.py -q`.**
- [ ] **Step 5: Commit the policy model and parser changes.**

### Task 2: Scope-aware scanning and ring resolution

**Files:**
- Modify: `src/arborescent/scanner.py`
- Modify: `src/arborescent/ports.py`
- Test: `tests/test_scanner.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Add `ScanData.scope_by_module: dict[str, str]` and matching `ScanResult.scope_by_module`.
- The scanner matches source paths against exactly one scope, derives module names using that scope's `source_roots`, and records the owner for every discovered module.
- Add `Policy.resolve_scope_ring(scope_name: str, arbor_index: Mapping[str, tuple[str | None, str | None]], module: str) -> str | None` and `Policy.scope_annotation_violation(scope_name: str, arbor_index: Mapping[str, tuple[str | None, str | None]], module: str) -> str | None`; both delegate to the selected child policy. Existing `resolve_ring` and `annotation_violation` remain the one-scope compatibility API.
- Keep the annotation index complete on full and partial scans; the scope index is likewise complete even when `only` limits enforcement modules.

- [ ] **Step 1: Write tests** named `test_scanner_assigns_each_module_to_its_source_scope`, `test_unowned_module_is_unclassified`, `test_overlapping_scope_ownership_fails`, `test_duplicate_module_names_across_scopes_fail`, `test_scope_annotation_ring_is_validated_locally`, and `test_partial_scope_indexes_match_full_scan`.
- [ ] **Step 2: Run the tests and confirm the new scope cases fail.**
- [ ] **Step 3: Implement scope ownership, module naming, and scope indexes while preserving legacy scan behavior.**
- [ ] **Step 4: Run `uv run pytest -p no:tach tests/test_scanner.py tests/test_engine.py -q`.**
- [ ] **Step 5: Commit scanner and port changes.**

### Task 3: Native ring and cross-scope enforcement

**Files:**
- Modify: `src/arborescent/rules.py`
- Modify: `src/arborescent/engine.py`
- Modify: `src/arborescent/domain.py`
- Test: `tests/test_rules.py`
- Test: `tests/test_effect_policy.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Add `Rule.SCOPE_IMPORT = "scope-import"` for both undeclared scope dependencies and imports outside a target's public patterns.
- Extend `check_modules` to receive `scope_by_module` and use the source module's child policy for same-scope ring, external, effect, and module-forbid rules.
- Add a pure cross-scope check that receives `scopes`, `scope_by_module`, and `modules`; it reports `scope-import` unless the source scope lists the target scope and the target module matches the target's `public` patterns.
- Keep module-cycle checks within each scope and reject scope dependency cycles during policy validation.

- [ ] **Step 1: Write tests** named `test_same_scope_rings_are_independent`, `test_cross_scope_import_requires_declared_dependency`, `test_cross_scope_import_requires_public_target`, `test_public_cross_scope_import_is_allowed`, `test_same_name_rings_resolve_in_the_source_scope`, and `test_annotation_findings_are_scope_local`.
- [ ] **Step 2: Run the tests and confirm each boundary assertion fails under the current single-policy behavior.**
- [ ] **Step 3: Implement scope-local rules and the `scope-import` boundary check.**
- [ ] **Step 4: Run `uv run pytest -p no:tach tests/test_rules.py tests/test_effect_policy.py tests/test_engine.py -q`.**
- [ ] **Step 5: Commit the native enforcement changes.**

### Task 4: Compose generated tool configs and certification

**Files:**
- Modify: `src/arborescent/generate.py`
- Modify: `src/arborescent/engine.py`
- Modify: `src/arborescent/certification.py`
- Modify: `src/arborescent/typecheckers.py` if the generated include paths require it
- Test: `tests/test_generate.py`
- Test: `tests/test_engine.py`
- Test: `tests/test_certification.py`

**Interfaces:**
- Generated output remains one root-level config per tool. Tach layer names are namespaced as `<scope>__<ring>`; module `depends_on` entries include permitted inner modules and only declared public cross-scope modules.
- Import-linter contracts group modules by owning scope and local ring, and add forbidden targets for undeclared/private cross-scope imports.
- Ruff composition ignores use each scope's outermost ring.
- Type checker includes cover the union of scope sources; root `output_dir` remains authoritative.
- Profile canaries run for every scope with at least one module in the required ring position; disabling `arbor-annotations` remains a root-wide profile failure.

- [ ] **Step 1: Write tests** named `test_tach_uses_namespaced_scope_layers`, `test_import_linter_blocks_private_cross_scope_targets`, `test_ruff_composition_ignore_is_scope_local`, `test_type_checker_includes_union_of_scope_sources`, and `test_profile_canaries_cover_each_scope`.
- [ ] **Step 2: Run the tests and confirm generated output is still single-policy-only.**
- [ ] **Step 3: Implement scope composition for generated configs and certification.**
- [ ] **Step 4: Assert generated output for the existing one-scope repository remains byte-for-byte identical.**
- [ ] **Step 5: Run `uv run pytest -p no:tach tests/test_generate.py tests/test_engine.py tests/test_certification.py -q`.**
- [ ] **Step 6: Commit generated-config and certification changes.**

### Task 5: Update remaining consumers and commands

**Files:**
- Modify: `src/arborescent/practices.py`
- Modify: `src/arborescent/selftest.py`
- Modify: `src/arborescent/cli.py`
- Modify: `src/arborescent/bench.py`
- Test: `tests/test_practices.py`
- Test: `tests/test_selftest.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- `docs_sync` compares each documented module against its owning scope's effective ring.
- `selftest` selects mutation hosts and forbidden targets from the same scope; it also probes cross-scope public-boundary rules.
- `discover` groups unclassified modules by owning scope and suggests that scope's outermost ring.
- `bench` reads one root policy and benchmarks the composed scope set.

- [ ] **Step 1: Write tests** named `test_docs_sync_uses_each_scope_ring`, `test_selftest_covers_cross_scope_boundary`, `test_discover_groups_modules_by_scope`, and `test_bench_accepts_scoped_policy`.
- [ ] **Step 2: Run these tests and confirm each fails at its current single-policy consumer.**
- [ ] **Step 3: Implement scope-aware practices, self-tests, discover, and bench.**
- [ ] **Step 4: Run `uv run pytest -p no:tach tests/test_practices.py tests/test_selftest.py tests/test_engine.py -q`.**
- [ ] **Step 5: Commit remaining consumer changes.**

### Task 6: Documentation and compatibility dogfood

**Files:**
- Modify: `README.md`
- Modify: `docs/configuration.md`
- Modify: `docs/architecture.md`
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml` only if the repository's own policy needs a scoped dogfood config
- Test: `tests/test_policy.py`, `tests/test_engine.py`, and the full suite

**Interfaces:**
- Document `[tool.arborescent.scopes.<name>]`, its local `rings`/`classify`/`sources`, `public`, `depends_on`, root-global settings, ownership validation, and the compatibility rule that legacy config means one implicit scope.
- Include a two-library example with a shared binary-parser scope; both libraries use the shared scope through its public module, while their core/domain rings remain independent.
- State that shared metadata does not duplicate source files and that extraction is optional.

- [ ] **Step 1: Add the README and configuration examples, then update the architecture table for scope-qualified module ownership.**
- [ ] **Step 2: Add a changelog entry.**
- [ ] **Step 3: Run the full suite with `uv run pytest -p no:tach`.**
- [ ] **Step 4: Run `uv run ty check src/arborescent` and `uv run arborescent check --no-tools`.**
- [ ] **Step 5: Review generated configs and docs for a one-scope project and a three-scope project; confirm one-scope output is unchanged.**
- [ ] **Step 6: Commit documentation and compatibility coverage.**
