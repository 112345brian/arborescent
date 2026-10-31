# Migration export

`arborescent packages export` publishes the annotation data of a monorepo-to-polyrepo (or monorepo-to-workspace)
migration as **one read-only, versioned JSON document**. Arborescent holds the facts (which paths belong to which
package, which module names change, what each package depends on and exposes); external tools (git filter-repo, rope,
LibCST, Copybara, deptry, ...) each need them in their own format. Arborescent does not convert, plan or migrate: it
supplies the arguments, and whoever runs a tool converts them (see the recipe below). It is built on the package catalog
(`packages.toml`, `package_catalog` in the policy; see [Package assignment](configuration.md#package-assignment)).

```sh
arborescent packages export --output migration.json      # always written; the gaps are part of the content
arborescent packages export --require-ready              # exit 1 while an error gap is open
arborescent packages schema                              # the JSON Schema the export validates against
```

The schema is published as [`schemas/migration-export-v1.schema.json`](schemas/migration-export-v1.schema.json) (JSON
Schema 2020-12); a test keeps that file equal to the one the code validates with. The text is deterministic: sorted
keys, two-space indent, trailing newline, every list sorted, so a changed byte is a changed fact.

## Annotations the export reads

These are catalog keys, set per `[[packages]]` entry, all optional so an existing catalog keeps working. A missing one is
reported as a gap, never guessed.

| Key | Meaning |
|---|---|
| `[packages.home]` | Where the package lives after the migration. `kind = "repository"` with `repository = "org/name"` (its own repository), or `kind = "workspace-member"` with `path = "packages/x"` (a member of a workspace in the same repository). Workspace members may not share or nest folders. |
| `public = [...]` | Dotted module globs (`fnmatch`) other packages may import: the public surface. The same key `package_parts` checks (#55); absent or empty means no declared surface, so nothing of the package counts as private. |
| `externals = [...]` | Top-level third-party import names the package may use. `[]` means none. |
| `[[packages.module_renames]]` | `from`/`to` dotted-module prefixes that change name in the target; the longest matching prefix wins. |

`depends_on`, `paths` (with `destination`), `root` and `[[assets]]` are the existing keys.

## Shape

```text
{ schema, version, catalog, ready,
  packages: [ { id, name, kind, summary, domain, capabilities, home,
                owned:        { modules, assets, path_renames, module_renames },
                surface:      { public, bypasses },
                dependencies: { packages, external },
                policy, pyproject } ],
  edges:     [ { source, target, kind } ],     # kind: depends_on (declared) | imports (observed)
  gaps:      [ { code, package, subject, detail, severity } ] }
```

- **Paths are repository-relative.** `owned.modules[]` carries `module`, `path`, `target_path` and `target_module`;
  `owned.assets[]` carries `path` and `target_path`; `owned.path_renames[]` is the directory-level `source -> target`
  map (`"."` is the repository root). A `target_path` is relative to the root of the repository that holds the package
  after the migration: below the new repository's root for `kind = "repository"`, below the member's `path` for a
  workspace member.
- **`target_module`** is the module name after the migration: the module itself, or the longest `module_renames` prefix
  applied. Arborescent does not check that it agrees with the target layout; that is the rewriting tool's input, not its
  job.
- **`surface.bypasses[]`** lists the modules of *other* packages that import a module of this package which is not on
  its public surface (`module`, importing `package`, the module it `imports`). A package that declares no surface has none.
- **`policy` and `pyproject` are reserved** (release, pin and history policy: #56; derivable `pyproject.toml` fields:
  #60). They are always `null` in version 1 and typed `object | null`, so they can be filled without a version bump. The
  schema already requires `policy` to be `null` for a workspace member: a workspace member exports no history or pin
  information.
- **Gaps are named entries.** `ready` is `true` when no gap has severity `error`; a consumer can refuse on `ready` or on
  a code it cares about. Codes are the package-assignment rule ids (`package-unassigned-module`,
  `package-multiply-assigned-module`, `package-unowned-asset`, `package-multiply-assigned-asset`,
  `package-undeclared-dependency`; `package-spans-rings` has severity `info`) and these export-only codes:

| Code | Raised when |
|---|---|
| `package-undeclared-home` | the package has no `[packages.home]` |
| `package-undeclared-public-surface` | other packages import it and it declares no `public` surface |
| `package-undeclared-external` | a module imports a third-party name that `externals` does not list (or `externals` is absent) |
| `package-unplaceable-path` | an owned path lies outside the package `root` and has no `destination` |
| `package-unplaced-asset` | an `[[assets]]` glob owns a file that lies below none of the package's paths |
| `package-module-rename-collision` | two modules of one package get the same `target_module` |
| `package-unused-module-rename` | a `module_renames` entry matches no owned module |

The export-only codes exist only in the export; they are not `check` rules, so they never enter the baseline.

## Recipe: a git filter-repo paths file

No emitter ships in Arborescent; this is the whole conversion. `--paths-from-file` needs the paths to keep *and* the
renames (a rename line alone does not select a path), so the file lists both:

```sh
id=core
{
  jq -r --arg id "$id" '.packages[] | select(.id == $id) | .owned.path_renames[] | .source' migration.json
  jq -r --arg id "$id" '.packages[] | select(.id == $id)
      | .owned.path_renames[] | if .target == "." then "\(.source)/==>" else "\(.source)==>\(.target)" end' migration.json
} > paths.txt
git clone . ../"$id" && cd ../"$id" && git filter-repo --paths-from-file ../paths.txt
```

Verified on a synthetic two-commit repository with git-filter-repo from PyPI: history is kept, only the package's files
remain, and its renames apply. A package with `kind = "workspace-member"` is not split off with this recipe; its
`target` paths already include the member folder.

## Comparison with blinkered's `blinkered.toml`

Compared against blinkered 0.0.6 (`SPEC.md` and `README.md` of the PyPI source distribution; blinkered was read, not
run). blinkered works on one node of a repository at a time with a sparse checkout; Arborescent publishes the whole
catalog once. The shapes overlap on edges and differ on nodes, deliberately.

| | blinkered | Arborescent export |
|---|---|---|
| Node | a directory (not the root) that holds a `blinkered.toml`; no nesting; `id` is its path below `nodes_root` | a package: a *set* of modules and assets that can span rings and interleave with other packages' folders (a split follows module sets, not folders); `id` is a stable lowercase slug |
| Node location | `directory`, one folder per node | `owned.path_renames[]` and per-module paths; no single `directory` |
| Tags | free labels, `tags = [...]` | none. `kind`, `domain` and `capabilities` are typed fields; `capabilities` is the closest equivalent |
| Edges | `[edges]` table: free kinds mapping to target ids, plus a `python_imports` plugin that keeps import edges as kind `imports` | `edges[]` with `source`, `target`, `kind`: `depends_on` (declared) and `imports` (observed), so the two can differ |
| Graph JSON | `{"version": 2, nodes: [{id, directory, tags}], edges: [{source, target, kind, implied}]}` | `{"schema", "version": 1, packages, edges, gaps, ...}` |
| Transitive reduction | `implied` flag on an edge | none: graph analytics are out of scope |

Interoperability: edge objects use blinkered's field names and its default plugin kind (`imports`), so
`jq '.edges'` is a valid `edges` array apart from the `implied` flag (add `"implied": false`, or compute it with
blinkered). Nodes are not interchangeable in general, because a package is not a folder. For a package that occupies one
folder, `{"id": .id, "directory": <that folder>, "tags": .capabilities}` is a faithful node. The difference is
deliberate: adopting `directory` and `tags` as the node shape would force a package into one folder and discard the
typed fields a migration needs.
