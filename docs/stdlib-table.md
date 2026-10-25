# Stdlib table

`archlint.stdlib` holds the top-level standard-library module names per Python minor version (`3.11` to `3.14`). The
policy's `python` key picks the row, so a module counts as stdlib when the *target* version ships it, whatever
interpreter runs ArchLint (`distutils` is stdlib for `python = "3.11"` and third-party for `"3.12"`). A `python` value
outside the table is a `PolicyError` that lists the supported versions.

The table is data: `_BASE` is `sys.stdlib_module_names` of 3.11, and `_CHANGES` lists what each later minor added and
removed. `tests/test_stdlib.py` compares the row for the running interpreter with `sys.stdlib_module_names`, so a
mismatch fails the suite.

## Refresh (a new Python minor, or a patch that changed the names)

1. Dump the names from a real interpreter of each supported version:

   ```bash
   uv run --no-project --python 3.14 python -c "import sys; print(sorted(sys.stdlib_module_names))"
   ```

2. Diff each version against the previous one (`added = new - old`, `removed = old - new`) and edit `_CHANGES`; for a new
   minor add an entry for it. `_BASE` only changes if the oldest supported version is dropped (then fold the removed
   versions' changes into a new `_BASE`).
3. Run `python -m pytest tests/test_stdlib.py`; add a case to `test_version_dependent_membership` for a notable module.
4. Add a line to `CHANGELOG.md`.

Without a local interpreter, `stdlib/VERSIONS` in [typeshed](https://github.com/python/typeshed) lists the same
information (`module: min-max`), though it covers public modules only, not private ones such as `_thread`.
