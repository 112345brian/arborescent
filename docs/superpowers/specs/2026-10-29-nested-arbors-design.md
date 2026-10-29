# Nested Arborescent Certification

## Intent

Let a vendored package keep its own Arborescent policy while making the parent repository's certification include that package. This preserves package ownership: child source is governed by the child's rings, config, and baseline, and the parent cannot claim certification while a declared child fails.

## Goals

- A parent policy explicitly declares nested package roots.
- Each child root has its own direct `arborescent.toml` or `pyproject.toml` policy.
- Parent Arborescent scanning excludes child source from the parent's own policy.
- Unlisted packages and source directories remain integrated into the parent's existing single policy or named scopes.
- `check` recursively checks each child and aggregates its result. `CERTIFIED` requires the parent's minimum profile and every child to certify.
- Children may declare their own children; the tree is finite and paths cannot escape the parent root.

## Non-goals

- Automatically discovering package roots or nested Git repositories.
- Rewriting Tach, Ruff, or type-checker configuration. Native tool configs remain project-owned; parent configs must exclude child paths when those tools should only govern parent source.
- Treating separately installed dependencies as children. They are checked in their own repositories.
- Sharing child baselines or flattening child policies into the parent's rings.

## Approaches

1. **Merge child policies into parent scopes.** Rejected because children would lose their independent config and baseline.
2. **Automatically discover every nested Git repository or `arborescent.toml`.** Rejected because discovery makes certification depend on incidental directory contents and can include unrelated fixtures.
3. **Explicit child roots with direct policies and recursive checks.** Selected. The parent declares its architectural children; each child remains the owner of its policy and recursively reports its own result.

## Architecture

`Policy` contains immutable child-root values. Configuration validates that each path is relative, unique, and non-overlapping. At runtime, Arborescent verifies that each child directory has a policy file directly inside it; it must not fall back to the parent's policy. The parent scanner skips declared child paths. The check use case invokes the same Arborescent installation from each child root, passes through tool and profile requirements, and records a child result alongside the parent's engines. Child baselines and generated configs remain rooted at the child.

A parent `CONSISTENT` result requires the parent's configured checks and all child checks to pass. A parent `CERTIFIED` result additionally requires the parent and all children to pass the requested minimum profile. A missing or malformed child policy is a failing child result.

## Constraints

- Legacy single-policy and current multi-scope configurations keep their behavior when `children` is absent.
- Child roots are relative to the containing policy root and cannot be `.` or contain `..`.
- The parent policy file must be outside every declared child root.
- Each child owns a separate baseline; no parent operation writes child state.
- Native engines remain independent adapters/configs. Documentation must state the parent-side exclusion requirement for child trees.

## Open implementation details

Use an explicit `children = ["packages/collection_core"]` list in the root policy. Child config discovery is direct-only: prefer `arborescent.toml`, otherwise accept a `pyproject.toml` that contains `[tool.arborescent]`. Parent output identifies each child path and whether it passed. Parent `--require-profile` is propagated to each child; ordinary checks require each child's ordinary check to pass.
