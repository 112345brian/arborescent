# SDD ledger — plan: docs/superpowers/plans/2026-10-28-library-scopes.md

Preflight: existing isolated worktree is `experimental/arbor-ring-annotations`, at current `origin/main` after fetch. The previously completed annotation foundation is committed as a prerequisite on this development branch; latest `origin/main` has no new commits. The two commits on `origin/fix/tach-optional-externals` are not part of `origin/main` and remain a separate feature branch, so they are not merged into this task.

Pre-flight interface scan:
- Task 1 -> Task 2: Task 1 adds `Policy.scopes` and child policies; scanner must consume source roots/rings per scope. Compatible: plan's child ordinary `Policy` keeps scanner access to existing behavior.
- Task 2 -> Task 3: Task 2 produces complete `scope_by_module`; rules consume it to select local policies and enforce cross-scope imports. Compatible, but `ScanData` vs `ScanResult` integration needs review during Task 2.
- Task 3 -> Task 4: Task 3 produces scope-aware ring/cross-scope rule behavior; generator and certification need equivalent scope ownership. Compatible; both should consume the same complete indexes.
- Task 4 -> Task 5: Task 4 composes generated configs and certification; remaining CLI/practices consumers use scope ownership. Compatible; keep one root policy and do not duplicate scan ownership.
- Task 5 -> Task 6: Task 5 exposes user-facing scope behavior; Task 6 documents that behavior. Compatible; docs examples must match the shipped TOML parser and policy.

Ruling: Execute the six tasks inline because they form a strongly sequential chain of shared policy/scanner/rule interfaces; splitting them as parallel work would require coordination around shared files and schemas. Cost if wrong: less independent review during implementation; a separate whole-branch review remains required.
Task 1: complete (commits a5de2fc..caa4bf8, tests: uv run pytest -p no:tach tests/test_policy.py -q → .........................................                                [100%])
Task 2: complete (commits caa4bf8..e300b49, tests: uv run pytest -p no:tach tests/test_scanner.py tests/test_engine.py -q → ..............................................                           [100%])
Task 3: complete (commits e300b49..b0aa787, tests: uv run pytest -p no:tach tests/test_rules.py tests/test_effect_policy.py tests/test_engine.py -q → .........                                                                [100%])
Task 4: complete (commits b0aa787..052ed93, tests: uv run pytest -p no:tach tests/test_generate.py tests/test_engine.py tests/test_certification.py -q → ..............................................                           [100%])
Task 5: complete (commits 052ed93..5e8c1b4, tests: uv run pytest -p no:tach tests/test_practices.py tests/test_selftest.py tests/test_engine.py -q → ...............................................................          [100%])
