# Ruff engine, ring-aware code rules, strict coverage and suggestions: design

Status: draft for approval. Date: 2026-10-26.

## Intent

ArchLint is the opinionated starting point for the first-party code of a Python project, aimed at one reader above all:
an LLM agent writing code. It should be impossible for that agent to quietly write code that is hard to migrate or hard
to test. Every rule is mechanical (a tool fails), never advice in prose.

Today ArchLint enforces *structure*: which ring imports which, which external packages each ring may import, OS
branching, a few repository practices. It enforces nothing about the code inside a module. Three of the author's repos
(`bripe-job-search`, `launch-runner`, `knowledge`) closed that gap with ~25 hand-written scanners and long prose in
`AGENTS.md`. A fourth (`deepinterview`) never did, and shows the cost: archlint covers 1 of ~60 modules there, through a
ring with `external = ["*"]`; ruff is configured but selects nothing useful and is not in CI; and in `apps/agent/src`
there are 1,115-line `worker.py` (one 277-line function), 69 broad `except`s, 102 function-level imports, clock/env/uuid/
subprocess calls in about 12 files, and 31 `monkeypatch`/`mock.patch` calls in tests.

## Goals

1. Ruff becomes a fourth engine (next to import-linter, tach and the type checker), configured from the policy.
2. Ring-dependent bans are native ArchLint rules (ambient clock, env, uuid, random, sleep, threads, subprocess,
   `mock.patch`), generalising today's `os-branching`.
3. Coverage is strict: every first-party module is classified; a catch-all ring or an uncovered source root is
   ratcheted baseline debt, never valid policy.
4. A report-only "suggestions" tier for hints that sometimes have a good reason to be ignored (file size first).
5. One baseline ratchet covers everything: ArchLint's own rules, ruff findings, and (separately) the suggestions never
   fail the check.
6. A repo-wide label (`standard rules` / `customised rules`): the shipped rule configuration untouched means
   standard; any customised rule configuration means not. Informally the author calls this "certified".

## Non-goals and anti-goals

- Not for dependencies or vendored code: only code you wrote is governed.
- Not a generic "good code" linter preset. Rules come from failure modes already seen in the author's repos.
- Not a hard file-length rule. A long file of CLI help or a table can be fine; a dense short one can be a mess.
- Anti-goal: an escape hatch an agent can use silently (a wildcard ring, a folder named `vendor/`, a ruff `noqa`
  sprinkled until the check passes). Anything that loosens is visible and counted.
- No certification machinery (hashes, signing, pinned versions) until something needs to read the label.

## Approaches considered

- **A. Ruff does everything, one run per ring.** Rejected. Ruff's banned-API rule (`TID251`) takes one global list, and
  per-file-ignores switch whole rules off, so different rings cannot ban different APIs from one config. Per-ring runs
  would work but cost N subprocesses, and ruff cannot do the file-size suggestion.
- **B. Native only, ruff stays generic.** Viable (ArchLint already parses every file and owns the ring map) but
  reinvents rules ruff does well (complexity, broad `except`, lazy imports, encoding).
- **C. Hybrid. Chosen** (the author said "do whatever you recommend"). Ruff for the rules that are the same in every
  ring; native rules for everything that depends on the ring. Both feed one baseline.

## Design

### 1. Ruff engine

