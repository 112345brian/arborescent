# `require_arbor_annotations`: a Ratchet Toward Annotations as Source of Truth

## Intent

`__arbor__` annotations already take precedence over `classify` wherever both are present (resolution order in
`docs/configuration.md`, "Module ring annotations"). But a project can leave every module unannotated forever and
`check` will never complain: `classify` is a silent, permanent coequal path to a ring. There is no way to say "this
project (or this part of it) has finished its migration; an unannotated module is a bug, not a fallback."

This adds an opt-in policy flag that turns that silence into a hard failure, using the existing `annotate` command
(#35, already shipped) as the migration tool. `classify` itself is not deprecated or weakened for anyone who does not
opt in — this follows the decision already recorded in
`docs/superpowers/specs/2026-10-29-in-file-metadata-design.md` ("Open questions", decision 2): `classify` is kept
indefinitely, and a strict profile adds an opt-in requirement on top of it. This is that opt-in, named
`require_arbor_annotations` to match the existing `__arbor__`/annotation vocabulary rather than introduce a second one.

## Goals

- A project (or one scope of a multi-scope project) can require that every module it classifies carries a valid
  `__arbor__` annotation; a module resolved only through `classify` becomes a new violation, `unannotated-module`.
- The flag is additive: default `false`, so every existing classify-only project is unaffected until it opts in.
- The flag is independently settable per scope, like `forbid_circular_imports` — a repository can require annotations
  in a newly-written library while a legacy one keeps the old behavior.
- A vendored or otherwise un-annotatable subtree within a single scope can be exempted by module-name glob
  (`require_arbor_annotations_exclude`), without forcing it into its own formal scope.
- `arborescent annotate` is the migration path: run it to backfill annotations from `classify`, then turn the flag on
  once `discover`/`check` show nothing left unannotated.
- `disabled_tools = ["arbor-annotations"]` and `require_arbor_annotations = true` together is a contradiction (you
  cannot require what you have turned off) and is rejected as a policy error, not silently resolved one way.

## Non-goals

- Changing resolution order or making `__arbor__` win in some new way it doesn't already. The flag only decides
  whether the existing fallback is *allowed to be used*, not which source wins.
- Deprecating or removing `classify`. It remains required infrastructure (every module still needs a ring from
  somewhere) and the only path for projects that never opt in.
- A "require annotations everywhere, forever" default. That is a much larger behavioral change this task explicitly
  does not make (confirmed with the user: ratchet flag only, no default-precedence change).
- Deriving `classify` from another tool's config (e.g. a `classify_from = "tach.toml"` drift check). Raised as
  real, separate pain during review; it is a different feature and out of scope here.
- A full scope split for projects that only need to exempt one vendored subtree. `require_arbor_annotations_exclude`
  solves that directly; multi-scope remains for projects that already want independent ring ladders per library.

## Design

### Policy shape

Two new `Policy` fields, both evaluated per scope (root policy in single-scope mode, each scope's own policy in
multi-scope mode) — the same positioning as `forbid_circular_imports` and `classify` themselves:

- `require_arbor_annotations: bool = False`
- `require_arbor_annotations_exclude: tuple[str, ...] = ()` — `classify`-style globs on the dotted module name;
  matching modules are exempt from the hard failure even when the flag is on (their ring still resolves normally,
  annotated or not).

`Policy.__post_init__` rejects `require_arbor_annotations=True` combined with `"arbor-annotations"` in
`disabled_tools`.

### Resolution

A new `Policy.annotation_required_violation(arbor_index, name) -> str | None`, parallel to the existing
`annotation_violation`/`annotation_ring_mismatch`:

1. `None` if the flag is off, the module matches an exclude glob, or `classify` has no ring for it at all (that case
   is `unclassified`'s job, not this rule's — firing both would be redundant noise for the same root cause).
2. `None` if the module has a *valid* annotation (same validity test `resolve_ring` already uses: no violation, and
   the ring is one of the policy's own rings).
3. Otherwise, a detail string: the module falls back to `classify` while the policy requires an annotation.

`RuleContext` (rules.py) gets the matching `annotation_required_violation(name)`, resolving through the module's
owning scope exactly like `annotation_violation` already does. `check_modules` emits `Rule.UNANNOTATED_MODULE` for it,
after the existing `invalid-arbor-annotation` / `arbor-ring-mismatch` / `unclassified` checks for that module and only
when a ring was actually resolved (so an unclassified module is reported once, as `unclassified`, not twice).

### Why not fold this into `annotation_violation`

`annotation_violation` answers "is the annotation that *is* present malformed?" — it is `None` for a module with no
annotation at all, by design (tested in `test_policy.py`). `require_arbor_annotations` answers a different question —
"is *no annotation* itself the problem?" — that only applies under an opt-in policy. Keeping them separate means a
project that turns `require_arbor_annotations` off still gets every existing annotation-quality check unchanged, and
the baseline-stable rule id for "missing" is distinct from the one for "malformed"; conflating them would make the
ratchet's failures indistinguishable from pre-existing annotation bugs in baseline entries.

### Self-test coverage

Arborescent's own policy does not enable `require_arbor_annotations` (its own source is not fully annotated yet —
that is a separate migration, not part of this change). `selftest.plan` only generates the `unannotated-module`
mutation when the local policy under test has the flag on, mirroring how `entry_scripts` mutations are conditional
on `policy.entry_scripts` being non-empty. The mutation overwrites a module that currently carries a *valid*
annotation with an invalid one (`__arbor__ = {"bogus": 1}`); appending text to an existing file can only ever add
an `__arbor__` assignment, never remove one, and the last unconditional assignment wins (`facts.py`), so this is the
only way to make a module *newly* lose a valid annotation within the append-only harness. A classify-only host
would not work: under the flag it already violates the rule in the unmutated baseline, so there would be nothing
"new" for the mutation to introduce.

Because the rule is conditional and off by default, it is excluded from the blanket
"every rule needs a selftest mutation for arborescent's own policy" check the same way
`undeclared-entry-dependency` is (`tests/test_rule_ids.py`): a dedicated test builds a policy with the flag on and
asserts the mutation fires, rather than adding a permanent `NO_MUTATION` excuse that would hide a real regression if
someone later enables the flag on a policy that *can* exercise it.

### Docs and migration story

`docs/configuration.md`'s "Module ring annotations: `__arbor__`" section gains a "Requiring annotations" subsection:
what the flag does, the exclude list, the `disabled_tools` conflict, and the three-step migration (`annotate --yes`,
confirm `discover`/`check` show no classify-only modules left, turn the flag on). `docs/architecture.md`'s rule table
gains the new id.

## Constraints

- Default-off; no existing policy's behavior changes without an explicit `require_arbor_annotations = true`.
- Per-scope, like the other architecture settings it sits beside in the config keys list.
- Rejects the `disabled_tools = ["arbor-annotations"]` combination at policy-construction time, not at check time.
- `require_arbor_annotations_exclude` only ever *removes* a finding; it cannot excuse a module that `classify` itself
  cannot assign a ring to (`unclassified` is unaffected by it).
