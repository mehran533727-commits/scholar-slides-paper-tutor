"""Canonical JSON Schema validation for Scholar Slides artifacts.

Use :func:`create_schema_validator` instead of instantiating jsonschema's
``Draft202012Validator`` directly. Scholar Slides schemas may declare explicit
cross-field keywords that standard JSON Schema deliberately leaves undefined.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError, validators


def resolve_skill_schema_path(
    name: str,
    *,
    anchor: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve a shipped Skill schema in source and Windows production layouts."""
    if not name or Path(name).name != name:
        raise ValueError("schema name must be a plain filename")
    env = environment if environment is not None else os.environ
    script = Path(anchor or __file__).resolve()
    candidates: list[Path] = []
    configured_root = str(env.get("SCHOLAR_SLIDES_ROOT", "")).strip()
    if configured_root:
        candidates.append(Path(configured_root).resolve() / "schemas" / name)
    parents = script.parents
    if len(parents) >= 2:
        candidates.append(parents[1] / "skill" / "scholar-slides" / "schemas" / name)
    if len(parents) >= 3:
        candidates.append(parents[2] / "schemas" / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"cannot locate shipped schema {name!r}; searched: {searched}")


def _normalized_page_bbox(
    validator: Any,
    enabled: object,
    instance: object,
    schema: Mapping[str, Any],
):
    """Implement the declared normalized-bbox cross-field schema keyword."""
    del validator, schema
    if enabled is not True or not isinstance(instance, Mapping):
        return

    try:
        x, y, width, height = (
            Decimal(str(instance[key])) for key in ("x", "y", "width", "height")
        )
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return

    if not all(value.is_finite() for value in (x, y, width, height)):
        yield ValidationError("bbox values must be finite")
        return

    if x + width > 1 or y + height > 1:
        yield ValidationError("bbox exceeds normalized page bounds")


ScholarSlidesValidator = validators.extend(
    Draft202012Validator,
    {"x-normalized-page-bbox": _normalized_page_bbox},
)


def create_schema_validator(schema: Mapping[str, Any]) -> Draft202012Validator:
    """Return the canonical validator, including declared cross-field constraints."""
    ScholarSlidesValidator.check_schema(schema)
    return ScholarSlidesValidator(schema)
