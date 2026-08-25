"""Валидация выгрузки: схема, ссылочная целостность, уникальность, логические правила.

Проверки выполняются до записи файла и повторяются командой ``validate`` над готовым
файлом, поэтому модуль работает с обычным словарём, а не с накопителем фактов.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.core import jsonschema
from agent.core.ids import entity_type_of
from agent.core.model import ENTITY_KEYS, RELATIONSHIP_ENDS

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "project-knowledge.schema.json"

#: Поля-ссылки: тип сущности -> имена полей, значения которых обязаны существовать в выгрузке.
REFERENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "repository": ("head_commit_id",),
    "branch": ("repository_id", "commit_id"),
    "tag": ("repository_id", "commit_id"),
    "commit": ("repository_id", "author_id", "committer_id", "parent_ids"),
    "directory": ("repository_id", "parent_id"),
    "file": ("repository_id", "directory_id"),
    "dependency": ("manifest_file_id",),
    "migration": ("repository_id", "file_id"),
    "database_object": ("parent_id",),
    "document": ("repository_id", "file_id"),
    "requirement": ("document_id",),
}


def load_schema(path: Path | None = None) -> dict[str, Any]:
    """Загрузить схему выгрузки; по умолчанию — поставляемую с агентом."""
    schema_path = path or DEFAULT_SCHEMA_PATH
    with schema_path.open(encoding="utf-8") as handle:
        return json.load(handle)


def validate_document(document: Any, schema: dict[str, Any]) -> list[str]:
    """Полная проверка выгрузки; пустой список означает, что файл годен."""
    errors = jsonschema.validate(document, schema)
    if errors:
        return errors

    errors.extend(_check_unique_ids(document))
    errors.extend(_check_references(document))
    errors.extend(_check_relationships(document))
    errors.extend(_check_logical_rules(document))
    errors.extend(_check_reachability(document))
    return errors


def _all_entities(document: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Пары «тип сущности, объект» по всем массивам выгрузки, включая project."""
    entities: list[tuple[str, dict[str, Any]]] = [("project", document["project"])]
    for entity_type, key in ENTITY_KEYS.items():
        if key == "project":
            continue
        entities.extend((entity_type, item) for item in document[key])
    return entities


def _known_ids(document: dict[str, Any]) -> set[str]:
    identifiers = {item["id"] for _, item in _all_entities(document)}
    identifiers.update(item["id"] for item in document["relationships"])
    identifiers.update(item["id"] for item in document["evidence"])
    return identifiers


