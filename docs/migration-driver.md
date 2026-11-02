# Migration driver

`arborescent packages migrate` is an **auxiliary driver** on top of the annotation layer. Its only job is to supply a
dedicated migration library with the arguments it needs, taken from the [migration arguments](migration-export.md) of
one package. **It does not migrate.** History extraction happens inside the history library (git filter-repo) and import
rewriting inside a second library (LibCST), each behind its own port. The driver holds no migration logic: it chooses
their inputs, refuses what they cannot do safely, and calls them in order.
The annotation layer (annotations, checks, migration arguments, export) never depends on the driver, and the arguments
stay usable by any other tool.

```sh
arborescent packages migrate core --target ../core            # dry run: prints the exact arguments, supplies nothing
arborescent packages migrate core --target ../core --yes      # supplies the library
arborescent packages migrate core --target ../core --branch release
```

It is an explicit command. `check` and `generate` never call it. It needs the same `package_catalog` as `packages export`.

## Where the new repository is checked out: a local config, not the catalog

`home.repository` in the catalog is the **remote identity** of a repository (`org/name` or a URL): the same on every
machine, so it is committed and appears in the export. A directory on one machine is not. It would break on the next
checkout, so it is never written to the catalog, the export, a derived `pyproject.toml` or an engine contract, and only
`packages migrate` ever reads it.

The directory comes from, in order:

1. `--target DIR` on the command line;
2. the package's entry in a git-ignored **local config**:

   ```toml
   # .arborescent/local.toml (not committed)
   [targets]
   core = "../checkouts/core"     # relative to this file's folder, not the current directory
   web  = "/work/web"
   ```

3. neither: the command exits 1, says so, and names both ways and the file it looked at. A package that is blocked or a
   workspace member is still explained first, because it needs no target.

The config file is found through, in order: the `ARBORESCENT_LOCAL_CONFIG` environment variable; the same variable in a
`.env` at the repository root (only that one variable is read, and nothing else in the file is printed); the default
`.arborescent/local.toml` under the repository root. The variable may point outside the worktree, so the file survives
branch switches, clean checkouts and linked worktrees. `.arborescent/` and `.env` are git-ignored in this repository.

The dry run prints which source supplied the target (`target from: --target`, or `target from: local config <path>
(found via the ARBORESCENT_LOCAL_CONFIG variable in .env)`). A config that names a variable's file which does not exist,
is not TOML, has an unknown key, a non-string path or an unknown package id is an error (exit 2), never ignored; with
`--target` given, the config is not read at all.

## `extract`: the same pipeline plus the manifest

`arborescent packages extract PACKAGE_ID --output DIR` is `migrate` with one more step. After the library and the rewriter have
run, Arborescent writes `.arborescent-package.toml` into the new tree and reads it back, so `packages validate --extracted` and the
preservation checks (`package_parts`) can use it. Everything else, including the dry run, the refusals and `--target`, is shared.
`migrate` alone leaves the tree without a manifest. Arborescent copies no files for either command.

## What the dry run prints

The package, the library, the source (the repository root) and target, the branch, every path rename and module rename
the request holds, any argument the library cannot apply (`NOT APPLIED  ...`), and the library's own rendering of the
exact arguments it would be called with. The dry run and `--yes` share one code path up to the call, so the dry run
cannot differ from the real run.

## When it refuses or does nothing

| Situation | Result |
|---|---|
| unknown package id | exit 2 |
| the package (or no package in particular) has an open annotation gap of severity `error` | the gaps are listed, nothing is supplied, exit 1 (with or without `--yes`) |
| other packages still import a module of this one that is not on its `public` surface (a bypass edge), or a first-party dependency is undeclared | the edges or gaps are listed (`[private-import]`, `[package-undeclared-dependency]`), nothing is supplied, exit 1; there is no flag to override it, because the packages left behind would import something that is no longer there |
| `home` is a workspace member | it stays in this repository, so there is no history to extract: stated, nothing supplied, exit 0 |
| a module of the package changes its name and the package's own files contain a statement the rewriter cannot rewrite provably correctly (see the table below: a file that does not parse, `from <old> import a, b` mixing a renamed module with another name from a renamed package, or `import <new> as x` for a module renamed to a top-level name) | the statements are listed (`[unsupported-import-form] file:line: text`), nothing is created or modified, exit 1; checked on the monorepo's own files before the library runs, and again on the new repository before any file is rewritten |
| a library is not installed (git filter-repo, or LibCST when a module changes its name) | a `SETUP` line per missing tool with the install command, **before** anything is created; a dry run still prints the plan and exits 0, `--yes` exits 1 |
| the target already exists | the library adapter refuses (the new repository must not exist); exit 1 |

A gap of another package does not block this one. Informational gaps (`NOTE`) never block.

## The library: git filter-repo

Evaluated first, and chosen:

| Candidate | Why not / why |
|---|---|
| **git filter-repo** | keeps history; explicit multi-path selection and renames; `--source`/`--target` read one repository and write another, so the monorepo is never modified; installs from PyPI (`pip install 'arborescent[filterrepo]'`; LibCST: `arborescent[libcst]`) |
| `git subtree split` | one prefix at a time; no multi-path selection or renames |
| splitsh-lite | Go binary, prefix based, not installable from PyPI |
| Josh, Copybara | servers or JVM tools built for continuous sync, not a one-off extraction |
| monorepo-tools (Embedded Artistry, Contao, Shopsys) | shell scripts around `git filter-branch`; Shopsys' maintainers will not develop theirs further |

What filter-repo does not do is rewrite imports; that is a second port, `ImportRewriter`, so the two jobs stay separate.

## Import rewriting: LibCST, with a deliberate refusal

