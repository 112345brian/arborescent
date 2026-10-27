"""Arborescent: one architecture policy that compiles to import-linter, tach and a type checker, and enforces more.

- ``domain`` / ``facts`` / ``rules`` -- the policy, one-pass AST facts, and the pure checks (no I/O).
- ``ports`` -- ``ToolRunner``, ``TypeChecker``, ``ModuleScanner``.
- ``generate`` / ``engine`` / ``baseline`` / ``selftest`` / ``practices`` / ``bench`` -- the use cases.
- ``adapters`` / ``typecheckers`` -- subprocesses, the file system + content-hash cache, pyright/basedpyright/ty/pyrefly.
- ``testing`` -- fakes. ``cli`` -- the composition root.
"""

from .domain import Policy, PolicyError, Violation

__all__ = ["Policy", "PolicyError", "Violation"]
__version__ = "0.1.0"
