from __future__ import annotations

from typing import Any

from .schema_validator import SchemaValidator


def repair_to_schema(payload: Any, schema_name: str, validator: SchemaValidator) -> Any:
    return validator.normalize_to_schema(payload, schema_name)

