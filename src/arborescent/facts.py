"""One AST pass per file: every import (lazy and type-only ones included), effect uses, PEP 723 metadata.

The result is plain data (``SourceFacts``) so it can be cached by content hash and reused by every check. Each family of
facts is extracted by its own module (``import_syntax``, ``effect_facts``, ``declaration_facts``, ``definition_facts``,
``call_facts``, ``forwarder_facts``, ``purity_facts``, ``import_time_facts``, ``seam_facts``); this one holds the result types, the one
pass that fills them, and the two queries over them that both ``unreachable-patch`` and its selftest probe ask.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace

from .call_facts import (
    CallShape,
    FunctionCalls,
    FunctionSpan,
    absolute_callee,
    bare_calls,
    call_aliases,
    call_shapes,
    call_sites,
    discard_call_lines,
    dotted_name,
    dynamic_imports,
    function_calls,
    function_spans,
    patch_targets,
    path_calls,
    private_attributes,
    unqualified_from_calls,
)
from .declaration_facts import (
    arbor_annotation,
    comment_facts,
    exemption_comments,
    script_deps,
)
from .definition_facts import (
    ClassMembers,
    ClassShape,
    TopLevelDef,
    class_shapes,
    main_guard_line,
    member_shapes,
    purity_helpers,
    result_defs,
    surface,
    top_level_defs,
    weak_port_returns,
)
from .effect_facts import (
    EffectUse,
    SharedState,
    SharedWrite,
    StateWrite,
    merge_sites,
    module_effects,
    rebound_names,
    running_ids,
    state_category,
)
from .forwarder_facts import Forwarder, forwarders
from .import_syntax import RawImport, import_aliases, raw_imports, walk_nodes
from .import_time_facts import (
    ImportCall,
    ImportTimeFacts,
    Walked,
    evaluated,
    import_time_facts,
)
from .purity_facts import PurityFacts, PurityUnit, purity_facts
from .seam_facts import SeamFacts, SeamFrame, SeamParam, SeamUse, seam_facts

__all__ = [
    "ClassMembers", "ClassShape", "Forwarder", "ImportCall", "ImportTimeFacts", "ModuleInfo", "PurityFacts", "PurityUnit", "SeamFacts",
    "SeamFrame", "SeamParam", "SeamUse", "SharedState", "SharedWrite", "SourceFacts", "extract", "file_module", "link_shared_state",
    "patched_targets", "unreached_bindings",
]


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
    import_time: ImportTimeFacts = field(default_factory=ImportTimeFacts)  # calls made at import and what each function runs (#230)
    seams: SeamFacts = field(default_factory=SeamFacts)  # real injectable effects inside functions, with the parameters around them (#233)
    function_spans: tuple[FunctionSpan, ...] = ()  # every function and method with its line span, to place `calls` inside them (#229)
    companion_exempt: tuple[tuple[int, str], ...] = ()  # (code line, reason) of `# companion-exempt:` comments (#231)
    call_shapes: tuple[CallShape, ...] = ()  # the arguments of each call on an imported or module-level name (#234)
    class_shapes: tuple[ClassShape, ...] = ()  # what each module-level class's constructor accepts (#234)
    private_attrs: tuple[tuple[int, str], ...] = ()  # (line, dotted) of ``._name`` reached through an import; relative targets keep leading dots (#218)
    patches: tuple[tuple[int, str], ...] = ()  # (line, dotted "module.name") a test double replaces (#226)
    from_calls: tuple[tuple[int, str, str], ...] = ()  # (line, origin "X.name", local name) of ``from X import name`` called bare in a function (#226)
    classes: tuple[ClassMembers, ...] = ()  # every class's bases and member names (#219)
    state_writes: tuple[StateWrite, ...] = ()  # writes to module-level mutable state outside the module's own locks (#232)
    module_locks: tuple[str, ...] = ()  # module-level names bound to a lock, so another module's `with locks.L:` can be believed (#232)
    shared_state: SharedState = field(default_factory=SharedState)  # containers others can write, and writes to imported names (#194)
    path_calls: tuple[tuple[int, str], ...] = ()  # (line, "pathlib.Path.<method>") of a method on a value known to be a Path (#229)

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


def file_module(path: str) -> tuple[str, bool]:
    """A test file's path as a dotted name, enough to resolve its relative imports."""
    parts = path.removesuffix(".py").split("/")
    is_package = parts[-1] == "__init__"
    return ".".join(parts[:-1] if is_package else parts), is_package


