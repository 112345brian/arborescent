# In-File Architecture Metadata Design

Status: proposal, revised after adversarial review. Nothing here is implemented except `__arbor__ = {"ring": ...}` (#24). Decisions are recorded under Open questions; two remain open.

## Intent

Architectural intent should live in the file it describes, so an edit to that file is the only way to change what it claims. Today meaning is keyed by location: `classify` globs match dotted names, `packages.toml` lists repository paths, and the extraction manifest records both. A move, rename or extraction changes the key, and meaning changes with it without any signal.

In-file metadata removes that one failure mode — it cannot drift from *location*, because it travels with the file. It does not stop drift from *behaviour* (the code changes under an unchanged claim) or from *other metadata* (a copy-paste carries a stale claim into a new directory; effect tags are easy to add and easy to forget to remove, so they tend to only grow). Those driftscan only be caught by verification — checking what the file does against what it claims — not by relocating where the claim lives. The value here is declared intent plus verification, not a drift-proof guarantee.

## Principle

- **In-file metadata is the source of truth for a module's own claims.** Everything placement-derived (package index, extraction manifest, semantic digest) is derived by scanning and regenerated, never hand-edited.
- **Claims are verified, not trusted.** A tag is a claim. The checker verifies what it can (imports against ring, detected effects against declared effects) and the host policy has the final say.
- **Location is provenance, not meaning.** Path, repository and destination are recorded as placement. They never carry semantics.

## Layers

| Layer | Holds | Where | Location-dependent |
|---|---|---|---|
| Intrinsic claims | ring, `public`, role, effects | in the file | no |
| Package facts | package id, kind, capabilities, `depends_on`, `vendors` | the package's own policy file (see Open questions) | no |
| Host verdict | ring definitions, which claims vendored code may make, overrides | the consuming repository's policy | no |
| Placement | repository, path, destination, origin revision, extraction status | derived manifest | yes |

Resolution order extends the existing one: in-file annotation, then `classify`, then host override. An invalid annotation never silently falls back and never erases a module's other checks (the #18 rule). `classify` stays indefinitely as a parallel, lower-fidelity source, not a deprecated migration aid (see Open questions, decision 2): some hosts will never annotate every file, and a strict profile can require annotations without removing `classify` for everyone else.

### Host-verdict table

A claim's outcome depends on who is making it, not just what it says:

| Claim origin | Verified against code | Host allows the claim | Outcome |
|---|---|---|---|
| In-file annotation | yes | yes | accepted |
| In-file annotation | yes | no | **error** — claim is true but the host forbids it (e.g. ring not permitted for this path) |
| In-file annotation | no (contradicted) | — | **error** — undeclared effect / ring violation, regardless of host policy |
| In-file annotation | unverifiable (e.g. dynamic/reflective call) | yes | warning, not accepted silently |
| `classify` fallback | n/a (classify is not verified against content) | yes | accepted, lower confidence |
| `classify` fallback | n/a | no | **error** |
| Missing (strict profile) | n/a | n/a | **error** — annotation required |

A host can always forbid a claim; it can never make a verified-false claim true.

## Schema (proposed)

One language-independent payload, different carriers:

```python
__arbor__ = {"ring": "domain", "public": True, "role": "source", "effects": ["clock"]}
```

- `ring` exists today. `public`, `role` and `effects` are proposed. `effects` values are drawn from the nine categories Arborescent already detects: `clock`, `env`, `identity`, `randomness`, `sleep`, `threads`, `process`, `dynamic-import`, `patch` (`src/arborescent/domain.py`). There is no `io` category; a blanket "io" tag cannot be verified against any single detector and is rejected.
- A file-local `public` flag survives moves, unlike central `public` globs.
- `__arbor__` must reject unknown keys and must not be read from inside conditional code (an assignment inside an `if` is invisible to the scanner today and must be flagged as a malformed annotation, not silently ignored). Both are open blind spots in the current `classify`-era scanner and are schema requirements for this design, not nice-to-haves.
- Carriers beyond Python are deferred (#31): a `# /// arbor` comment block for formats with comments (generalising the PEP 723 convention already parsed in `facts.py`), front matter for Markdown, and a sidecar for formats without comments. Sidecars can orphan, so they need an orphan lint. Generated files must emit their own metadata or be excluded explicitly.

## Identity

- A unit is a module name. The scanner already enforces **globally unique module names** across the whole scan, including across scopes (`scanner.py`, `duplicate module name` check, ~line 74) — this predates this design and is not something this design introduces.
- `(package id, module name)` as proposed identity is not additive: it is a rewrite of scanner, rules, generators and baseline keys, all of which are keyed by `Violation.key = "rule|subject|detail"` with a single `subject`, not a `(package, subject)` pair. This design does not propose that rewrite. Identity stays module-name-global for now (see Open questions, decision 4): two packages vendoring copies of the same module name remain a uniqueness error, not two distinct units.
- Vendoring and depending are different relationships. A shared dependency is one identity and one copy; a vendored copy is private and must sit under a namespace the owning package controls (so its module name does not collide globally). The descriptor states which, using a declared `vendors = [...]`.
- Distinct for enforcement does not mean unrelated for audit: a provenance fingerprint (origin id, revision, content digest) lets a report show that two vendored copies share an origin and have diverged, without changing how uniqueness is enforced.

## Closure and effect reachability (one engine, two queries)

A reachability walk over the import graph, with tags as labels:

1. **Extractability closure.** Every import of a package must resolve to itself, to the public surface of a declared dependency, or to declared third-party dependencies. The package concept here is a **nested arbor** (`children` / `child_policy_mode`, see "Scopes, packages and nested arbors" below), not a new index — the closure check walks a nested arbor's declared boundary. Run in CI, a package stays extractable continuously instead of discovering the problem at extraction time.
2. **Indirect effects.** A file tagged `effects: ["clock"]` and any ring that forbids that effect cannot reach it through imports it controls. Findings carry the path as evidence, labelled with the effect category and the edge kind that carried it (`direct import`, `re-export`, `parameter injection`): `domain.rules -[import]-> util.time -[import]-> adapters.clock [clock]`. This catches project-defined wrappers that launder effects the stdlib category table does not know about.

Verification loop: an effect detected in a file but not declared is an "undeclared effect" violation; over-declaring is allowed.

Limits, stated so the tool does not over-promise (the second group below is reasoned, not independently re-verified against the code, and should be re-checked before being relied on):

- Module-level reachability over-approximates (reachable is not used); function-level call graphs under-approximate in Python (methods on objects, callbacks, `getattr`). Direct and definite findings are errors; transitive findings are warnings with the path, until the false-positive rate is known. Transitive findings need a baseline key that is stable across refactors of the *path* between source and sink — a key that embeds the full path breaks on every intermediate rename, so the key must be `(rule, effect, sink)` or similar, excluding the path, with the path kept only as evidence in the report.
- Module-level reachability likely misses two shapes that need explicit handling, not just a caveat: implicit imports through a parent `__init__.py` (a forbidden effect reachable via the package's own `__init__` re-exporting it), and re-export definition sites (the effect's origin is not where it is finally imported from). Both need a test before stage 2 is considered done.
- An incremental scan (`--changed`) cannot locally decide a graph-wide property: a cycle, or whether a changed file newly closes an open reachability path, can depend on files outside the changed set. `--changed` already has this limitation for existing graph-wide rules; this design does not fix it, but the engine must compute a complete edge+effect index on a full scan and treat `--changed` as a filter over findings, not a redefinition of the graph, so results stay consistent between incremental and full runs.
- Dependency injection is intentional: inner code receives an effectful function as a parameter. A `boundary: "port"` tag marks a permitted crossing so this is not flagged. This tag is cut from stage 1-2 (see Staging) until the base engine ships.
- Vendored private types should not appear in public signatures. Static detection is heuristic, so this is a warning.
- Every proposed rule needs a mutation probe in `selftest`. The current selftest harness can only append synthetic violations to existing text, not synthesize new control flow; several of the rules above (conditional-assignment detection, re-export tracing) cannot be mutation-tested without extending the harness first. That extension is in scope for stage 1, not assumed.

## Rules (candidate)

1. A duplicate module name across the scan is an error (existing behaviour, restated — not new).
2. `__arbor__` with an unknown key, or assigned inside conditional code, is a malformed-annotation error.
3. A top-level import name shared across packages without a private namespace is an error.
4. Same-origin vendored copies that have diverged are reported.
5. A private vendored type in a public signature is a warning.
6. An effect detected but undeclared is an error.
7. An orphaned sidecar, or conflicting package claims on one file, is an error.

Each rule needs a mutation probe in `selftest` (extending the harness where noted above) and a test that fails if the rule is removed.

## Scopes, packages and nested arbors

Scopes and `packages.toml` are not two names for one concept, and this design does not merge them:

- A **scope** is an enforcement unit: which ring ladder and rules apply to a set of files.
- A **package** (`packages.toml`) is an extraction/ownership unit: what ships together, including docs and tests that a scope does not govern.
- A **nested arbor** (`children`, `child_policy_mode`, already implemented — see `docs/superpowers/specs/2026-10-29-nested-arbors-design.md`) is "a package owns its architecture": a child root with its own independently certified policy, or one the parent governs directly.

The extractability closure check in this design is a nested-arbor concept: a package that wants continuous closure checking declares itself as a nested arbor (or a `children` entry under `certify` mode) rather than this design inventing a second package index. `packages.toml` remains the extraction/placement record (layer: Placement); it does not become the scope, and scopes do not become it.

## Staging

1. Harden `__arbor__`: reject unknown keys, reject conditional assignment, report annotation-vs-`classify` disagreement. Extend the mutation-test harness to cover these. No new keys yet.
2. Extend the schema (`public`, `role`, `effects` on the real nine categories) with the no-blind-spot validation from stage 1 applied to the new keys.
3. Build the graph engine: extractability closure over nested arbors, plus file-level effect reachability and the undeclared-effect check, with stable (path-excluded) baseline keys and the `--changed` full-index contract above. Handle implicit parent-`__init__` imports and re-export sites explicitly, with tests.
4. Add the `port` crossing tag.
5. Add a strict profile requiring annotations.
6. Cross-package vendoring rules (3-5 above) and declared `vendors`.
7. Function-level tags — deferred indefinitely (see Open questions, decision 5); revisit only if file-level reachability proves insufficient in practice.

`packages.toml` is not touched by this staging; it continues to serve placement, unaffected by stages 1-6.

## Relationship to existing work

- #24: `__arbor__` ring annotations are stage zero of this design.
- #27 and the manifest v2 work: that manifest separates semantics, origin, location and state, but still derives semantics from catalog paths for package-level facts. This design moves intrinsic *file*-level claims into the file; package-level facts move to the package's own policy file (see Open questions), not to `__init__.py` or to the manifest.
- Nested arbors (`docs/superpowers/specs/2026-10-29-nested-arbors-design.md`): the existing mechanism this design's closure check builds on. See "Scopes, packages and nested arbors" above.
- #31: polyglot units and the meaning of "package". For now a package is a Python distribution-sized ownership unit; non-Python files are assets.

## Non-goals

- Arborescent does not perform history-preserving extraction, vendoring, import rewriting, graph analytics, repository creation or release, and does not plan a migration or emit tool-specific inputs (filter-repo, Copybara). Other tools do that; Arborescent supplies and verifies the declared intent those tools cannot derive (ownership, public surface, legitimate dependency direction), compiles it into engine contracts, and exports it as stable JSON. In scope: annotations, their completeness and consistency checks, the export. The existing `packages extract` (a path copy with no history) is frozen. See `2026-10-31-monorepo-split-annotations-design.md`.
- Multi-language ring enforcement. Carrying metadata in other file types is cheap; analysing their imports is a per-language commitment that is not proposed here.
- Content hashing of extracted packages. They are expected to evolve; the manifest is a provenance snapshot, not a seal.
- Merging scopes and `packages.toml` into one concept. They stay distinct (see "Scopes, packages and nested arbors").
- Rewriting identity to `(package id, module name)`. Module-name uniqueness stays global (see Open questions, decision 4).

## Open questions

Decided, based on the adversarial review:

1. **Package-level facts live in the package's own policy file** (`arborescent.toml` or a `[tool.arborescent]` table in `pyproject.toml`), not `__init__.py`. This keeps package facts next to the nested-arbor policy mechanism that already reads them, instead of mixing two meanings into one source file.
2. **`classify` is kept indefinitely**, not deprecated. A strict profile adds an opt-in `require_annotations` that rejects `classify`-only coverage for repos that want it; `classify` remains the default path for everyone else.
4. **Two versions of one vendored package are rejected for now.** The scanner keeps its global module-name-uniqueness error (see Identity); a vendored copy must be renamed into a private namespace rather than coexisting under the same module name.
5. **Function-level tagging is deferred indefinitely.** File-level reachability is believed sufficient; revisit only if its false-positive rate proves unworkable in practice (see Closure and effect reachability, Limits).

Still open:

3. **Who owns ring names: the package, with a host mapping on vendoring, or the host?** Leaning toward package-owned ladders with an explicit mapping (what scopes already provide), but this should not be decided from one example. Run an experiment with two real ring ladders from different packages before committing.
