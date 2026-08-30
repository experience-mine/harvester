"""Нормализация результатов внешнего анализа кода в модель выгрузки.

Анализ кода выполняется отдельной утилитой до прогона агента, его документы лежат
в каталоге результатов и передаются ключом ``--code-analysis``. Анализатор приводит
их адресацию к идентификаторам файлов выгрузки и раскладывает содержание по сущностям
``code_unit`` и ``finding`` и связям между ними.

Факт, ссылку которого не удалось привести к собранному файлу, отбрасывается
с предупреждением: сущность под отсутствующий файл нарушила бы требование
подтверждения источником и сделала бы выгрузку недостижимой из проекта.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.core.ids import code_unit_id, evidence_id, finding_id
from agent.core.model import Evidence, FactCollector
from agent.core.paths import analysis_file_id, relative_to_project
from agent.core.registry import AnalyzerContext

#: Имена документов анализа кода в каталоге результатов.
DOCUMENT_NAMES: dict[str, str] = {
    "structure": "tldr-structure.json",
    "calls": "tldr-calls.json",
    "health": "tldr-health.json",
}

ANALYZER_NAME = "code_analysis"
ANALYZER_VERSION = "1.0.0"

#: Виды определений, которые могут содержать другие определения.
CONTAINER_KINDS: frozenset[str] = frozenset({"class", "interface"})


class CodeAnalysisAnalyzer:
    """Анализатор нормализации: превращает документы внешнего анализа в факты выгрузки."""

    name = ANALYZER_NAME
    version = ANALYZER_VERSION

    def __init__(self) -> None:
        #: Число фактов, отброшенных из-за ссылок на файлы вне выгрузки.
        self.dropped = 0

    def analyze(self, context: AnalyzerContext, collector: FactCollector) -> None:
        documents = load_documents(getattr(context.config, "code_analysis_path", None))
        if not documents:
            return
        context.progress.stage("нормализация анализа кода")
        self.dropped = 0
        collector.analysis_scope = describe_scope(documents, context)

        structure = documents.get("structure")
        if isinstance(structure, dict):
            self._collect_definitions(structure, context, collector)

        calls = documents.get("calls")
        if isinstance(calls, dict):
            self._collect_calls(calls, context, collector)

        health = documents.get("health")
        if isinstance(health, dict):
            self._collect_metrics(health, context, collector)
            self._collect_findings(health, context, collector)

        # Число промахов принадлежит охвату: по нему потребитель судит о полноте
        # нормализации, а манифест прогона берёт его оттуда.
        collector.analysis_scope["dropped_facts"] = self.dropped
        if self.dropped:
            collector.warnings.append(
                f"анализ кода: отброшено фактов со ссылкой вне выгрузки — {self.dropped}"
            )

    def _collect_definitions(
        self, structure: dict[str, Any], context: AnalyzerContext, collector: FactCollector
    ) -> None:
        """Определения кода: сущности ``code_unit``, связи с файлом и с объемлющим классом."""
        analysis_root = str(structure.get("root") or "")
        project_root = str(context.root)

        for entry in structure.get("files") or []:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "")
            identifier = analysis_file_id(
                context.repository_slug, analysis_root, project_root, path
            )
            relative = relative_to_project(analysis_root, project_root, path)
            definitions = entry.get("definitions") or []
            if identifier is None or relative is None or not collector.has_entity(identifier):
                self.dropped += len(definitions)
                continue
            self._collect_file_definitions(
                definitions, identifier, relative, context, collector
            )

    def _collect_calls(
        self, calls: dict[str, Any], context: AnalyzerContext, collector: FactCollector
    ) -> None:
        """Вызовы между определениями: связь ставится только между собранными определениями."""
        analysis_root = str(calls.get("root") or "")
        project_root = str(context.root)
        slug = context.repository_slug

        # Одна пара определений может вызываться несколько раз с разными видами вызова,
        # а связь между ними в выгрузке одна. Виды собираются заранее, иначе при сведении
        # дублей сохранился бы только вид, встреченный первым.
        edges: dict[tuple[str, str, str], set[str]] = {}
        for edge in calls.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            source = _unit_reference(
                slug, analysis_root, project_root, edge.get("src_file"), edge.get("src_func")
            )
            target = _unit_reference(
                slug, analysis_root, project_root, edge.get("dst_file"), edge.get("dst_func")
            )
            if source is None or target is None:
                self.dropped += 1
                continue

            source_file, caller = source
            _, callee = target
            if not collector.has_entity(caller) or not collector.has_entity(callee):
                self.dropped += 1
                continue

            kinds = edges.setdefault((caller, callee, source_file), set())
            call_type = edge.get("call_type")
            if call_type:
                kinds.add(str(call_type))

        for (caller, callee, source_file), kinds in edges.items():
            collector.add_relationship(
                "CALLS",
                caller,
                callee,
                code_evidence(
                    caller,
                    "tldr.calls",
                    caller.split(":", 1)[-1],
                    context.collected_at,
                    source_file,
                ),
                attributes={"call_types": sorted(kinds)} if kinds else {},
            )

    def _collect_metrics(
        self, health: dict[str, Any], context: AnalyzerContext, collector: FactCollector
    ) -> None:
        """Измерения элементов кода: поля сущности ``code_unit``, а не отдельные факты.

        Суждения анализатора (``rank``, отнесение к проблемным) здесь не разбираются:
        измерение объективно и воспроизводимо, суждение зависит от порогов и потому
        попадает в находки.
        """
        analysis_root = str(health.get("root") or "")
        project_root = str(context.root)
        details = health.get("details")
        if not isinstance(details, dict):
            return

        self._apply_metrics(
            _section(details, "complexity", "hotspots"),
            ("cyclomatic", "cognitive", "loc"),
            "tldr.complexity",
            analysis_root,
            project_root,
            context,
            collector,
        )
        self._apply_metrics(
            _section(details, "cohesion", "classes"),
            ("lcom4", "method_count", "field_count"),
            "tldr.cohesion",
            analysis_root,
            project_root,
            context,
            collector,
        )

    def _collect_findings(
        self, health: dict[str, Any], context: AnalyzerContext, collector: FactCollector
    ) -> None:
        """Суждения анализатора: сущность ``finding`` и связь ``ABOUT`` с её предметом."""
        analysis_root = str(health.get("root") or "")
        project_root = str(context.root)
        details = health.get("details")
        if not isinstance(details, dict):
            return

        self._apply_findings(
            _section(details, "complexity", "hotspots"),
            "hotspot",
            "tldr.complexity",
            ("rank",),
            analysis_root,
            project_root,
            context,
            collector,
        )
        self._apply_findings(
            [
                entry
                for entry in _section(details, "cohesion", "classes")
                if isinstance(entry, dict) and entry.get("verdict") not in (None, "cohesive")
            ],
            "low_cohesion",
            "tldr.cohesion",
            ("verdict", "split_suggestion"),
            analysis_root,
            project_root,
            context,
            collector,
        )
        self._apply_findings(
            _section(details, "dead", "dead_functions"),
            "dead_code",
            "tldr.dead",
            ("visibility",),
            analysis_root,
            project_root,
            context,
            collector,
        )

        self._collect_pairs(details, analysis_root, project_root, context, collector)

        # Метрики пакетов адресуют пакет, которому в модели выгрузки нет сущности:
        # нормализовать такое суждение нечем, поэтому оно считается промахом.
        packages = _section(details, "metrics", "packages")
        if packages:
            self.dropped += len(packages)
            collector.warnings.append(
                f"анализ кода: суждения о пакетах не нормализованы — {len(packages)}"
            )

    def _collect_pairs(
        self,
        details: dict[str, Any],
        analysis_root: str,
        project_root: str,
        context: AnalyzerContext,
        collector: FactCollector,
    ) -> None:
        """Парные суждения: связь между файлами, а не сущность находки.

        Предмет такого суждения — сама пара, и связь выражает её напрямую; сущность
        потребовала бы двух дополнительных связей, не добавив ничего сверх.
        """
        tight = [
            entry
            for entry in _section(details, "coupling", "top_pairs")
            if isinstance(entry, dict) and entry.get("verdict") == "tight"
        ]
        for entry in tight:
            ends = self._pair_ends(
                context,
                collector,
                analysis_root,
                project_root,
                entry.get("source"),
                entry.get("target"),
            )
            if ends is None:
                continue
            first, second = ends
            attributes = {
                field: entry[field]
                for field in ("import_count", "call_count", "score", "verdict")
                if entry.get(field) is not None
            }
            collector.add_relationship(
                "COUPLED_WITH",
                first,
                second,
                code_evidence(first, "tldr.coupling", first.split("/", 1)[-1], context.collected_at, first),
                attributes=attributes,
            )

        # Один и тот же файл может содержать несколько клонов из другого файла,
        # а связь между парой файлов одна: показатели сводятся заранее.
        clones: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in _section(details, "similar", "clone_pairs"):
            if not isinstance(entry, dict):
                continue
            first_fragment = entry.get("fragment1") or {}
            second_fragment = entry.get("fragment2") or {}
            ends = self._pair_ends(
                context,
                collector,
                analysis_root,
                project_root,
                first_fragment.get("file"),
                second_fragment.get("file"),
            )
            if ends is None:
                continue
            if ends[0] == ends[1]:
                continue

            state = clones.setdefault(ends, {"clone_count": 0, "clone_types": set(), "similarity": 0.0})
            state["clone_count"] += 1
            if entry.get("clone_type"):
                state["clone_types"].add(str(entry["clone_type"]))
            similarity = entry.get("similarity")
            if isinstance(similarity, (int, float)):
                state["similarity"] = max(state["similarity"], float(similarity))

        for (first, second), state in clones.items():
            collector.add_relationship(
                "SIMILAR_TO",
                first,
                second,
                code_evidence(first, "tldr.similar", first.split("/", 1)[-1], context.collected_at, first),
                attributes={
                    "clone_count": state["clone_count"],
                    "clone_types": sorted(state["clone_types"]),
                    "max_similarity": state["similarity"],
                },
            )

    def _pair_ends(
        self,
        context: AnalyzerContext,
        collector: FactCollector,
        analysis_root: str,
        project_root: str,
        source: Any,
        target: Any,
    ) -> tuple[str, str] | None:
        """Концы парного суждения в устойчивом порядке либо ``None`` при промахе.

        Суждение о паре симметрично, поэтому концы упорядочиваются: иначе одна и та же
        пара, встреченная в обратном порядке, дала бы вторую связь.
        """
        slug = context.repository_slug
        first = analysis_file_id(slug, analysis_root, project_root, str(source or ""))
        second = analysis_file_id(slug, analysis_root, project_root, str(target or ""))
        if first is None or second is None:
            self.dropped += 1
            return None
        if not collector.has_entity(first) or not collector.has_entity(second):
            self.dropped += 1
            return None
        return (first, second) if first <= second else (second, first)

    def _apply_findings(
        self,
        entries: list[Any],
        kind: str,
        analyzer: str,
        fields: tuple[str, ...],
        analysis_root: str,
        project_root: str,
        context: AnalyzerContext,
        collector: FactCollector,
    ) -> None:
        """Находки одного раздела; предмет находки — уже собранное определение кода."""
        slug = context.repository_slug
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            reference = _unit_reference(
                slug, analysis_root, project_root, entry.get("file"), entry.get("name")
            )
            if reference is None:
                self.dropped += 1
                continue

            source_file, subject = reference
            if not collector.has_entity(subject):
                self.dropped += 1
                continue

            identifier = finding_id(kind, subject, analyzer)
            data: dict[str, Any] = {"kind": kind, "subject_id": subject, "analyzer": analyzer}
            for field in fields:
                if entry.get(field) is not None:
                    data[field] = entry[field]

            locator = subject.split(":", 2)[-1]
            line = entry.get("line")
            if isinstance(line, int):
                locator = f"{locator}:{line}"

            if collector.add_entity(
                "finding",
                identifier,
                data,
                code_evidence(identifier, analyzer, locator, context.collected_at, source_file),
            ) is None:
                continue
            collector.add_relationship(
                "HAS_FINDING",
                subject,
                identifier,
                code_evidence(identifier, analyzer, locator, context.collected_at, source_file),
            )

    def _apply_metrics(
        self,
        entries: list[Any],
        fields: tuple[str, ...],
        analyzer: str,
        analysis_root: str,
        project_root: str,
        context: AnalyzerContext,
        collector: FactCollector,
    ) -> None:
        """Дописать измерения в поля уже собранных определений кода."""
        slug = context.repository_slug
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            reference = _unit_reference(
                slug, analysis_root, project_root, entry.get("file"), entry.get("name")
            )
            if reference is None:
                self.dropped += 1
                continue

            source_file, identifier = reference
            if not collector.has_entity(identifier):
                self.dropped += 1
                continue

            measured = {field: entry[field] for field in fields if entry.get(field) is not None}
            if not measured:
                continue

            locator = f"{identifier.split(':', 2)[-1]}"
            line = entry.get("line")
            if isinstance(line, int):
                locator = f"{locator}:{line}"
            collector.add_entity(
                "code_unit",
                identifier,
                measured,
                code_evidence(identifier, analyzer, locator, context.collected_at, source_file),
            )

    def _collect_file_definitions(
        self,
        definitions: list[Any],
        file_identifier: str,
        relative: str,
        context: AnalyzerContext,
        collector: FactCollector,
    ) -> None:
        """Определения одного файла; принадлежность члена классу — по вложенности строк."""
        ordered = sorted(
            (item for item in definitions if isinstance(item, dict)),
            key=lambda item: (_line(item, "line_start"), -_line(item, "line_end")),
        )
        containers: list[tuple[int, int, str, str]] = []

        for item in ordered:
            name = str(item.get("name") or "").strip()
            kind = str(item.get("kind") or "").strip()
            if not name or not kind:
                continue

            start, end = _line(item, "line_start"), _line(item, "line_end")
            while containers and start > containers[-1][1]:
                containers.pop()

            owner = containers[-1] if containers else None
            qualified = f"{owner[2]}.{name}" if owner else name
            identifier = code_unit_id(context.repository_slug, relative, qualified)

            data: dict[str, Any] = {"name": name, "kind": kind, "qualified_name": qualified}
            for field in ("signature", "line_start", "line_end"):
                if item.get(field) is not None:
                    data[field] = item[field]

            record = code_evidence(
                identifier,
                "tldr.structure",
                f"{relative}:{start}",
                context.collected_at,
                file_identifier,
            )
            if collector.add_entity("code_unit", identifier, data, record) is None:
                continue

            collector.add_relationship(
                "DEFINES",
                file_identifier,
                identifier,
                code_evidence(
                    identifier,
                    "tldr.structure",
                    f"{relative}:{start}",
                    context.collected_at,
                    file_identifier,
                ),
            )
            if owner is not None:
                collector.add_relationship(
                    "CONTAINS",
                    owner[3],
                    identifier,
                    code_evidence(
                        identifier,
                        "tldr.structure",
                        f"{relative}:{start}",
                        context.collected_at,
                        file_identifier,
                    ),
                )
            if kind in CONTAINER_KINDS:
                containers.append((start, end, qualified, identifier))


def _unit_reference(
    slug: str, analysis_root: str, project_root: str, path: Any, qualified: Any
) -> tuple[str, str] | None:
    """Идентификаторы файла и определения по ссылке анализатора либо ``None`` при промахе."""
    name = str(qualified or "").strip()
    relative = relative_to_project(analysis_root, project_root, str(path or ""))
    if not name or relative is None:
        return None
    return f"file:{slug}/{relative}", code_unit_id(slug, relative, name)


def code_evidence(
    subject_id: str,
    analyzer: str,
    locator: str,
    collected_at: str,
    source_file_id: str | None = None,
) -> Evidence:
    """Подтверждение факта анализа кода: анализатор, его версия и место в коде.

    Факт анализа кода всегда лежит внутри файла, поэтому ссылка на файл-источник
    обязательна: без неё выгрузка не проходит проверку целостности.
    """
    return Evidence(
        id=evidence_id(subject_id, "code_analysis", source_file_id, locator, analyzer),
        subject_id=subject_id,
        subject_type=subject_id.split(":", 1)[0],
        source_kind="code_analysis",
        source_file_id=source_file_id,
        analyzer=analyzer,
        analyzer_version=ANALYZER_VERSION,
        collected_at=collected_at,
        source_locator=locator,
    )


def _section(details: dict[str, Any], name: str, key: str) -> list[Any]:
    """Список записей раздела анализа; отсутствующий или неуспешный раздел даёт пустой список."""
    section = details.get(name)
    if not isinstance(section, dict) or not section.get("success", True):
        return []
    inner = section.get("details")
    if not isinstance(inner, dict):
        return []
    entries = inner.get(key)
    return entries if isinstance(entries, list) else []


def _line(item: dict[str, Any], field: str) -> int:
    """Номер строки из определения; отсутствующее значение не участвует в упорядочении."""
    value = item.get(field)
    return value if isinstance(value, int) else 0


def describe_scope(documents: dict[str, Any], context: AnalyzerContext) -> dict[str, Any]:
    """Охват анализа кода: что покрыто, чем и с какой полнотой.

    Счётчики анализатора относятся к его области, а не ко всему проекту: без явного
    охвата потребитель прочитал бы их как характеристику всего репозитория. Значения,
    зависящие от условий выполнения, сюда не входят — они описывают прогон, а не проект.
    """
    scope: dict[str, Any] = {"analyzer": "tldr", "documents": sorted(documents)}

    reference = documents.get("structure") or documents.get("calls") or documents.get("health")
    if isinstance(reference, dict):
        root = relative_to_project(
            str(reference.get("root") or ""), str(context.root), str(reference.get("root") or "")
        )
        scope["root"] = root if root is not None else "."
        if reference.get("language"):
            scope["language"] = str(reference["language"])

    calls = documents.get("calls")
    if isinstance(calls, dict):
        scope["calls_truncated"] = bool(calls.get("truncated"))
        for field in ("total_edges", "shown_edges"):
            if calls.get(field) is not None:
                scope[field] = calls[field]

    health = documents.get("health")
    if isinstance(health, dict):
        details = health.get("details")
        if isinstance(details, dict):
            scope["sections"] = {
                name: bool(section.get("success"))
                for name, section in sorted(details.items())
                if isinstance(section, dict)
            }
    return scope


def load_documents(directory: Path | None) -> dict[str, Any]:
    """Прочитать документы анализа кода из каталога; отсутствующие пропускаются."""
    if directory is None or not directory.is_dir():
        return {}

    documents: dict[str, Any] = {}
    for key, name in DOCUMENT_NAMES.items():
        path = directory / name
        if not path.is_file():
            continue
        try:
            documents[key] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return documents
