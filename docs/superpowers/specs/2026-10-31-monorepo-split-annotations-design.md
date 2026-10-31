# Annotations for a mechanical monorepo-to-polyrepo migration: the boundary

Status: decision record for issue #51 (Phase 0 of program #59). Docs only; no code change. Revised after owner clarification: Arborescent supplies the migration arguments and a required driver layer runs external tools with them.

## Principle

A monorepo-to-polyrepo split is mostly mechanical once the repository says, in annotations, what each unit is.
Arborescent's core job is to **supply the annotation data and the objects that make a migration possible**: for any
package, the exact arguments a migration needs, derived from the annotations and checked for completeness and
consistency. Specialized tools (git filter-repo, rope, LibCST, grimp, deptry, import-linter, tach, Copybara, Josh) do
the work with those arguments. Arborescent never reimplements what they do better and more robustly.

Arborescent has two layers. The **annotation layer** (annotations, checks, migration arguments, export) is the
foundation. The **driver layer** sits on top of it and is a required part of Arborescent: it takes the migration
arguments and runs the specialized tools with them through ports and adapters (see "Wrapper rules"). The driver
contains no migration logic of its own; it owns the sequencing and the translation from arguments to each tool's input.
The annotation layer never depends on the driver, and the arguments stay usable by any external tool.

Placement rule used by the first downstream case: a package gets its own repository only if it would be its own
product; every other generic package becomes a workspace member in the same repository. Which one applies is a
per-package annotation (`home`), not an architecture fork.

## What Arborescent supplies: migration arguments

Typed, tool-neutral value objects in the domain ring, built from the annotations and available both as Python objects
and through the stable JSON export. For one package:

- identity and `home` (own repository, or workspace member and where);
- the modules and assets it owns (exactly one package each), as repository-relative paths, and the path-to-path
  renames from monorepo location to target location;
- the module-name map (old import name to new) for import rewriting;
- its public surface (`public`) and the monorepo modules that import it through anything else;
- its declared dependencies: other first-party packages and allowed external distributions per ring;
- release, pin and history policy (what history to keep, how dependents pin it);
- the derivable `pyproject.toml` fields (#60).

Tool-neutral means these objects name no tool. Turning them into one tool's input (a filter-repo paths file, a rope
move, a Copybara workflow) is done by that tool's adapter in the driver layer; the data itself lives in the annotation layer.
What Arborescent can already check about them (unassigned or doubly assigned paths, public-surface violations,
undeclared dependencies, annotation-vs-`classify` drift) is reported by rule id, so a gap is found before a tool runs.

## Wrapper rules (clean architecture)

- Every external tool sits behind a `typing.Protocol` in the ports ring (for example a history extractor, an import
  rewriter). The application ring orchestrates through the port and holds no knowledge of any tool's command line.
- Each tool has an adapter in the adapters ring that invokes it as a subprocess or library, following the existing
  engine adapters. Adapters do not import the application ring; only composition wires them.
- The driver is core; the individual tools are optional dependencies (extras) installed per use. A missing tool reports a `SETUP` line naming the install command, as
  missing engines do today. Arborescent never vendors or reimplements a tool.
- The application ring only translates annotations into the tool's inputs (paths, renames, targets) and sequences the
  steps. If a step needs logic the tool lacks, the answer is a different tool or an upstream change, not code here.
- Execution is an explicit command, never a side effect of `check` or `generate`. Planning and dry-run output come from the same path, so what is shown is what runs.
- Tests use in-memory fake tools through the ports, never patch a concrete module.

## In scope

- Annotation schema: package membership of every module and asset, public surface (`public`, #33), allowed external
  dependencies per ring (exists), release and history policy.
- Checks that the annotations are complete and consistent: unassigned or doubly assigned modules, public-surface
  violations, undeclared dependencies.
- Compiling the annotations into the configuration of the engines Arborescent already drives (import-linter, tach), so
  the engines' own violation output is the cut list.
- One stable, read-only JSON export of the annotation data for any external tool to consume.
- Deriving a package's `pyproject.toml` fields from the annotations, with a drift check (#60).
- The driver layer: running the migration through the ports and adapters above, using the arguments supplied here.

## Out of scope

- Reimplementing any tool's job: history rewriting, import rewriting, graph analytics (closure over the whole
  repository, leaf detection, clustering), dependency checking.
