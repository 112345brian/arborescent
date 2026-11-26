"""Declaration facts: what a module's author tells tooling about the module, rather than what its code does.

The ``__arbor__`` dictionary (ring, role, ring reason, or why it is unusable), suppression comments (``noqa``,
``type: ignore``) and ``# deferred: <reason>`` justifications from one tokenize pass, and the PEP 723 ``# /// script``
block's dependencies."""

from __future__ import annotations

import ast
import io
import re
import tokenize
import tomllib
from collections.abc import Sequence

__all__ = ["DEV_TOOL_ROLE", "arbor_annotation", "comment_facts", "exemption_comments", "script_deps"]



_PEP723 = re.compile(r"^# /// script\n((?:#.*\n)+?)# ///$", re.MULTILINE)


_DEFERRED = re.compile(r"(?:^|#)\s*deferred\s*:\s*(\S.*)", re.IGNORECASE)
_COMPANION_EXEMPT = re.compile(r"(?:^|#)\s*companion-exempt\s*:\s*(\S.*)")
_SKIPPED_TOKENS = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT})


def _suppression_kind(comment: str) -> str | None:
    ruff = re.match(r"ruff\s*:\s*noqa\b(?:\s*:\s*(.*))?", comment, re.IGNORECASE)
    noqa = re.match(r"noqa\b(?:\s*:\s*(.*))?", comment, re.IGNORECASE)
    ignored_type = re.match(r"type\s*:\s*ignore\b(?:\s*\[([^\]]*)\])?", comment, re.IGNORECASE)
    if ruff is not None:
        return "ruff-noqa-coded" if (ruff.group(1) or "").strip() else "ruff-noqa-blanket"
    if noqa is not None:
        return "noqa-coded" if (noqa.group(1) or "").strip() else "noqa-blanket"
    if ignored_type is not None:
        return "type-ignore-coded" if (ignored_type.group(1) or "").strip() else "type-ignore-blanket"
    return None


def _token_error_text(error: tokenize.TokenError | SyntaxError) -> str:
    """``"<line>: <message>"`` for a tokenizer failure; the line is 0 when the error carries none."""
    message = str(error.args[0]) if error.args else type(error).__name__
    where = error.args[1] if len(error.args) > 1 and isinstance(error.args[1], tuple) else ()
    line = where[0] if where and isinstance(where[0], int) else 0
    if isinstance(error, SyntaxError) and line == 0 and error.lineno is not None:
        line = error.lineno
    return f"{line}: {message}"


