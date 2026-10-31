"""Behavioral checks for the versioned Arborescent minimum profile."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .domain import Policy, RingRule
    from .facts import ModuleInfo
    from .ports import FileSystem, ScanResult, ToolRunner

from .domain import NOISE_DIRS, ToolResult

PROFILE = "v1"
RUFF_VERSION = "0.17.2"
TACH_VERSION = "0.35.3"
REQUIRED_RUFF = (
    "BLE001", "E722", "F401", "B006", "C901", "PLR0912", "PLR0913", "PLR0915", "TID252",
    "PLW1514", "DTZ001", "T201", "PLC0415",
)
_PROBES = {
    "BLE001": "try:\n pass\nexcept Exception:\n pass\n",
    "E722": "try:\n pass\nexcept:\n pass\n",
    "F401": "import os\n",
    "B006": "def f(value=[]):\n return value\n",
    "C901": "def f(x):\n" + "".join(f" if x == {i}:\n  x += 1\n" for i in range(12)) + " return x\n",
    "PLR0912": "def f(x):\n" + "".join(f" if x == {i}:\n  x += 1\n" for i in range(13)) + " return x\n",
    "PLR0913": "def f(a,b,c,d,e,f):\n return a\n",
    "PLR0915": "def f():\n" + "".join(f" x{i} = {i}\n" for i in range(60)),
    "TID252": "from .. import sibling\n",
    "PLW1514": "open('file.txt')\n",
    "DTZ001": "from datetime import datetime\ndatetime(2024, 1, 1)\n",
    "T201": "print('hello')\n",
    "PLC0415": "def f():\n import os\n return os.name\n",
}


def certify(root: Path, policy: Policy, scan: ScanResult, runner: ToolRunner, fs: FileSystem) -> tuple[str, ...]:
    """Verify exact tool versions and Ruff rules using probes beneath a configured source root."""
    failures = _native_minimum(policy, scan)
    failures.extend(_tool_versions(root, runner, policy.disabled_tools))
    if "ruff" not in policy.disabled_tools and runner.available("ruff"):
        failures.extend(_ruff_probes(root, policy.source_roots, runner, fs))
    if "tach" not in policy.disabled_tools:
        failures.extend(_tach_probes(root, policy, scan, runner, fs))
    return tuple(failures)


def _native_minimum(policy: Policy, scan: object) -> list[str]:
    failures: list[str] = []
    local_policies = tuple((scope.name, scope.policy) for scope in policy.scopes) if policy.scopes else (("", policy),)
    if any(not local_policy.forbid_circular_imports for _, local_policy in local_policies):
        suffix = " in every scope" if policy.scopes else ""
        failures.append(f"minimum profile requires `forbid_circular_imports = true`{suffix}")
    failures.extend(
        f"required profile tool {tool} is disabled by policy"
        for tool in ("ruff", "tach")
        if tool in policy.disabled_tools
    )
    if "arbor-annotations" in policy.disabled_tools:
        failures.append("minimum profile requires `__arbor__` annotations to be enabled")
    for scope_name, local_policy in local_policies:
        failures.extend(_native_scope_minimum(scope_name, local_policy))
    unclassified = getattr(scan, "unclassified", ())
    if unclassified:
        failures.append(f"{len(unclassified)} first-party module(s) are unclassified")
    return failures


def _native_scope_minimum(scope_name: str, policy: Policy) -> list[str]:
    failures: list[str] = []
    outer = policy.rings[-1].name
    prefix = f"scope {scope_name} " if scope_name else ""
    for rule in policy.rings:
        if rule.name != outer and "*" in rule.external:
            failures.append(f"{prefix}ring {rule.name} has wildcard external access")
        if rule.name != outer and "*" in rule.may_import:
            failures.append(f"{prefix}ring {rule.name} has wildcard internal imports")
    for rule in policy.rings[:-1]:
        required = policy.forbidden_effects(rule.name)
        if rule.forbid is not None and not required <= rule.forbid:
            failures.append(f"{prefix}ring {rule.name} weakens positional effect defaults")
    return failures


def _tool_versions(root: Path, runner: ToolRunner, disabled_tools: tuple[str, ...] = ()) -> list[str]:
    failures: list[str] = []
    for executable, expected in (("ruff", RUFF_VERSION), ("tach", TACH_VERSION)):
        if executable in disabled_tools:
            continue
        if not runner.available(executable):
            failures.append(f"{executable} is not installed")
            continue
        result = runner.run([executable, "--version"], root)
        actual = result.stdout.strip().split()[-1] if result.returncode == 0 and result.stdout.strip() else "unknown"
        if actual != expected:
            failures.append(f"{executable} version {actual}; profile {PROFILE} requires {expected}")
    return failures


def _ruff_probes(root: Path, sources: tuple[str, ...], runner: ToolRunner, fs: FileSystem) -> list[str]:
    failures: list[str] = []
    source_root = next((root / source for source in sources if fs.is_dir(root / source)), root)
    try:
        with fs.temporary_directory("arborescent-profile-", source_root) as temp:
            package = temp / "probe_package"
            nested = package / "nested"
            fs.mkdir(nested)
            fs.write_text(package / "__init__.py", "")
            fs.write_text(nested / "__init__.py", "")
            filename = nested / "probe.py"
            fs.write_text(filename, "\n")
            for code, snippet in _PROBES.items():
                fs.write_text(filename, snippet)
                result = runner.run(
                    ["ruff", "check", "--ignore-noqa", "--exit-zero", "--output-format", "json", str(filename)], root
                )
                if result.returncode != 0:
                    failures.append(f"Ruff {code} probe exited with status {result.returncode}")
                    continue
                try:
                    diagnostics = json.loads(result.stdout)
                except json.JSONDecodeError:
                    failures.append(f"Ruff {code} probe returned malformed JSON")
                    continue
                if not isinstance(diagnostics, list) or not any(
                    isinstance(item, dict) and item.get("code") == code for item in diagnostics
                ):
                    failures.append(f"Ruff required rule {code} is not effective for {filename.relative_to(root)}")
    except OSError as exc:
        failures.append(f"could not create profile canaries: {exc}")
    return failures


def _tach_probes(root: Path, policy: Policy, scan: ScanResult, runner: ToolRunner, fs: FileSystem) -> list[str]:
    """Prove active Tach config rejects a forbidden ring edge and undeclared external import."""
    if not runner.available("tach"):
        return ["Tach is not installed; profile behavior canaries cannot run"]
    failures: list[str] = []
    if not fs.is_file(root / "tach.toml"):
        return ["Tach profile canaries require the active tach.toml at the project root"]
    canaries = _tach_canary_modules_by_scope(policy, scan)
    missing = [name or "legacy" for name, (source, targets) in canaries.items() if source is None or not targets]
    if missing:
        return [f"Tach profile canaries need classified innermost and outermost modules in scope {name!r}" for name in missing]
    try:
        with fs.temporary_directory("arborescent-tach-profile-") as temp:
            _copy_tach_project(root, policy.source_roots, temp, fs)
            failures.extend(_tach_baseline(temp, runner))
            if failures:
                return failures
            for scope_name, (source_module, outer_modules) in canaries.items():
                failures.extend(_tach_scope_probe(temp, (scope_name, source_module, outer_modules), runner, fs))
    except (OSError, UnicodeDecodeError) as exc:
        failures.append(f"could not create Tach profile canaries: {exc}")
    return failures


def _tach_scope_probe(
    temp: Path, canary: tuple[str, ModuleInfo | None, tuple[str, ...]], runner: ToolRunner, fs: FileSystem,
) -> list[str]:
    scope_name, source_module, outer_modules = canary
    if source_module is None:
        return []
    source_path = temp / source_module.path
    if not fs.is_file(source_path):
        return [f"Tach canary source is unavailable in the isolated project: {source_module.path}"]
    failures = []
    if not _tach_layer_probe(temp, source_module, outer_modules, runner, fs):
        failures.append(
            f"Tach layer canary is not effective in scope {scope_name!r}: "
            f"{source_module.name} may import every outermost module"
        )
    if not _tach_external_probe(temp, source_path, runner, fs):
        failures.append(f"Tach external-dependency canary is not effective in scope {scope_name!r}")
    return failures


def _tach_baseline(temp: Path, runner: ToolRunner) -> list[str]:
    failures: list[str] = []
    for command, label in ((["tach", "check", "--dependencies"], "layer"), (["tach", "check-external"], "external-dependency")):
        result = runner.run(command, temp)
        if result.returncode != 0:
            failures.append(f"Tach {label} baseline failed in isolated project: {_result_detail(result)}")
    return failures


def _tach_layer_probe(
    temp: Path, source_module: ModuleInfo, outer_modules: tuple[str, ...], runner: ToolRunner, fs: FileSystem
) -> bool:
    source_path = temp / source_module.path
    original = fs.read_bytes(source_path)
    accepted_command = ["tach", "check", "--dependencies"]
    for target in outer_modules:
        _append_import(fs, source_path, original, target)
        result = runner.run(accepted_command, temp)
        fs.write_bytes(source_path, original)
        if result.returncode != 0:
            return True
    return False


def _tach_external_probe(temp: Path, source_path: Path, runner: ToolRunner, fs: FileSystem) -> bool:
    original = fs.read_bytes(source_path)
    _append_import(fs, source_path, original, "__arborescent_profile_missing_external_7f0f6c19__")
    result = runner.run(["tach", "check-external"], temp)
    fs.write_bytes(source_path, original)
    return result.returncode != 0


def _append_import(fs: FileSystem, path: Path, original: bytes, module: str) -> None:
    source = original.decode("utf-8-sig")
    fs.write_text(path, f"{source.rstrip()}\nimport {module}\n")


def _canary_modules(
    modules: Sequence[ModuleInfo], rings: tuple[RingRule, ...], resolve_ring: Callable[[str], str | None],
) -> tuple[ModuleInfo | None, tuple[str, ...]]:
    """Pick a leaf module in the innermost ring (the probe target) and leaves in the outermost ring (the
    expected violators), over whatever ``modules`` already is — the single policy's, or one scope's."""
    leaves = tuple(
        module for module in modules if not any(other.name.startswith(module.name + ".") for other in modules)
    )
    inner = rings[0].name
    outer = rings[-1].name
    sources = sorted((module for module in leaves if resolve_ring(module.name) == inner), key=lambda item: item.name)
    targets = tuple(sorted(module.name for module in leaves if resolve_ring(module.name) == outer))
    source = sources[0] if sources else None
    return source, tuple(target for target in targets if source is None or target != source.name)


def _tach_canary_modules(policy: Policy, scan: ScanResult) -> tuple[ModuleInfo | None, tuple[str, ...]]:
    return _canary_modules(
        tuple(scan.modules.values()), policy.rings, lambda name: policy.resolve_ring(scan.arbor_index, name),
    )


def _tach_canary_modules_by_scope(
    policy: Policy, scan: ScanResult,
) -> dict[str, tuple[ModuleInfo | None, tuple[str, ...]]]:
    if not policy.scopes:
        return {"": _tach_canary_modules(policy, scan)}
    return {
        scope.name: _canary_modules(
            tuple(module for module in scan.modules.values() if scan.scope_by_module.get(module.name) == scope.name),
            scope.policy.rings,
            lambda name, scope_name=scope.name: policy.resolve_scope_ring(scope_name, scan.arbor_index, name),
        )
        for scope in policy.scopes
    }


def _copy_tach_project(root: Path, source_roots: tuple[str, ...], temp: Path, fs: FileSystem) -> None:
    """Clone only source roots and dependency/tool config into an isolated relative-path-preserving project."""
    roots = sorted({source.strip("/") or "." for source in source_roots}, key=lambda value: (value != ".", value.count("/")))
    if "." in roots:
        _copy_project_root(root, temp, fs)
    else:
        _copy_source_roots(root, roots, temp, fs)
    _copy_project_configs(root, temp, fs)


def _copy_project_root(root: Path, temp: Path, fs: FileSystem) -> None:
    ignored = {*NOISE_DIRS, "dist", "build"}
    for source in [*fs.glob(root, "*"), *fs.glob(root, ".*")]:
        if source.name in ignored:
            continue
        target = temp / source.name
        if fs.is_dir(source):
            fs.copy_tree(source, target, ignore=[*ignored, "__pycache__"])
        elif fs.is_file(source):
            fs.write_bytes(target, fs.read_bytes(source))


def _copy_source_roots(root: Path, roots: list[str], temp: Path, fs: FileSystem) -> None:
    copied: list[str] = []
    for source in roots:
        if any(source == parent or source.startswith(parent + "/") for parent in copied):
            continue
        source_path = root / source
        if fs.is_dir(source_path):
            fs.copy_tree(source_path, temp / source, ignore=[*NOISE_DIRS, ".git", "node_modules"])
            copied.append(source)


def _copy_project_configs(root: Path, temp: Path, fs: FileSystem) -> None:
    for relative in ("tach.toml", "pyproject.toml", "poetry.lock", "uv.lock", "requirements.txt", "setup.cfg"):
        source = root / relative
        target = temp / relative
        if fs.is_file(source) and not fs.exists(target):
            fs.mkdir(target.parent)
            fs.write_bytes(target, fs.read_bytes(source))


def _result_detail(result: ToolResult) -> str:
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    return stderr or stdout or "Tach exited unsuccessfully"