When a module changes its name (`[[packages.module_renames]]`), the new repository's files still say the old name. After
the library has run, the driver applies the package's module-name map to **the new repository only, never the
monorepo**, by running LibCST's own `RenameCommand` once per renamed module, longest name first. The driver rewrites
nothing itself. Rope was evaluated first and cannot do it: once filter-repo has moved the files, rope's rename acts only
on the first segment of a name it can no longer resolve (`app.core.api` became `core.core.api`), and its import utilities
leave attribute uses such as `app.core.api.f()` untouched.

| Idiom (checked against real LibCST) | Result |
|---|---|
| `import M`, `import M as a`, `from M import name`, `from M import *` | rewritten by LibCST's `RenameCommand` |
| `from <old> import <module>` (also with `as`, with several names, and `from app import core` for module `app.core`), and `from <old> import *` where `<old>` is itself a renamed module | rewritten by the adapter's own transformer, before `RenameCommand` runs (#71), see below |
| `from <old> import a, b` where `<old>` is itself renamed, `a` is a renamed module and `b` is not one | **refused** |
| `import <new> as x` where `<new>` is a top-level name some module is renamed to (already in the file, or what `from app import core as c` would become) | **refused** |
| a file below the rewritten paths that does not parse | **refused** (`unparsable`, nothing is modified) |
| any other import that still names an old module afterwards (for example `from app.core import f`, an attribute of the renamed package, on which LibCST's rename itself raises) | the run **fails**, naming the file |

### How `from <old> import <module>` is rewritten

LibCST's rename gets this form wrong in a way a syntax check cannot catch (it drops `from app.core import api as api2` and
leaves `api2.f()` renamed to an unbound `api.f()` when `import app.core.api` is also present; the real-LibCST test
`test_libcst_own_rename_still_leaves_a_name_unbound_on_the_issue_71_input` pins that output, and fails the day upstream fixes it).
The adapter therefore rewrites the statement itself, with a small LibCST transformer (`driver_libcst_scan rewrite`, run as a
subprocess like the scanner) that touches **only** `ImportFrom` nodes and never a use of a name:

- Every name the statement bound stays bound, under the same name, to the same module. `from app.core import api` with
  `app.core.api -> core.service` becomes `from core import service as api`; `from app import core as c` is refused (see above).
  Because no use is rewritten, a local variable or parameter called `core`, `service` or `api` can neither be captured nor
  collide, and attribute uses (`api.f()`) need no analysis.
- A new name with a parent becomes `from <parent> import <leaf>[ as <bound>]`; a top-level new name becomes
  `import <new>[ as <bound>]`.
- When every name moves to the same new parent and keeps its leaf, only the module part of the original statement changes;
  parentheses, `as` and comments stay. Otherwise the statement is split into one statement per name, in order, on the same line
  (`;`-separated), and names that are not renamed modules stay in `from <old> import ...`. Comments inside a *split*
  parenthesised list are not carried over.
- `from <old> import *` becomes `from <new> import *` when `<old>` is a renamed module, since it is the same module under another name.
- Relative imports (`from . import api`), and anything whose module is not in the map, are not touched.
- A second run changes nothing: the old names are gone, so no statement matches.

Assumption, same as for the original statement: the renamed module's contents are unchanged by the move, and the new parent
package does not define an attribute with the module's name that would shadow the submodule. Arborescent writes this
transformer itself, which the follow-up issue had hoped to avoid (#51); it is limited to this one statement form, and everything
else stays with LibCST.

LibCST runs with `--no-format`: by default it pipes every file it writes through `black`, which this extra does not
install and which would reformat code the migration has no business touching. Only the renamed names change.

The rewrite is left as an **uncommitted change** in the new repository (`git diff` shows it); the history the library
wrote is not altered. A failure part-way (LibCST raising on one file) leaves the files renamed so far changed, and the
command says so: delete the new repository and rerun after fixing the cause. One LibCST process runs per renamed
module, so a package with many modules is slow.

How the arguments become filter-repo's: the adapter creates the target with `git init`, then runs `git-filter-repo
--source <repo> --target <new repo> --refs <branch>` with, per owned path, `--path <path>` and
`--path-rename <path>:<new path>` (to the root: `<path>/:`). Only the branch you name is carried over.

## Known limits

- **Tags are not carried.** Only the branch you name is written to the new repository (`--refs <branch>`).
- **Workspace members are not moved.** They stay in this repository; the driver states so and supplies nothing.
- **Asset folders with a destination are not exercised end to end.** The real-tool test moves a source folder to the
  new repository's root; renames of asset folders are checked only as arguments.
- **Only the new repository's imports are rewritten.** The packages that stay behind are not changed; their imports of
  the moved package (through its public surface) need a dependency on the new repository, which nothing here adds.
- **Rewriting covers the idioms in the table above**; the others are refused or fail loudly, never skipped silently. Still refused, because a rewrite cannot be shown correct: the mixed `from <old> import a, b` from a renamed package, `import <new> as x` for a top-level `<new>`, and files that do not parse.

## Layout

Modules per ring: `driver_request`, `driver_port` (both ports), `driver_plan` and `driver_rewrite` (the translation),
`driver_filter_repo`, `driver_libcst` and `driver_libcst_scan` (the adapters; the scanner and the `from ... import` transformer are run as a subprocess and
never imported by the command line, so a missing LibCST cannot break it), `cli_migrate`. Wired only in `cli`; see [Arborescent's architecture](architecture.md#migration-supply-the-arguments-do-not-reimplement-the-tools).
Tests use `arborescent.testing.FakeMigrationLibrary` through the port; one test runs the real tool when it is installed.
The constraint that the annotation layer cannot import the driver is `module_forbid_imports` in Arborescent's own
policy, probed by `selftest`.
