"""``untokenizable-source`` (#181): a file ``ast`` accepts but ``tokenize`` rejects must not abort the run.

Callers found by grep (the owner's rule for shared code), and what each does with a file today:

- ``facts.extract``: called by ``scanner._parse_files`` (no guard: a ``TokenError`` aborts the run), ``scanner._excluded_modules``
  (guards ``SyntaxError``/``ValueError`` only), ``practices`` (same), ``cli_admit`` and ``admission_probes``.
- ``suggestions.logical_code_lines``: the second ``tokenize`` pass, called per module by ``file_size_suggestions``.
- every other ``tokenize.generate_tokens`` use in ``src/``: pinned by ``test_every_tokenize_pass_handles_token_errors``.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files

from arborescent.adapters import FsScanner
from arborescent.domain import INFORMATIONAL_RULES, Rule
from arborescent.engine import run_own
from arborescent.facts import extract
from arborescent.suggestions import logical_code_lines

SRC = Path(__file__).resolve().parents[1] / "src" / "arborescent"
CRCRLF = "x = 1\r\r\ny = 2\r\r\n"


def _tokenizes(source: str) -> bool:
    try:
        _ = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return False
    return True


# --- pins: what works today and must not change -------------------------------------------------------------------


def test_comment_facts_of_a_normal_file_are_unchanged() -> None:
    facts = extract("import os  # noqa\n# deferred: lazy on purpose\nx = 1\ny = 2  # type: ignore[arg-type]\n")
    assert facts.suppressions == ((1, "noqa-blanket"), (4, "type-ignore-coded"))
    assert facts.deferred == ((3, "lazy on purpose"),)
    assert facts.untokenizable == ""


def test_a_syntax_error_still_raises_from_extract() -> None:
    with pytest.raises(SyntaxError):
        extract("def (:\n")


def test_empty_and_comment_only_files_have_no_comment_facts_and_no_finding() -> None:
    assert extract("").untokenizable == "" and extract("").suppressions == ()
    only = extract("# just a comment\n# noqa\n")
    assert only.untokenizable == "" and only.suppressions == ((2, "noqa-blanket"),)


def test_logical_code_lines_counts_a_normal_file() -> None:
    assert logical_code_lines('"""doc"""\nx = 1\n# c\ny = 2\n') == 2


def test_cached_facts_equal_fresh_facts_for_an_untokenizable_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write_files(root, {"app/domain.py": CRCRLF})
    scanner = FsScanner(tmp_path / "cache")
    cold = scanner.scan(mini_policy(), root)
    warm = scanner.scan(mini_policy(), root)
    assert warm.parsed == 0
    assert warm.modules["app.domain"].facts == cold.modules["app.domain"].facts == extract(CRCRLF, "app/domain.py")
    assert warm.modules["app.domain"].facts.untokenizable


# --- the bug: ast accepts, tokenize rejects -----------------------------------------------------------------------

EDGE_SOURCES = {
    "crcrlf": CRCRLF,
    "crcr at eof": "x = 1\r\r",
    "crcrlf after a comment": "x = 1  # noqa\r\r\n",
    "lone cr inside a triple-quoted string": "s = '''a\rb'''\nt = 2\n",
    "lone cr at eof": "x = 1\r",
    "lone cr after a comment at eof": "x = 1  # noqa\r",
    "empty": "",
    "only comments": "# one\n# two\n",
}


@pytest.mark.parametrize("name", sorted(EDGE_SOURCES))
def test_extract_never_raises_for_a_parsable_file(name: str) -> None:
    source = EDGE_SOURCES[name]
    _ = ast.parse(source)  # the premise: Python itself accepts it
    facts = extract(source, "m.py")
    assert bool(facts.untokenizable) == (not _tokenizes(source))
    if facts.untokenizable:
        assert facts.suppressions == () and facts.deferred == ()


def test_the_finding_names_the_line_the_tokenizer_stopped_on() -> None:
    facts = extract(CRCRLF, "m.py")
    assert facts.untokenizable.startswith("1:")
    assert "U+000D" in facts.untokenizable


def test_ast_facts_survive_when_comment_facts_cannot() -> None:
    facts = extract("import os\r\r\nx = os.sep\r\r\n", "m.py")
    assert facts.untokenizable
    assert [imp.module for imp in facts.imports] == ["os"]


def test_logical_code_lines_does_not_raise() -> None:
    assert logical_code_lines(CRCRLF) == 2


# --- the run: one odd file must not stop the others, and must be visible ----------------------------------------


def _scan(root: Path, files: dict[str, str]) -> list[str]:
    write_files(root, files)
    found, _ = run_own(mini_policy(), root, services(), practices=False)
    return [v.render() for v in found if v.rule == Rule.UNTOKENIZABLE_SOURCE]


def test_one_untokenizable_file_does_not_stop_the_others(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    files = {
        "app/domain.py": '"""d."""\n\n\ndef f() -> int:\n    return 1\n',
        "app/adapters.py": "import os  # noqa\r\r\nSEP = os.sep\r\r\n",
        "app/main.py": "import sys\n\nfrom . import adapters\n\nARGS = (sys.argv, adapters.SEP)  # noqa\n",
    }
    write_files(root, files)
    found, scan = run_own(mini_policy(), root, services(), practices=False)
    assert set(scan.modules) >= {"app.domain", "app.adapters", "app.main"}
    assert scan.modules["app.main"].facts.suppressions == ((5, "noqa-blanket"),)  # scanned after the bad file
    assert scan.modules["app.adapters"].facts.suppressions == ()
    hits = [v for v in found if v.rule == Rule.UNTOKENIZABLE_SOURCE]
    assert [v.subject for v in hits] == ["app.adapters"]
    assert hits[0].line == 1


def test_the_finding_is_baselinable_not_informational() -> None:
    assert Rule.UNTOKENIZABLE_SOURCE not in INFORMATIONAL_RULES


def _keys(root: Path, cache: Path) -> set[str]:
    found, _ = run_own(mini_policy(), root, services(cache=cache), practices=False)
    return {v.key for v in found if v.rule == Rule.UNTOKENIZABLE_SOURCE}


def test_the_baseline_key_survives_a_cache_hit_and_a_line_move(tmp_path: Path) -> None:
    root, cache = tmp_path / "repo", tmp_path / "cache"
    write_files(root, {"app/domain.py": "x = 1\r\r\n"})
    cold = _keys(root, cache)
    warm = _keys(root, cache)  # served from the cache
    write_files(root, {"app/domain.py": "\n\ny = 0\nx = 1\r\r\n"})  # the bad line moves
    moved = _keys(root, cache)
    assert len(cold) == 1 and cold == warm == moved


def test_a_tokenizable_run_reports_nothing(tmp_path: Path) -> None:
    assert _scan(tmp_path / "repo", {"app/domain.py": "x = 1\r\ny = 2\r\n"}) == []


# --- the guard against the next one -------------------------------------------------------------------------------


def test_every_tokenize_pass_handles_token_errors() -> None:
    """``ast.parse`` succeeding does not promise ``tokenize`` will: each module that tokenizes must catch ``TokenError``."""
    offenders = [
        path.name
        for path in sorted(SRC.glob("*.py"))
        if re.search(r"\bgenerate_tokens\(|\btokenize\.tokenize\(", text := path.read_text(encoding="utf-8"))
        and "TokenError" not in text
    ]
    assert offenders == []
