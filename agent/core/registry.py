"""Контракт анализатора и его реестр.

Ядро не знает ни одной технологии: анализатор получает корень рабочей копии,
перечень путей и конфигурацию, а факты кладёт в общий накопитель. Новый источник
данных добавляется реализацией контракта и регистрацией, без правки ядра.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agent.core.model import FactCollector
from agent.core.progress import Progress


@dataclass
class AnalyzerContext:
    """Вход анализатора: корень рабочей копии, конфигурация и сведения о репозитории.

    Анализаторы не обращаются друг к другу: общие сведения — идентификатор репозитория
    и признак его наличия — вычисляет ядро и кладёт сюда.
    """

    root: Path
    config: Any
    project_id: str
    repository_slug: str
    collected_at: str
    has_repository: bool = False
    repository_id: str | None = None
    paths: list[str] = field(default_factory=list)
    excluded_paths: set[str] = field(default_factory=set)
    progress: Progress = field(default_factory=lambda: Progress(enabled=False))


@runtime_checkable
class Analyzer(Protocol):
    """Контракт анализатора: имя, версия и один метод сбора."""

    name: str
    version: str

    def analyze(self, context: AnalyzerContext, collector: FactCollector) -> None:
        """Собрать факты из своего вида источника и положить их в накопитель."""


_REGISTRY: list[Analyzer] = []


def register(analyzer: Analyzer) -> Analyzer:
    """Зарегистрировать анализатор; повторная регистрация того же имени запрещена."""
    if any(existing.name == analyzer.name for existing in _REGISTRY):
        raise ValueError(f"анализатор уже зарегистрирован: {analyzer.name}")
    _REGISTRY.append(analyzer)
    return analyzer


def analyzers() -> list[Analyzer]:
    """Зарегистрированные анализаторы в порядке регистрации."""
    return list(_REGISTRY)


def clear() -> None:
    """Очистить реестр; используется проверками."""
    _REGISTRY.clear()