def patched_targets(tests: Mapping[str, SourceFacts]) -> dict[str, dict[str, int]]:
    """Test file -> dotted ``module.name`` it patches -> the first line that does."""
    out: dict[str, dict[str, int]] = {}
    for path, facts in tests.items():
        name, is_package = file_module(path)
        found = out.setdefault(path, {})
        for line, target in facts.patches:
            if (dotted := absolute_callee(name, is_package, target)) is not None:
                found.setdefault(dotted, line)
    return out


def unreached_bindings(modules: Mapping[str, ModuleInfo], known: Collection[str]) -> list[tuple[str, str, str, int]]:
    """``(module, origin "X.name", local name, line)`` of each bare call through ``from X import name``, X first-party.

    ``from package import submodule`` is a module, not a bound function, and is left out."""
    return [
        (name, origin, local, line)
        for name, info in sorted(modules.items())
        for line, target, local in info.facts.from_calls
        if (origin := absolute_callee(name, info.is_package, target)) is not None
        and origin not in known and (owner := origin.rpartition(".")[0]) in known and owner != name
    ]


_REEXPORT_DEPTH = 5  # a chain of package re-exports has to end


def _imported_container(modules: Mapping[str, ModuleInfo], dotted: str, depth: int = _REEXPORT_DEPTH) -> str | None:
    """``module.REG`` or ``module.Class.attr`` when ``dotted`` names a container of a scanned module, re-exports followed."""
    parts = dotted.split(".")
    cut = next((end for end in range(len(parts) - 1, 0, -1) if ".".join(parts[:end]) in modules), None)
    if cut is None:
        return None
    module, rest = modules[".".join(parts[:cut])], parts[cut:]
    if ".".join(rest) in module.facts.shared_state.containers:
        return dotted
    for exported, level, source, imported in module.facts.reexports if depth else ():
        if exported == rest[0]:
            origin = "." * level + ".".join(part for part in (source, imported, *rest[1:]) if part)
            target = absolute_callee(module.name, module.is_package, origin)
            return None if target is None else _imported_container(modules, target, depth - 1)
    return None


def _linked(modules: Mapping[str, ModuleInfo], info: ModuleInfo) -> ModuleInfo:
    facts = info.facts
    if not (facts.shared_state.writes or facts.import_time.function_writes):
        return info

    def container(target: str) -> str | None:
        dotted = absolute_callee(info.name, info.is_package, target)
        return None if dotted is None else _imported_container(modules, dotted)

    uses = {
        EffectUse(write.line, state_category(found.rpartition(".")[2]), f"{write.function}.{found}")
        for write in facts.shared_state.writes
        if (found := container(write.target)) is not None
    }
    effects = {
        (function, state_category(found.rpartition(".")[2]), f"{function.rpartition('.')[2]}.{found}")
        for function, target in facts.import_time.function_writes
        if (found := container(target)) is not None
    }
    if uses <= set(facts.effect_uses) and effects <= set(facts.import_time.function_effects):
        return info
    import_time = replace(facts.import_time, function_effects=tuple(sorted({*facts.import_time.function_effects, *effects})))
    return replace(info, facts=replace(facts, effect_uses=tuple(sorted({*facts.effect_uses, *uses})), import_time=import_time))


