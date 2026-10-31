"""Render the ``.arborescent-package.toml`` text of an extraction. Pure."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .package_model import MANIFEST_VERSION, PackageDescriptor, Placement


def render_manifest(
    package: PackageDescriptor,
    included: Sequence[PackageDescriptor],
    placements: Sequence[Placement],
    repository: str,
) -> str:
    lines = [f"manifest_version = {MANIFEST_VERSION}", ""]
    lines += ["[package]", *semantic_lines(package), ""]
    lines += [
        "[origin]",
        f"repository = {toml_str(package.repository)}",
        f"root = {toml_str(package.root)}",
        f"extraction_status = {toml_str(package.extraction_status)}",
        "",
        "[location]",
        f"repository = {toml_str(repository)}",
        'root = "."',
        "",
        "[extraction]",
        'status = "extracted"',
        f"included_packages = {toml_list([item.id for item in included])}",
        "",
    ]
    for descriptor in included:
        if descriptor.id == package.id:
            continue
        lines += [
            "[[dependencies]]",
            *semantic_lines(descriptor),
            f"origin_repository = {toml_str(descriptor.repository)}",
            f"origin_root = {toml_str(descriptor.root)}",
            f"current_root = {toml_str(f'dependencies/{descriptor.id}')}",
            "",
        ]
    for placement in placements:
        lines += [
            "[[paths]]",
            f"package_id = {toml_str(placement.package_id)}",
            f"role = {toml_str(placement.item.role)}",
            f"metadata = {inline_table(placement.item.metadata or {})}",
            f"origin_path = {toml_str(placement.item.path)}",
            f"current_path = {toml_str(placement.current_path)}",
            "",
        ]
    return "\n".join(lines)


def semantic_lines(package: PackageDescriptor) -> list[str]:
    lines = [
        f"id = {toml_str(package.id)}",
        f"name = {toml_str(package.name)}",
        f"kind = {toml_str(package.kind)}",
    ]
    if package.domain is not None:
        lines.append(f"domain = {toml_str(package.domain)}")
    lines += [
        f"summary = {toml_str(package.summary)}",
        f"capabilities = {toml_list(package.capabilities)}",
        f"depends_on = {toml_list(package.depends_on)}",
    ]
    return lines


def toml_str(value: str) -> str:
    """A TOML basic string. ``json.dumps`` is not one: it writes non-BMP characters as invalid surrogate pairs."""
    escapes = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r"}
    return (
        '"'
        + "".join(
            escapes.get(char)
            or (
                f"\\u{ord(char):04x}" if ord(char) < 0x20 or ord(char) == 0x7F else char
            )
            for char in value
        )
        + '"'
    )


def toml_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(toml_str(value) for value in values) + "]"


def inline_table(value: Mapping[str, str]) -> str:
    return (
        "{ "
        + ", ".join(
            f"{toml_str(key)} = {toml_str(item)}" for key, item in value.items()
        )
        + " }"
        if value
        else "{}"
    )
