# Port abstraction rule (`require_abstract` / `non-abstract-port`): design

Status: approved design. Date: 2026-10-30.

## Intent

Arborescent enforces the Dependency Rule (import direction between rings) but not the Dependency Inversion
Principle: a `ports` ring can be 100% concrete classes and every existing check still passes, because Arborescent
only looks at import direction, never at whether the inner ring actually exposes an abstraction. This closes that
gap with a mechanical, ast-based check, consistent with every other native rule.

## Goals

1. A per-ring policy switch, `require_abstract`, that a project turns on for whichever ring is meant to hold
   interfaces (typically `ports`, but the check is generic over any ring name, like every other `[ring.<name>]` key).
2. When set, every public (non-`_`-prefixed) top-level class or function in that ring's modules must be a
   recognized abstract shape. A concrete one fires a new rule, baseline-ratchetable like every other rule.
3. Arborescent dog-foods the rule: its own `ports` ring (already pure `typing.Protocol` classes) turns it on.

## Non-goals

- Not a type-aware check. Like every other native rule, it is one ast pass per file; it does not resolve imports
  across files to see whether a base class is "really" abstract elsewhere, and it does not understand runtime
  `abc.ABCMeta` registration or duck-typed protocols without a `Protocol` base.
- Not a check on methods inside a class (a `Protocol`/`ABC` class may mix abstract and concrete default methods,
  which is normal DIP-respecting design); only top-level, module-scope classes and functions are examined.
- Not a check on private members (leading `_`): a ring may still have concrete helpers that are not part of its
  public surface.

## Design

### Recognized abstract shapes

- **Class**: abstract when it directly inherits `abc.ABC` or `typing.Protocol` (also `typing_extensions.Protocol`),
  or declares `metaclass=ABCMeta`/`abc.ABCMeta`. Resolved through the same import-alias table `facts.py` already
  builds for effect detection (`_aliases`/`_dotted`), so `from abc import ABC` and `import abc; ...abc.ABC` both
  work. A class inheriting from any other (concrete) base is **not** abstract, even if every method it adds is a
  stub — inheriting a concrete base is itself a Dependency Inversion violation.
- **Function**: a bare top-level function cannot "inherit" anything, so it is treated as abstract when its body is
  a stub: nothing but an optional docstring followed by `...`, `pass`, or `raise NotImplementedError`. Anything
  else is a concrete implementation that does not belong in an abstraction-only ring.

### Where the facts live

`facts.extract()` gains `SourceFacts.top_level_defs: tuple[TopLevelDef, ...]`, one entry per module-level
`ClassDef`/`FunctionDef`/`AsyncFunctionDef`, each carrying `name`, `line`, `is_class` and `is_abstract`. Whether a
class/function *shape* is abstract is a static fact independent of policy (the same split as `_effect_category`:
facts.py decides the shape, a rule decides whether the ring allows it) — so it is computed unconditionally for
every module, including rings that never set `require_abstract`, the same way effect-use facts are extracted
regardless of whether any ring forbids that effect.

`FsScanner`'s content-hash cache now also stores `top_level_defs`; the cache version bumps from 4 to 5 so caches
written before this change are naturally invalidated instead of returning stale (empty) member lists.

### Where the rule lives

`rules._abstraction_violations(name, ring, rule, info)` runs once per module inside `check_modules`'s existing
per-module loop (next to `_os_branching`/`_effect_uses`), gated on `rule.require_abstract`. It reports
`Rule.NON_ABSTRACT_PORT` (`"non-abstract-port"`) once per offending public member, at that member's line, with
`subject` the owning module (consistent with every other per-module rule) — not once per ring, so a ring with a
mix of abstract and concrete members gets one finding per concrete member, and a ring with zero members, or whose
one module has zero top-level defs, produces none (`top_level_defs` is simply empty; nothing to iterate).

### Config surface

`[tool.arborescent.ring.<name>]` gains `require_abstract` (default `false`), parsed in `config._ring_from_mapping`
exactly like the existing `os_branching` boolean (`bool(table.get(...))`, no extra validation — booleans in this
table are already permissive in that way). `RingRule` gains the field at the end, defaulted, so the one
positionally-constructed `RingRule(...)` call in `config.py` and in `tests/test_policy.py` are unaffected.

### Baseline and selftest

The violation key is `non-abstract-port|<module>|<detail>`, so it ratchets through the existing generic baseline
machinery with no change there; `baseline._in_family` already treats any `rule == family or rule.startswith(f"{family}-")`
as one adoptable family, so `--adopt non-abstract-port` works unprompted.

`selftest._abstraction_mutations` adds a concrete class (`class ArborescentProbeConcrete: ...`) to a ring's host
module when that ring sets `require_abstract`, following the same shape as `_effect_mutations`. Arborescent's own
policy turns `require_abstract` on for `ports`, so `arborescent selftest` actually exercises the mutation (rather
than needing a `NO_MUTATION` excuse) and `tests/test_rule_ids.py::test_every_rule_has_a_selftest_mutation_or_a_documented_reason`
stays green.

## Interaction with multi-scope configs (`scopes.<name>.ring`)

Multi-scope governance (PR #40, `consolidate/arbor-scopes-and-fixes`) was still an open PR, not yet merged into
`main`, at the time of this change. It builds one independent `Policy` object per scope by re-entering
`config.policy_from_mapping` with that scope's own `ring` table spliced into a per-scope raw mapping (see
`_scoped_policy`/`_SCOPE_ARCHITECTURE_KEYS`, which includes `"ring"`), and `rules.check_modules` resolves each
module's *local* policy/ring before applying any ring-level rule (`local_policy.ring(ring)`, via its
`RuleContext.policy_for`). Because `require_abstract` is parsed by the exact same `_ring_from_mapping` function
and consumed by reading `rule.require_abstract` off the module's own resolved `RingRule` — never off a shared or
global default — it composes with scopes with no extra code: `[scopes.ports_lib.ring.ports]` and
`[scopes.other_lib.ring.ports]` get independent `require_abstract` settings the same way they already get
independent `may_import`/`external`/`forbid` settings. `tests/test_abstraction_policy.py` proves the isolation
property directly at the `Policy`/`RingRule` level (two independently-built policies with different
`require_abstract` settings for a same-named ring do not affect each other); re-running it against literal
`[scopes.<name>.ring.<name>]` TOML is left to the multi-scope suite once PR #40 lands, since this change does not
touch `config._scoped_policy` at all.

## Edge cases covered by tests

- A ring with mixed abstract and concrete top-level members: only the concrete ones are reported.
- A ring with zero members (an empty module, or no module at all classified into that ring): no violations, no
  crash.
- A class inheriting from a concrete (non-`ABC`/`Protocol`) base: reported, even if its own body is all stubs.
- `require_abstract` left at its default (`false`): no violations, regardless of what the ring contains (no
  behavior change for every existing policy that does not opt in).
- Private (`_`-prefixed) top-level members: never reported, abstract or not.
- Policy isolation under independently-built `Policy`/`RingRule` objects (the multi-scope case above).
