"""End-to-end checks for the optional Rust save-analysis prototype."""

from __future__ import annotations

from pathlib import Path

import pytest

from arborescent.config import policy_from_mapping
from tools.incremental_analysis import Analysis, Worker


def test_saved_helper_changes_untouched_caller_and_matches_full_scan(
    worker_binary: Path,
    tmp_path: Path,
) -> None:
    # A second allowed inner ring lets an effect cross an otherwise legal edge.
    strict = policy_from_mapping(
        {
            "sources": ["."],
            "rings": ["helpers", "domain", "adapters"],
            "ring": {
                "helpers": {"traits": ["permits-effects"], "external": ["os"]},
                "domain": {"may_import": ["helpers"], "traits": ["no-env"]},
            },
            "classify": {
                "domain": "domain",
                "helper": "helpers",
                "adapters": "adapters",
            },
        }
    )
    permissive = policy_from_mapping(
        {
            "sources": ["."],
            "rings": ["helpers", "domain", "adapters"],
            "ring": {
                "helpers": {"traits": ["permits-effects"], "external": ["os"]},
                "domain": {"may_import": ["helpers"], "traits": ["permits-effects"]},
            },
            "classify": {
                "domain": "domain",
                "helper": "helpers",
                "adapters": "adapters",
            },
        }
    )
    caller = tmp_path / "domain.py"
    helper = tmp_path / "helper.py"
    caller.write_text(
        "from helper import load\n\ndef quote():\n    return load()\n", encoding="utf-8"
    )
    helper.write_text("def load():\n    return 'fixed'\n", encoding="utf-8")
    (tmp_path / "adapters.py").write_text("", encoding="utf-8")

    def full(policy):
        with Worker(worker_binary) as fresh:
            return Analysis(fresh, tmp_path).refresh(policy).violations

    with Worker(worker_binary) as worker:
        analysis = Analysis(worker, tmp_path)
        assert analysis.refresh(strict).violations == full(strict) == ()
        helper.write_text(
            "import os as operating_system\n\ndef load():\n    return operating_system.getenv('QUOTE')\n",
            encoding="utf-8",
        )
        added = analysis.refresh(strict)
        assert added.parsed == 1
        assert added.violations == full(strict)
        assert [(v.rule, v.subject, v.line) for v in added.violations] == [
            ("effect-via-call", "domain", 4),
        ]
        assert added.violations[0].chain == ("domain.quote", "helper.load", "os.getenv")
        relaxed = analysis.refresh(permissive)
        assert relaxed.parsed == 0
        assert relaxed.violations == full(permissive) == ()
        assert analysis.refresh(strict).violations == added.violations
        helper.write_text("def load():\n    return 'fixed'\n", encoding="utf-8")
        removed = analysis.refresh(strict)
        assert removed.parsed == 1
        assert removed.violations == full(strict) == ()

        caller.write_text("import adapters\n", encoding="utf-8")
        blocked = analysis.refresh(strict)
        assert blocked.violations == full(strict)
        assert [(v.rule, v.subject, v.line) for v in blocked.violations] == [
            ("internal-import", "domain", 1),
        ]
        caller.write_text("", encoding="utf-8")
        assert analysis.refresh(strict).violations == full(strict) == ()


def test_deleted_functions_and_syntax_errors_remove_stale_recursive_paths(
    worker_binary: Path,
    tmp_path: Path,
) -> None:
    policy = policy_from_mapping(
        {
            "sources": ["."],
            "rings": ["helpers", "domain", "adapters"],
            "ring": {
                "helpers": {"traits": ["permits-effects"], "external": ["os"]},
                "domain": {"may_import": ["helpers"], "traits": ["no-env"]},
            },
            "classify": {"domain": "domain", "helper": "helpers"},
        }
    )
    caller = tmp_path / "domain.py"
    helper = tmp_path / "helper.py"
    caller.write_text(
        "from helper import first\n\ndef quote():\n    return first()\n",
        encoding="utf-8",
    )
    effectful = (
        "import os\ndef first():\n    return second()\n"
        "def second():\n    if os.getenv('QUOTE'):\n        return first()\n"
    )
    helper.write_text(effectful, encoding="utf-8")

    def assert_full(analysis: Analysis):
        result = analysis.refresh(policy)
        with Worker(worker_binary) as fresh:
            expected = Analysis(fresh, tmp_path).refresh(policy)
        assert result.violations == expected.violations
        assert result.syntax_errors == expected.syntax_errors
        return result

    with Worker(worker_binary) as worker:
        analysis = Analysis(worker, tmp_path)
        initial = assert_full(analysis)
        assert initial.violations[0].chain == (
            "domain.quote",
            "helper.first",
            "helper.second",
            "os.getenv",
        )

        helper.write_text("def first(:\n", encoding="utf-8")
        broken = assert_full(analysis)
        assert broken.syntax_errors == ("helper.py",)
        assert broken.violations == ()

        helper.write_text(effectful, encoding="utf-8")
        recovered = assert_full(analysis)
        assert recovered.violations == initial.violations
        assert recovered.syntax_errors == ()

        helper.write_text(
            effectful.replace("os.getenv('QUOTE')", "True"), encoding="utf-8"
        )
        cleared = assert_full(analysis)
        assert cleared.violations == ()
        assert cleared.parsed == 1

        helper.write_text(effectful, encoding="utf-8")
        assert assert_full(analysis).violations == initial.violations
        helper.rename(tmp_path / "relocated.py")
        renamed = assert_full(analysis)
        assert renamed.parsed == 1
        # The effect path is gone. The old name is now an unknown external and the new file is unclassified.
        assert sorted((v.rule, v.subject) for v in renamed.violations) == [
            ("external-import", "domain"),
            ("unclassified", "relocated"),
        ]


@pytest.mark.parametrize(
    "caller",
    [
        "from helper import load\nload = lambda: 'fixed'\ndef quote():\n    return load()\n",
        "from helper import load\ndef quote():\n    def load():\n        return 'fixed'\n    return load()\n",
    ],
)
def test_shadowed_import_does_not_create_effect_path(
    worker_binary: Path,
    tmp_path: Path,
    caller: str,
) -> None:
    policy = policy_from_mapping(
        {
            "sources": ["."],
            "rings": ["helpers", "domain", "adapters"],
            "ring": {
                "helpers": {"traits": ["permits-effects"], "external": ["os"]},
                "domain": {"may_import": ["helpers"], "traits": ["no-env"]},
            },
            "classify": {"domain": "domain", "helper": "helpers"},
        }
    )
    (tmp_path / "helper.py").write_text(
        "import os\ndef load():\n    return os.getenv('QUOTE')\n", encoding="utf-8"
    )
    (tmp_path / "domain.py").write_text(caller, encoding="utf-8")
    with Worker(worker_binary) as worker:
        assert Analysis(worker, tmp_path).refresh(policy).violations == ()
