"""The versioned JSON export of the migration arguments (#54) and its JSON Schema. Pure.

One read-only document per catalog: the packages (nodes), their edges and the open annotation gaps. The schema is
published as ``docs/schemas/migration-export-v<version>.schema.json`` (``arborescent packages schema`` prints it) and a
test keeps the file equal to ``SCHEMA``. ``policy`` and ``pyproject`` are reserved slots, always ``null`` until the
release/pin/history policy (#56) and the derived ``pyproject.toml`` fields (#60) are annotated; they widen within this
version because they are already typed ``object | null``. Anything else that changes shape bumps ``EXPORT_VERSION``.
"""

from __future__ import annotations

import json
from typing import Any

from .migration_arguments import MigrationArguments, PackageArguments
from .package_model import HOME_KINDS

__all__ = ["EXPORT_NAME", "EXPORT_VERSION", "SCHEMA", "export_document", "render_export", "render_schema", "schema_filename"]

EXPORT_NAME = "arborescent-migration-export"
EXPORT_VERSION = 1


def schema_filename() -> str:
    return f"migration-export-v{EXPORT_VERSION}.schema.json"


def export_document(arguments: MigrationArguments, catalog: str) -> dict[str, Any]:
    return {
        "schema": EXPORT_NAME,
        "version": EXPORT_VERSION,
        "catalog": catalog,
        "ready": arguments.ready,
        "packages": [_package(item) for item in arguments.packages],
        "edges": [{"source": e.source, "target": e.target, "kind": e.kind} for e in arguments.edges],
        "gaps": [
            {"code": g.code, "package": g.package, "subject": g.subject, "detail": g.detail, "severity": g.severity}
            for g in arguments.gaps
        ],
    }


def render_export(document: dict[str, Any]) -> str:
    """Stable text: sorted keys, two-space indent, trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_schema() -> str:
    return json.dumps(SCHEMA, indent=2, sort_keys=True) + "\n"


def _package(item: PackageArguments) -> dict[str, Any]:
    home = None if item.home is None else {"kind": item.home.kind, "repository": item.home.repository, "path": item.home.path}
    return {
        "id": item.id,
        "name": item.name,
        "kind": item.kind,
        "summary": item.summary,
        "domain": item.domain,
        "capabilities": list(item.capabilities),
        "home": home,
        "owned": {
            "modules": [
                {"module": m.module, "path": m.path, "target_path": m.target_path, "target_module": m.target_module}
                for m in item.modules
            ],
            "assets": [{"path": a.path, "target_path": a.target_path} for a in item.assets],
            "path_renames": [{"source": r.source, "target": r.target} for r in item.path_renames],
            "module_renames": [{"source": r.source, "target": r.target} for r in item.module_renames],
        },
        "surface": {
            "public": None if item.public_surface is None else list(item.public_surface),
            "bypasses": [{"module": b.module, "package": b.package, "imports": b.imports} for b in item.bypasses],
        },
        "dependencies": {
            "packages": list(item.depends_on),
            "external": None if item.externals is None else list(item.externals),
        },
        "policy": None,
        "pyproject": None,
    }


def _object(properties: dict[str, Any], all_of: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "required": sorted(properties), "additionalProperties": False}
    if all_of is not None:
        schema["allOf"] = all_of
    return schema


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


_STRING: dict[str, Any] = {"type": "string"}
_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}
_STRINGS = _array(_STRING)
_NULLABLE_STRINGS: dict[str, Any] = {"type": ["array", "null"], "items": _STRING}
_RESERVED: dict[str, Any] = {"type": ["object", "null"], "description": "reserved: null until its issue lands"}

_PACKAGE = _object(
    {
        "id": _STRING,
        "name": _STRING,
        "kind": _STRING,
        "summary": _STRING,
        "domain": _NULLABLE_STRING,
        "capabilities": _STRINGS,
        "home": {
            "oneOf": [
                {"type": "null"},
                _object({"kind": {"enum": list(HOME_KINDS)}, "repository": _NULLABLE_STRING, "path": _NULLABLE_STRING}),
            ],
            "description": "where the package lives after the migration; null while not annotated (see gaps)",
        },
        "owned": _object(
            {
                "modules": _array(_object({"module": _STRING, "path": _STRING, "target_path": _NULLABLE_STRING, "target_module": _STRING})),
                "assets": _array(_object({"path": _STRING, "target_path": _NULLABLE_STRING})),
                "path_renames": _array(_object({"source": _STRING, "target": _STRING})),
                "module_renames": _array(_object({"source": _STRING, "target": _STRING})),
            }
        ),
        "surface": _object(
            {
                "public": _NULLABLE_STRINGS,
                "bypasses": _array(_object({"module": _STRING, "package": _STRING, "imports": _STRING})),
            }
        ),
        "dependencies": _object({"packages": _STRINGS, "external": _NULLABLE_STRINGS}),
        "policy": {**_RESERVED, "description": "release, pin and history policy (#56); never set for a workspace member"},
        "pyproject": {**_RESERVED, "description": "derivable pyproject.toml fields (#60)"},
    },
    all_of=[
        {
            "if": {
                "properties": {"home": {"type": "object", "properties": {"kind": {"const": "workspace-member"}}, "required": ["kind"]}},
                "required": ["home"],
            },
            "then": {"properties": {"policy": {"type": "null"}}},
        }
    ],
)

SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"urn:arborescent:migration-export:{EXPORT_VERSION}",
    "title": "Arborescent migration export",
    "description": "Read-only annotation data for external migration tools; carries unresolved annotation gaps as named entries.",
    **_object(
        {
            "schema": {"const": EXPORT_NAME},
            "version": {"const": EXPORT_VERSION},
            "catalog": _STRING,
            "ready": {"type": "boolean", "description": "true when no gap has severity error"},
            "packages": _array(_PACKAGE),
            "edges": _array(
                _object({"source": _STRING, "target": _STRING, "kind": {"enum": ["depends_on", "imports"]}})
            ),
            "gaps": _array(
                _object(
                    {
                        "code": _STRING,
                        "package": _NULLABLE_STRING,
                        "subject": _STRING,
                        "detail": _STRING,
                        "severity": {"enum": ["error", "info"]},
                    }
                )
            ),
        }
    ),
}
