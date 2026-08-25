"""Модель выгрузки: типы сущностей, перечни значений, типы связей и накопитель фактов.

Модуль не знает, откуда берутся факты: анализаторы кладут в накопитель сущности,
связи и подтверждения, а накопитель следит за инвариантом «факт без источника
в выгрузку не попадает».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"
AGENT_VERSION = "1.0.0"

#: Соответствие типа сущности корневому ключу выгрузки.
ENTITY_KEYS: dict[str, str] = {
    "project": "project",
    "repository": "repositories",
    "branch": "branches",
    "tag": "tags",
    "commit": "commits",
    "person": "people",
    "directory": "directories",
    "file": "files",
    "technology": "technologies",
    "dependency": "dependencies",
    "migration": "migrations",
    "database_object": "database_objects",
    "document": "documents",
    "requirement": "requirements",
}

#: Корневые ключи выгрузки в порядке их следования в файле.
ROOT_KEYS: tuple[str, ...] = (
    "schema_version",
    "generated_at",
    "filters",
    "project",
    "repositories",
    "branches",
    "tags",
    "commits",
    "people",
    "directories",
    "files",
    "technologies",
    "dependencies",
    "migrations",
    "database_objects",
    "documents",
    "requirements",
    "relationships",
    "evidence",
)

SOURCE_KINDS: frozenset[str] = frozenset(
    {"git", "filesystem", "manifest", "migration", "document", "llm"}
)

#: Допустимые пары концов для каждого типа связи: тип связи -> {(откуда, куда)}.
RELATIONSHIP_ENDS: dict[str, frozenset[tuple[str, str]]] = {
    "HAS_REPOSITORY": frozenset({("project", "repository")}),
    "HAS_COMMIT": frozenset({("repository", "commit")}),
    "HAS_BRANCH": frozenset({("repository", "branch")}),
    "HAS_TAG": frozenset({("repository", "tag")}),
    "POINTS_TO": frozenset({("branch", "commit"), ("tag", "commit")}),
    "PARENT_OF": frozenset({("commit", "commit")}),
    "AUTHORED_BY": frozenset({("commit", "person")}),
    "COMMITTED_BY": frozenset({("commit", "person")}),
    "MODIFIES": frozenset({("commit", "file")}),
    "CONTAINS": frozenset(
        {
            ("project", "directory"),
            ("repository", "directory"),
            ("directory", "directory"),
            ("directory", "file"),
        }
    ),
    "DECLARES": frozenset({("file", "dependency")}),
    "DEFINES": frozenset({("file", "migration"), ("file", "document")}),
    "AFFECTS": frozenset({("migration", "database_object")}),
    "BELONGS_TO": frozenset({("database_object", "database_object")}),
    "STATES": frozenset({("document", "requirement")}),
    "REFINES": frozenset({("requirement", "requirement")}),
    "VERIFIES": frozenset({("requirement", "requirement")}),
    "USES": frozenset({("project", "technology")}),
    "INDICATES": frozenset({("dependency", "technology"), ("file", "technology")}),
}


@dataclass
class Evidence:
    """Подтверждение факта: чем, откуда, когда и какой версией анализатора он получен."""

    id: str
    subject_id: str
    subject_type: str
    source_kind: str
    analyzer: str
    analyzer_version: str
    collected_at: str
    source_file_id: str | None = None
    source_commit_id: str | None = None
    source_locator: str | None = None
    model: str | None = None
    prompt_version: str | None = None

    def to_json(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "id": self.id,
            "subject_id": self.subject_id,
            "subject_type": self.subject_type,
            "source_kind": self.source_kind,
            "source_file_id": self.source_file_id,
            "source_commit_id": self.source_commit_id,
            "source_locator": self.source_locator,
            "analyzer": self.analyzer,
            "analyzer_version": self.analyzer_version,
            "collected_at": self.collected_at,
        }
        if self.model is not None:
            document["model"] = self.model
        if self.prompt_version is not None:
            document["prompt_version"] = self.prompt_version
        return document


@dataclass
class Entity:
    """Сущность выгрузки: тип, идентификатор, поля и накопленные подтверждения."""

    type: str
    id: str
    data: dict[str, Any]
    evidence_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {**self.data, "id": self.id, "evidence_ids": sorted(set(self.evidence_ids))}


@dataclass
class Relationship:
    """Направленная связь между двумя сущностями выгрузки."""

    id: str
    type: str
    from_id: str
    to_id: str
    attributes: dict[str, Any] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "from_id": self.from_id,
            "to_id": self.to_id,
            "attributes": self.attributes,
            "evidence_ids": sorted(set(self.evidence_ids)),
        }


class FactCollector:
    """Накопитель фактов, общий для всех анализаторов.

    Дубли сводятся по идентификатору: повторно добавленная сущность не создаёт вторую
    запись, а получает дополнительные подтверждения. Факт без подтверждения отбрасывается
    и попадает в предупреждения прогона.
    """

    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}
        self._relationships: dict[str, Relationship] = {}
        self._evidence: dict[str, Evidence] = {}
        self.warnings: list[str] = []

    def add_entity(
        self,
        entity_type: str,
        entity_id: str,
        data: dict[str, Any],
        evidence: Evidence | Iterable[Evidence],
    ) -> str | None:
        """Добавить сущность с подтверждениями; вернуть её идентификатор либо ``None``."""
        records = _as_list(evidence)
        if not records:
            self.warnings.append(
                f"факт отброшен без источника: сущность {entity_id} не имеет подтверждений"
            )
            return None
        if entity_type not in ENTITY_KEYS:
            self.warnings.append(f"неизвестный тип сущности: {entity_type} ({entity_id})")
            return None

        existing = self._entities.get(entity_id)
        if existing is None:
            self._entities[entity_id] = Entity(type=entity_type, id=entity_id, data=dict(data))
        else:
            self._merge_data(existing, data)

        for record in records:
            self._register_evidence(record)
            self._entities[entity_id].evidence_ids.append(record.id)
        return entity_id

    def add_relationship(
        self,
        relationship_type: str,
        from_id: str,
        to_id: str,
        evidence: Evidence | Iterable[Evidence],
        attributes: dict[str, Any] | None = None,
    ) -> str | None:
        """Добавить связь с подтверждениями; вернуть её идентификатор либо ``None``."""
        from agent.core.ids import relationship_id

        records = _as_list(evidence)
        if not records:
            self.warnings.append(
                f"факт отброшен без источника: связь {relationship_type} "
                f"{from_id} -> {to_id} не имеет подтверждений"
            )
            return None
        if relationship_type not in RELATIONSHIP_ENDS:
            self.warnings.append(f"неизвестный тип связи: {relationship_type}")
            return None

        identifier = relationship_id(from_id, relationship_type, to_id)
        relationship = self._relationships.get(identifier)
        if relationship is None:
            relationship = Relationship(
                id=identifier,
                type=relationship_type,
                from_id=from_id,
                to_id=to_id,
                attributes=dict(attributes or {}),
            )
            self._relationships[identifier] = relationship

        for record in records:
            self._register_evidence(record)
            relationship.evidence_ids.append(record.id)
        return identifier

    def has_entity(self, entity_id: str) -> bool:
        """Собрана ли сущность с таким идентификатором: связи ставятся только на собранное."""
        return entity_id in self._entities

    def entities(self, entity_type: str | None = None) -> list[Entity]:
        values = self._entities.values()
        if entity_type is None:
            return list(values)
        return [entity for entity in values if entity.type == entity_type]

    def relationships(self) -> list[Relationship]:
        return list(self._relationships.values())

    def evidence(self) -> list[Evidence]:
        return list(self._evidence.values())

    def _register_evidence(self, record: Evidence) -> None:
        if record.source_kind not in SOURCE_KINDS:
            self.warnings.append(
                f"неизвестный вид источника: {record.source_kind} ({record.subject_id})"
            )
        self._evidence.setdefault(record.id, record)

    def _merge_data(self, entity: Entity, data: dict[str, Any]) -> None:
        """Слить поля повторно встреченной сущности; расхождение остаётся за первым фактом."""
        for key, value in data.items():
            current = entity.data.get(key)
            if key not in entity.data:
                entity.data[key] = value
            elif current != value:
                self.warnings.append(
                    f"расхождение поля {key} у {entity.id}: сохранено {current!r}, "
                    f"отброшено {value!r}"
                )


def _as_list(evidence: Evidence | Iterable[Evidence]) -> list[Evidence]:
    if isinstance(evidence, Evidence):
        return [evidence]
    return [record for record in evidence]
