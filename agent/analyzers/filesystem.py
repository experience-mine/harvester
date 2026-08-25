"""Анализатор файловой системы: каталоги и файлы рабочей копии.

Перечень путей берётся у git, когда репозиторий есть: команда ``git ls-files`` применяет
``.gitignore`` теми же правилами, что и сам git, поэтому агент не повторяет их разбор.
Без репозитория обходится дерево каталогов, а фильтрация выполняется только по списку
``exclude`` из конфигурации.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Iterable

from agent.core.ids import directory_id, evidence_id, file_id
from agent.core.model import Evidence, FactCollector
from agent.core.registry import AnalyzerContext

ROLES_PATH = Path(__file__).resolve().parent.parent / "data" / "file-roles.json"
DEFAULT_ROLE = "other"
BINARY_PROBE_BYTES = 8192
HASH_CHUNK_BYTES = 1 << 20


class FilesystemAnalyzer:
    """Собирает Directory и File со связями ``CONTAINS``."""

    name = "filesystem"
    version = "1.0.0"

    def __init__(self, roles_path: Path | None = None) -> None:
        self._rules = _load_roles(roles_path or ROLES_PATH)

    def analyze(self, context: AnalyzerContext, collector: FactCollector) -> None:
        context.progress.stage("обход рабочей копии")
        paths = []
        for path in sorted(self._collect_paths(context)):
            if not is_utf8(path):
                # Имя файла не является корректным UTF-8: такой путь нельзя записать в выгрузку,
                # поэтому файл пропускается, а не ломает прогон.
                collector.warnings.append(f"имя файла вне UTF-8, файл пропущен: {safe(path)}")
                continue
            paths.append(path)
        context.paths = paths
        known_directories: set[str] = set()
        context.progress.done(f"найдено файлов: {len(paths)}")
        context.progress.stage("чтение файлов")

        for number, path in enumerate(paths, start=1):
            context.progress.tick(number, len(paths), "файлов")
            target = context.root / path
            try:
                size = target.stat().st_size
                digest, is_binary = _read_file(target)
            except OSError as error:
                collector.warnings.append(f"файл не прочитан и пропущен: {path}: {error}")
                continue

            directory = self._ensure_directories(path, context, collector, known_directories)
            identifier = file_id(context.repository_slug, path)
            evidence = self._evidence(identifier, "file", path, context)
            collector.add_entity(
                "file",
                identifier,
                {
                    "repository_id": context.repository_id,
                    "path": path,
                    "name": Path(path).name,
                    "extension": Path(path).suffix.lstrip(".").lower(),
                    "directory_id": directory,
                    "size_bytes": size,
                    "sha256": digest,
                    "is_binary": is_binary,
                    "role": self._role(path),
                },
                evidence,
            )
            collector.add_relationship("CONTAINS", directory, identifier, evidence)

        context.progress.done(f"файлов собрано: {len(collector.entities('file'))}")

    def _collect_paths(self, context: AnalyzerContext) -> Iterable[str]:
        """Пути файлов от корня рабочей копии, уже отфильтрованные."""
        excluded = tuple(context.config.exclude)
        own = context.excluded_paths

        def keep(path: str) -> bool:
            # Собственные файлы прогона — выгрузка и кэш — в выгрузку не попадают:
            # иначе повторный прогон видел бы результат предыдущего и не был бы воспроизводим.
            if path in own or any(path.startswith(f"{item}/") for item in own):
                return False
            return not _is_excluded(path, excluded)

        if context.has_repository:
            listed = _git_listed_paths(context.root)
            if listed is not None:
                return (path for path in listed if keep(path))
        return (path for path in _walk(context.root) if keep(path))

    def _ensure_directories(
        self,
        path: str,
        context: AnalyzerContext,
        collector: FactCollector,
        known: set[str],
    ) -> str:
        """Создать цепочку каталогов до файла и вернуть идентификатор ближайшего."""
        parts = Path(path).parent.parts
        parent = context.repository_id if context.has_repository else context.project_id
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            identifier = directory_id(context.repository_slug, current)
            if identifier not in known:
                evidence = self._evidence(identifier, "directory", current, context)
                collector.add_entity(
                    "directory",
                    identifier,
                    {
                        "repository_id": context.repository_id,
                        "path": current,
                        "name": part,
                        "parent_id": parent,
                    },
                    evidence,
                )
                collector.add_relationship("CONTAINS", parent, identifier, evidence)
                known.add(identifier)
            parent = identifier

        if not parts:
            # Файл лежит в корне: каталогом-владельцем становится корневой каталог рабочей копии.
            identifier = directory_id(context.repository_slug, ".")
            if identifier not in known:
                owner = context.repository_id if context.has_repository else context.project_id
                evidence = self._evidence(identifier, "directory", ".", context)
                collector.add_entity(
                    "directory",
                    identifier,
                    {
                        "repository_id": context.repository_id,
                        "path": ".",
                        "name": context.root.name,
                        "parent_id": owner,
                    },
                    evidence,
                )
                collector.add_relationship("CONTAINS", owner, identifier, evidence)
                known.add(identifier)
            return identifier
        return parent

    def _role(self, path: str) -> str:
        name = Path(path).name
        for role, patterns in self._rules:
            for pattern in patterns:
                target = path if "/" in pattern else name
                if fnmatch.fnmatchcase(target, pattern):
                    return role
        return DEFAULT_ROLE

    def _evidence(self, subject_id: str, subject_type: str, locator: str, context: AnalyzerContext) -> Evidence:
        source_file = subject_id if subject_type == "file" else None
        return Evidence(
            id=evidence_id(subject_id, "filesystem", source_file, locator, self.name),
            subject_id=subject_id,
            subject_type=subject_type,
            source_kind="filesystem",
            analyzer=self.name,
            analyzer_version=self.version,
            collected_at=context.collected_at,
            source_file_id=source_file,
            source_locator=locator,
        )


def is_utf8(value: str) -> bool:
    """Можно ли записать строку в выгрузку: имена вне UTF-8 несут суррогатные пары."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def safe(value: str) -> str:
    """Строка, пригодная для вывода в журнал прогона: суррогаты заменяются."""
    return value.encode("utf-8", "replace").decode("utf-8")