def _check_unique_ids(document: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    everything = (
        [item for _, item in _all_entities(document)]
        + document["relationships"]
        + document["evidence"]
    )
    for item in everything:
        identifier = item["id"]
        if identifier in seen:
            duplicates.append(f"$.id — уникальность — {identifier} встречается повторно")
        seen.add(identifier)
    return duplicates


def _check_references(document: dict[str, Any]) -> list[str]:
    known = _known_ids(document)
    errors: list[str] = []

    for entity_type, item in _all_entities(document):
        for field_name in REFERENCE_FIELDS.get(entity_type, ()):
            value = item.get(field_name)
            if value is None:
                continue
            values = value if isinstance(value, list) else [value]
            for reference in values:
                if reference not in known:
                    errors.append(
                        f"$.{entity_type}[{item['id']}].{field_name} — ссылочная целостность"
                        f" — {reference} отсутствует в выгрузке"
                    )
        for reference in item.get("evidence_ids", []):
            if reference not in known:
                errors.append(
                    f"$.{entity_type}[{item['id']}].evidence_ids — ссылочная целостность"
                    f" — {reference} отсутствует в выгрузке"
                )

    for relationship in document["relationships"]:
        for field_name in ("from_id", "to_id"):
            if relationship[field_name] not in known:
                errors.append(
                    f"$.relationships[{relationship['id']}].{field_name} — ссылочная целостность"
                    f" — {relationship[field_name]} отсутствует в выгрузке"
                )
        for reference in relationship["evidence_ids"]:
            if reference not in known:
                errors.append(
                    f"$.relationships[{relationship['id']}].evidence_ids — ссылочная целостность"
                    f" — {reference} отсутствует в выгрузке"
                )

    for record in document["evidence"]:
        if record["subject_id"] not in known:
            errors.append(
                f"$.evidence[{record['id']}].subject_id — ссылочная целостность"
                f" — {record['subject_id']} отсутствует в выгрузке"
            )
        if record["subject_type"] != entity_type_of(record["subject_id"]):
            errors.append(
                f"$.evidence[{record['id']}].subject_type — соответствие префиксу"
                f" — {record['subject_type']} против {record['subject_id']}"
            )
    return errors


def _check_relationships(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for relationship in document["relationships"]:
        ends = (entity_type_of(relationship["from_id"]), entity_type_of(relationship["to_id"]))
        allowed = RELATIONSHIP_ENDS.get(relationship["type"], frozenset())
        if ends not in allowed:
            errors.append(
                f"$.relationships[{relationship['id']}].type — типы концов"
                f" — {relationship['type']} не соединяет {ends[0]} и {ends[1]}"
            )
        key = (relationship["from_id"], relationship["type"], relationship["to_id"])
        if key in seen:
            errors.append(
                f"$.relationships[{relationship['id']}] — уникальность"
                f" — связь {key} встречается повторно"
            )
        seen.add(key)
    return errors


def _check_logical_rules(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    # При выборке по автору перечень родителей усечён до попавших в выгрузку коммитов,
    # поэтому строгое соответствие is_merge числу родителей проверяется только на полной истории.
    truncated = bool(document.get("filters", {}).get("authors"))
    for commit in document["commits"]:
        parents = len(commit["parent_ids"])
        if not truncated and commit["is_merge"] != (parents >= 2):
            errors.append(
                f"$.commits[{commit['id']}].is_merge — соответствие числу родителей"
                f" — {commit['is_merge']} при {parents} родителях"
            )
        if truncated and not commit["is_merge"] and parents >= 2:
            errors.append(
                f"$.commits[{commit['id']}].is_merge — слияние не помечено"
                f" — {parents} родителя в выгрузке"
            )
        if not commit["sha"].startswith(commit["short_sha"]):
            errors.append(
                f"$.commits[{commit['id']}].short_sha — префикс sha — {commit['short_sha']}"
            )

    defaults: dict[str, int] = {}
    for branch in document["branches"]:
        defaults.setdefault(branch["repository_id"], 0)
        if branch["is_default"]:
            defaults[branch["repository_id"]] += 1
    for repository_id, count in defaults.items():
        if count != 1:
            errors.append(
                f"$.branches[repository_id={repository_id}].is_default — ровно одна ветка"
                f" по умолчанию — {count}"
            )

    for database_object in document["database_objects"]:
        is_table = database_object["kind"] == "table"
        has_parent = database_object["parent_id"] is not None
        if is_table and has_parent:
            errors.append(
                f"$.database_objects[{database_object['id']}].parent_id — у таблицы null"
                f" — {database_object['parent_id']}"
            )
        if not is_table and not has_parent:
            errors.append(
                f"$.database_objects[{database_object['id']}].parent_id — обязателен для"
                f" {database_object['kind']} — null"
            )

    char_counts = {item["id"]: item["char_count"] for item in document["documents"]}
    for requirement in document["requirements"]:
        start, end = requirement["source_char_range"]
        if start >= end:
            errors.append(
                f"$.requirements[{requirement['id']}].source_char_range — начало меньше конца"
                f" — {requirement['source_char_range']}"
            )
        limit = char_counts.get(requirement["document_id"])
        if limit is not None and end > limit:
            errors.append(
                f"$.requirements[{requirement['id']}].source_char_range — не длиннее документа"
                f" — {end} при char_count {limit}"
            )
        if requirement["extraction"]["method"] == "llm":
            for field_name in ("model", "prompt_version"):
                if not requirement["extraction"].get(field_name):
                    errors.append(
                        f"$.requirements[{requirement['id']}].extraction.{field_name}"
                        f" — обязателен при method llm — отсутствует"
                    )
        if requirement["extraction"]["confidence"] == "low":
            errors.append(
                f"$.requirements[{requirement['id']}].extraction.confidence — low не"
                f" выгружается — low"
            )

    for record in document["evidence"]:
        if record["source_kind"] == "git" and not record.get("source_commit_id"):
            errors.append(
                f"$.evidence[{record['id']}].source_commit_id — обязателен при source_kind git"
                f" — отсутствует"
            )
        if record["source_kind"] != "git" and not record.get("source_file_id"):
            # Источник вида filesystem — место в рабочей копии: у проекта и каталога нет сущности
            # File, поэтому точное место указывает source_locator. Для остальных видов источника
            # факт всегда лежит внутри файла, и ссылка на него обязательна.
            filesystem_locator = record["source_kind"] == "filesystem" and record.get("source_locator")
            if not filesystem_locator:
                errors.append(
                    f"$.evidence[{record['id']}].source_file_id — обязателен при source_kind"
                    f" {record['source_kind']} — отсутствует"
                )
        if record["source_kind"] == "llm":
            for field_name in ("model", "prompt_version"):
                if not record.get(field_name):
                    errors.append(
                        f"$.evidence[{record['id']}].{field_name} — обязателен при source_kind"
                        f" llm — отсутствует"
                    )

    pairs: set[tuple[str, str]] = set()
    for dependency in document["dependencies"]:
        key = (dependency["ecosystem"], dependency["name"].lower())
        if key in pairs:
            errors.append(
                f"$.dependencies[{dependency['id']}] — уникальность пары ecosystem и name"
                f" — {key}"
            )
        pairs.add(key)

    slugs: set[str] = set()
    for technology in document["technologies"]:
        if technology["slug"] in slugs:
            errors.append(
                f"$.technologies[{technology['id']}].slug — уникальность — {technology['slug']}"
            )
        slugs.add(technology["slug"])
    return errors


def _check_reachability(document: dict[str, Any]) -> list[str]:
    """Каждая сущность, кроме project, достижима из project по направлениям связей."""
    outgoing: dict[str, set[str]] = {}
    for relationship in document["relationships"]:
        outgoing.setdefault(relationship["from_id"], set()).add(relationship["to_id"])

    start = document["project"]["id"]
    reached: set[str] = set()
    queue = [start]
    while queue:
        current = queue.pop()
        for neighbour in outgoing.get(current, ()):  # порядок обхода на результат не влияет
            if neighbour not in reached:
                reached.add(neighbour)
                queue.append(neighbour)

    errors: list[str] = []
    for entity_type, item in _all_entities(document):
        if entity_type == "project":
            continue
        if item["id"] not in reached:
            errors.append(
                f"$.{ENTITY_KEYS[entity_type]}[{item['id']}] — достижимость из project"
                f" — недостижима"
            )
    return errors
