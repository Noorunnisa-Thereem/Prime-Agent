from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ValidationIssue
from .utils import coerce_integer, coerce_number, coerce_string, dedupe_preserve_order, read_json


@dataclass(slots=True)
class SchemaBundle:
    name: str
    schema: dict[str, Any]


class SchemaValidator:
    def __init__(self, schema_root: Path):
        self.schema_root = schema_root
        self._cache: dict[str, SchemaBundle] = {}

    def load(self, schema_name: str) -> dict[str, Any]:
        if schema_name in self._cache:
            return self._cache[schema_name].schema
        path = self.schema_root / f"{schema_name}.schema.json"
        schema = read_json(path, {})
        if not schema:
            raise FileNotFoundError(f"Schema not found: {path}")
        bundle = SchemaBundle(name=schema_name, schema=schema)
        self._cache[schema_name] = bundle
        return schema

    def default(self, schema_name: str) -> Any:
        schema = self.load(schema_name)
        return self._default_for_schema(schema, schema)

    def validate(self, instance: Any, schema_name: str) -> list[ValidationIssue]:
        schema = self.load(schema_name)
        issues: list[ValidationIssue] = []
        self._validate(instance, schema, f"$", schema_name, issues, schema)
        return issues

    def _resolve_ref(self, schema: dict[str, Any], ref: str) -> dict[str, Any]:
        if ref.startswith("#/"):
            target: Any = schema
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target[part]
            if not isinstance(target, dict):
                raise TypeError(f"Reference {ref!r} did not resolve to a schema object")
            return target

        ref_path, fragment = (ref.split("#", 1) + [""])[:2] if "#" in ref else (ref, "")
        external_path = (self.schema_root / ref_path).resolve()
        if not external_path.exists():
            raise FileNotFoundError(f"Referenced schema not found: {external_path}")
        external_schema = read_json(external_path, {})
        if not isinstance(external_schema, dict) or not external_schema:
            raise FileNotFoundError(f"Referenced schema is empty: {external_path}")
        if fragment:
            target: Any = external_schema
            fragment = fragment.lstrip("/")
            if fragment:
                for part in fragment.split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    target = target[part]
            if not isinstance(target, dict):
                raise TypeError(f"Reference {ref!r} did not resolve to a schema object")
            return target
        return external_schema

    def _schema_type(self, schema: dict[str, Any]) -> list[str]:
        schema_type = schema.get("type")
        if schema_type is None:
            return []
        if isinstance(schema_type, str):
            return [schema_type]
        return list(schema_type)

    def _matches_type(self, instance: Any, schema_types: list[str]) -> bool:
        if not schema_types:
            return True
        if instance is None:
            return "null" in schema_types
        if isinstance(instance, bool):
            return "boolean" in schema_types
        if isinstance(instance, int) and not isinstance(instance, bool):
            return "integer" in schema_types or "number" in schema_types
        if isinstance(instance, float):
            return "number" in schema_types
        if isinstance(instance, str):
            return "string" in schema_types
        if isinstance(instance, list):
            return "array" in schema_types
        if isinstance(instance, dict):
            return "object" in schema_types
        return False

    def _default_for_schema(self, schema: dict[str, Any], root_schema: dict[str, Any]) -> Any:
        if "$ref" in schema:
            schema = self._resolve_ref(root_schema, schema["$ref"])
        if "const" in schema:
            return deepcopy(schema["const"])
        if "enum" in schema:
            return deepcopy(schema["enum"][0])
        schema_types = self._schema_type(schema)
        if "object" in schema_types:
            result: dict[str, Any] = {}
            for key, child_schema in schema.get("properties", {}).items():
                result[key] = self._default_for_schema(child_schema, root_schema)
            return result
        if "array" in schema_types:
            return []
        if "string" in schema_types:
            if "null" in schema_types:
                return None
            return ""
        if "integer" in schema_types:
            if "null" in schema_types:
                return None
            return 0
        if "number" in schema_types:
            if "null" in schema_types:
                return None
            return 0.0
        if "boolean" in schema_types:
            if "null" in schema_types:
                return None
            return False
        if "null" in schema_types:
            return None
        return None

    def _validate(
        self,
        instance: Any,
        schema: dict[str, Any],
        path: str,
        schema_name: str,
        issues: list[ValidationIssue],
        root_schema: dict[str, Any],
    ) -> None:
        if "$ref" in schema:
            schema = self._resolve_ref(root_schema, schema["$ref"])

        schema_types = self._schema_type(schema)
        if schema_types and not self._matches_type(instance, schema_types):
            issues.append(
                ValidationIssue(
                    schema_name=schema_name,
                    path=path,
                    message=f"Expected type {schema_types}, got {type(instance).__name__}",
                    issue_key=f"{schema_name}:{path}:type-mismatch",
                )
            )
            return

        if instance is None:
            return

        if "const" in schema and instance != schema["const"]:
            issues.append(
                ValidationIssue(
                    schema_name=schema_name,
                    path=path,
                    message=f"Expected constant value {schema['const']!r}",
                    issue_key=f"{schema_name}:{path}:const-mismatch",
                )
            )
            return

        if "enum" in schema and instance not in schema["enum"]:
            issues.append(
                ValidationIssue(
                    schema_name=schema_name,
                    path=path,
                    message=f"Value {instance!r} not in enum {schema['enum']!r}",
                    issue_key=f"{schema_name}:{path}:enum-mismatch",
                )
            )
            return

        if "format" in schema and isinstance(instance, str):
            format_name = schema["format"]
            if format_name == "date-time":
                try:
                    datetime.fromisoformat(instance.replace("Z", "+00:00"))
                except ValueError:
                    issues.append(
                        ValidationIssue(
                            schema_name=schema_name,
                            path=path,
                            message="Expected RFC 3339 date-time string",
                            issue_key=f"{schema_name}:{path}:bad-datetime",
                        )
                    )
            elif format_name == "date":
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", instance):
                    issues.append(
                        ValidationIssue(
                            schema_name=schema_name,
                            path=path,
                            message="Expected ISO date string YYYY-MM-DD",
                            issue_key=f"{schema_name}:{path}:bad-date",
                        )
                    )

        if isinstance(instance, dict):
            required = schema.get("required", [])
            for key in required:
                if key not in instance:
                    issues.append(
                        ValidationIssue(
                            schema_name=schema_name,
                            path=f"{path}.{key}",
                            message="Missing required property",
                            issue_key=f"{schema_name}:{path}.{key}:missing",
                        )
                    )
            properties = schema.get("properties", {})
            for key, value in instance.items():
                if key not in properties:
                    if schema.get("additionalProperties", True) is False:
                        issues.append(
                            ValidationIssue(
                                schema_name=schema_name,
                                path=f"{path}.{key}",
                                message="Unexpected additional property",
                                issue_key=f"{schema_name}:{path}.{key}:extra",
                            )
                        )
                    continue
                self._validate(value, properties[key], f"{path}.{key}", schema_name, issues, root_schema)
            return

        if isinstance(instance, list):
            items_schema = schema.get("items")
            if items_schema is None:
                return
            if "minItems" in schema and len(instance) < int(schema["minItems"]):
                issues.append(
                    ValidationIssue(
                        schema_name=schema_name,
                        path=path,
                        message=f"Expected at least {schema['minItems']} items",
                        issue_key=f"{schema_name}:{path}:too-few-items",
                    )
                )
            if "maxItems" in schema and len(instance) > int(schema["maxItems"]):
                issues.append(
                    ValidationIssue(
                        schema_name=schema_name,
                        path=path,
                        message=f"Expected at most {schema['maxItems']} items",
                        issue_key=f"{schema_name}:{path}:too-many-items",
                    )
                )
            for index, value in enumerate(instance):
                self._validate(value, items_schema, f"{path}[{index}]", schema_name, issues, root_schema)

    def normalize_to_schema(self, instance: Any, schema_name: str) -> Any:
        schema = self.load(schema_name)
        defaults = self.default(schema_name)
        return self._merge_with_schema(defaults, instance, schema, schema)

    def _merge_with_schema(
        self,
        base: Any,
        candidate: Any,
        schema: dict[str, Any],
        root_schema: dict[str, Any],
    ) -> Any:
        if "$ref" in schema:
            schema = self._resolve_ref(root_schema, schema["$ref"])
        if candidate is None:
            return deepcopy(base)
        if "const" in schema:
            return deepcopy(schema["const"])
        if "enum" in schema:
            return candidate if candidate in schema["enum"] else deepcopy(base)

        schema_types = self._schema_type(schema)
        if "object" in schema_types:
            if not isinstance(candidate, dict):
                return deepcopy(base)
            properties = schema.get("properties", {})
            result = deepcopy(base) if isinstance(base, dict) else {}
            for key, child_schema in properties.items():
                if key in candidate:
                    result[key] = self._merge_with_schema(
                        result.get(key),
                        candidate[key],
                        child_schema,
                        root_schema,
                    )
            return result

        if "array" in schema_types:
            if not isinstance(candidate, list):
                if candidate is None:
                    return deepcopy(base)
                candidate = [candidate]
            item_schema = schema.get("items", {})
            normalized_items = [
                self._merge_with_schema(None, item, item_schema, root_schema) for item in candidate
            ]
            return dedupe_preserve_order(normalized_items)

        if "integer" in schema_types:
            coerced = coerce_integer(candidate)
            if coerced is not None:
                return coerced
            if isinstance(base, int):
                return base
            return deepcopy(base)

        if "number" in schema_types:
            coerced = coerce_number(candidate)
            if coerced is not None:
                return coerced
            if isinstance(base, (int, float)):
                return base
            return deepcopy(base)

        if "boolean" in schema_types:
            if isinstance(candidate, bool):
                return candidate
            if isinstance(candidate, str):
                lowered = candidate.strip().lower()
                if lowered in {"true", "yes", "1"}:
                    return True
                if lowered in {"false", "no", "0"}:
                    return False
            return deepcopy(base)

        if "string" in schema_types:
            coerced = coerce_string(candidate)
            if coerced is not None:
                return coerced
            return deepcopy(base)

        return deepcopy(candidate)
