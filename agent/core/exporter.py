"""Сборка и запись выгрузки.

Массивы сортируются по идентификатору перед записью, поэтому порядок обхода источников
на результат не влияет: повторный прогон того же состояния рабочей копии даёт тот же файл
с точностью до поля ``generated_at``.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from typing import Any

from agent.core.model import ENTITY_KEYS, ROOT_KEYS, SCHEMA_VERSION, Entity, FactCollector


def build_document(
    collector: FactCollector, generated_at: str, filters: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Собрать выгрузку из накопленных фактов.

    ``filters`` описывает применённую выборку — по каким авторам и каким исключениям путей
    собрана выгрузка, — чтобы потребитель отличал полную историю от усечённой.
    """
    projects = collector.entities("project")
    if len(projects) != 1:
        raise ValueError(f"в выгрузке должен быть ровно один project, собрано: {len(projects)}")

    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "filters": {
            "authors": sorted((filters or {}).get("authors", [])),
            "exclude": sorted((filters or {}).get("exclude", [])),
        },
        "project": projects[0].to_json(),
        "analysis_scope": dict(collector.analysis_scope),
    }
    for entity_type, key in ENTITY_KEYS.items():
        if key == "project":
            continue
        document[key] = _sorted_json(collector.entities(entity_type))
    document["relationships"] = sorted(
        (item.to_json() for item in collector.relationships()), key=lambda item: item["id"]
    )
    document["evidence"] = sorted(
        (item.to_json() for item in collector.evidence()), key=lambda item: item["id"]
    )
    return document


def serialize(document: dict[str, Any]) -> str:
    """Текст файла выгрузки: UTF-8, отступ два пробела, перевод строки в конце."""
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def write_document(document: dict[str, Any], output: str) -> None:
    """Записать выгрузку в файл; путь ``-`` направляет её в стандартный поток вывода."""
    payload = serialize(document)
    if output == "-":
        sys.stdout.write(payload)
        return
    path = Path(output)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _sorted_json(entities: list[Entity]) -> list[dict[str, Any]]:
    return sorted((entity.to_json() for entity in entities), key=lambda item: item["id"])


#: Секции, растущие вместе с проектом: каждая пишется отдельным объектом.
SECTION_KEYS: tuple[str, ...] = (
    "commits",
    "directories",
    "files",
    "technologies",
    "dependencies",
    "migrations",
    "database_objects",
    "documents",
    "requirements",
    "code_units",
    "findings",
    "relationships",
    "evidence",
)

#: Имя объекта со скалярной частью выгрузки и малыми секциями.
CORE_NAME = "core.json.gz"


def core_document(document: dict[str, Any]) -> dict[str, Any]:
    """Скалярная часть выгрузки и малые секции: всё, что не растёт с размером проекта."""
    return {key: value for key, value in document.items() if key not in SECTION_KEYS}


def write_sections(document: dict[str, Any], directory: str | Path) -> list[Path]:
    """Записать выгрузку посекционно: скалярная часть одним объектом, секции — по объекту.

    Секция пишется построчно — одна запись в строке — и сжимается отдельно от остальных:
    внутри общего объекта секция не адресуема, а общий архив лишил бы смысла разделение.
    Пустая секция записывается объектом с нулём строк: её отсутствие не отличалось бы
    от невыполненного сбора.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    core = target / CORE_NAME
    with gzip.open(core, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(core_document(document), ensure_ascii=False, indent=2) + "\n")
    written.append(core)

    for key in SECTION_KEYS:
        records = document.get(key) or []
        path = target / f"{key}.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        written.append(path)
    return written