def _load_roles(path: Path) -> list[tuple[str, list[str]]]:
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    return [(rule["role"], list(rule["patterns"])) for rule in document["rules"]]


def _git_listed_paths(root: Path) -> list[str] | None:
    """Пути, которые git считает частью рабочей копии; ``None`` — команда недоступна."""
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                # Без quotePath=false git экранирует не-ASCII пути в кавычках с octal-escape,
                # и такой путь не открывается на файловой системе.
                "-c",
                "core.quotePath=false",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    # surrogateescape: имена вне UTF-8 не ломают декодирование и остаются пригодными
    # для обращения к файловой системе.
    listed = completed.stdout.decode("utf-8", "surrogateescape").splitlines()
    return [line for line in listed if line]


def _walk(root: Path) -> Iterable[str]:
    """Обход дерева каталогов; каталог .git не обходится ни при каких настройках."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name == ".git":
                    continue
                stack.append(entry)
            elif entry.is_file():
                yield entry.relative_to(root).as_posix()


def _is_excluded(path: str, patterns: tuple[str, ...]) -> bool:
    name = Path(path).name
    for pattern in patterns:
        if pattern.endswith("/"):
            prefix = pattern.rstrip("/")
            if path == prefix or path.startswith(f"{prefix}/") or f"/{prefix}/" in f"/{path}":
                return True
        elif fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def _read_file(path: Path) -> tuple[str, bool]:
    """Вернуть sha256 содержимого и признак бинарного файла."""
    digest = hashlib.sha256()
    is_binary = False
    with path.open("rb") as handle:
        first = handle.read(BINARY_PROBE_BYTES)
        is_binary = b"\x00" in first
        digest.update(first)
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest(), is_binary
