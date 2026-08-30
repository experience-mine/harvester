"""Анализаторы: поставщики фактов для ядра.

Порядок регистрации значим: файловая система собирает файлы и каталоги, на которые
затем ссылается анализатор git, ставя связи ``MODIFIES`` только на собранные файлы.
Нормализация анализа кода идёт последней: она ссылается на уже собранные файлы
и отбрасывает факты, для которых файла в выгрузке нет.
"""

from __future__ import annotations

from agent.analyzers.code_analysis import CodeAnalysisAnalyzer
from agent.analyzers.filesystem import FilesystemAnalyzer
from agent.analyzers.vcs_git import GitAnalyzer
from agent.core import registry


def register_default_analyzers() -> None:
    """Зарегистрировать анализаторы первой версии в порядке их выполнения."""
    registry.clear()
    registry.register(FilesystemAnalyzer())
    registry.register(GitAnalyzer())
    registry.register(CodeAnalysisAnalyzer())


__all__ = [
    "CodeAnalysisAnalyzer",
    "FilesystemAnalyzer",
    "GitAnalyzer",
    "register_default_analyzers",
]
