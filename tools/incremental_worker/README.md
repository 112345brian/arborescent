# Incremental architecture analysis prototype

The Python driver watches one Python source root and reads its Arborescent policy.
The Rust worker retains the source facts and reverse call edges between saves.
On a save, it parses the changed modules and replaces their edges. It walks backwards
from the requested effect targets to provide call evidence. Python applies the policy to
that evidence and calls the existing import rules.

From the repository root, with Arborescent installed in the active Python environment:

```sh
cargo build --release --locked --manifest-path tools/incremental_worker/Cargo.toml
python -m tools.incremental_analysis \
  --worker tools/incremental_worker/target/release/arborescent-incremental-worker \
  --policy /path/to/pyproject.toml --source-root /path/to/src --watch
```

The policy file can also be named `arborescent.toml`. Use a source root under
the policy directory and classify the example modules in the policy. On Windows, add
`.exe` to the worker path. Watch mode prints a complete JSON diagnostic list whenever
the findings change, including an empty list when findings clear. Omit `--watch`
for a fresh full scan with a nonzero exit code on violations or syntax errors.

The prototype exercises internal and cross-scope import rules and statically resolved
calls between synchronous, non-generator module-level functions. The effect target is `os.getenv`. It uses
policy-file classification, with one source root and UTF-8 source files. The Python driver
polls and reads that source root. Each source update returns a full evidence snapshot and
reevaluates the Python import rules. This keeps the prototype small while testing
incremental parsing and call-edge updates. Its limited function analysis does not
provide purity guarantees or coverage for dynamic dispatch and arbitrary Python binding.

Run the save-replay tests from the repository root:

```sh
python -m pytest -p no:tach tools/incremental_worker/test_save.py
```
