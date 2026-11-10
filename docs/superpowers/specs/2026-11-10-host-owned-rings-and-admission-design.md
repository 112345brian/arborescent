# Host-Owned Rings, Package Constraints and an Admission Gate for New Modules

Status: design only. No implementation ships with this document. Refs #33 (resolves its decision 3).
Companion work: #136, #138 (re-export and forwarding laundering, PR #144), #137 (`TYPE_CHECKING` category), #139 (hand-edited ratchets).

## Intent

A ring answers "how much of the world may this module touch". Three questions are tangled together today:

1. **Who owns the ladder?** How many rungs exist, what each permits, and which rung a module sits on.
2. **What may a package say about itself?** Facts the scanner can compute must not be restated by hand; intent the scanner cannot compute (this package must stay pure) may be declared.
3. **How does a new module get its ring?** Today a ring can be reached by copying a `classify` glob result (`annotate`), by hand, or by a broad glob matching a file nobody looked at. Nothing asks whether the placement is justified or whether a tighter one fits.

This spec fixes the answers: the **host** owns the ladder and every placement; packages declare **constraints**, never position; and a **new** module is **admitted** into a ring by a check that is re-run in CI, so hand-annotating cannot bypass it.

## Audit: what the code does today

Findings from reading `annotate.py`, `classify_audit.py`, `facts.py` (`__arbor__` parsing), `domain.py` (`resolve_ring`), `rules.py` (`fits_ring`), `baseline.py`, `cli.py`, `package_*.py`, and the issues named above.

### Already matches the design

