"""Pins the behaviour #173 (``mixed-purity``) builds on, written before the change (the owner's rule for shared code).

What the rule touches, and the callers found by grep:

- ``facts.extract`` / ``SourceFacts``: built in ``scanner._facts_from_json`` (positional, indexes 0-21) and ``extract``;
  read by rules, ``classify_audit``, ``coupling``, ``practices``, ``taint``, ``selftest``.
- ``scanner._CACHE_VERSION`` and the per-file JSON layout: ``tests/test_engine.py`` pins the version.
- ``check_modules``: every rule test; a module with pure and effectful code must stay silent unless the new rule is on.
- ``ScanData`` / ``ScanResult``: built in ``scanner`` and two ``test_fs_behaviour`` fixtures (keyword arguments).
"""

from __future__ import annotations

from pathlib import Path

from arborescent_helpers import mini_policy, write_files

from arborescent.adapters import FsScanner
from arborescent.domain import Rule
from arborescent.facts import EffectUse, extract
from arborescent.rules import check_modules

HYDRATION = '''\
from __future__ import annotations

import logging
import time

_MEMO: dict[str, int] = {}
LOG = logging.getLogger(__name__)


def parse(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        out[key] = int(value)
    return out


def validate(data: dict[str, int]) -> list[str]:
    errors: list[str] = []
    for key, value in data.items():
        if value < 0:
            errors.append(key)
    return errors


def stamp() -> float:
    LOG.info("stamping")
    return time.time()
'''


def test_extract_still_reports_the_existing_effect_facts() -> None:
    facts = extract(HYDRATION)
    assert [(u.category, u.target) for u in facts.effect_uses] == [("clock", "time.time")]
    assert [d.name for d in facts.top_level_defs] == ["parse", "validate", "stamp"]


def test_cached_facts_equal_fresh_facts(repo: Path, tmp_path: Path) -> None:
    write_files(repo, {"app/domain.py": HYDRATION})
    scanner = FsScanner(tmp_path / "cache")
    cold = scanner.scan(mini_policy(), repo)
    warm = scanner.scan(mini_policy(), repo)
    assert cold.parsed > 0 and warm.parsed == 0
    assert warm.modules["app.domain"].facts == cold.modules["app.domain"].facts == extract(HYDRATION, "app/domain.py")


def test_a_mixed_module_is_silent_when_the_rule_is_off(repo: Path) -> None:
    """The rule is on by default (#173); turning it off restores exactly the findings main reported before it existed."""
    write_files(repo, {"app/domain.py": HYDRATION})
    policy = mini_policy(mixed_purity={"enabled": False})
    scan = FsScanner().scan(policy, repo)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    assert Rule.EFFECT_CLOCK in {v.rule for v in found}  # the clock effect in `domain` is already reported
    assert {v.rule for v in found} <= {Rule.EFFECT_CLOCK, Rule.EFFECT_VIA_IMPORT, Rule.EXTERNAL_IMPORT}


def test_effect_use_shape_is_unchanged() -> None:
    assert EffectUse(1, "clock", "time.time") == EffectUse(1, "clock", "time.time", 0)
