"""Проверка документа по JSON Schema без внешних зависимостей.

Поддерживается подмножество словаря draft 2020-12, которым описана схема выгрузки:
``type``, ``properties``, ``required``, ``additionalProperties``, ``items``, ``enum``,
``const``, ``pattern``, ``minLength``, ``minItems``, ``maxItems``, ``minimum``
и локальные ссылки ``$ref`` вида ``#/$defs/имя``.
"""

from __future__ import annotations

import re
from typing import Any

_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def validate(document: Any, schema: dict[str, Any]) -> list[str]:
    """Вернуть список ошибок вида «путь — правило — значение»; пустой список — документ годен."""
    errors: list[str] = []
    _validate(document, schema, schema, "$", errors)
    return errors


def _validate(
    value: Any,
    schema: dict[str, Any],
    root: dict[str, Any],
    path: str,
    errors: list[str],
) -> None:
    if "$ref" in schema:
        schema = _resolve(schema["$ref"], root)

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path} — const {schema['const']!r} — {value!r}")
        return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} — enum {schema['enum']} — {value!r}")
        return

    if "type" in schema and not _matches_type(value, schema["type"]):
        errors.append(f"{path} — type {schema['type']} — {type(value).__name__}")
        return

    if isinstance(value, str):
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            errors.append(f"{path} — pattern {pattern} — {value!r}")
        minimum_length = schema.get("minLength")
        if minimum_length is not None and len(value) < minimum_length:
            errors.append(f"{path} — minLength {minimum_length} — {value!r}")

    if isinstance(value, bool):
        return

    if isinstance(value, int) and "minimum" in schema and value < schema["minimum"]:
        errors.append(f"{path} — minimum {schema['minimum']} — {value!r}")

    if isinstance(value, list):
        _validate_array(value, schema, root, path, errors)

    if isinstance(value, dict):
        _validate_object(value, schema, root, path, errors)


def _validate_array(
    value: list[Any],
    schema: dict[str, Any],
    root: dict[str, Any],
    path: str,
    errors: list[str],
) -> None:
    minimum_items = schema.get("minItems")
    if minimum_items is not None and len(value) < minimum_items:
        errors.append(f"{path} — minItems {minimum_items} — {len(value)}")
    maximum_items = schema.get("maxItems")
    if maximum_items is not None and len(value) > maximum_items:
        errors.append(f"{path} — maxItems {maximum_items} — {len(value)}")
    item_schema = schema.get("items")
    if isinstance(item_schema, dict):
        for index, item in enumerate(value):
            _validate(item, item_schema, root, f"{path}[{index}]", errors)


def _validate_object(
    value: dict[str, Any],
    schema: dict[str, Any],
    root: dict[str, Any],
    path: str,
    errors: list[str],
) -> None:
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in value:
            errors.append(f"{path}.{name} — required — отсутствует")
    if schema.get("additionalProperties") is False:
        for name in value:
            if name not in properties:
                errors.append(f"{path}.{name} — additionalProperties false — лишнее поле")
    for name, property_schema in properties.items():
        if name in value:
            _validate(value[name], property_schema, root, f"{path}.{name}", errors)


def _matches_type(value: Any, expected: Any) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        python_type = _TYPES.get(name)
        if python_type is None:
            continue
        if name == "integer" and isinstance(value, bool):
            continue
        if name in {"string", "array", "object", "null", "boolean"} and isinstance(value, bool):
            if name != "boolean":
                continue
        if isinstance(value, python_type):
            return True
    return False


def _resolve(reference: str, root: dict[str, Any]) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError(f"поддерживаются только локальные ссылки: {reference}")
    node: Any = root
    for part in reference[2:].split("/"):
        node = node[part]
    return node