def comment_facts(source: str) -> tuple[tuple[tuple[int, str], ...], tuple[tuple[int, str], ...], str]:
    """One tokenize pass: suppression directives ``(line, kind)`` and ``# deferred: <reason>`` justifications, then the error.

    ``ast`` accepts sources ``tokenize`` rejects (a doubled carriage return, #181), so the comment facts of such a file
    are unknowable: both tuples are empty and the third item is ``"<line>: <message>"``, ``""`` when the pass succeeded.
    Newlines are deliberately not normalised first: facts are keyed by ``ast`` line numbers, which a rewrite could shift.

    A deferred comment justifies the code line it trails, or the next code line when it stands alone (comment lines
    that continue it are skipped; a blank line ends it). The reason must not be empty.
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError) as error:
        return (), (), _token_error_text(error)
    suppressions, deferred = _comments_in(tokens)
    return suppressions, deferred, ""


def _comments_in(tokens: Sequence[tokenize.TokenInfo]) -> tuple[tuple[tuple[int, str], ...], tuple[tuple[int, str], ...]]:
    suppressions = tuple(
        (token.start[0], kind) for token in tokens
        if token.type == tokenize.COMMENT and (kind := _suppression_kind(token.string[1:].strip())) is not None
    )
    return suppressions, _justified(tokens, _DEFERRED)


def _justified(tokens: Sequence[tokenize.TokenInfo], pattern: re.Pattern[str]) -> tuple[tuple[int, str], ...]:
    """``(code line, reason)`` for each comment matching ``pattern``: the line it trails, or the next one when it stands alone."""
    found: list[tuple[int, str]] = []
    pending: str | None = None
    for token in tokens:
        if token.type == tokenize.COMMENT:
            reason = pattern.search(token.string[1:].strip())
            if reason is None:
                continue
            if token.line.lstrip().startswith("#"):
                pending = str(reason.group(1)).strip()
            else:
                found.append((token.start[0], str(reason.group(1)).strip()))
        elif token.type == tokenize.NL and not token.line.strip():
            pending = None
        elif pending is not None and token.type not in _SKIPPED_TOKENS:
            found.append((token.start[0], pending))
            pending = None
    return tuple(found)


def exemption_comments(source: str) -> tuple[tuple[int, str], ...]:
    """``# companion-exempt: <reason>`` comments (#231), placed like ``# deferred:``; empty when the source does not tokenize.

    Only a file that spells the marker is tokenized a second time, so the rest of a repository pays nothing for it.
    """
    if "companion-exempt" not in source:
        return ()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return ()  # reported as `untokenizable-source`
    return _justified(tokens, _COMPANION_EXEMPT)


_ARBOR_LEGAL_KEYS = frozenset({"ring", "role", "ring_reason"})
DEV_TOOL_ROLE = "dev_tool"
_CONDITIONAL_CONTAINERS = (ast.If, ast.Try, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)


def _is_arbor_target(stmt: ast.stmt) -> bool:
    is_assignment = isinstance(stmt, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "__arbor__" for target in stmt.targets
    )
    is_annotated_assignment = (
        isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "__arbor__"
    )
    return is_assignment or is_annotated_assignment


def _conditional_bodies(stmt: ast.stmt) -> list[list[ast.stmt]]:
    if isinstance(stmt, ast.If):
        return [stmt.body, stmt.orelse]
    if isinstance(stmt, ast.Try):
        return [stmt.body, stmt.orelse, stmt.finalbody, *(handler.body for handler in stmt.handlers)]
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        return [stmt.body, stmt.orelse]
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return [stmt.body]
    return []


def _find_arbor_assignments(stmts: list[ast.stmt], conditional: bool) -> list[tuple[ast.expr | None, bool]]:
    found: list[tuple[ast.expr | None, bool]] = []
    for stmt in stmts:
        if _is_arbor_target(stmt):
            value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
            found.append((value, conditional))
        elif isinstance(stmt, _CONDITIONAL_CONTAINERS):
            for body in _conditional_bodies(stmt):
                found += _find_arbor_assignments(body, True)
    return found


def _arbor_dictionary(found: list[tuple[ast.expr | None, bool]]) -> dict[str, object] | str:
    """The module's ``__arbor__`` literal dictionary, or the error message saying why it is not usable."""
    if any(conditional for _, conditional in found):
        return "__arbor__ must not be assigned inside conditional code"
    value = found[-1][0]
    try:
        annotation = ast.literal_eval(value) if value is not None else None
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return "__arbor__ must be a literal dictionary"
    if not isinstance(annotation, dict):
        return "__arbor__ must be a dictionary"
    unknown = sorted(set(annotation) - _ARBOR_LEGAL_KEYS)
    if unknown:
        return f"__arbor__ has unknown key(s): {', '.join(map(repr, unknown))}"
    return {str(key): item for key, item in annotation.items()}


def _arbor_reason(annotation: dict[str, object]) -> tuple[str | None, str | None]:
    """``(reason, error)`` for ``ring_reason``: a non-blank string without NUL characters, kept as written apart from trimming."""
    if "ring_reason" not in annotation:
        return None, None
    reason = annotation["ring_reason"]
    if not isinstance(reason, str) or "\x00" in reason or not reason.strip():
        return None, '__arbor__."ring_reason" must be a non-blank string'
    return reason.strip(), None


def _arbor_fields(annotation: dict[str, object]) -> tuple[str | None, str | None, str | None, str | None]:
    reason, reason_error = _arbor_reason(annotation)
    if reason_error is not None:
        return None, reason_error, None, None
    if "role" in annotation and annotation.get("role") != DEV_TOOL_ROLE:
        return None, f'__arbor__."role" must be "{DEV_TOOL_ROLE}"', None, None
    role = DEV_TOOL_ROLE if "role" in annotation else None
    if "ring" not in annotation:
        return None, (None if role is not None else '__arbor__ dictionary is missing "ring"'), role, None
    ring = annotation["ring"]
    if not isinstance(ring, str):
        return None, '__arbor__."ring" must be a string', None, None
    return ring, None, role, reason


def arbor_annotation(tree: ast.Module) -> tuple[str | None, str | None, str | None, str | None]:
    """``(ring, error, role, ring_reason)`` from the module's ``__arbor__`` assignment."""
    found = _find_arbor_assignments(list(tree.body), False)
    if not found:
        return None, None, None, None
    annotation = _arbor_dictionary(found)
    if isinstance(annotation, str):
        return None, annotation, None, None
    return _arbor_fields(annotation)


def script_deps(source: str) -> tuple[str, ...] | None:
    if "# /// script" not in source:
        return None
    block = _PEP723.search(source.replace("\r\n", "\n"))
    if not block:
        return None
    text = "\n".join(line[2:] if line.startswith("# ") else line[1:] for line in block.group(1).splitlines())
    try:
        deps = tomllib.loads(text).get("dependencies", [])
    except tomllib.TOMLDecodeError:
        return None
    if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
        return None
    return tuple(str(re.split(r"[<>=!~\[ ;]", dep, maxsplit=1)[0]) for dep in deps)