- A second closure engine. The nested-arbor check is the only one; graph questions go to grimp or tach.

## Existing extraction code

`arborescent packages extract` already exists (`src/arborescent/packages.py`, `docs/configuration.md` "Package
metadata and extraction"). It copies declared paths itself, with no history, import rewriting or repository creation.
That is the one place where Arborescent currently does a tool's job. Under the wrapper model it is frozen (no new
capability) and is expected to be replaced by an adapter over a history-preserving tool. The replacement and the fate
of the copy command are open decisions for the owner.

## Tool landscape (researched 2026-10-31)

| need | use | Arborescent's part |
|---|---|---|
| Extract paths with history | [git filter-repo](https://github.com/newren/git-filter-repo) (`--paths-from-file`, `old==>new` renames), [git subtree split](https://github.com/git/git/blob/master/contrib/subtree/git-subtree.adoc), GitHub's [guide to splitting a subfolder out](https://docs.github.com/en/get-started/using-git/splitting-a-subfolder-out-into-a-new-repository) | adapter behind a port (wrapper) |
| Dedicated split tools, driven by path maps | [embeddedartistry/monorepo-tools](https://github.com/embeddedartistry/monorepo-tools), [Contao monorepo-tools](https://github.com/contao/monorepo-tools), [Shopsys monorepo-tools](https://github.com/shopsys/monorepo-tools) (maintainers will not develop it further), [splitsh-lite](https://github.com/splitsh/lite), [Josh](https://github.com/josh-project/josh), [Copybara](https://github.com/google/copybara) | adapter behind a port (wrapper) |
| Architecture policy | [Nx module boundaries](https://nx.dev/docs/features/enforce-module-boundaries), import-linter [contract types](https://import-linter.readthedocs.io/en/v2.3/contract_types.html) | compile annotations into import-linter and tach contracts |
| Annotations plus closure | [blinkered](https://pypi.org/project/blinkered/) (alpha 0.0.6, 2026-10-01): per-node `blinkered.toml` manifest of tags and edges, graph validation, sparse checkout to a node's dependency closure. Documented scope is the working tree, not creating or syncing repositories | evaluate before proposing any closure or export feature (#53, #54) |
| Rewriting imports | [rope](https://rope.readthedocs.io/), [LibCST](https://libcst.readthedocs.io/) | adapter behind a port (wrapper) |
| Import graph and boundaries | [grimp](https://grimp.readthedocs.io/), import-linter, [Tach](https://docs.gauge.sh/usage/commands) | import-linter and tach are already driven |
| Dependency declarations | [deptry](https://deptry.com/), [pipreqs](https://pydevtools.com/handbook/reference/pipreqs/), `tach check-external` | adapter behind a port (wrapper) |

Nx's [import guide](https://nx.dev/docs/kb/import-project) is explicit about what remains manual after a move:
external code references, shared configuration and local dependencies; it advises moving leaf projects first.

## The gap, stated narrowly

These tools take configured paths and transformations as input. No established tool was found that takes architecture
annotations as the source of truth for extraction readiness and scope: which complete package boundary is safe to
extract, what its dependency and public-API constraints are, and whether what remains still satisfies its contracts,
then hands that boundary to path-and-history machinery. That is a search result, not proof that none exists. blinkered
shows that manifests plus dependency closure are an accepted idea. The defensible gap is the annotation-to-extraction
boundary, not a splitter and not a new closure engine.

## Relationship to existing specs

- `2026-10-29-in-file-metadata-design.md`: its non-goals are updated to point here. The nested-arbor closure check is
  the only closure engine; this program adds no second one.
- Issues #25, #27, #31, #33, #41, #49, #50 are built on, not duplicated.
