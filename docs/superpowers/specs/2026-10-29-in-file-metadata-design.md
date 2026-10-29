# In-File Architecture Metadata Design

Status: proposal. Nothing here is implemented except `__arbor__ = {"ring": ...}` (#24). Open questions are listed at the end.

## Intent

Architectural intent should live in the file it describes, so it cannot silently drift from the code. Today meaning is keyed by location: `classify` globs match dotted names, `packages.toml` lists repository paths, and the extraction manifest records both. A move, rename or extraction changes the key, and meaning changes with it without any signal. That is the failure Arborescent exists to prevent (architecture that is only declared elsewhere, and decays unnoticed), applied to identity instead of lint rules.

A file name is a poor description of content. Someone editing a file can keep a claim in that file correct; they will not reliably keep a distant table correct.

## Principle

- **In-file metadata is the only source of truth for a module.** Everything else (package index, extraction manifest, semantic digest) is derived by scanning and regenerated, never hand-edited.
- **Claims are verified, not trusted.** A tag is a claim. The checker verifies what it can (imports against ring, detected effects against declared effects) and the host policy has the final say.
- **Location is provenance, not meaning.** Path, repository and destination are recorded as placement. They never carry semantics.

## Layers

| Layer | Holds | Where | Location-dependent |
|---|---|---|---|
| Intrinsic claims | ring, `public`, role, effects; package id, kind, capabilities, `depends_on`, `vendors` | in the file (package-level facts: see open questions) | no |
| Host verdict | ring definitions, which claims vendored code may make, overrides | the consuming repository's policy | no |
| Placement | repository, path, destination, origin revision, extraction status | derived manifest | yes |

Resolution order extends the existing one: in-file annotation, then `classify`, then host override. An invalid annotation never silently falls back and never erases a module's other checks (the #18 rule). `classify` stays as a migration aid; a strict profile may require annotations.

## Schema (proposed)

One language-independent payload, different carriers:

```python
__arbor__ = {"ring": "domain", "public": True, "role": "source", "effects": ["io"]}
```

- `ring` exists today. `public`, `role` and `effects` are proposed.
- A file-local `public` flag survives moves, unlike central `public` globs.
- Carriers beyond Python are deferred (#31): a `# /// arbor` comment block for formats with comments (generalising the PEP 723 convention already parsed in `facts.py`), front matter for Markdown, and a sidecar for formats without comments. Sidecars can orphan, so they need an orphan lint. Generated files must emit their own metadata or be excluded explicitly.

## Identity

- A unit is `(package id, module name)`. Within a package a module name must be unique; a duplicate is an error. Build output such as `build/lib/...` contains copies and must stay excluded (#26).
- Across packages, two modules with the same name are distinct units. Each package's boundary rules apply to its own copy.
- Metadata encapsulation is not runtime encapsulation. One interpreter holds a single `sys.modules` entry per import name, so two packages vendoring a top-level `util` silently share whichever loads first. Vendored copies must sit under a namespace the owning package controls, and a lint must reject a top-level import name shared across packages.
- Vendoring and depending are different relationships. A shared dependency is one identity and one copy; a vendored copy is private. The descriptor states which, using a declared `vendors = [...]`. Content fingerprints are used only to warn about undeclared copies.
- Distinct for enforcement does not mean unrelated for audit: a provenance fingerprint (origin id, revision, content digest) lets a report show that two vendored copies share an origin and have diverged.

## Closure and effect reachability (one engine, two queries)

A reachability walk over the import graph, with tags as labels:

1. **Extractability closure.** Every import of a package must resolve to itself, to the public surface of a declared dependency, or to declared third-party dependencies. Run in CI, a package stays extractable continuously instead of discovering the problem at extraction time.
2. **Indirect effects.** Tag a file `effects: ["io"]` and any ring not allowed that effect cannot reach it. Findings carry the path as evidence (`domain.rules -> util.http -> adapters.client [io]`). This catches project-defined wrappers that launder effects the stdlib category table does not know about.

Verification loop: an effect detected in a file but not declared is an "undeclared effect" violation; over-declaring is allowed.

Limits, stated so the tool does not over-promise:

- Module-level reachability over-approximates (reachable is not used); function-level call graphs under-approximate in Python (methods on objects, callbacks, `getattr`). Direct and definite findings are errors; transitive findings are warnings with the path, until the false-positive rate is known.
- Dependency injection is intentional: inner code receives an IO function as a parameter. A `boundary: "port"` tag marks a permitted crossing so this is not flagged.
- Vendored private types should not appear in public signatures. Static detection is heuristic, so this is a warning.

## Rules (candidate)

1. A duplicate module name within a package is an error.
2. A top-level import name shared across packages without a private namespace is an error.
3. Same-origin copies that have diverged are reported.
4. A private vendored type in a public signature is a warning.
5. An effect detected but undeclared is an error.
6. An orphaned sidecar, or conflicting package claims on one file, is an error.

Each rule needs a mutation probe in `selftest` and a test that fails if the rule is removed.

## Staging

1. Extend the annotation schema (`public`, `role`, `effects`) with no-blind-spot validation.
2. Build the graph engine: extractability closure plus file-level effect reachability and the undeclared-effect check. This stage delivers architectural discipline without touching extraction.
3. Add the `port` crossing tag.
4. Derive the package index and a semantic digest from the scan; the extraction manifest becomes a thin placement record.
5. Add a strict profile requiring annotations, and the vendoring rules.
6. Function-level tags, only if the call-graph limits above prove acceptable.

## Relationship to existing work

- #24: `__arbor__` ring annotations are stage zero of this design.
- #27 and the manifest v2 work: that manifest separates semantics, origin, location and state, but still derives semantics from catalog paths. This design replaces the catalog as the source of truth with the files themselves.
- Scopes and `packages.toml` describe the same thing twice. A scope is essentially a package's architecture; a later stage merges them so the package descriptor is the scope and `packages.toml` becomes a discovered index.
- #31: polyglot units and the meaning of "package". For now a package is a Python distribution-sized ownership unit; non-Python files are assets.

## Non-goals

- Arborescent does not need to perform extraction or vendoring itself. Other tools can move files; Arborescent supplies and verifies the declared intent those tools cannot derive (ownership, public surface, legitimate dependency direction) and checks the result afterwards.
- Multi-language ring enforcement. Carrying metadata in other file types is cheap; analysing their imports is a per-language commitment that is not proposed here.
- Content hashing of extracted packages. They are expected to evolve; the manifest is a provenance snapshot, not a seal.

## Open questions

1. Where do package-level facts (id, capabilities, `depends_on`, `vendors`) live: a clearly separate section in the package's `__init__.py` (most local, but mixes two meanings in one place) or `pyproject.toml`?
2. Is `classify` deprecated under the strict profile, or supported indefinitely?
3. Who owns ring names: the package (with a host mapping on vendoring) or the host? Leaning toward package-owned ladders with an explicit mapping, which is what scopes already provide.
4. How are two versions of one vendored package handled when the scanner currently assumes globally unique module names?
5. Does function-level tagging justify the call-graph work, or is file-level reachability enough?
