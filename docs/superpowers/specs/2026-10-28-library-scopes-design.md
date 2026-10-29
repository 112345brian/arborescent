# Independently Scoped Libraries Design

## Intent

Let one repository contain several libraries that each enforce their own Clean Architecture, without forcing all libraries and the application core into one shared ring hierarchy. Keep the configuration centralized and allow the libraries to be extracted into separate repositories later.

## Goals

- One root-level Arborescent invocation can check the complete repository.
- Each named library has its own sources, module roots, ring hierarchy, and classifications.
- Every source module has one owning scope; scopes do not duplicate files to apply multiple policies.
- Each library declares the modules that form its supported boundary for consumers.
- Cross-library dependencies are explicit and may target only a dependency's declared boundary.
- Shared helpers belong to a named shared scope (or to the library that owns their capability) and are consumed through that scope's declared boundary.
- The application/core can have its own scope and architecture policy.
- Preserve the existing single-policy configuration as a compatible shorthand for one scope.

## Non-goals

- Automatically extracting or packaging a library.
- Requiring a library's configuration to physically move with it in this first change.
- Replacing Clean Architecture's dependency rules with directory naming conventions.

## Approaches

1. **Independent config and invocation per library.** This maps directly to today's one-policy-per-project behavior, but offers no single root check and duplicates orchestration in a monorepo.
2. **One root config with independently governed scopes (selected).** Each scope owns its ring graph and boundary; the root composes the checks and declares cross-scope dependencies. This preserves shared metadata and gives each library a distinct architecture.
3. **One global hierarchy with prefixed ring names.** This is smaller, but still makes every library participate in one ordering and does not provide independent boundaries, so it fails the stated goal.

## Proposed behavior

The root policy contains named scopes. Each source module belongs to exactly one scope, even if multiple scopes depend on it. A scope owns one or more source paths, source roots, a ring hierarchy, and classifications. A library scope also declares its public module patterns and the scopes it may depend on. Modules inside one scope are checked against only that scope's ring rules. Imports crossing scopes are checked against the dependency declaration and the target scope's public module patterns. The application's core is modeled as another scope, so its own direction and layer rules are explicit rather than implicit.

When two libraries need the same low-level capability, that does not merge their core logic or ring hierarchies. If it is an installed dependency (for example, a binary parser package), each scope declares the dependency in the ring that uses it—normally an outer adapter—and translates parsed data into its own domain types. If the parser is code maintained in this repository, it has one owner in a shared scope with its own policy and narrow public boundary; each consuming library declares an allowed dependency on that scope. The helper is never duplicated just to make it fit two scopes.

The exact TOML spelling and migration details are implementation choices, provided existing one-scope configurations keep their current meaning and can be migrated without changing enforcement.

## Constraints and open implementation detail

This feature is a separate follow-on to the open `__arbor__` annotation program (#24), which explicitly defers package boundaries and says they should be designed atop stable ring resolution. Do not fold library boundaries into that program or implement this feature alongside its ring-resolution foundation. Once that foundation lands on the intended development line, build scope-aware rules on its resolved-ring API and coordinate generated-engine output with #22. Keep documentation honest about package boundaries until this follow-on is implemented.

## Approval

Approved by the user on 2026-10-28.
