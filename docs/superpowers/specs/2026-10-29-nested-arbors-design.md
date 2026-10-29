# Nested Arborescent Certification

## Intent

Let a vendored package keep its own Arborescent policy when the parent chooses, while also supporting a parent that deliberately overrides child policies and governs an inclusive source tree.

## Goals

- A parent policy explicitly lists candidate nested package roots.
- `child_policy_mode` lets the parent either certify direct child policies or override them with the parent policy.
- A listed root without its own direct Arborescent policy remains integrated into the parent scan.
- `scan_vendors` lets the parent include convention-named vendored folders in its own scan.
- `check` recursively checks each independently governed child and aggregates its result.
- Children may declare their own children; the tree is finite and paths cannot escape the parent root.

## Non-goals

- Automatically discovering package roots or nested Git repositories.
- Rewriting Tach, Ruff, or type-checker configuration. Native tool configs remain project-owned; parent configs must exclude child paths when those tools should only govern parent source.
- Treating separately installed dependencies as children. They are checked in their own repositories.
- Sharing child baselines or flattening child policies into the parent's rings.

## Approaches

1. **Merge child policies into parent scopes.** Rejected because children would lose their independent config and baseline.
2. **Automatically discover every nested Git repository or `arborescent.toml`.** Rejected because discovery makes certification depend on incidental directory contents and can include unrelated fixtures.
3. **Explicit child roots with configurable ownership.** Selected. The parent lists candidate roots, then configures whether direct child policies are independently certified or overridden by the parent.

## Architecture

`Policy` contains immutable child-root values, a child policy mode, and a vendor scanning flag. Configuration validates that each child path is relative, unique, and non-overlapping. In `certify` mode, a direct child policy makes that package independent: the parent scanner skips it and the check use case invokes Arborescent from that child root, passing tool and profile requirements. A path without a direct policy remains parent-owned. In `parent` mode, the parent scans listed roots even when they have their own config. Vendored-folder scanning is configurable. Child baselines and generated configs remain rooted at independently certified children.

A parent `CONSISTENT` result requires the parent's configured checks and all independently certified child checks to pass. A parent `CERTIFIED` result additionally requires the parent and those children to pass the requested minimum profile. A listed root without a child policy is governed by the parent.

## Constraints

- Legacy single-policy and current multi-scope configurations keep their behavior when `children` is absent.
- Child roots are relative to the containing policy root and cannot be `.` or contain `..`.
- The parent policy file must be outside every declared child root.
- Each child owns a separate baseline; no parent operation writes child state.
- Native engines remain independent adapters/configs. Documentation must state the parent-side exclusion requirement for child trees.

## Open implementation details

Use an explicit `children = ["packages/collection_core"]` list in the root policy. Child config discovery is direct-only: prefer `arborescent.toml`, otherwise accept a `pyproject.toml` that contains `[tool.arborescent]`. Parent output identifies each child path and whether it passed. Parent `--require-profile` is propagated to each child; ordinary checks require each child's ordinary check to pass.