- **The ladder is already host-owned.** `__arbor__["ring"]` is validated against the *owning scope's* `policy.rings` (`Policy.annotation_violation`: "names unknown ring"). No package-level ladder, ring name or `__onion__` exists. Decision 3 in #33 was framed as an open experiment, but the implementation settled it in the host's favour; this spec records the decision and forbids reversing it by accident.
- **`__arbor__` carries no computable fact.** `_ARBOR_LEGAL_KEYS = {"ring", "role"}` (`facts.py:491`); unknown keys are `invalid-arbor-annotation`. `role` is validated against `dev_tools` paths, so it cannot disagree with where the file lives without being reported.
- **Extraction already treats rings as host-relative.** `package_preservation.py` compares each module's effective ring "under the part's own policy" with the origin's and reports a change. A ring name is never assumed portable.
- **Function-local and `TYPE_CHECKING` imports are already in the facts.** `RawImport` has `lazy` (inside a function) and `type_only` (under `if TYPE_CHECKING`); `fits_ring` iterates every import in `facts.imports`.
- **A read-only evidence view exists.** `discover --suggest` calls `classify_audit.suggest_ring` (#132) and prints the tightest ring with evidence.
- **A "could sit in an inner ring" signal exists** as `classify-facts` (`classify_audit._inner_ring`), information-tier.
- **A diff-against-ref pattern exists.** `check --growth-against REF` runs `git show REF:<baseline>` through the `runner` port and `baseline.growth_consistent` compares keys and the append-only `growth` log.

### Contradicts or differs from the design

1. **`annotate` does not read facts.** The design says `annotate` "launders current behaviour into intent". The code is narrower: `plan_annotations` copies the ring `classify` already resolves (`RuleContext.resolve_ring`) into the file, and skips modules `classify` does not match (`unmatched`). It never infers a ring from imports or effects. The laundering is real but different: a new module that a broad glob (`app.*: adapters`) happened to match is frozen into an explicit-looking annotation that reads as a deliberate placement and, under `require_arbor_annotations`, is the *required* path. Enforcement does not change when `annotate` runs. The gate must therefore also cover **glob-placed new modules**, not only annotated ones (see Decision 4).
2. **No per-module admission concept, and no "new since REF" notion.** Nothing records or recomputes why a module has its ring. `growth_consistent` guards baseline *keys*, not modules.
3. **`classify-facts` "inner ring" is a different test from "a tighter ring fits".** `_inner_ring` skips modules with no top-level defs, a `__main__` guard, abstract classes, or an entry-script entry before it asks `fits_ring`. A bare-import shim passes every inner ring's rules yet is exempted from the note. Admission must not inherit these exemptions blindly (see Stage 3).
4. **`fits_ring` judges outgoing edges only.** It asks whether the module's own imports, externals, effects and abstraction pass ring R. It does not ask whether the module's *importers* may import ring R, so "ring R may import only itself" closure problems (#133) surface later, in `check`, not at admission.
5. **Dynamic imports are an effect category, not an edge.** `importlib.import_module(...)` and `__import__` are recorded as `dynamic-import` effect uses (`facts.py:236`), not resolved to import targets. A function-level scan that only walks `facts.imports` would miss them.
6. **`TYPE_CHECKING` imports are treated like runtime imports by most rules** (#137, open). Admission must pick a side explicitly.
7. **No package-constraint concept.** `must_be_pure` and `forbid_effects` do not exist anywhere (`grep` finds neither). `packages.toml` carries ownership, roles (`source`, ...), pins and history policy, and the `PACKAGE_KEYS` allow-list rejects unknown keys.

### Behaviours that would change, and their callers

| Behaviour | Callers (grep) | Change |
|---|---|---|
| `annotate` writing a ring | `cli._annotate` (`cli.py:505`), `annotate.plan_annotations`, `annotate.apply_annotations`, `tests/test_annotate.py` (all of it), `docs/configuration.md` "Migrating" steps (about line 619), the `require_arbor_annotations` spec and `unannotated-module` message | Stage 2 |
| `classify_audit._inner_ring` | `engine.py:162` (`audit_classification`), `selftest` mutation probes for `classify-facts`, `tests/` for classify-facts | Stage 3 (shared helper; the note stays information-tier for existing modules) |
| `classify_audit.suggest_ring` | `cli._print_ring_suggestions` only | Stage 1 reuses it; signature must not change |
| `rules.fits_ring` | `classify_audit` (two sites) | Stage 1 extracts a strict variant; `fits_ring` itself must not change |
| `__arbor__` legal keys | `facts._ARBOR_LEGAL_KEYS`, `_arbor_fields`, `Policy._has_valid_annotation`, `annotation_violation`, `annotation_ring_mismatch`, `docs/configuration.md` | Stage 1 adds one key, `ring_reason` |
| `check` options | `cli.py` (parser, `Options`), `engine.Options.growth_against` / `engine.py:389` argv forwarding, `_growth_check` | Stage 3 adds `--admitted-against` |

Per the project's shared-code rule, each stage below starts with tests that pin each caller's current use of the behaviour it touches, then the edge inputs, then the change.

## Decisions

### 1. The host owns the ring ladder

The host repository's policy decides how many rungs exist, what each permits, and every module's ring. A ring name is relative to one repository's ladder: the same package can be `domain` in one repo and `core` in another, or sit one rung lower in a repo with a finer ladder.

Consequences:

- No package-level ring name, ladder, or "layer" key is ever added. This closes #33 decision 3 in favour of host-owned.
- `__arbor__["ring"]` on a module stays. It is the host's placement written next to the file, resolved against the *host's* ladder, not the package's claim about itself. It is therefore **not portable**: a vendored or extracted file whose `ring` names a rung the new host lacks reports `invalid-arbor-annotation` ("names unknown ring"). That is the correct failure (the new host has not placed the module) and extraction already re-resolves rings per part. Docs must say plainly that an in-file ring is host-local data stored in the file for convenience.
- For code the host does not control (vendored, third-party source in the scan), `classify` in host policy remains the placement mechanism; editing someone else's files to add `__arbor__` is not required.

### 2. No package-level layer or onion annotation

Facts the scanner can compute (does I/O, imports X, uses effect Y) are computed, never annotated. An annotation of a computable fact goes stale and disagrees with the code, and then the annotation is either ignored (dead metadata) or trusted (wrong enforcement). `__arbor__` and the package catalog gain no key that restates a computed fact.

The one new in-file key, `ring_reason` (Decision 4), is a human justification, not a fact; it can be stale but cannot contradict the code.

### 3. Optional package constraints

A package may declare **intent as constraints, not position**. Initial vocabulary:

- `must_be_pure = true`: no effect use in any category, no import of an external package outside the stdlib's pure/structural allow-list (see `docs/stdlib-table.md`), no import of a first-party module that is not itself declared pure or placed in a ring the host has marked effect-free.
- `forbid_effects = ["clock", "network", ...]`: no use of the named effect categories. Names are the scanner's existing categories (the nine from #33 plus `network`, `console`, `global`, database and filesystem as they land); an unknown name is a policy error, not ignored.

Where declared: in the **package's own policy file** (`arborescent.toml` / `pyproject.toml` at the package root, per #33 decision 1), under a `[package.constraints]` table. They are **separate from** `__arbor__` and from `packages.toml` identity, ownership, role and public metadata: the metadata describes what the package *is*, constraints describe what it *promises not to do*. Constraints apply to paths with role `source`; test, docs and asset paths are exempt.

Two independent checks, both new rules:

- **`package-constraint-facts`**: a module in the package uses a forbidden effect or import, whatever ring it is in. The constraint is enforced on facts, so a host cannot loosen it by placement.
- **`package-constraint-placement`**: the host's ring for a module permits something the constraint forbids (ring `adapters` permits `network`; the package says `forbid_effects = ["network"]`). The ring is looser than the declared intent, so the host must either tighten the placement or the package must drop the constraint. Failing here, before any module actually misbehaves, is the point: the ring is the host's guard rail, and a guard rail wider than the promise is a gap.

The host maps the package onto its rings through ordinary placement (`classify`, `__arbor__`), per module, per scope. In multi-scope mode each module is judged against its own scope's ladder; a package spanning scopes is judged module by module.

Constraints travel with the package (they live in its policy file), positions do not. A constraint is only checked where the package's source is part of the scan; a pip-installed dependency is out of scope.

### 4. An admission gate for new modules

`annotate` stops copying rings for new modules; new modules enter a ring through `admit`, and CI re-checks it.

**`arborescent admit <module> --ring R`**

- Scans the module's imports at **function level** (every `RawImport`, including function-local (`lazy`), `TYPE_CHECKING` (`type_only`, see #137) and dynamic imports).
  - `type_only` imports **count** for admission, even if `check` later learns to treat them as a separate category (#137). Admission is the strict entry bar; relaxing it is a per-edge decision, not a default.
  - Dynamic imports with a literal target (`importlib.import_module("pkg.x")`) are resolved and treated as edges. A dynamic import with a non-literal target is **unverifiable**: admission refuses unless the module carries `ring_reason` naming it, because a gate that cannot see an edge must not certify its absence.
- Refuses if the facts violate ring R (the same rule functions as `fits_ring`, applied to every import regardless of `lazy`/`type_only`).
- Refuses if a **tighter ring fits**, unless a reason is recorded. The reason is `__arbor__ = {"ring": "adapters", "ring_reason": "..."}` (non-blank string). It sits in the same diff as the module, is attributable by blame, and survives a rename. A reason with no tighter ring that fits is allowed but reported as information (stale reason).
- On success writes `__arbor__` (reusing `annotate.insert_annotation`) and prints the evidence it used (`classify_audit._evidence`).
- `--dry-run` is the default; `--yes` writes, matching `annotate`.
- `discover --suggest` stays the read-only evidence view and is unchanged.

**`arborescent check --admitted-against REF`**

A CI diff check, modelled on `--growth-against`:

- Computes the modules **new since REF**: `.py` files added by `git diff --name-status -M --diff-filter=A <merge-base(REF, HEAD)> HEAD`, mapped to module names with the current source roots. Reading is through the existing `runner` port. If the diff cannot be read (shallow clone, unknown ref), the check **fails loudly**, never passes.
- For each new module, **re-runs the admission procedure at HEAD** against the module's *current* ring and fails if admission would refuse it, or if the module has no explicit `__arbor__` ring (a ring that comes only from a `classify` glob does not count as admitted). Because the check recomputes instead of trusting a record, hand-annotating cannot bypass it: the annotation is accepted only if it passes. Contrast with `baseline.growth_consistent`, which can only compare two files that are both editable; this check is stateless apart from the human `ring_reason`.
- The `classify-facts` "could sit in an inner ring" note becomes a **failure for new modules only** (rule `admission-looser-than-needed`), unless `ring_reason` is present. Existing modules keep the information-tier note.
- Renames: with `-M`, a module whose content similarity keeps it a rename is not new. A moved module that falls below the similarity threshold is new and is admitted afresh. Both outcomes are documented.

**Existing modules are not forced through admission.** The shrink-only baseline carries the legacy unclassified and looser-than-needed debt (`baseline` / `--adopt`). `--admitted-against` looks only at modules new since REF, so adopting it creates no backlog.

### 5. Per-module admission does not stop cross-module laundering

Admission judges one module against one ring. A new pass-through module, a re-export block or a forwarding function can satisfy every per-module rule while leaving the real dependency unchanged (#136, #138, PR #144). Those are companion work with their own detectors; this design does not claim to subsume them and adds no heuristic for them. A reviewer should read "admitted" as "this module's own facts fit this ring", not "this module's contribution to the architecture is sound".

## Non-goals

- Package-level ring names, ladders, `__onion__`, or any annotation of a computable fact.
- Forcing existing modules through admission.
- Detecting laundering across modules (#136, #138).
- Making `check` treat `TYPE_CHECKING` imports as a separate category (#137). Admission only fixes its own strict stance.
- Checking constraints on packages whose source is not in the scan.
- Making admission a cryptographic or tamper-proof record. The gate recomputes in CI; it does not defend against a PR that also weakens the policy (that is the hole #139 addresses for ratchets, and the same remedy, a protected-branch CI job, applies).
- Changing `fits_ring`, `suggest_ring` or `discover --suggest` behaviour.

## Migration impact on existing `__arbor__` annotations

- Every existing `__arbor__ = {"ring": ...}` stays valid and is not re-validated by `admit`. `check` behaves as today (`annotation_ring_mismatch`, `unknown ring`, `unannotated-module`).
- `ring_reason` is a new legal key. Because unknown keys are an error (`_ARBOR_LEGAL_KEYS`), **a tool older than Stage 1 reports `invalid-arbor-annotation` on a file using `ring_reason`**. Mitigation: Stage 1 ships and is released before `admit` writes the key; document the minimum version.
- `annotate` changes (Stage 2): it no longer writes rings for modules new since a reference. Legacy modules that a human-written `classify` glob already places can still be migrated, behind an explicit flag, because that migration is how projects reach `require_arbor_annotations` and does not change enforcement. The output says so: the annotation records the policy's placement, not a validated one.
- Projects using `require_arbor_annotations` are not affected until they pass `--admitted-against`; that flag is the new-modules-only, cheaper rung of the same idea.

## Edge cases and failure modes

- **Tighter ring is a heuristic, not intent.** A new adapter that has no I/O yet fits the innermost ring and would be refused. `ring_reason` is the escape hatch; the failure message names the ring that fits and the evidence so the author can decide. If reasons become boilerplate, that is visible in review, not hidden.
- **Outgoing edges only.** `fits_ring` ignores importers, so admission can approve a ring that existing importers cannot reach (#133). Stage 1 reports importers that would violate ring R as a warning and `check` already fails on them; making it an admission refusal is deferred.
- **Mixed modules.** A module mixing pure functions and an effect wrapper fits only the outer ring; `suggest` already lists categories. Admission does not split modules.
- **`__init__.py` and re-exports.** A package `__init__` re-exporting submodules is admitted on its own imports; re-export laundering is #136.
- **New since REF, but REF is stale.** Using the tip of the target branch instead of the merge base would report unrelated modules as new. The check resolves the merge base and fails if it cannot.
- **Source roots changed between REF and HEAD.** Files are mapped with HEAD's roots; a moved root makes everything look renamed or new. Documented; not special-cased.
- **Excluded sets.** Admission applies to the rule's normal subject set only: not `exclude`d paths, vendor folders, `dev_tools` (their `role` placement is separate) or generated code.
- **`--changed` runs.** `--admitted-against` is diff-based, not changed-file-based, and runs on the full module set; the cache key must include REF.
- **Policy edited in the same PR.** A PR that adds a broad `classify` glob or loosens a ring passes admission on the loosened policy. Out of scope; protected-branch CI and #139.
- **Constraint vs. unknown effect names.** `forbid_effects = ["netwrok"]` must be a policy error (no blind spot), not a silent no-op.
- **`must_be_pure` and transitive purity.** Stage 5 checks direct imports and effect uses; whether an imported first-party module is pure uses the existing file-level reachability (#98 taint, #33 stage 3) and is limited to what that engine can see. Function-level tagging stays deferred (#33 decision 5).
- **Soundness limits.** The scanner cannot see builtins such as `open()` or `print()`, calls through objects or `getattr`, or what a function does with its arguments (same caveat as `classify-facts`). "Admitted" means "no recorded fact contradicts the ring", never "provably pure".

## Staged implementation plan

Each stage is independently shippable and test-first. Every stage begins by writing tests for each existing caller's use of what it touches (the callers are listed in the audit table), then the edge inputs (empty module, only-docstring module, blank `ring_reason`, NUL characters, non-literal dynamic import, a failure on the second `git` call), then the change. Each rule gets a `selftest` mutation probe and a test that fails if the rule is removed, and a scan test that fails if a caller discards the new result type (a refused or skipped admission outcome must not be silently ignorable).

1. **`admit`: strict function-level scan and `ring_reason`.** New `admit_check`-style core reused by `suggest_ring`; `ring_reason` accepted by `facts`/`domain`; `admit <module> --ring R` with dry-run default. No CI check yet. Depends on nothing.
2. **`annotate` stops laundering.** Refuse new modules, route them to `admit`; keep an explicit legacy-migration flag; update `docs/configuration.md` and `tests/test_annotate.py`. Depends on 1.
3. **`check --admitted-against REF`.** New-module detection through the `runner` port, recompute admission, failure on `classify`-only placement, `admission-looser-than-needed` for new modules only, shared helper with `classify_audit._inner_ring` (its exemptions documented). Depends on 1.
4. **Package constraints: schema and no-blind-spot validation.** `[package.constraints]` parsing with `must_be_pure`, `forbid_effects`; unknown keys and unknown effect names are errors; nothing enforced yet. Independent of 1-3.
5. **Package constraints: enforcement.** `package-constraint-facts` and `package-constraint-placement`, with mutation probes. Depends on 4.

Docs land with each stage, not after.

## Where the design may be unsound

Said plainly, for the owner:

- **`annotate` is not the laundering vector the brief describes.** It copies `classify`, which is already human-written policy. The real hole is glob-placed new modules; Decision 4 therefore requires an explicit `__arbor__` ring for new modules even when a glob matches. This is stricter than the brief and is the one place the design extends rather than follows it.
- **"Refuse if a tighter ring fits" will produce false refusals** for stub adapters and thin wrappers. The reason key makes it escapable, but its value depends on review discipline.
- **A recorded "validated" marker would be forgeable**, so the design deliberately stores none and recomputes in CI instead. The cost is that admission rules are re-evaluated against whatever the policy says at HEAD, so a same-PR policy change can bypass it (#139).
- **In-file `ring` is host-local data in files the host may not own.** It is fine for first-party code and breaks quietly on vendoring (loudly, via `invalid-arbor-annotation`). That is accepted, not solved.
