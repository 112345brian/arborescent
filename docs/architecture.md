# Arborescent's architecture

Arborescent is checked by Arborescent: the policy is the `[tool.arborescent]` table in `pyproject.toml`, and `arborescent check`
fails when this document's module table disagrees with it (practice `docs-sync`).

## Rings

Five rings, innermost first. A ring may import itself and the rings it lists; nothing may import an outer ring.

| ring | may import | external packages | role |
|---|---|---|---|
| domain | nothing | stdlib basics, no I/O | vocabulary and pure rules: the validated policy value types, rule ids (`Rule`), violations, facts about a parsed module, presets |
| ports | domain | `pathlib`, `contextlib` | the protocols the world must satisfy |
| application | domain, ports | `pathlib`, `concurrent` | use cases: generate, check, baseline, selftest, suggestions, practices |
| adapters | domain, ports | `pathlib`, `contextlib`, `json`, `hashlib`, `os`, `shutil`, `subprocess`, `tempfile`, `time`, `sysconfig`, `tomllib` | focused file-system, scanner, clock, subprocess, engine and type-checker adapters |
| composition | everything | `argparse`, `pathlib`, `os`, `shlex` | the command line: wires adapters to use cases and presents reports |

The application ring does no file I/O of its own: it reads, writes, lists, copies and creates temporary folders through
the `FileSystem` port (`Services.fs`), which is why `shutil` and `tempfile` are not on its list. What remains there is
computation, not I/O: `pathlib` for `Path` values (joining and `relative_to`; never `read_text`, `exists` and the like).
JSON goes through `FileSystem.read_json`/`write_json` and the stopwatch through the `Clock` port (`Services.clock`).
The application explicitly allows the `threads` effect because `engine.run_check` runs independent engines in a thread
pool; it still forbids every other effect category under the strict preset.
`tests/test_filesystem.py` scans
`application` and `composition` for any file access that bypasses the port.

Adapters do **not** import `application`. Use cases receive adapters through the ports (`Services` in `engine.py`), and
only `cli.py` knows both sides. Only `adapters` may branch on the OS.

## Modules

| module | ring |
|---|---|
| `arborescent` | domain |
| `arborescent.domain` | domain |
| `arborescent.facts` | domain |
| `arborescent.stdlib` | domain |
| `arborescent.presets` | domain |
| `arborescent.rules` | domain |
| `arborescent.ports` | ports |
| `arborescent.baseline` | application |
| `arborescent.bench` | application |
| `arborescent.config` | application |
| `arborescent.certification` | application |
| `arborescent.install_tools` | application |
| `arborescent.engine` | application |
| `arborescent.generate` | application |
| `arborescent.practices` | application |
| `arborescent.selftest` | application |
| `arborescent.suggestions` | application |
| `arborescent.adapters` | adapters |
| `arborescent.installer_adapter` | adapters |
| `arborescent.clock_adapter` | adapters |
| `arborescent.filesystem_adapter` | adapters |
| `arborescent.scanner` | adapters |
| `arborescent.tool_runner` | adapters |
| `arborescent.engines` | adapters |
| `arborescent.testing` | adapters |
| `arborescent.typecheckers` | adapters |
| `arborescent.__main__` | composition |
| `arborescent.cli` | composition |
| `arborescent.cli_install` | composition |
| `arborescent.cli_report` | composition |

## Data flow of `arborescent check`

1. `cli` loads the policy (`config.policy_from_mapping` -> `domain.Policy`, which validates itself; with its `presets.Preset`) and builds `Services`: a scanner, a tool
   runner, the file system (`LocalFileSystem`; tests use `testing.FakeFileSystem`), a clock, the engine adapters (`engines`: import-linter, tach) and the type-checker adapters.
2. `engine.run_check` starts every `Engine` in `Services` (import-linter, tach) and the chosen type checker in parallel. They run as
   subprocesses behind the `ToolRunner` port; a new engine is an adapter, not an edit to `run_check`. An unknown type
   checker name is a `PolicyError`, and the worker pool is always shut down.
3. Meanwhile the scanner finds every module and parses each file once (content-hash cached); `rules.check_modules`,
   suppression and `discard()` inventories, and the practices run over that one parse.
4. `generate.generate` renders the expected tool configs in memory; `engine.drift` diffs them against the committed files.
5. `baseline.apply` compares the violations with the baseline: new ones fail, entries that vanished are reported stale.

## Rules of the house

- The domain and ports rings do no I/O. A second import-linter contract in `pyproject.toml` enforces it directly, as a
  check that does not depend on the generated configs.
- A new behaviour switch goes into the policy (a preset field or a key), never into a check as a constant, so
  `generate`, `rules` and `selftest` agree on it.
- Every rule has a mutation in `selftest`: if a rule stops biting, `arborescent selftest` fails.
- Any additional clean-architecture constraint must be added to Arborescent's policy and enforced by a self-test mutation;
  keep Arborescent's own minimum-profile certification passing with `arborescent check --require-profile`. Update the
  standard/customized certification label when the added constraint changes whether the policy is standard.
- The policy defaults `forbid_circular_imports` to true. Arborescent's minimum profile requires it, and `selftest` injects
  a self-import to prove that the cycle rule remains active.
