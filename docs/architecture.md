# ArchLint's architecture

ArchLint is checked by ArchLint: the policy is the `[tool.archlint]` table in `pyproject.toml`, and `archlint check`
fails when this document's module table disagrees with it (practice `docs-sync`).

## Rings

Five rings, innermost first. A ring may import itself and the rings it lists; nothing may import an outer ring.

| ring | may import | external packages | role |
|---|---|---|---|
| domain | nothing | stdlib basics, no I/O | vocabulary and pure rules: the policy, rule ids (`Rule`), violations, facts about a parsed module, presets |
| ports | domain | `pathlib` | the protocols the world must satisfy |
| application | domain, ports | `pathlib`, `json`, `tempfile`, `shutil`, `time`, `concurrent` | use cases: generate (`write_generated`), check, baseline, selftest, practices |
| adapters | domain, ports | `pathlib`, `json`, `hashlib`, `os`, `shutil`, `subprocess`, `time` | the real file system, subprocess and type-checker implementations |
| composition | everything | `argparse`, `pathlib` | the command line: wires adapters to use cases |

Adapters do **not** import `application`. Use cases receive adapters through the ports (`Services` in `engine.py`), and
only `cli.py` knows both sides. Only `adapters` may branch on the OS.

## Modules

| module | ring |
|---|---|
| `archlint` | domain |
| `archlint.domain` | domain |
| `archlint.facts` | domain |
| `archlint.presets` | domain |
| `archlint.rules` | domain |
| `archlint.ports` | ports |
| `archlint.baseline` | application |
| `archlint.bench` | application |
| `archlint.config` | application |
| `archlint.engine` | application |
| `archlint.generate` | application |
| `archlint.practices` | application |
| `archlint.selftest` | application |
| `archlint.adapters` | adapters |
| `archlint.testing` | adapters |
| `archlint.typecheckers` | adapters |
| `archlint.__main__` | composition |
| `archlint.cli` | composition |

## Data flow of `archlint check`

1. `cli` loads the policy (`config` -> `domain.Policy`, with its `presets.Preset`) and builds `Services`: a scanner, a tool
   runner and the type-checker adapters.
2. `engine.run_check` starts import-linter, tach and the type checker in parallel. They run as subprocesses behind the
   `ToolRunner` port.
3. Meanwhile the scanner finds every module and parses each file once (content-hash cached); `rules.check_modules` and
   the practices run over that one parse.
4. `generate.generate` renders the expected tool configs in memory; `engine.drift` diffs them against the committed files.
5. `baseline.apply` compares the violations with the baseline: new ones fail, entries that vanished are reported stale.

## Rules of the house

- The domain and ports rings do no I/O. A second import-linter contract in `pyproject.toml` enforces it directly, as a
  check that does not depend on the generated configs.
- A new behaviour switch goes into the policy (a preset field or a key), never into a check as a constant, so
  `generate`, `rules` and `selftest` agree on it.
- Every rule has a mutation in `selftest`: if a rule stops biting, `archlint selftest` fails.
