"""Анализаторы: поставщики фактов для ядра.

Порядок регистрации значим: файловая система собирает файлы и каталоги, на которые
затем ссылается анализатор git, ставя связи ``MODIFIES`` только на собранные файлы.
"""

from __future__ import annotations

from agent.analyzers.filesystem import FilesystemAnalyzer
from agent.analyzers.vcs_git import GitAnalyzer
from agent.core import registry


def register_default_analyzers() -> None:
    """Зарегистрировать анализаторы первой версии в порядке их выполнения."""
    registry.clear()
    registry.register(FilesystemAnalyzer())
    registry.register(GitAnalyzer())


__all__ = ["FilesystemAnalyzer", "GitAnalyzer", "register_default_analyzers"]