- `archlint generate` also writes `ruff.toml`; `drift` covers it like the other generated files. Select list
  (initial, to be tuned by running the tool on the author's four repos): `PLC0415` (function-level import), `BLE001` and
  `E722` (broad/bare except), `C901` and `PLR0912/0913/0915` (complexity, arguments, statements), `TID252` (relative
  imports), `PLW1514` (explicit encoding), `DTZ` (naive datetimes), `T20` (print), `F`, `B`.
  `per-file-ignores` are generated from the ring map where a rule is wrong for a ring (e.g. composition may print).
- The engine runs through the existing `ToolRunner` port in parallel with the others, with `--output-format json`.
- **No silent suppression (verified on ruff 0.16.3, 2026-10-26).** Ruff is run with `--ignore-noqa`. A test file
  with a blind `except Exception` under `# noqa: BLE001`, `# ruff: noqa: BLE001`, `# ruff: ignore[BLE001]`,
  `# ruff: file-ignore[BLE001]` and a `# ruff: disable[BLE001]` block reported nothing without the flag and all five
  findings with it; the `# flake8: noqa` alias (bare and `# flake8: noqa: BLE001`) behaved the same, so seven forms
  in all. Every inline and file-level suppression form tried is ignored and every finding reaches the
  baseline. `--isolated` cannot be combined with `--config` (ruff errors), so the command uses `--config` alone
  with the effective `ruff.toml`, and a stray `[tool.ruff]` table in a nested `pyproject.toml` was confirmed not to
  change the rule set. Because a future ruff could add another suppression form, the builder turns this experiment
  into a contract test (all seven forms above plus a stray config, asserting all findings appear) that runs in CI, and
  the `ruff` extra's floor is the version it passes on. Config-level
  ignores exist only as `[tool.archlint.ruff]` overrides, which print "customised rules". An intentional exception is
  a baseline entry recorded with `--allow-growth --reason`, nothing else. ArchLint's own native rules ignore `noqa`
  too.
- **Install.** A new extra `ruff = ["ruff>=<floor verified by the spike>"]`, added to `all` and to `dev`.
  `--require-tools` fails if ruff is missing, as for the other engines; CI installs `.[dev]`, so it runs there. The
  README install section and the repo's own CI are updated in the same change.
- **The `Engine` port must change.** Today `Engine.passed(result)` returns only a bool, and engine output never reaches
  the baseline. Ruff's findings must become `Violation`s (`rule = "ruff-<CODE>"`, subject = file, detail = message with
  no line number, so the key is stable) so the ratchet can hold them. The builder must treat this as a shared-contract
  change: grep every caller and fake of `Engine` first and cover each with a test (see the global rule on changing
  shared code).
- Vendored folders are excluded from ruff the same way as from every other check (section 3).

### 2. Native ring-aware rules

- Generalise `SourceFacts.os_uses` into effect uses: clock (`time.time`, `datetime.now/utcnow/today`, `date.today`),
  env (`os.environ`, `os.getenv`), identity (`uuid.uuid1/4`), randomness (`random.*`, `secrets.*`), sleep, threads/
  process spawning (`threading`, `multiprocessing`, `subprocess`, `os.system`), and test-side `mock.patch` /
  `monkeypatch.setattr` of project modules. `os-branching` keeps its id and meaning.
- Each ring gets a `forbid` set of effect categories (preset-defaulted: inner rings forbid all; adapters allow most;
  composition allows all). A new rule id per category (never renamed once shipped, like every `Rule`).
- Inner-ring code receives these as ports (`Clock` exists already; env, id, random are the same shape), which is what
  makes the code testable without patching.

### 3. Strict coverage and vendored code

- A first-party `.py` file under the policy root that is not covered by `sources` or classified is an error
  (`unclassified` already exists; extend it to files outside `sources`, which is the deepinterview case).
- A ring whose `external` is `*` or whose `may_import` is `*` outside composition is reported as a ratcheted finding,
  not accepted policy. The baseline may only shrink.
- Folders named by convention (`vendor`, `vendored`, `third_party`, `site-packages`, and the existing `NOISE_DIRS`)
  are excluded automatically; explicit `exclude` globs also work. Every `check` lists the auto-excluded folders. Each
  auto-excluded folder is fingerprinted into the baseline (see "Vendor folders are fingerprinted"), so adding or
  changing code in `vendor/` is a visible, recorded act.

### 4. Suggestions tier (report only)

- A new severity: a suggestion prints in `check`, never changes the exit code, and is not in the baseline.
- First suggestion: file size, counted as logical code lines (skip blanks, comments, docstrings; mostly skip large
  data literals), default threshold chosen by the builder from the author's repos. Per-function size/complexity stays
  a ruff hard rule.
- No per-file pragma: the author prefers repo-level configuration, and the threshold is just config.

### 5. Certification

- `archlint check` prints `standard rules (baseline vN)` when the shipped rule configuration is untouched, else
  `customised rules: ruff, ...` naming what differs (exact guarantee under "Settled after the builder check").
  Customising is always allowed and never fails a check.
