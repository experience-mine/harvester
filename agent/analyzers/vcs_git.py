"""Анализатор git: репозиторий, ветки, теги, коммиты, участники и изменения файлов.

История читается установленным в системе исполняемым файлом ``git`` — тремя проходами
по одному перечню ссылок: метаданные коммитов, типы изменений и счётчики строк.
Собираются только локальные ветки и теги: ``refs/remotes`` не обходится.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent.analyzers.filesystem import is_utf8, safe
from agent.core.identity import IdentityMap
from agent.core.ids import branch_id, commit_id, evidence_id, file_id, person_id, tag_id
from agent.core.model import Evidence, FactCollector
from agent.core.registry import AnalyzerContext

RECORD = "\x1e"
FIELD = "\x1f"
DATE_FORMAT = "--date=format-local:%Y-%m-%dT%H:%M:%SZ"
META_FORMAT = (
    f"--pretty=format:{RECORD}%H{FIELD}%P{FIELD}%an{FIELD}%ae{FIELD}%ad"
    f"{FIELD}%cn{FIELD}%ce{FIELD}%cd{FIELD}%s{FIELD}%B"
)
CHANGE_TYPES = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied"}
DEFAULT_BRANCH_PREFERENCE = ("main", "master")


class GitAnalyzer:
    """Собирает Repository, Branch, Tag, Commit, Person и связи между ними."""

    name = "vcs.git"
    version = "1.1.0"

    def __init__(self) -> None:
        self._identities: IdentityMap | None = None

    def _resolved_by_map(self, person_identifier: str) -> bool:
        """Личность взята из карты тождества: её имя задано конфигурацией."""
        if not self._identities:
            return False
        email = person_identifier.split(":", 1)[1]
        return self._identities.resolve("", email) is not None

    def analyze(self, context: AnalyzerContext, collector: FactCollector) -> None:
        self._identities = getattr(context.config, "identities", None)
        self._identities = IdentityMap(self._identities) if self._identities else None
        if not context.has_repository:
            return
        if not _git_available():
            collector.warnings.append(
                "исполняемый файл git недоступен: история изменений не собрана"
            )
            return

        root = context.root
        context.progress.stage("чтение репозитория")
        head = _run(root, ["rev-parse", "HEAD"])
        head_commit = commit_id(head) if head else None
        references = _references(root)
        default_branch = _default_branch(root, references)

        repository = context.repository_id or ""
        repository_evidence = self._evidence(repository, "repository", context, commit=head)

        self._collect_commits(context, collector)
        context.progress.stage("чтение ссылок")
        self._collect_references(context, collector, references, default_branch)

        # Репозиторий создаётся после истории: при выборке по автору HEAD может не попасть
        # в выгрузку, и ссылка на него была бы битой.
        if head_commit and not collector.has_entity(head_commit):
            head_commit = None
        remote_url = _run(root, ["config", "--get", "remote.origin.url"])
        collector.add_entity(
            "repository",
            repository,
            {
                "name": context.repository_slug,
                "path": ".",
                **({"remote_url": remote_url} if remote_url else {}),
                "default_branch": default_branch,
                "head_commit_id": head_commit,
                "is_dirty": bool(_run(root, ["status", "--porcelain"])),
            },
            repository_evidence,
        )
        collector.add_relationship("HAS_REPOSITORY", context.project_id, repository, repository_evidence)
        context.progress.done(
            f"собрано: коммитов {len(collector.entities('commit'))},"
            f" веток {len(collector.entities('branch'))},"
            f" тегов {len(collector.entities('tag'))},"
            f" участников {len(collector.entities('person'))}"
        )

        if head and head_commit is None:
            collector.warnings.append(
                f"HEAD не попал в выгрузку и не записан в репозиторий: {head[:7]}"
            )

    def _collect_commits(self, context: AnalyzerContext, collector: FactCollector) -> None:
        root = context.root
        latest: dict[str, tuple[str, str, str]] = {}
        context.progress.stage("чтение истории: метаданные коммитов")
        authors = _author_flags(context)
        raw = _run(
            root,
            ["log", "--branches", "--tags", *authors, DATE_FORMAT, META_FORMAT],
            allow_failure=True,
        )
        if not raw:
            return

        context.progress.stage("чтение истории: типы изменений")
        statuses = _changes_by_commit(root, ["--name-status", "-M", *authors], _parse_status_line)
        context.progress.stage("чтение истории: счётчики строк")
        numbers = _changes_by_commit(root, ["--numstat", "-M", *authors], _parse_numstat_line)

        records = [item for item in raw.split(RECORD) if item.strip()]
        selected = {
            commit_id(item.split(FIELD)[0]) for item in records if item.split(FIELD)[0].strip()
        }
        context.progress.stage("разбор коммитов")
        for number, record in enumerate(records, start=1):
            context.progress.tick(number, len(records), "коммитов")
            if not record.strip():
                continue
            fields = record.split(FIELD)
            if len(fields) < 10:
                collector.warnings.append(f"запись истории не разобрана: {fields[0][:40]}")
                continue
            sha, parents, author_name, author_email, authored_at = fields[:5]
            committer_name, committer_email, committed_at, subject, message = fields[5:10]

            identifier = commit_id(sha)
            evidence = self._evidence(identifier, "commit", context, commit=sha)
            author = self._person(author_name, author_email, context, collector, sha)
            committer = self._person(committer_name, committer_email, context, collector, sha)
            _remember_latest(latest, author, authored_at, sha, author_name)
            _remember_latest(latest, committer, committed_at, sha, committer_name)
            all_parents = [commit_id(item) for item in parents.split() if item]
            parent_ids = [item for item in all_parents if item in selected]
            changes, skipped = _merge_changes(statuses.get(sha, {}), numbers.get(sha, {}))
            for path in skipped:
                collector.warnings.append(
                    f"имя файла вне UTF-8, изменение пропущено в коммите {sha[:7]}: {safe(path)}"
                )

            collector.add_entity(
                "commit",
                identifier,
                {
                    "repository_id": context.repository_id,
                    "sha": sha,
                    "short_sha": sha[:7],
                    "subject": subject,
                    "message": message.rstrip("\n"),
                    "authored_at": authored_at,
                    "committed_at": committed_at,
                    "author_id": author,
                    "committer_id": committer,
                    "parent_ids": parent_ids,
                    # Признак слияния берётся из самой истории, а не из усечённого перечня:
                    # при фильтре по автору часть родителей может не попасть в выборку.
                    "is_merge": len(all_parents) >= 2,
                    "changes": changes,
                },
                evidence,
            )
            collector.add_relationship("HAS_COMMIT", context.repository_id, identifier, evidence)
            collector.add_relationship("AUTHORED_BY", identifier, author, evidence)
            collector.add_relationship("COMMITTED_BY", identifier, committer, evidence)
            for parent in parent_ids:
                collector.add_relationship("PARENT_OF", identifier, parent, evidence)
            for change in changes:
                target = file_id(context.repository_slug, change["path"])
                if not collector.has_entity(target):
                    # Файл удалён либо отфильтрован .gitignore: сущности File нет, связь не ставится.
                    continue
                collector.add_relationship(
                    "MODIFIES",
                    identifier,
                    target,
                    evidence,
                    {
                        "change_type": change["change_type"],
                        "insertions": change["insertions"],
                        "deletions": change["deletions"],
                    },
                )

        for person, (_, _, name) in latest.items():
            entity = next((item for item in collector.entities("person") if item.id == person), None)
            # Имя из карты тождества каноническое и самым поздним коммитом не перекрывается.
            if entity is not None and not self._resolved_by_map(entity.id):
                entity.data["name"] = name

    def _collect_references(
        self,
        context: AnalyzerContext,
        collector: FactCollector,
        references: list[tuple[str, str, str, str, str]],
        default_branch: str,
    ) -> None:
        for kind, name, object_name, peeled, subject in references:
            sha = peeled or object_name
            target = commit_id(sha)
            if not collector.has_entity(target):
                collector.warnings.append(f"ссылка {name} указывает на несобранный коммит {sha}")
                continue
            if kind == "branch":
                identifier = branch_id(context.repository_slug, name)
                evidence = self._evidence(identifier, "branch", context, commit=sha)
                collector.add_entity(
                    "branch",
                    identifier,
                    {
                        "repository_id": context.repository_id,
                        "name": name,
                        "commit_id": target,
                        "is_default": name == default_branch,
                    },
                    evidence,
                )
                collector.add_relationship("HAS_BRANCH", context.repository_id, identifier, evidence)
            else:
                identifier = tag_id(context.repository_slug, name)
                evidence = self._evidence(identifier, "tag", context, commit=sha)
                data = {
                    "repository_id": context.repository_id,
                    "name": name,
                    "commit_id": target,
                }
                if peeled and subject:
                    data["message"] = subject
                collector.add_entity("tag", identifier, data, evidence)
                collector.add_relationship("HAS_TAG", context.repository_id, identifier, evidence)
            collector.add_relationship("POINTS_TO", identifier, target, evidence)

    def _person(
        self,
        name: str,
        email: str,
        context: AnalyzerContext,
        collector: FactCollector,
        sha: str,
    ) -> str:
        """Свести участника по карте тождества, иначе по адресу в нижнем регистре."""
        identity = self._identities.resolve(name, email) if self._identities else None
        canonical_email = identity.primary_email if identity else email
        canonical_name = identity.name if identity else name
        identifier = person_id(canonical_email, canonical_name)
        evidence = self._evidence(identifier, "person", context, commit=sha)
        existing = next(
            (item for item in collector.entities("person") if item.id == identifier), None
        )
        alias = {"name": name, "email": email}
        if existing is None:
            collector.add_entity(
                "person",
                identifier,
                {"name": canonical_name, "email": canonical_email.lower(), "aliases": [alias]},
                evidence,
            )
        else:
            if alias not in existing.data["aliases"]:
                existing.data["aliases"].append(alias)
            collector.add_entity("person", identifier, {}, evidence)
        return identifier

    def _evidence(
        self,
        subject_id: str,
        subject_type: str,
        context: AnalyzerContext,
        commit: str | None,
    ) -> Evidence:
        locator = commit or "HEAD"
        return Evidence(
            id=evidence_id(subject_id, "git", None, locator, self.name),
            subject_id=subject_id,
            subject_type=subject_type,
            source_kind="git",
            analyzer=self.name,
            analyzer_version=self.version,
            collected_at=context.collected_at,
            source_commit_id=commit_id(commit) if commit else None,
            source_locator=locator,
        )


def _author_flags(context: AnalyzerContext) -> list[str]:
    """Флаги отбора по автору: совпадение подстрокой по имени либо адресу, регистр не важен."""
    authors = list(getattr(context.config, "authors", []) or [])
    if not authors:
        return []
    identities = getattr(context.config, "identities", None)
    if identities:
        mapping = IdentityMap(identities)
        expanded: list[str] = []
        for item in authors:
            for value in mapping.expand(item):
                if value not in expanded:
                    expanded.append(value)
        authors = expanded
    return ["--regexp-ignore-case", *[f"--author={item}" for item in authors]]


def _remember_latest(
    latest: dict[str, tuple[str, str, str]], person: str, moment: str, sha: str, name: str
) -> None:
    """Запомнить самое позднее написание имени участника.

    Сравнение идёт по дате коммита; при совпадении дат — по ``sha``, иначе выбор зависел бы
    от порядка обхода истории и менялся бы между прогонами.
    """
    current = latest.get(person)
    if current is None or (moment, sha) > (current[0], current[1]):
        latest[person] = (moment, sha, name)


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=False)
    except OSError:
        return False
    return True


def _run(root: Path, arguments: list[str], allow_failure: bool = True) -> str:
    """Выполнить команду git в каталоге репозитория; при отказе вернуть пустую строку."""
    environment = {"TZ": "UTC", "PATH": _path(), "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1"}
    completed = subprocess.run(
        # quotePath=false: пути в выводе git остаются как есть, без octal-escape.
        ["git", "-C", str(root), "-c", "core.quotePath=false", *arguments],
        capture_output=True,
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        if allow_failure:
            return ""
        raise RuntimeError(completed.stderr.decode("utf-8", "replace").strip())
    # surrogateescape: имена файлов вне UTF-8 встречаются в истории и не должны ломать разбор.
    return completed.stdout.decode("utf-8", "surrogateescape").strip("\n")


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")


def _references(root: Path) -> list[tuple[str, str, str, str, str]]:
    """Локальные ветки и теги: вид, имя, объект, разыменованный коммит, тема аннотации."""
    raw = _run(
        root,
        [
            "for-each-ref",
            "refs/heads",
            "refs/tags",
            f"--format=%(refname)%(if)%(HEAD)%(then)%(end){FIELD}%(refname:short)"
            f"{FIELD}%(objectname){FIELD}%(*objectname){FIELD}%(contents:subject)",
        ],
    )
    references: list[tuple[str, str, str, str, str]] = []
    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split(FIELD)
        if len(parts) != 5:
            continue
        full, name, object_name, peeled, subject = parts
        kind = "branch" if full.startswith("refs/heads/") else "tag"
        references.append((kind, name, object_name, peeled, subject))
    return references


def _default_branch(root: Path, references: list[tuple[str, str, str, str, str]]) -> str:
    """Ветка по умолчанию: текущая, иначе main или master, иначе первая по алфавиту."""
    current = _run(root, ["symbolic-ref", "--short", "HEAD"])
    names = sorted(name for kind, name, *_ in references if kind == "branch")
    if current and current in names:
        return current
    for preferred in DEFAULT_BRANCH_PREFERENCE:
        if preferred in names:
            return preferred
    return names[0] if names else ""


def _changes_by_commit(root: Path, flags: list[str], parse) -> dict[str, dict[str, dict]]:
    """Изменения по коммитам: ``{sha: {путь: поля}}`` из одного прохода по истории."""
    raw = _run(
        root,
        ["log", "--branches", "--tags", "-m", "--first-parent", *flags, f"--pretty=format:{RECORD}%H"],
    )
    result: dict[str, dict[str, dict]] = {}
    for record in raw.split(RECORD):
        lines = [line for line in record.splitlines() if line.strip()]
        if not lines:
            continue
        sha, entries = lines[0].strip(), result.setdefault(lines[0].strip(), {})
        for line in lines[1:]:
            parsed = parse(line)
            if parsed is not None:
                path, values = parsed
                entries.setdefault(path, {}).update(values)
        result[sha] = entries
    return result


def _parse_status_line(line: str) -> tuple[str, dict] | None:
    parts = line.split("\t")
    if len(parts) < 2:
        return None
    code = parts[0][:1]
    change_type = CHANGE_TYPES.get(code)
    if change_type is None:
        return None
    if change_type in {"renamed", "copied"} and len(parts) >= 3:
        return parts[2], {"change_type": change_type, "previous_path": parts[1]}
    return parts[1], {"change_type": change_type}


def _parse_numstat_line(line: str) -> tuple[str, dict] | None:
    parts = line.split("\t")
    if len(parts) != 3:
        return None
    insertions, deletions, path = parts
    return _expand_path(path), {
        "insertions": 0 if insertions == "-" else int(insertions),
        "deletions": 0 if deletions == "-" else int(deletions),
    }


def _expand_path(path: str) -> str:
    """Развернуть путь переименования: ``docs/{a.md => b.md}`` и ``a.md => b.md``."""
    if "{" in path and " => " in path:
        head, rest = path.split("{", 1)
        inner, tail = rest.split("}", 1)
        _, new = inner.split(" => ", 1)
        return f"{head}{new}{tail}".replace("//", "/")
    if " => " in path:
        return path.split(" => ", 1)[1]
    return path


def _merge_changes(
    statuses: dict[str, dict], numbers: dict[str, dict]
) -> tuple[list[dict], list[str]]:
    """Соединить типы изменений и счётчики строк по пути файла.

    Возвращает изменения и перечень путей, пропущенных из-за имени вне UTF-8.
    """
    changes: list[dict] = []
    skipped: list[str] = []
    for path in sorted(set(statuses) | set(numbers)):
        if not is_utf8(path):
            skipped.append(path)
            continue
        status = statuses.get(path, {})
        counters = numbers.get(path, {})
        change = {
            "path": path,
            "change_type": status.get("change_type", "modified"),
            "insertions": counters.get("insertions", 0),
            "deletions": counters.get("deletions", 0),
        }
        if "previous_path" in status and is_utf8(status["previous_path"]):
            change["previous_path"] = status["previous_path"]
        changes.append(change)
    return changes, skipped
