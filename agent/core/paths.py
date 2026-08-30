"""Приведение путей внешнего анализатора кода к идентификаторам файлов выгрузки.

Анализатор кода адресует файлы иначе, чем выгрузка: относительно собственного корня
анализа либо абсолютным путём внутри контейнера, тогда как выгрузка адресует их путём
от корня репозитория. Модуль сводит обе формы к одному идентификатору.

Работа ведётся над строками: файловая система не опрашивается, поэтому результат
не зависит от того, где выполняется приведение — при прогоне или при разборе выгрузки.
"""

from __future__ import annotations

import posixpath

from agent.core.ids import file_id

#: Части пути, выводящие за пределы корня проекта.
_OUTSIDE = ".."


def relative_to_project(analysis_root: str, project_root: str, path: str) -> str | None:
    """Путь от корня проекта либо ``None``, если он выводит за его пределы.

    ``analysis_root`` — корень области анализа, ``project_root`` — корень проекта;
    ``path`` понимается как абсолютный, если начинается с разделителя, иначе как
    относительный к корню области анализа.
    """
    if not path or not path.strip():
        return None

    normalized = posixpath.normpath(path.strip())
    if not posixpath.isabs(normalized):
        normalized = posixpath.normpath(posixpath.join(_clean(analysis_root), normalized))

    project = _clean(project_root)
    relative = posixpath.relpath(normalized, project)
    if relative == "." or relative.split("/", 1)[0] == _OUTSIDE:
        return None
    return relative


def analysis_file_id(
    repository_slug: str, analysis_root: str, project_root: str, path: str
) -> str | None:
    """Идентификатор файла выгрузки для ссылки анализатора либо ``None`` при промахе."""
    relative = relative_to_project(analysis_root, project_root, path)
    if relative is None:
        return None
    return file_id(repository_slug, relative)


def _clean(root: str) -> str:
    """Корень в виде абсолютного нормализованного пути."""
    value = (root or "").strip() or "/"
    if not posixpath.isabs(value):
        value = "/" + value
    return posixpath.normpath(value)
