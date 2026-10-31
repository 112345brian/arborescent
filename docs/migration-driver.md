# Migration driver

`arborescent packages migrate` is an **auxiliary driver** on top of the annotation layer. Its only job is to supply a
dedicated migration library with the arguments it needs, taken from the [migration arguments](migration-export.md) of
one package. **It does not migrate.** History extraction, and import rewriting where a library offers it, happen inside
the library. The driver holds no migration logic and no sequencing beyond calling the library with the right arguments.
The annotation layer (annotations, checks, migration arguments, export) never depends on the driver, and the arguments
stay usable by any other tool.

```sh
arborescent packages migrate core --target ../core            # dry run: prints the exact arguments, supplies nothing
arborescent packages migrate core --target ../core --yes      # supplies the library
arborescent packages migrate core --target ../core --branch release
```

It is an explicit command. `check` and `generate` never call it. It needs the same `package_catalog` as `packages export`.

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
| `home` is a workspace member | it stays in this repository, so there is no history to extract: stated, nothing supplied, exit 0 |
| the library is not installed | a `SETUP` line with the install command; a dry run still prints the arguments and exits 0, `--yes` exits 1 |
| the target already exists | the library adapter refuses (the new repository must not exist); exit 1 |

A gap of another package does not block this one. Informational gaps (`NOTE`) never block.

## The library: git filter-repo

Evaluated first, and chosen:

| Candidate | Why not / why |
|---|---|
| **git filter-repo** | keeps history; explicit multi-path selection and renames; `--source`/`--target` read one repository and write another, so the monorepo is never modified; installs from PyPI (`pip install 'arborescent[filterrepo]'`) |
| `git subtree split` | one prefix at a time; no multi-path selection or renames |
| splitsh-lite | Go binary, prefix based, not installable from PyPI |
| Josh, Copybara | servers or JVM tools built for continuous sync, not a one-off extraction |
| monorepo-tools (Embedded Artistry, Contao, Shopsys) | shell scripts around `git filter-branch`; Shopsys' maintainers will not develop theirs further |

What filter-repo does not do is rewrite imports. A request that carries module renames therefore prints
`NOT APPLIED  module renames (git filter-repo does not rewrite imports)`; the driver does not rewrite them itself. An
import-rewriting library (rope, LibCST) would be a second adapter over the same port.

How the arguments become filter-repo's: the adapter creates the target with `git init`, then runs `git-filter-repo
--source <repo> --target <new repo> --refs <branch>` with, per owned path, `--path <path>` and
`--path-rename <path>:<new path>` (to the root: `<path>/:`). Only the branch you name is carried over.

## Layout

One module per ring, wired only in `cli`; see [Arborescent's architecture](architecture.md#migration-supply-the-arguments-do-not-reimplement-the-tools).
Tests use `arborescent.testing.FakeMigrationLibrary` through the port; one test runs the real tool when it is installed.
The constraint that the annotation layer cannot import the driver is `module_forbid_imports` in Arborescent's own
policy, probed by `selftest`.
