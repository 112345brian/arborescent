# DeepInterview Arborescent dogfood, 2026-10-26

Read-only scan of `/Users/bri/programming/deepinterview/apps/agent/pyproject.toml` at DeepInterview revision
`b10e636`. The scan used the Arborescent checkout in this worktree and did not write into DeepInterview.

Command:

```sh
uv run python -m arborescent \
  --config /Users/bri/programming/deepinterview/apps/agent/pyproject.toml \
  check --require-tools --no-cache
```

The check exited 1 with 143 unbaselined findings after parsing 16 policy-selected modules. The main strictness and
coverage findings were:

- 85 modules are unclassified. The current policy only classifies a subset of `deepinterview_agent.core.adapters`;
  the rest of the tracked project, including tests, is outside that classification.
- 1 non-composition wildcard ring: `other_adapters.external = ["*"]`.
- 187 Ty diagnostics, including diagnostics in the vendored `llmhost` package.
- 55 Ruff diagnostics under Ruff's native configuration.
- 1 inventoried `noqa` suppression in `deepinterview_agent.core.adapters.llm`.
- Tach's layer check passed. Tach's external-dependency check reported 12 undeclared `livekit` imports.
- import-linter could not read a configuration for this invocation.
- The existing `vendor/` tree was excluded and fingerprinted (25 files).

The Arborescent CLI itself passed. Generated import-linter and Tach presets differed from the checked-in files; drift was
informational. These counts describe only revision `b10e636` and should be refreshed before using them as an adoption
baseline.
