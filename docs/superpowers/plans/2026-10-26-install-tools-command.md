# Missing checker setup implementation plan

**Goal:** Keep `arborescent check` non-mutating by default while making missing checker setup discoverable and safe through
explicit `--install-tools` and `arborescent install-tools` commands.

**Design:** `docs/superpowers/specs/2026-10-26-missing-tool-install-prompt-design.md` (approved).

## Tasks

1. **Model tool requirements and resolve executables**
   - [x] Add one adapter registry mapping tool name to executable, package requirement, and supported extra/version floor.
   - [x] Resolve executables from `sysconfig`'s active scripts directory, then PATH; use the same path for availability and
     subprocess execution.
   - [x] Test POSIX/Windows scripts directory lookup, PATH fallback, package mapping against declared project extras, and
     run an offline package-manager dry run against the real requirements.

2. **Add the explicit installer command**
   - [x] Add `install-tools` and optional `--type-checker` / `--yes` flags.
   - [x] List only missing tools needed by the loaded policy; prompt once with No as the default.
   - [x] Use `sys.executable -m pip` when available; otherwise use `uv pip` targeting `sys.executable` when available.
   - [x] Never pass `--upgrade`; run `pip check` or `uv pip check` and re-resolve tools after installation.
   - [x] In non-interactive mode without `--yes`, print the command and make no changes.
   - [x] For Poetry projects, add a dedicated `arborescent` checker group, include it in `dev`, update the lock, and
     validate with `poetry check --lock`.
   - [x] Resolve executables from the Poetry environment for checks and refresh that path after installation.
   - [x] Cover accepted, declined, non-interactive, installer unavailable, install failure, dependency-check failure, and
     successful post-install re-resolution.

3. **Keep check read-only and align certification**
   - [x] By default, print the exact environment-specific install command and preserve skip/fail behavior.
   - [x] Add `check --install-tools` with a default-No prompt and `check --install-tools --yes` for unattended setup;
     continue to checks after a declined install.
   - [x] Make `--require-profile` imply `--require-tools`.
   - [x] Test that `check` never runs an installer and profile mode cannot pass with missing tools.

4. **Docs and verification**
   - [x] Document `install-tools`, active interpreter resolution, uv fallback, and non-interactive behavior.
   - [x] Run the targeted tests, full suite, Arborescent checks, and `git diff --check`.

Pip and uv do not edit a project manifest. Poetry updates `pyproject.toml` and `poetry.lock` through its CLI, plus a
targeted TOML group-inclusion edit so Arborescent's checker group remains part of development dependencies. No commit or
push is in scope.
