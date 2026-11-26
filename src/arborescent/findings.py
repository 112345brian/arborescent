"""Rule identities and immutable findings shared by the architecture checks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["CONSTRAINT_RULES", "EFFECT_RULES", "FAN_OUT_RULES", "INFORMATIONAL_RULES", "Rule", "Violation", "root_cause_hint"]


class Rule(StrEnum):
    """Every rule id, in one place. The values are stored in baseline files (``rule|subject|detail``): never rename one."""

    UNCLASSIFIED = "unclassified"
    FORBIDDEN_MODULE = "forbidden-module"
    NOISE_DIR_CODE = "noise-dir-code"
    VENDOR_FOLDER = "vendor-folder"
    WILDCARD_RING = "wildcard-ring"
    SUPPRESSION = "suppression"
    DISCARD = "discard"
    LAZY_IMPORT = "lazy-import"
    ENTRYPOINT_EAGER_COMMANDS = "entrypoint-eager-commands"
    RESULT_DISCARD = "result-discard"
    EFFECT_CLOCK = "effect-clock"
    EFFECT_ENV = "effect-env"
    EFFECT_IDENTITY = "effect-identity"
    EFFECT_RANDOMNESS = "effect-randomness"
    EFFECT_SLEEP = "effect-sleep"
    EFFECT_THREADS = "effect-threads"
    EFFECT_PROCESS = "effect-process"
    DEV_TOOL_IMPORT = "dev-tool-import"
    DEV_TOOL_MISPLACED = "dev-tool-misplaced"
    DEV_TOOL_PRODUCT_IMPORT = "dev-tool-product-import"
    EFFECT_DATABASE = "effect-database"
    EFFECT_NETWORK = "effect-network"
    EFFECT_FILESYSTEM = "effect-filesystem"
    EFFECT_DYNAMIC_IMPORT = "effect-dynamic-import"
    EFFECT_PATCH = "effect-patch"
    EFFECT_CONSOLE = "effect-console"
    EFFECT_GLOBAL = "effect-global"
    EFFECT_STATE = "effect-state"
    EFFECT_MONOTONIC = "effect-monotonic"
    EFFECT_HOST = "effect-host"
    EFFECT_FS_QUERY = "effect-fs-query"
    EFFECT_UNSANCTIONED_IMPORT = "effect-unsanctioned-import"
    EFFECT_VIA_IMPORT = "effect-via-import"
    EFFECT_AT_IMPORT = "effect-at-import"
    EFFECT_SEAM_BYPASSED = "effect-seam-bypassed"
    INTERNAL_IMPORT = "internal-import"
    FORWARDED_IMPORT = "forwarded-import"
    RESTRICTED_IMPORT = "restricted-import"
    LITERAL_OUTSIDE_OWNER = "literal-outside-owner"
    CONSTRUCTION_OUTSIDE_SITE = "construction-outside-site"
    MISSING_MODULE_DOCSTRING = "missing-module-docstring"
    DOCSTRING_RING_MISMATCH = "docstring-ring-mismatch"
    NON_ABSTRACT_PORT = "non-abstract-port"
    WEAKLY_TYPED_PORT = "weakly-typed-port"
    REEXPORT_RATIO = "reexport-ratio"
    MIXED_PURITY = "mixed-purity"
    MIXED_PURITY_EXCLUDED = "mixed-purity-excluded"
    MIXED_PURITY_WEAK = "mixed-purity-weak"
    UNTOKENIZABLE_SOURCE = "untokenizable-source"
    SCOPE_IMPORT = "scope-import"
    SCOPE_EFFECT_LEAK = "scope-effect-leak"
    CIRCULAR_IMPORT = "circular-import"
    EXTERNAL_IMPORT = "external-import"
    OS_BRANCHING = "os-branching"
    BAD_IMPORT = "bad-import"
    UNDECLARED_ENTRY_DEPENDENCY = "undeclared-entry-dependency"
    # repository practices (`[tool.arborescent.practices]` keys use the same ids)
    GITIGNORE_SHADOW = "gitignore-shadow"
    SIBLING_VERSIONS = "sibling-versions"
    SECRETS_GUARD = "secrets-guard"
    DOCS_SYNC = "docs-sync"
    CRLF = "crlf"
    UNDECLARED_IMPORTS = "undeclared-imports"
    STALE_CONFIG_PATH = "stale-config-path"
    SOURCE_ROOT_DRIFT = "source-root-drift"
    TEST_REFERENCE = "test-reference"
    ADAPTER_EQUIVALENCE = "adapter-equivalence"
    ADAPTER_EQUIVALENCE_REPORT = "adapter-equivalence-report"
    INVALID_ARBOR_ANNOTATION = "invalid-arbor-annotation"
    ARBOR_RING_MISMATCH = "arbor-ring-mismatch"
    UNANNOTATED_MODULE = "unannotated-module"
    CLASSIFY_FACTS = "classify-facts"
    # package assignment (`package_catalog`): which package owns each module and asset
    PACKAGE_UNASSIGNED_MODULE = "package-unassigned-module"
    PACKAGE_MULTIPLY_ASSIGNED_MODULE = "package-multiply-assigned-module"
    PACKAGE_UNOWNED_ASSET = "package-unowned-asset"
    PACKAGE_MULTIPLY_ASSIGNED_ASSET = "package-multiply-assigned-asset"
    PACKAGE_SPANS_RINGS = "package-spans-rings"
    PACKAGE_UNDECLARED_DEPENDENCY = "package-undeclared-dependency"
    PACKAGE_DEPENDENCY_CYCLE = "package-dependency-cycle"
    PACKAGE_UNUSED_DEPENDENCY = "package-unused-dependency"
    PACKAGE_OWNS_EVERYTHING = "package-owns-everything"
    PACKAGE_NEVER_DEPENDED_ON = "package-never-depended-on"
    TACH_INVISIBLE_IMPORT = "tach-invisible-import"
    # annotation preservation (`package_parts`): a split part keeps the catalog's annotations and public contracts
    PACKAGE_SEMANTICS_DRIFT = "package-semantics-drift"
    PACKAGE_MEMBERSHIP_DRIFT = "package-membership-drift"
    PACKAGE_RING_DRIFT = "package-ring-drift"
    PACKAGE_PUBLIC_SYMBOL_DROPPED = "package-public-symbol-dropped"
    PACKAGE_PRIVATE_IMPORT = "package-private-import"
    PACKAGE_RELEASE_POLICY_MISSING = "package-release-policy-missing"
    PACKAGE_PYPROJECT_DRIFT = "package-pyproject-drift"
    # package constraints (`[package.constraints]`, #165): a promise about effects and imports, enforced on facts and on placement
    PACKAGE_CONSTRAINT_FACTS = "package-constraint-facts"
    PACKAGE_CONSTRAINT_PLACEMENT = "package-constraint-placement"


EFFECT_RULES: Mapping[str, Rule] = {
    "clock": Rule.EFFECT_CLOCK,
    "env": Rule.EFFECT_ENV,
    "identity": Rule.EFFECT_IDENTITY,
    "randomness": Rule.EFFECT_RANDOMNESS,
    "sleep": Rule.EFFECT_SLEEP,
    "threads": Rule.EFFECT_THREADS,
    "process": Rule.EFFECT_PROCESS,
    "database": Rule.EFFECT_DATABASE,
    "network": Rule.EFFECT_NETWORK,
    "filesystem": Rule.EFFECT_FILESYSTEM,
    "dynamic-import": Rule.EFFECT_DYNAMIC_IMPORT,
    "patch": Rule.EFFECT_PATCH,
    "console": Rule.EFFECT_CONSOLE,
    "global": Rule.EFFECT_GLOBAL,
    "state": Rule.EFFECT_STATE,
    "monotonic": Rule.EFFECT_MONOTONIC,
    "host": Rule.EFFECT_HOST,
    "fs-query": Rule.EFFECT_FS_QUERY,
    "registry": Rule.EFFECT_STATE,  # #194: module state besides locks; the same rule id as `state`, the category decides who opts in
    "cache": Rule.EFFECT_STATE,
}

# Not catalog gaps despite the prefix: a constraint is a promise, so `modularity = "report"` never demotes these.
CONSTRAINT_RULES: frozenset[Rule] = frozenset({Rule.PACKAGE_CONSTRAINT_FACTS, Rule.PACKAGE_CONSTRAINT_PLACEMENT})

# Findings that are reported but never fail a check and never enter the baseline: a package spanning rings is normal.
# A package's pyproject.toml is owner-edited, so a differing derived field is advice, not a failure (like generated-preset drift).
INFORMATIONAL_RULES: frozenset[Rule] = frozenset({
    Rule.PACKAGE_SPANS_RINGS, Rule.PACKAGE_PYPROJECT_DRIFT, Rule.PACKAGE_UNUSED_DEPENDENCY, Rule.PACKAGE_OWNS_EVERYTHING, Rule.PACKAGE_NEVER_DEPENDED_ON,
    Rule.TACH_INVISIBLE_IMPORT, Rule.CLASSIFY_FACTS, Rule.TEST_REFERENCE, Rule.MIXED_PURITY_EXCLUDED, Rule.MIXED_PURITY_WEAK,
    Rule.ADAPTER_EQUIVALENCE_REPORT, Rule.ENTRYPOINT_EAGER_COMMANDS,
})


@dataclass(frozen=True)
class Violation:
    """One broken rule. ``key`` is stable across line moves so a baseline can hold it."""

    rule: str
    subject: str
    detail: str
    line: int = 0
    target: str = field(default="", compare=False)  # the imported module, for import findings; not part of the key
    hint: str = field(default="", compare=False)  # extra advice appended when rendered (#133); not part of the key
    root_cause: str = field(default="", compare=False)  # fan-out rules only (FAN_OUT_RULES): the shared cause, `<rule>|...`; not part of the key (#201)

    @property
    def key(self) -> str:
        return f"{self.rule}|{self.subject}|{self.detail}"

    @property
    def cause(self) -> str:
        """What counts as one root cause: the declared ``root_cause``, else the finding itself."""
        return self.root_cause or self.key

    def render(self) -> str:
        where = f"{self.subject}:{self.line}" if self.line else self.subject
        hint = _RENDER_HINTS.get(self.rule, "")
        return f"{where}: [{self.rule}] {self.detail}{hint}{self.hint}"


# Rules that emit one finding per symptom and so must name the cause they share (#201); a test scans their construct sites.
FAN_OUT_RULES = frozenset({Rule.LAZY_IMPORT, Rule.EFFECT_UNSANCTIONED_IMPORT, Rule.EFFECT_VIA_IMPORT})


def root_cause_hint(cause: str, findings: int) -> str:
    """What one fix to a shared root cause achieves, for the `--by-root-cause` view; empty when the rule has no advice (#198)."""
    parts = cause.split("|")
    count = f"{findings} finding{'' if findings == 1 else 's'}"
    if parts[0] == Rule.EFFECT_VIA_IMPORT and len(parts) == 3:
        edge = parts[2]
        importer, arrow, origin = edge.partition("->")
        if arrow:
            return (
                f"inject a port at {importer} (so it stops importing {origin}), or add {origin} to `effects.sanctioned` "
                f"if it is a capability seam, to clear {count}"
            )
        return f"add {edge} to `effects.sanctioned` if it is a capability seam, or inject a port in each importer, to clear {count}"
    if parts[0] == Rule.EFFECT_UNSANCTIONED_IMPORT and len(parts) == 2:
        return f"list {parts[1]} in `effects.sanctioned` if it is a capability seam, or reach it through one, to clear {count}"
    if parts[0] == Rule.LAZY_IMPORT and len(parts) == 2:
        return f"import {parts[1]} at module level, or record why with `# deferred: <reason>`, to clear {count}"
    return ""


_RENDER_HINTS = {
    Rule.LAZY_IMPORT: " (put it at module level, or record why it stays with `# deferred: <reason>` on the statement or above it)",
    Rule.ENTRYPOINT_EAGER_COMMANDS: (
        "; split into one module per command with its imports at the top and mount each lazily from the composition root,"
        " declared in `[coupling.lazy_mounts]`"
    ),
    Rule.VENDOR_FOLDER: (
        " (folder excluded from the scan; to govern it list it under `vendor_roots` or set `scan_vendors = true`,"
        " otherwise first-party imports of it are reported as external)"
    ),
}
