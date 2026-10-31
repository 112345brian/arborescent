# Migration recipes

This page shows how the [annotations and the JSON export](migration-export.md) feed the tools that actually perform a
monorepo-to-polyrepo (or monorepo-to-workspace) migration. **Recipes are documentation, not shipped emitters.**
Arborescent publishes the facts once; you convert them with `jq` or a few lines of your own. Nothing here is a command
Arborescent runs, and the tools' own documentation, linked at every claim about them, stays the authority on what they do.

How much of each recipe was run:

| Recipe | Status |
|---|---|
| git filter-repo paths file (repository home and workspace member) | run on the worked example below, git-filter-repo `a40bce548d2c` |
| `git subtree split` | run on the worked example, git 2.43.0 |
| leaf-first order with `tsort` | run on the worked example |
| rope module rename | run on the worked example, rope 1.15.0, with the caveat below |
| deptry | run on the split `web` tree, deptry 0.25.1 |
| LibCST | **not shipped**: see [LibCST](#libcst-and-the-module-name-map) |
| Copybara, Josh | **not run**: the commands are taken from each tool's reference and are starting points |

## The layers: what supplies what

Arborescent has two layers.

- **The annotation layer** is the foundation. It holds the annotations (a package catalog in `packages.toml`,
  `__arbor__` ring annotations in the modules, and policy such as `public` surfaces), the checks that they are complete and
  consistent (reported by rule id, so a gap shows up before any tool runs), the typed *migration arguments* built from
  them, and the one stable JSON export of those arguments.
- **The auxiliary driver** sits on top of that layer. Its only job is to supply a dedicated migration library with the
  arguments it needs, taken from the migration arguments, through a port. **The driver does not migrate.** History
  extraction, import rewriting and everything else is done inside the library or tool. The annotation layer never depends
  on the driver, and the arguments stay usable by any tool. The boundary is recorded in
  [the design note](superpowers/specs/2026-10-31-monorepo-split-annotations-design.md) and in
  [architecture.md](architecture.md). **No driver ships on this branch**; until it does, the recipes below are how you
  supply the same arguments by hand.

The migration arguments are tool-neutral value objects in `arborescent.migration_arguments`, serialised by
`arborescent.migration_json`:

| Object | What it carries |
|---|---|
| `MigrationArguments` | all packages, all edges, all gaps, and `ready` (no gap of severity `error`) |
| `PackageArguments` | identity (`id`, `name`, `kind`, `summary`, `domain`, `capabilities`), `home`, the owned `modules` and `assets`, `path_renames`, `module_renames`, `public_surface`, `bypasses`, `depends_on`, `externals` |
| `OwnedModule` / `OwnedAsset` | a module or asset with its repository-relative `path` and its `target_path` (and `target_module` for modules) |
| `PathRename` | a `source` path and the `target` where it lands (`"."` is the repository root) |
| `Bypass` | a `module` of another `package` that `imports` a module that is not on this package's public surface |
| `Edge` | `source`, `target` and `kind`: `depends_on` (declared) or `imports` (observed) |
| `Gap` | a named, severity-tagged annotation that is missing or inconsistent |

The same data is available as JSON with a published schema ([migration-export.md](migration-export.md)). Release, pin and
history policy (`history`, `[packages.release]`; #56) and derivable `pyproject.toml` fields (`package_pyproject`; #60) are
annotation-layer features in sibling pull requests (#62, #63) and are documented on those branches. Annotation
preservation across extracted parts (`package_parts`, `package-semantics-drift` and related rules; #55) is a `check`-time
verification in #64. Their fields are not yet written into the export; see [What the schema left](#what-the-schema-left).

## Worked example

The example is a small synthetic monorepo, `shop`, with three packages in a namespace package (`src/shop` has no
`__init__.py`):

```text
src/shop/core/      __init__.py  _ids.py  models.py      package core: its own repository
src/shop/billing/   __init__.py  invoices.py             package billing: workspace member
src/shop/web/       __init__.py  views.py  templates/index.html   package web: workspace member
```

`web.views` imports `flask`, `shop.billing.invoices`, `shop.core.models` and also the private `shop.core._ids`. Every
module carries `__arbor__ = {"ring": "..."}` (`domain` for core and billing, `interface` for web). The catalog:

```toml
# packages.toml (trimmed to one entry per home kind; billing mirrors web)
[[packages]]
id = "core"
name = "Core"
kind = "library"
repository = "example/shop"
root = "src/shop/core"
summary = "Domain models."
public = ["shop.core.models"]
externals = []
[packages.home]
kind = "repository"
repository = "example/shop-core"
[[packages.module_renames]]
from = "shop.core"
to = "shop_core"
[[packages.paths]]
path = "src/shop/core"
role = "source"
destination = "src/shop_core"

[[packages]]
id = "web"
name = "Web"
kind = "application"
repository = "example/shop"
root = "src/shop/web"
summary = "HTTP views."
depends_on = ["billing", "core"]
public = []
externals = ["flask"]
[packages.home]
kind = "workspace-member"
path = "packages/web"
[[packages.module_renames]]
from = "shop.web"
to = "web"
[[packages.paths]]
path = "src/shop/web"
role = "source"
destination = "src/web"

[[assets]]
pattern = "src/shop/web/templates/*.html"
package = "web"
```

with `package_catalog = "packages.toml"` in the `[tool.arborescent]` policy.

Building this example turned up two problems, both fixed in the catalog above:

- `check` failed on the first run with `package-unassigned-module` for `src/shop/__init__.py` and `unclassified` for the
  four `__init__.py` files, so the example makes `shop` a namespace package and annotates the three package
  `__init__.py` files (Arborescent reported this);
- a workspace member without `destination` exported `target_path = packages/web/__init__.py` next to
  `target_module = shop.web`: the module name and the new location disagreed. `destination` and `module_renames` above fix
  it. **Arborescent did not report this one**, because it does not check that the two agree (gap 2 below).

```sh
arborescent check                                  # complete and consistent?
arborescent packages export --output migration.json
jq '.ready, .gaps' migration.json                  # true, []
```

A trimmed export of `core` (the full document is in the [schema](schemas/migration-export-v1.schema.json)):

```json
{
  "id": "core",
  "home": { "kind": "repository", "path": null, "repository": "example/shop-core" },
  "owned": {
    "modules": [
      { "module": "shop.core.models", "path": "src/shop/core/models.py",
        "target_module": "shop_core.models", "target_path": "src/shop_core/models.py" }
    ],
    "path_renames": [ { "source": "src/shop/core", "target": "src/shop_core" } ],
    "module_renames": [ { "source": "shop.core", "target": "shop_core" } ]
  },
  "surface": {
    "public": [ "shop.core.models" ],
    "bypasses": [
      { "module": "shop.web.views", "package": "web", "imports": "shop.core" },
      { "module": "shop.web.views", "package": "web", "imports": "shop.core._ids" }
    ]
  },
  "dependencies": { "packages": [], "external": [] }
}
```

The `bypasses` entries are the deliberate blocking edge: `web` reaches into `core` outside its declared public surface
(`from shop.core import _ids` is listed twice, once against the package itself; see gap 6). The export names it, so a consumer can refuse to proceed, and `package-private-import` (#55) and the engine contracts (#53)
report the same edge from the verification side.

## Reading the cut list

The export lists *what is wrong with the annotations* (`gaps`) and *which imports bypass a surface* (`bypasses`). The
**cut list** (the import edges that must be removed before a package can leave) is not in the export, by design: it is the
violation output of the engines Arborescent already drives. Compile the catalog into import-linter and tach contracts
(#53, in sibling pull request #65), run `arborescent check`, and read the engines' own violations. Graph questions
beyond that (closure over the whole repository, clustering) belong to [grimp](https://grimp.readthedocs.io/),
[import-linter](https://import-linter.readthedocs.io/) and [tach](https://docs.gauge.sh/), not to Arborescent.

## Order: leaf-first

Move packages that depend on nothing first, then the ones that depend on them, so that every package arrives with its
dependencies already in place. [Nx's import guide](https://nx.dev/docs/guides/adopting-nx/import-project) says the same:
"start with the leaf projects in the dependency graph (the projects without any dependencies) and then work your way up
to the top level applications". The declared edges give the order directly (`tsort` is in coreutils; the self-pairs keep
packages with no edges in the output):

```sh
{ jq -r '.packages[] | "\(.id) \(.id)"' migration.json
  jq -r '.edges[] | select(.kind == "depends_on") | "\(.target) \(.source)"' migration.json; } | tsort
# core
# billing
# web
```

A cycle makes `tsort` fail, which is correct: such packages cannot be ordered, and `package-undeclared-dependency` and
the dependency-acyclic catalog check are where to look first.

## What Nx's manual items map to

[Nx lists three kinds of external reference](https://nx.dev/docs/guides/adopting-nx/import-project#migrate-external-code-and-configuration)
that an import leaves for you to migrate by hand. Which of them the export names:

| Nx item | In the export? |
|---|---|
| Local project dependencies that are not present or have a different name in the destination | **Yes.** `dependencies.packages`, `edges`, and `module_renames` / `target_module` name them. |
| Scripts outside the project folder that the project needs | **No.** The export knows owned modules and assets below `sources`. A script elsewhere in the repository is unowned and unnamed. |
| Project configuration files that extend root configuration files | **No.** Same reason. (`pyproject.toml` fields can be derived by #60, but the root config a package extends is not tracked.) |

## History rewriting: read this before running anything

All history-rewriting tools below change commit hashes. From
[git filter-repo's documentation](https://github.com/newren/git-filter-repo/blob/main/Documentation/git-filter-repo.txt):

- **Work on a fresh clone.** By default filter-repo aborts when it is not run from a fresh clone, because the rewrite is
  irreversible and a non-fresh clone may hold local-only changes (the manual's "fresh clone" section explains the check).
- **Collaborators must re-clone.** Anyone with a clone of the old history has to discard it; mixing old and new history by
  merging old branches back in defeats the rewrite. The manual describes this under the consequences of sharing rewritten
  history and recommends a new clone URL.
- **Pinned SHAs must stay reachable.** A SHA that a dependent pins to is a SHA of the *old* history. After a rewrite it no
  longer names a commit in the new repository. Keep the old repository (or a mirror) readable, and re-pin dependents to
  new tags. This is why the release policy in #56 (`pin`, `merge = "merge-commit"`, `tag_before_branch_delete`) is an
  annotation: squash or rebase merges orphan pinned commits.

A package with `home.kind = "workspace-member"` exports no history or pin information, because it stays in the same
repository.

## Recipes

### git filter-repo: paths file

`--paths-from-file` takes one directive per line, and [lines containing `==>` are renames](https://github.com/newren/git-filter-repo/blob/main/Documentation/git-filter-repo.txt).
Verified on the worked example: **list the paths to keep first, then the renames.** The same file with the rename lines
first produced a repository with no commits, so keep this order.

```sh
id=core
{
  jq -r --arg id "$id" '.packages[] | select(.id == $id) | .owned | (.modules[].path, .assets[].path)' migration.json
  jq -r --arg id "$id" '.packages[] | select(.id == $id) | .owned
      | (.modules[], .assets[]) | "\(.path)==>\(.target_path)"' migration.json
} > "paths-$id.txt"
git clone . "../$id" && cd "../$id" && git filter-repo --paths-from-file "../paths-$id.txt"
```

This uses the per-file `modules` and `assets` entries, not the directory-level `path_renames`, because a package follows
module sets and not folders: when a folder is shared between packages, only the file-level lists select the right files.
(`path_renames` is shorter and fine for a package that owns whole folders; [migration-export.md](migration-export.md)
shows that form.) On the example, `core` ends up with `src/shop_core/{__init__,_ids,models}.py` and its two commits.

For `web` (a workspace member with an asset) the same commands give `packages/web/src/web/{__init__.py,views.py,templates/index.html}`.
The result is a rewritten repository whose history holds only that package, at the path it will have inside the workspace.
Bringing it into the workspace repository (fetch and merge with unrelated histories) is a separate step that was not run
here.

### git subtree split

For a package that owns exactly one folder, [`git subtree split`](https://github.com/git/git/blob/master/contrib/subtree/git-subtree.adoc)
extracts its history with the folder as the new root, in the existing repository, without rewriting it:

```sh
git subtree split --prefix=src/shop/core -b core-only   # the folder's history, at the root
```

On the example this gives `__init__.py`, `_ids.py`, `models.py` at the root. It does not apply renames and cannot select
module sets, so a package that spans shared folders or needs `destination` should use filter-repo above.

### rope: rename modules and rewrite imports

The export's `owned.module_renames` is the module-name map. [rope](https://rope.readthedocs.io/en/latest/library.html)
moves a package and rewrites the imports that refer to it (`create_move(...).get_changes(destination)` and
`Rename(...).get_changes(new_name)`):

```python
import json
from rope.base.project import Project
from rope.refactor.move import create_move
from rope.refactor.rename import Rename

doc = json.load(open("migration.json"))
project = Project("src")  # the source root: module names are relative to it
for package in doc["packages"]:
    for rename in package["owned"]["module_renames"]:
        old, new = rename["source"], rename["target"]
        folder = project.get_resource(old.replace(".", "/"))
        project.do(create_move(project, folder).get_changes(project.root))  # out of the old parent
        leaf = old.rsplit(".", 1)[-1]
        if leaf != new:
            project.do(Rename(project, project.get_resource(leaf)).get_changes(new))
project.close()
```

Caveats found by running it:

- The project root must be the **source root** (`Project("src")`). With the repository root, rope rewrote imports to
  `src.core`.
- **rope did not rewrite imports that go through a namespace package.** With `src/shop/` having no `__init__.py`,
  `from shop.billing.invoices import invoice` in `web` was left unchanged while `from shop.core ...` was rewritten. With
  an `__init__.py` in `src/shop/` (added only in the working copy for the run), every import was rewritten. The export
  cannot tell you which parents are namespace packages (see the gap list).
- rope moves files itself: run it in a working copy, as its own commit, and treat the result as the migrated tree.

### LibCST and the module-name map

[LibCST](https://libcst.readthedocs.io/en/latest/codemods_tutorial.html) codemods can use the same map: each
`{source, target}` pair is an old and a new dotted prefix. This page does **not** ship a LibCST command. The built-in
`rename.RenameCommand` (`--old_name`, `--new_name`) is documented as renaming "a local or imported object"; applied to the
package prefixes (`shop.core` to `shop_core`) it left the example tree unchanged and reported failures to parse two
files under the default parser version. A working LibCST pass would be a small custom codemod over `ImportFrom` and
`Import` nodes driven by `owned.module_renames`; write it against your code base.

### deptry: declared dependencies of the new package

[deptry](https://deptry.com/) checks that the dependencies a project declares match what its code imports. The export's
`dependencies.external` is the list of third-party *import* names the package may use, and `dependencies.packages` the
first-party siblings. After the split, give each new package a `pyproject.toml` declaring those and run:

```sh
cd packages/web && deptry .
```

On the split `web` tree with an empty `dependencies = []`, deptry reported `DEP001` for `flask`
([missing dependencies](https://deptry.com/rules-violations/)), which is exactly the export's `externals = ["flask"]`, and
for `shop`, the first-party imports that the rope pass above rewrites to `billing` and `shop_core`, which belong in
`dependencies.packages`. Where an import name differs from its distribution name, deptry's
[`--package-module-name-map`](https://deptry.com/usage/) bridges it; the export carries import names only.

### Copybara (not run)

Use [Copybara](https://github.com/google/copybara) when the monorepo and a split-off repository must stay in sync for a
period. A workflow selects files with `origin_files` and moves them with `core.move`
([reference](https://github.com/google/copybara/blob/master/docs/reference.md)):

```python
# copy.bara.sky, generated by you from migration.json for package "core"
core.workflow(
    name = "core",
    origin = git.origin(url = "https://example.invalid/shop.git", ref = "main"),   # replace
    destination = git.destination(url = "https://example.invalid/shop-core.git"),  # replace
    origin_files = glob(["src/shop/core/**"]),
    authoring = authoring.pass_thru("shop <shop@example.invalid>"),
    transformations = [core.move("src/shop/core", "src/shop_core")],
)
```

The `glob`, the `core.move` pairs and the destination come straight from `owned.path_renames` and `home.repository`;
generating them is a loop over the export. Import rewriting is a separate transformation or a rope pass.

### Josh (not run)

[Josh](https://josh-project.dev/docs/) exposes a subtree as a repository through filters. From the
[filter reference](https://josh-project.dev/docs/reference/filters.html), `:/a` selects a subdirectory as the new root and
`:prefix=a` places the tree under a subdirectory, so the `core` rename is the chain `:/src/shop/core:prefix=src/shop_core`.
Both come from `owned.path_renames`.

## What the schema left

This section is the case-study ledger: every gap the export schema left, so that the tools above still needed something
Arborescent did not give them. Sources are explicit.

- **Built from:** the synthetic `shop` example on this page, the schema in `docs/schemas/migration-export-v1.schema.json`,
  and the sibling branches #55, #56, #60, #53 as they stand.
- **Not yet recorded:** the findings of the real downstream case study of #57 (a large application, about 1,400 modules).
  #57 has not landed, so none of its results are on this page, and no downstream paths or data are published here
  (this repository is public). When #57 lands, its gaps are added to this list rather than a second one.

| # | Gap the schema left | Seen where | Consequence |
|---|---|---|---|
| 1 | **No entity for files outside owned modules and assets**: root configuration that packages extend, scripts and CI outside the package folders. | Nx's manual items | Two of Nx's three manual items stay manual; nothing in the export names them. |
| 2 | **`target_module` and `target_path` are not checked against each other.** A workspace member with no `destination` exports a path that does not match its module name. | the example's first export | The mismatch is found only when a tool or import fails. |
| 3 | **Namespace-package parents are not marked.** rope silently skipped imports through a namespace parent. | rope run | Nothing in the export warns that the rewrite will be partial. |
| 4 | **External names are import names, not distributions.** The mapping (`dependencies` in the policy) is not in the export. | deptry run | deptry needs `--package-module-name-map` supplied separately. #60 derives `pyproject.toml` with that table. |
| 5 | **No global module map.** `module_renames` is per package (its own modules). Rewriting importers needs the union over all packages. | rope, LibCST | Every consumer merges the lists (the rope recipe above loops over all packages). |
| 6 | **A `from pkg import private_module` import is reported as two bypasses**, one against the package `__init__` and one against the submodule, when the surface lists neither the `__init__` nor the module. | `core` bypasses above | The count of bypasses overstates the number of edges to cut. |
| 7 | **`policy` and `pyproject` are always `null`.** The #56 and #63 branches add the annotations and the derivation but neither diff touches `migration_arguments.py` or `migration_json.py`. | release policy, `package_pyproject` | A consumer cannot read history, pin or derived `pyproject.toml` fields from the export yet. |
| 8 | **Preservation results are not in the export.** `package_parts` (#55) is a `check`-time verification of already-extracted parts. | #55 | The export is read before the split; verification after the split comes from `check`. |
| 9 | **No cut list.** Which imports to remove comes from the engines' violation output (#53), not the export. | by design | The export alone cannot tell you how to unblock a package, only that a bypass exists. |
| 10 | **The workspace-member join is not described.** Where a rewritten package's history enters the workspace repository is not an annotation. | filter-repo run | The final merge step is yours and was not run for this page. |