def link_shared_state(modules: Mapping[str, ModuleInfo]) -> dict[str, ModuleInfo]:
    """Each module with its writes to another module's containers added as ``registry``/``cache`` uses (#194): only the whole
    set of modules says whether an imported name is a container. A write to anything not scanned stays unseen."""
    return {name: _linked(modules, info) for name, info in modules.items()}


def extract(source: str, filename: str = "<source>") -> SourceFacts:
    tree = ast.parse(source, filename=filename)
    arbor_ring, arbor_error, arbor_role, arbor_reason = arbor_annotation(tree)
    suppressions, deferred, untokenizable = comment_facts(source)
    nodes = walk_nodes(tree)  # one walk, shared by every fact below
    of = _by_class(nodes)  # a fact that only looks at some node classes is handed those, not the whole walk
    calls = of[ast.Call]
    functions = [*of[ast.FunctionDef], *of[ast.AsyncFunctionDef]]
    definitions = [*functions, *of[ast.ClassDef]]
    imports = [node for node in nodes if node.__class__ is ast.Import or node.__class__ is ast.ImportFrom]  # a later binding wins
    aliases = import_aliases(imports)  # computed once: each fact below reads it
    calls_aliases = call_aliases(of[ast.ImportFrom], aliases)
    rebound = rebound_names([*of[ast.Name], *definitions, *of[ast.arg]])
    effects = module_effects(tree, nodes, aliases, rebound, calls_aliases)
    sites = effects.sites
    running = running_ids([*calls, *definitions])
    at_import = list(evaluated(tree.body))
    on_paths = path_calls(tree, calls, aliases)
    branches = [*of[ast.If], *of[ast.Try], *of[ast.TryStar], *of[ast.Match]]  # with a function's calls, they give it paths
    return SourceFacts(
        raw_imports(tree),
        merge_sites(sites, {id(node) for node in at_import}, running),
        script_deps(source),
        suppressions,
        discard_call_lines(calls),
        top_level_defs(tree, aliases),
        arbor_ring,
        arbor_error,
        arbor_role,
        _file_ref_lines(of[ast.Name]),
        main_guard_line(tree),
        _string_literals([tree, *definitions, *of[ast.Constant]]),
        forwarders(tree),
        weak_port_returns(of[ast.ClassDef], aliases),
        *surface(tree),
        deferred,
        call_sites(tree, calls, calls_aliases),
        result_defs(tree),
        bare_calls(of[ast.Expr], calls_aliases),
        function_calls(tree, aliases),
        ast.get_docstring(tree),
        arbor_reason,
        dynamic_imports(calls, aliases),
        purity_facts(tree, [*calls, *of[ast.Subscript], *functions, *of[ast.Name]], aliases, purity_helpers(aliases), calls_aliases),
        untokenizable,
        import_time_facts(Walked(tree, at_import, sites, running, effects.state_runs, effects.foreign_runs), calls_aliases, dotted_name),
        seam_facts(tree, sites, running, aliases, dotted_name),
        function_spans(tree, [*calls, *branches], calls_aliases, on_paths),
        exemption_comments(source),
        call_shapes(tree, calls, calls_aliases),
        class_shapes(tree, calls_aliases),
        private_attributes(of[ast.Attribute], calls_aliases, rebound),
        patch_targets([*of[ast.withitem], *calls], calls_aliases, rebound),
        unqualified_from_calls(tree, [*calls, *functions, *of[ast.Lambda], *imports], rebound),
        member_shapes(tree, calls_aliases),
        effects.state_writes,
        effects.locks,
        effects.shared,
        on_paths,
    )


def _by_class(nodes: Sequence[ast.AST]) -> defaultdict[type, list[ast.AST]]:
    """The nodes of each concrete class, each list in walk order."""
    found: defaultdict[type, list[ast.AST]] = defaultdict(list)
    for node in nodes:
        found[node.__class__].append(node)
    return found


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