- Each engine accepts its own config (ruff included). Ruff overrides compose over the generated base (see "Settled
  after the builder check"); the label's exact guarantee and name are defined there too.

## Decided by the author

Ruff as a repo-wide-config engine, hybrid approach, strict coverage with debt ratchet, report-only suggestions measured
in logical lines, certification as a repo-wide label, convention-plus-explicit vendored exclusion.

## Left to the builder

Exact ruff select list and thresholds; baseline-version scheme; the name of the baseline growth flag; the data-literal
rule for logical lines; names for the new rule ids; staging (suggested order: coverage strictness, Engine port change
and ruff engine, effect rules, suggestions, certification label).

## Decided by the author, after the builder check

- **The baseline is shrink-only.** `archlint baseline` creates the first baseline, or removes stale entries from an
  existing one; it refuses to add entries to an existing baseline. Adding entries takes an explicit extra flag (name
  left to the builder), so an agent cannot silence a failing check by re-running the command. Adopting a new rule
  family (ruff, effect rules) on a repo that already has a baseline is therefore one deliberate, visible step.
  (Author's words: "whatever you recommend"; my recommendation, the job-search repo's `--growth-against` model.)
  Review follow-up on how adoption works, my design: `archlint baseline --adopt <family>` records the current
  findings of a rule family (`ruff`, `effects`, ...) only when the baseline has no entries for that family yet, and
  refuses otherwise. Any other growth needs `--allow-growth --reason "<text>"`; the reason and the entry count are
  written into the baseline file's `growth` log, so a reviewer sees them in the diff. Each log entry lists the exact
  keys it added (`{reason, added: [keys]}`, not just a count). `archlint check --growth-against <git ref>` fails
  unless (baseline keys now) minus (baseline keys at that ref) equals the union of `added` over the log entries new
  since that ref, so every added key is accounted for by a reasoned entry. Limit, stated plainly: this is a
  consistency check, not proof the command produced the file; someone who edits the baseline and its log by hand
  passes it. The protection against that is review of the baseline diff (the baseline file belongs under CODEOWNERS or
  an equivalent), and the design describes it as mechanical consistency plus a review convention.

## My calls (check these)

- Hybrid, not all-native, because ruff already does complexity/except/lazy-import well and ArchLint already owns the ring
  map for the rest.
- Ruff findings go into the baseline; suggestions do not (the author said hints may have good reasons to be ignored).
- `ruff-<CODE>` as the rule id family, and no line numbers in the baseline key.
- Auto-excluded vendor folders are listed every run and a new one is flagged.

Settled after the builder check (all my calls, not yet seen by the author):

- **Baseline shape.** Today's baseline is a sorted *set* of `rule|subject|detail` keys (`baseline.py`, `version: 1`).
  Ruff findings carry no line number, so the key is `ruff-<CODE>|<file>|<message>|<fp>`, where `<fp>` is a short hash
  of the finding's normalised source line (whitespace-stripped). Findings with the same key get `#2`, `#3` suffixes.
  Because the fingerprint comes from the code and not its position, inserting a line elsewhere shifts nothing, and
  "fix one, add a different one" changes a fingerprint and fails as a new key. Identical lines are interchangeable, so
  only their count matters, and a new duplicate always adds a `#n` key that is not in the baseline. Fixing one of two
  removes a key, which the ratchet reports as stale.
- **Only ArchLint's own rules and ruff feed the baseline.** import-linter, tach and the type checker keep the current
  pass/fail on exit code (goal 5 means "one ratchet", not "every engine parsed"). The `Engine` port gains a second,
  optional method that returns violations; `passed` stays for the others.
- **Ruff failure modes.** Ruff is run with `--exit-zero --output-format json`, so exit 0 means "ran to completion"
  and any other exit status is an infrastructure failure, never a finding. The stdout must parse completely as a JSON
  list (a truncated or partial stream fails to parse, which fails the engine). A diagnostic with no rule code
  (syntax error) or with code `E902` (cannot read the file) is also an infrastructure failure for that file, not a
  finding that could be baselined. The engine's result is the baseline diff over valid findings only. Ruff missing is
  "skipped", or "fail" with `--require-tools` (as today).
- **Wildcard rings.** Finding id `wildcard-ring`, subject the ring name, detail `external` or `may_import`, so it
  shrinks per ring and per axis. "Composition" means the outermost ring (last in `rings`), not a ring by name. Making a
  ring narrower but still `*` on that axis is not progress (same key).
- **Effect-rule policy.** A per-ring `forbid` list of effect categories, overridable per ring. Defaults by position:
  innermost ring forbids all, the outermost allows all, middle rings follow the preset. Effect findings go in the
  baseline. Custom ring names get the positional default.
- **Test patching.** Not a ring. Detected in files under a configured tests folder (same shape as the `secrets-guard`
  setting) when the first argument of `mock.patch` or `monkeypatch.setattr` is a string literal (or attribute) whose top
  package is a project package. Dynamically built targets are out of scope.
- **Vendor folders are fingerprinted (author decision).** Each auto-excluded folder is a ratcheted finding keyed
  `vendor-folder|<path>|<tree-hash>`, where the hash covers every file's relative path and content. A new folder, or
  any change inside an existing one (a file added, edited or removed), is a new key, so it fails until recorded in
  the baseline with `--allow-growth --reason "<text>"` (a legitimate vendor sync is exactly that deliberate act). This
  closes the hole where code dropped into a baselined `vendor/` folder escaped every check. A folder named `vendor`
  that holds first-party code is still excluded, but it is fingerprinted, so it cannot change unseen.
- **Ruff configuration ownership.** Overrides compose with the generated policy and never replace it. The repo sets
  keys under `[tool.archlint.ruff]` (extra select/ignore, per-file-ignores, thresholds); ArchLint merges them over the
  ring-derived base and writes the *effective* `ruff.toml`, which `drift` always checks. There is no "hand-written
  ruff.toml" mode in this version (an eject option can come later), so the effective configuration is always
  reproducible from the policy.
- **Effect detection scope.** Detected forms: `import x`, `import x as y`, `from x import f [as g]`, and attribute
  chains on those names (`x.f()`, `y.f()`, `g()`), resolved with a per-module alias table. Not detected, by design
  and documented as false negatives: calls through `getattr`, or stored in variables, and wrappers that hide the
  effect behind one of those forms. The claim is only "direct uses are mechanically forbidden", not "all effects" and
  not "wrappers are prevented" (a wrapper that reaches the API through an undetected form is not caught).
  One cheap mitigation: a ring that forbids any effect category also forbids the dynamic-import forms (`importlib`,
  `__import__`) as a category of their own, since they are the usual route around an import-based check.
- **Strict-coverage scan boundary.** First-party means "the `.py` files in the repository that are not git-ignored":
  build output (`build/`, `dist/`, egg-info) is normally ignored and so out of scope. Where git is unavailable the scan
  walks the policy root instead, minus `NOISE_DIRS`. Precedence: an explicit `exclude` glob, then the vendored-folder
  convention, then `sources`. A `.py` file that is not excluded and not matched by `classify` is `unclassified`, whether
  or not it sits under `sources`. Documentation examples, scripts and generated code that are tracked are therefore
  in scope until classified (a ring such as composition for scripts) or excluded. Excluded files are counted per
  folder and printed on every run, so the covered set cannot silently shrink.
- **Noise directories.** `NOISE_DIRS` (`.venv`, `node_modules`, caches, `.git`, ...) are skipped entirely, since they hold
  installed dependencies and tool state, not code you wrote. To stop them being a hiding place, a git-*tracked* `.py`
  file inside a noise directory is a ratcheted finding (`noise-dir-code|<path>|`), and a vendored folder inside a
  noise directory is not auto-excluded at all.
- **Adopting strict coverage on an existing repo.** `archlint discover` lists every in-scope `.py` file outside
  `sources`/`classify`/`exclude`, grouped by folder with counts and a suggested ring or exclude. After the author
  classifies or excludes what they choose, `archlint baseline --adopt coverage` records what is left as ratcheted
  `unclassified` debt, so there is no flood of new failures on day one and the debt can only shrink.
- **Certification guarantee.** The label says "the shipped rule configuration is in force." It is customised when any
  of these differ from the shipped baseline: ruff overrides, per-ring `forbid` overrides, thresholds, `preset`, or an
  explicit `exclude`. It makes no claim about ring layout or classification (every project defines its own), but
  coverage strictness (wildcard rings, uncovered files) is not configurable, so it is always on. The label is named
  `standard rules (baseline vN)` / `customised rules: ...`, not "certified", in `check` output, to match that scope.
