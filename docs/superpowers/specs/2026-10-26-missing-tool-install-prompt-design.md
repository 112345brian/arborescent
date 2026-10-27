# Missing checker installation prompt

**Status:** Approved, including Poetry group inclusion.  
**Date:** 2026-10-26.

## Intent

When a project selects an Arborescent strictness and checker set, Arborescent should identify missing checker executables and
make setup straightforward. Installation is always opt-in: ordinary checks show an actionable command, while the
`--install-tools` flag enables a prompt during `check`.

## Current behavior

`check` invokes import-linter, Tach, Ruff, and the policy-selected type checker. A missing executable is skipped by
default and fails with `--require-tools`. The project publishes checker-specific extras and version constraints, but
Arborescent does not currently connect a missing executable to an installation workflow.

## Proposed behavior

- `check` resolves missing required tools, prints the exact environment-specific install command naming each missing
  tool, then applies the existing skip/fail behavior. It never prompts or mutates the environment by default.
- `check --install-tools` prints the install command, prompts with No as the default, installs on confirmation, and
  then runs checks. If declined, it runs checks with the existing skip/fail behavior. `--yes` skips the prompt and is
  valid only with `--install-tools`.
- `arborescent install-tools` resolves the configured tool set and lists missing tools and the version-constrained package
  requirements that will be installed. It asks once for confirmation, defaulting to No. `--yes` allows an explicitly
  requested unattended setup. A declined prompt changes nothing.
- Resolve an executable from the active interpreter's scripts directory first (via Python's scripts path), then PATH.
  Use the same resolver before and after installation so an installed tool is not misreported just because its scripts
  directory is absent from PATH. Do not assume module invocation is supported by every tool.
- Maintain an explicit mapping from adapter to executable, distribution, and declared version constraint. Keep these
  constraints aligned with `pyproject.toml` extras. Install only missing selected tools, without `--upgrade`, then
  re-resolve executables and run `pip check` (or the equivalent supported by the selected installer).
- For a Poetry-managed project (`poetry.lock` or Poetry project configuration is present and Poetry is available), use
  Poetry's project dependency workflow: `poetry add --group arborescent <requirements...>`. Poetry dependency groups are
  named labels, not nested groups. Keep checker requirements isolated in an optional `arborescent` group and include that
  group from `dev` (`include-groups = ["arborescent"]`), so the checkers are development dependencies and are installed
  with the project's normal dev group. Preserve other group entries and TOML content. Validate with `poetry check
  --lock` and then refresh the Poetry environment's executable directory.
- During ordinary checks in a Poetry project, query `poetry env info --executable` and include that interpreter's
  scripts directory in the same executable resolver used for availability and execution. Do not create the Poetry
  environment during `check`; if it does not exist, report missing tools and direct the user to `install-tools`.
- Outside Poetry projects, prefer `sys.executable -m pip install ...` when pip is available. If pip is absent and uv
  can target the active interpreter, use `uv pip install --python <sys.executable> ...` and `uv pip check --python`.
  If no supported installer can target the active interpreter, print the exact command and exit without changing the
  environment.
- A failed install, failed dependency check, or executable still missing is an error; never report success based only
  on the installer exit code.
- `--require-profile` implies `--require-tools`, so certification cannot silently skip a checker in local or CI runs.

## Approaches

1. Prompt on every `check` when tools are missing. Rejected because hooks, editors, and CI can unexpectedly prompt.
2. `check` diagnoses by default; `--install-tools` enables a confirmed install in the same invocation, and the
   `install-tools` command provides the same flow without running checks (selected). This keeps mutation explicit and
   lets developers set up and check in one command.
3. Print commands only. Rejected because Arborescent can target the active interpreter and the user wants an opt-in
   guided install.

## Constraints and non-goals

- Installation is explicit and requires confirmation unless `--yes` is provided.
- CI and other non-interactive `check` callers remain read-only and deterministic unless `--install-tools` is passed;
  without a usable TTY, that flag prints the command and skips installation unless `--yes` is provided.
- This does not change which strictness rules are enabled, install optional type checkers not selected by policy, or
  rewrite checker configuration files.
- The install command targets checker distributions using the ranges declared in Arborescent's extras. Pip and uv install
  into the active interpreter without editing project manifests; Poetry adds the isolated checker group, includes it
  in the dev group, and updates its lock so future syncs preserve the tools.

## Open implementation detail

Test adapter-to-distribution and adapter-to-executable mappings against real installed entry points. Dry-run package
manager checks should verify distribution names; post-install dependency/lock checks and executable re-resolution must
detect incomplete setup.
