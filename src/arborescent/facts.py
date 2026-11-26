"""One AST pass per file: every import (lazy and type-only ones included), effect uses, PEP 723 metadata.

The result is plain data (``SourceFacts``) so it can be cached by content hash and reused by every check. Each family of
facts is extracted by its own module (``import_syntax``, ``effect_facts``, ``declaration_facts``, ``definition_facts``,
``call_facts``, ``forwarder_facts``, ``purity_facts``); this one holds the result types and the one pass that fills them.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .call_facts import (
    FunctionCalls,
    FunctionSpan,
    bare_calls,
    call_aliases,
    call_sites,
    discard_call_lines,
    dynamic_imports,
    function_calls,
    function_spans,
)
from .declaration_facts import (
    arbor_annotation,
    comment_facts,
    exemption_comments,
    script_deps,
)
from .definition_facts import (
    TopLevelDef,
    main_guard_line,
    purity_helpers,
    result_defs,
    surface,
    top_level_defs,
    weak_port_returns,
)
from .effect_facts import EffectUse, effect_uses
from .forwarder_facts import Forwarder, forwarders
from .import_syntax import RawImport, import_aliases, raw_imports
from .purity_facts import PurityFacts, PurityUnit, purity_facts

__all__ = ["Forwarder", "ModuleInfo", "PurityFacts", "PurityUnit", "SourceFacts", "extract"]


@dataclass(frozen=True)
class SourceFacts:
    imports: tuple[RawImport, ...]
    effect_uses: tuple[EffectUse, ...]
    script_deps: tuple[str, ...] | None  # PEP 723 `dependencies`, None when the file has no block
    suppressions: tuple[tuple[int, str], ...] = ()  # (line, kind); comments only, not strings
    discard_calls: tuple[int, ...] = ()  # `discard(value, reason=...)`; no type-aware Result claim
    top_level_defs: tuple[TopLevelDef, ...] = ()  # module-level classes and functions only
    arbor_ring: str | None = None
    arbor_error: str | None = None
    arbor_role: str | None = None  # `__arbor__ = {"role": "dev_tool"}`: the module claims to be developer tooling
    file_refs: tuple[int, ...] = ()  # lines that read `__file__`: a location-dependent path breaks when the module moves
    main_guard: int = 0  # line of a top-level `if __name__ == "__main__":`, 0 when there is none: the module is also a program
    string_literals: tuple[tuple[int, str], ...] = ()  # (line, value) of identifier-like string constants, docstrings excluded (#112)
    forwarders: tuple[Forwarder, ...] = ()  # module-level pass-through functions (#138)
    weak_port_returns: tuple[tuple[int, str, str], ...] = ()  # (line, "Protocol.method", why): a Protocol method whose return type says nothing (#140)
    surface_size: int = 0  # public names: a static `__all__`, else public top-level bindings (#136)
    surface_line: int = 0  # line of `__all__`, else of the first public binding
    reexports: tuple[tuple[str, int, str, str], ...] = ()  # surface names bound by an import: (name, level, module, imported name or "")
    deferred: tuple[tuple[int, str], ...] = ()  # (code line, reason): lines justified by a `# deferred: <reason>` comment (#149)
    calls: tuple[tuple[int, str], ...] = ()  # (line, dotted callee) of calls on an imported or module-level name; a relative import keeps its dots (#85)
    result_defs: tuple[str, ...] = ()  # module-level functions annotated `-> *Result` / `-> *Outcome` (#141)
    bare_calls: tuple[tuple[int, str], ...] = ()  # (line, dotted callee) of a call used as a whole statement; relative targets keep leading dots (#141)
    function_calls: tuple[FunctionCalls, ...] = ()  # module-level functions and class methods that call something imported (#104)
    module_doc: str | None = None  # the module docstring, cleaned; None when there is none (#110)
    arbor_reason: str | None = None  # `__arbor__["ring_reason"]`: why the module sits in a ring a tighter one would also fit (#161)
    dynamic_imports: tuple[tuple[int, str], ...] = ()  # (line, literal module name, or "" when the target is not a plain absolute string literal) (#161)
    purity: PurityFacts = field(default_factory=PurityFacts)  # top-level definition spans plus logging, memo and module-state facts (#173)
    untokenizable: str = ""  # "<line>: <message>" when `ast` accepted the source but `tokenize` did not, so `suppressions` and `deferred` are unknown (#181)
    function_spans: tuple[FunctionSpan, ...] = ()  # every function and method with its line span, to place `calls` inside them (#229)
    companion_exempt: tuple[tuple[int, str], ...] = ()  # (code line, reason) of `# companion-exempt:` comments (#231)

    @property
    def os_uses(self) -> tuple[tuple[int, str], ...]:
        """Compatibility view for the original OS-branching rule."""
        return tuple((use.line, use.target) for use in self.effect_uses if use.category == "os-branching")


@dataclass(frozen=True)
class ModuleInfo:
    name: str
    path: str  # posix, relative to the policy root
    is_package: bool
    facts: SourceFacts


def extract(source: str, filename: str = "<source>") -> SourceFacts:
    tree = ast.parse(source, filename=filename)
    arbor_ring, arbor_error, arbor_role, arbor_reason = arbor_annotation(tree)
    suppressions, deferred, untokenizable = comment_facts(source)
    nodes = list(ast.walk(tree))  # one walk, shared by every fact below
    aliases = import_aliases(nodes)  # computed once: each fact below reads it
    calls_aliases = call_aliases(nodes, aliases)
    return SourceFacts(
        raw_imports(tree),
        effect_uses(tree, nodes, aliases),
        script_deps(source),
        suppressions,
        discard_call_lines(nodes),
        top_level_defs(tree, aliases),
        arbor_ring,
        arbor_error,
        arbor_role,
        _file_ref_lines(nodes),
        main_guard_line(tree),
        _string_literals(nodes),
        forwarders(tree),
        weak_port_returns(nodes, aliases),
        *surface(tree),
        deferred,
        call_sites(tree, nodes, calls_aliases),
        result_defs(tree),
        bare_calls(nodes, calls_aliases),
        function_calls(tree, aliases),
        ast.get_docstring(tree),
        arbor_reason,
        dynamic_imports(nodes, aliases),
        purity_facts(tree, nodes, aliases, purity_helpers(aliases)),
        untokenizable,
        function_spans(tree, nodes, calls_aliases),
        exemption_comments(source),
    )


_LITERAL_NAME = re.compile(r"[A-Za-z_][\w.\-]{0,79}")
_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _string_literals(nodes: Sequence[ast.AST]) -> tuple[tuple[int, str], ...]:
    """Identifier-like string constants (an environment variable, a key, a protocol name); a docstring is prose, not a spelling."""
    docstrings = {
        id(node.body[0].value) for node in nodes
        if isinstance(node, _DOCSTRING_OWNERS) and node.body and isinstance(node.body[0], ast.Expr)
    }
    return tuple(sorted({
        (node.lineno, node.value) for node in nodes
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
        and _LITERAL_NAME.fullmatch(node.value)
    }))


def _file_ref_lines(nodes: Sequence[ast.AST]) -> tuple[int, ...]:
    return tuple(sorted(node.lineno for node in nodes if isinstance(node, ast.Name) and node.id == "__file__"))
