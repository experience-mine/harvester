"""Приёмка шага 1: ядро, схема и командный интерфейс.

Скрипт выполняет проверки, по которым шаг 1 считается закрытым, и печатает отчёт.
Запуск: ``python tests/step1_acceptance.py``. Код возврата ``0`` — все проверки пройдены.
Внешних зависимостей нет; проверки, требующие файла конфигурации, пропускаются,
если в окружении нет PyYAML.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cli import EXIT_INVALID, EXIT_OK, EXIT_USAGE, main
from agent.core import registry
from agent.core.ids import (
    branch_id,
    commit_id,
    directory_id,
    evidence_id,
    file_id,
    person_id,
    repository_id,
    technology_id,
)
from agent.core.model import Evidence
from agent.core.validator import load_schema, validate_document

TIMESTAMP = "2026-08-24T09:15:42Z"
SHA = "9f3c1d2a7b48e5f0c6d1a2b3c4d5e6f708192a3b"

_results: list[tuple[bool, str, str]] = []


def check(condition: bool, title: str, detail: str = "") -> None:
    _results.append((bool(condition), title, detail))


def _evidence(subject_id: str, subject_type: str, source_kind: str, **extra: object) -> Evidence:
    return Evidence(
        id=evidence_id(
            subject_id, source_kind, extra.get("source_file_id"), extra.get("source_locator"), "probe"
        ),
        subject_id=subject_id,
        subject_type=subject_type,
        source_kind=source_kind,
        analyzer="probe",
        analyzer_version="1.0.0",
        collected_at=TIMESTAMP,
        **extra,
    )


class _Probe:
    """Фиктивный анализатор: проверяет контракт реестра на непустых данных."""

    name = "probe"
    version = "1.0.0"

    def analyze(self, context, collector) -> None:
        slug = "demo"
        repository, directory = repository_id(slug), directory_id(slug, "src")
        file_, commit = file_id(slug, "src/app.py"), commit_id(SHA)
        person, branch = person_id("m.orlova@acme.dev"), branch_id(slug, "main")

        git = lambda subject, kind: _evidence(subject, kind, "git", source_commit_id=commit)
        disk = lambda subject, kind: _evidence(
            subject, kind, "filesystem", source_file_id=file_, source_locator="src/app.py"
        )

        collector.add_entity(
            "repository",
            repository,
            {
                "name": slug,
                "path": ".",
                "default_branch": "main",
                "head_commit_id": commit,
                "is_dirty": False,
            },
            git(repository, "repository"),
        )
        collector.add_entity(
            "commit",
            commit,
            {
                "repository_id": repository,
                "sha": SHA,
                "short_sha": SHA[:7],
                "subject": "BG-214: почасовая тарификация",
                "message": "BG-214: почасовая тарификация\n\nтело сообщения",
                "authored_at": TIMESTAMP,
                "committed_at": TIMESTAMP,
                "author_id": person,
                "committer_id": person,
                "parent_ids": [],
                "is_merge": False,
                "changes": [
                    {"path": "src/app.py", "change_type": "added", "insertions": 84, "deletions": 0}
                ],
            },
            git(commit, "commit"),
        )
        collector.add_entity(
            "person",
            person,
            {
                "name": "Мария Орлова",
                "email": "m.orlova@acme.dev",
                "aliases": [{"name": "Мария Орлова", "email": "m.orlova@acme.dev"}],
            },
            git(person, "person"),
        )
        collector.add_entity(
            "branch",
            branch,
            {"repository_id": repository, "name": "main", "commit_id": commit, "is_default": True},
            git(branch, "branch"),
        )
        collector.add_entity(
            "directory",
            directory,
            {"repository_id": repository, "path": "src", "name": "src", "parent_id": repository},
            disk(directory, "directory"),
        )
        file_data = {
            "repository_id": repository,
            "path": "src/app.py",
            "name": "app.py",
            "extension": "py",
            "directory_id": directory,
            "size_bytes": 4213,
            "sha256": "3b" * 32,
            "is_binary": False,
            "role": "source",
        }
        collector.add_entity("file", file_, file_data, disk(file_, "file"))
        collector.add_entity(
            "file",
            file_,
            file_data,
            _evidence(file_, "file", "filesystem", source_file_id=file_, source_locator="повтор"),
        )
        collector.add_relationship("HAS_REPOSITORY", context.project_id, repository, git(repository, "repository"))
        collector.add_relationship("HAS_BRANCH", repository, branch, git(branch, "branch"))
        collector.add_relationship("POINTS_TO", branch, commit, git(commit, "commit"))
        collector.add_relationship("AUTHORED_BY", commit, person, git(person, "person"))
        collector.add_relationship("COMMITTED_BY", commit, person, git(person, "person"))
        collector.add_relationship("CONTAINS", repository, directory, disk(directory, "directory"))
        collector.add_relationship("CONTAINS", directory, file_, disk(file_, "file"))
        collector.add_relationship(
            "MODIFIES",
            commit,
            file_,
            git(file_, "file"),
            {"change_type": "added", "insertions": 84, "deletions": 0},
        )
        # Факт без подтверждения отбрасывается с предупреждением.
        collector.add_entity(
            "technology",
            technology_id("python"),
            {"name": "Python", "slug": "python", "category": "language", "confidence": "declared"},
            [],
        )


def _normalize(document: dict) -> dict:
    """Обнулить отметки времени: они меняются от прогона к прогону."""
    copied = copy.deepcopy(document)
    copied["generated_at"] = ""
    for record in copied["evidence"]:
        record["collected_at"] = ""
    return copied


def run_empty_project(workspace: Path) -> dict:
    """Группа «Структуры хранилища и схема»: прогон на пустом каталоге."""
    empty = workspace / "empty"
    empty.mkdir()
    output = workspace / "empty.json"

    code = main(["export", str(empty), "-o", str(output)])
    check(code == EXIT_OK, "export на пустом каталоге завершается кодом 0", f"код {code}")

    document = json.loads(output.read_text(encoding="utf-8"))
    schema = load_schema()
    expected_keys = set(schema["required"])
    check(set(document) == expected_keys, "в выгрузке все 18 корневых ключей", str(len(document)))
    check(document["schema_version"] == "1.0", "версия схемы равна 1.0", document["schema_version"])
    scalar_keys = {"schema_version", "generated_at", "filters", "project", "evidence"}
    check(
        all(document[key] == [] for key in expected_keys - scalar_keys),
        "массивы сущностей пусты на пустом каталоге",
    )
    check(
        document["filters"] == {"authors": [], "exclude": []},
        "выборка в выгрузке пуста, когда фильтры не заданы",
        str(document["filters"]),
    )
    check(
        main(["validate", str(output)]) == EXIT_OK, "validate на годном файле завершается кодом 0"
    )
    return document


def run_invalid_files(workspace: Path, document: dict) -> None:
    """Отказные сценарии команды validate."""
    cases: dict[str, dict] = {}

    without = copy.deepcopy(document)
    del without["project"]["evidence_ids"]
    cases["удалённый evidence_ids даёт код 2"] = without

    empty_evidence = copy.deepcopy(document)
    empty_evidence["project"]["evidence_ids"] = []
    cases["пустой evidence_ids даёт код 2"] = empty_evidence

    extra_key = copy.deepcopy(document)
    extra_key["code_entities"] = []
    cases["лишний корневой ключ даёт код 2"] = extra_key

    wrong_version = copy.deepcopy(document)
    wrong_version["schema_version"] = "2.0"
    cases["чужая версия схемы даёт код 2"] = wrong_version

    for index, (title, broken) in enumerate(cases.items()):
        path = workspace / f"broken-{index}.json"
        path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        check(main(["validate", str(path)]) == EXIT_INVALID, title)

    malformed = workspace / "malformed.json"
    malformed.write_text("{ не json", encoding="utf-8")
    check(main(["validate", str(malformed)]) == EXIT_INVALID, "нечитаемый JSON даёт код 2")


def run_analyzer_contract(workspace: Path) -> dict:
    """Контракт анализатора и правила целостности на непустых данных."""
    registry.clear()
    registry.register(_Probe())
    output = workspace / "probe.json"
    code = main(["export", str(workspace), "-o", str(output)])
    registry.clear()

    check(code == EXIT_OK, "export с анализатором завершается кодом 0", f"код {code}")
    document = json.loads(output.read_text(encoding="utf-8"))
    check(len(document["commits"]) == 1, "коммит собран", str(len(document["commits"])))
    check(len(document["files"]) == 1, "повторно добавленный файл не дублируется", str(len(document["files"])))
    check(
        len(document["files"][0]["evidence_ids"]) == 2,
        "повторный факт добавляет подтверждение",
        str(len(document["files"][0]["evidence_ids"])),
    )
    check(document["technologies"] == [], "факт без источника отброшен")
    check(len(document["relationships"]) == 8, "связи собраны", str(len(document["relationships"])))
    check(not validate_document(document, load_schema()), "выгрузка с данными проходит валидацию")
    return document


def run_integrity_rules(document: dict) -> None:
    """Каждое правило целостности перехватывает своё нарушение."""
    schema = load_schema()
    cases: dict[str, dict] = {}

    broken = copy.deepcopy(document)
    broken["commits"][0]["author_id"] = "person:unknown@example.com"
    cases["битая ссылка"] = broken

    broken = copy.deepcopy(document)
    broken["relationships"][0]["type"] = "MODIFIES"
    cases["недопустимые типы концов связи"] = broken

    broken = copy.deepcopy(document)
    broken["commits"][0]["is_merge"] = True
    cases["is_merge без родителей"] = broken

    broken = copy.deepcopy(document)
    broken["branches"][0]["is_default"] = False
    cases["нет ветки по умолчанию"] = broken

    broken = copy.deepcopy(document)
    broken["relationships"] = [item for item in broken["relationships"] if item["type"] != "CONTAINS"]
    cases["сущность недостижима из project"] = broken

    broken = copy.deepcopy(document)
    broken["relationships"].append({**broken["relationships"][0], "id": "relationship:" + "0" * 16})
    cases["дубль связи"] = broken

    broken = copy.deepcopy(document)
    broken["people"].append(copy.deepcopy(broken["people"][0]))
    cases["дубль идентификатора"] = broken

    broken = copy.deepcopy(document)
    record = next(item for item in broken["evidence"] if item["source_kind"] == "git")
    record["source_commit_id"] = None
    cases["подтверждение git без коммита"] = broken

    broken = copy.deepcopy(document)
    record = next(item for item in broken["evidence"] if item["subject_type"] != "file")
    record["subject_type"] = "file"
    cases["subject_type не совпадает с префиксом"] = broken

    for title, case in cases.items():
        errors = validate_document(case, schema)
        check(bool(errors), f"перехвачено: {title}", errors[0] if errors else "нарушение пропущено")


def run_reproducibility(workspace: Path) -> None:
    """Повторный прогон даёт ту же выгрузку с точностью до отметок времени."""
    # Анализируется отдельный каталог: выгрузки пишутся рядом, чтобы состав файлов
    # проекта не менялся между прогонами.
    source = workspace / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.py").write_text("print('привет')\n", encoding="utf-8")
    (source / "README.md").write_text("# Демонстрация\n", encoding="utf-8")

    first, second = workspace / "first.json", workspace / "second.json"
    main(["export", str(source), "-o", str(first)])
    main(["export", str(source), "-o", str(second)])
    left = _normalize(json.loads(first.read_text(encoding="utf-8")))
    right = _normalize(json.loads(second.read_text(encoding="utf-8")))
    check(left == right, "повторный прогон даёт ту же выгрузку")


def run_progress(workspace: Path) -> None:
    """Прогресс идёт в поток ошибок, а флаг --quiet его убирает."""
    source = workspace / "progress-source"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    output = workspace / "progress.json"

    stream = io.StringIO()
    with contextlib.redirect_stderr(stream):
        main(["export", str(source), "-o", str(output)])
    printed = stream.getvalue()
    check("обход рабочей копии" in printed, "прогресс называет этапы прогона")
    check("готово:" in printed and str(output) in printed, "итоговая строка называет путь выгрузки")

    stream = io.StringIO()
    with contextlib.redirect_stderr(stream):
        main(["export", str(source), "-o", str(output), "--quiet"])
    check(stream.getvalue() == "", "флаг --quiet убирает прогресс", repr(stream.getvalue()[:60]))

    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        main(["export", str(source), "-o", "-", "--quiet"])
    check(stream.getvalue().startswith("{"), "при --output - в стандартный вывод идёт только выгрузка")


def run_versioned_name(workspace: Path) -> None:
    """Версионированное имя: шаблон, каталог назначения и исключение прежних версий."""
    import re

    source = workspace / "versioned-source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    target = workspace / "versions"
    target.mkdir()
    check(main(["export", str(source), "-o", str(target), "--versioned", "--quiet"]) == EXIT_OK,
          "export с --versioned завершается кодом 0")
    produced = sorted(target.glob("Version*_export.json"))
    check(len(produced) == 1, "версионированный файл создан", str([item.name for item in produced]))
    check(
        bool(re.fullmatch(r"Version\d{14}_versioned-source_export\.json", produced[0].name)),
        "имя соответствует шаблону Version{ГГГГММДДЧЧММСС}_{проект}_export.json",
        produced[0].name,
    )
    check(main(["validate", str(produced[0])]) == EXIT_OK, "версионированная выгрузка валидна")

    # Прежние версии лежат внутри проекта и не должны попадать в следующую выгрузку.
    main(["export", str(source), "-o", str(source), "--versioned", "--quiet"])
    main(["export", str(source), "-o", str(source), "--versioned", "--quiet"])
    inside = sorted(source.glob("Version*_export.json"))
    latest = json.loads(inside[-1].read_text(encoding="utf-8"))
    check(
        all(not item["path"].startswith("Version") for item in latest["files"]),
        "прежние версии выгрузки не попадают в новую",
        str([item["path"] for item in latest["files"]]),
    )


def run_usage_errors(workspace: Path) -> None:
    """Ошибки запуска: недоступный каталог и неверная конфигурация."""
    check(
        main(["export", str(workspace / "missing"), "-o", str(workspace / "x.json")]) == EXIT_USAGE,
        "недоступный каталог даёт код 1",
    )

    try:
        import yaml  # noqa: F401
    except ImportError:
        check(True, "проверки конфигурации пропущены: PyYAML не установлен")
        return

    project = workspace / "configured"
    project.mkdir()
    (project / "agent.yml").write_text("project:\n  name: Демо\nunknown_key: 1\n", encoding="utf-8")
    check(
        main(["export", str(project), "-o", str(workspace / "x.json")]) == EXIT_USAGE,
        "неизвестный ключ конфигурации даёт код 1",
    )

    (project / "agent.yml").write_text(
        "project:\n  name: Демо проект\n  description: Проверка\nrequirements:\n  enabled: false\n",
        encoding="utf-8",
    )
    output = workspace / "configured.json"
    check(
        main(["export", str(project), "-o", str(output)]) == EXIT_OK,
        "корректная конфигурация принимается",
    )
    configured = json.loads(output.read_text(encoding="utf-8"))["project"]
    check(configured["name"] == "Демо проект", "имя проекта берётся из конфигурации", configured["name"])
    check(configured["slug"] == "demo-proekt", "slug транслитерирован", configured["slug"])


def main_script() -> int:
    with tempfile.TemporaryDirectory(prefix="step1-") as directory:
        workspace = Path(directory)
        document = run_empty_project(workspace)
        run_invalid_files(workspace, document)
        collected = run_analyzer_contract(workspace)
        run_integrity_rules(collected)
        run_reproducibility(workspace)
        run_progress(workspace)
        run_versioned_name(workspace)
        run_usage_errors(workspace)

    failed = 0
    for passed, title, detail in _results:
        mark = "ok  " if passed else "СБОЙ"
        suffix = f" — {detail}" if detail and not passed else ""
        print(f"{mark} {title}{suffix}")
        failed += 0 if passed else 1
    print(f"\nпроверок: {len(_results)}, сбоев: {failed}")
    return EXIT_OK if failed == 0 else EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main_script())
