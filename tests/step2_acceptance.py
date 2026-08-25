"""Приёмка шага 2: история изменений и рабочая копия.

Скрипт создаёт временный git-репозиторий с известным содержанием, прогоняет ``export``
и сверяет выгрузку с самим git. Запуск: ``python tests/step2_acceptance.py``.
Код возврата ``0`` — все проверки пройдены; при отсутствии git проверки пропускаются.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cli import EXIT_INVALID, EXIT_OK, main
from agent.core.validator import load_schema, validate_document

_results: list[tuple[bool, str, str]] = []


def is_writable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def check(condition: bool, title: str, detail: str = "") -> None:
    _results.append((bool(condition), title, detail))


_moment = 0


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=False
    )
    return completed.stdout.strip()


def commit(root: Path, *arguments: str) -> str:
    """Коммит с фиксированной датой: даты возрастают, история детерминирована."""
    global _moment
    _moment += 60
    moment = f"2026-07-14T{10 + _moment // 3600:02d}:{_moment % 3600 // 60:02d}:00+00:00"
    completed = subprocess.run(
        ["git", "-C", str(root), "commit", "-q", *arguments],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": moment,
            "GIT_COMMITTER_DATE": moment,
        },
    )
    return completed.stdout.strip()


def merge(root: Path, branch: str, message: str) -> None:
    """Слияние с фиксированной датой."""
    global _moment
    _moment += 60
    moment = f"2026-07-14T{10 + _moment // 3600:02d}:{_moment % 3600 // 60:02d}:00+00:00"
    subprocess.run(
        ["git", "-C", str(root), "merge", "-q", "--no-ff", branch, "-m", message],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_AUTHOR_DATE": moment, "GIT_COMMITTER_DATE": moment},
    )


def tracked_utf8_files(root: Path) -> list[bytes]:
    """Отслеживаемые git файлы, имя которых является корректным UTF-8."""
    completed = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotePath=false", "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    listed = [item for item in completed.stdout.split(b"\x00") if item]
    result = []
    for item in listed:
        try:
            item.decode("utf-8")
        except UnicodeDecodeError:
            continue
        result.append(item)
    return result


def build_repository(root: Path) -> None:
    """Репозиторий с известной историей: переименование, слияние, тег, игнорируемые файлы."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main", ".")
    git(root, "config", "user.email", "m.orlova@acme.dev")
    git(root, "config", "user.name", "Мария Орлова")

    (root / "src").mkdir()
    (root / "docs").mkdir()
    (root / "migrations").mkdir()
    (root / "src" / "app.py").write_text("print('привет')\n", encoding="utf-8")
    (root / "docs" / "readme.md").write_text("# Демонстрация\n", encoding="utf-8")
    (root / "migrations" / "0001_init.sql").write_text("create table orders ();\n", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (root / ".gitignore").write_text(".env\n*.log\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=секрет\n", encoding="utf-8")
    (root / "run.log").write_text("журнал\n", encoding="utf-8")
    git(root, "add", "-A")
    commit(root, "-m", "DEMO-001: первый коммит\n\nтело сообщения")

    git(root, "mv", "docs/readme.md", "docs/README.md")
    commit(root, "-m", "DEMO-002: переименование документа")

    git(root, "tag", "-a", "v1.0.0", "-m", "релиз 1.0.0")

    git(root, "checkout", "-q", "-b", "feature")
    (root / "src" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", "-A")
    commit(root, "-m", "DEMO-003: ветка с новым модулем")
    git(root, "checkout", "-q", "main")
    merge(root, "feature", "DEMO-004: слияние feature")

    git(root, "config", "user.name", "Maria Orlova")
    (root / "src" / "app.py").write_text("print('привет, мир')\n", encoding="utf-8")
    commit(root, "-am", "DEMO-005: второе написание имени участника")

    # Не-ASCII имена и расширение с необычным символом: git экранирует такие пути,
    # а строгий шаблон расширения отвергал резервные копии вида index.php~.
    (root / "шаблоны").mkdir()
    (root / "шаблоны" / "Блок на 4 ссылки.snp").write_text("<блок>\n", encoding="utf-8")
    (root / "index.php~").write_text("<?php // резервная копия\n", encoding="utf-8")
    git(root, "add", "-A")
    commit(root, "-m", "DEMO-006: файлы с не-ASCII именами")

    # Имя вне UTF-8 (cp1251): такой файл нельзя записать в выгрузку, он пропускается.
    with open(bytes(root) + b"/\xd0\xf0\xe0\xe9\xf1.php", "wb") as handle:
        handle.write(b"<?php // cp1251 name\n")
    git(root, "add", "-A")
    commit(root, "-m", "DEMO-007: имя файла вне UTF-8")

    (root / "src" / "obsolete.py").write_text("OLD = True\n", encoding="utf-8")
    git(root, "add", "-A")
    commit(root, "-m", "DEMO-008: временный модуль")
    git(root, "rm", "-q", "src/obsolete.py")
    commit(root, "-m", "DEMO-009: удаление временного модуля")


def run_repository(repository: Path, output: Path) -> dict:
    code = main(["export", str(repository), "-o", str(output)])
    check(code == EXIT_OK, "export на репозитории завершается кодом 0", f"код {code}")
    document = json.loads(output.read_text(encoding="utf-8"))
    check(main(["validate", str(output)]) == EXIT_OK, "validate на выгрузке завершается кодом 0")
    check(not validate_document(document, load_schema()), "выгрузка проходит правила целостности")
    return document


def check_against_git(document: dict, repository: Path) -> None:
    """Сверка с самим git: числа берутся из источника, а не из ожиданий теста."""
    commits = int(git(repository, "rev-list", "--branches", "--tags", "--count"))
    branches = len(git(repository, "for-each-ref", "refs/heads").splitlines())
    tags = len(git(repository, "for-each-ref", "refs/tags").splitlines())
    files = len(tracked_utf8_files(repository))

    check(len(document["commits"]) == commits, "число коммитов совпадает с git", f"{len(document['commits'])} против {commits}")
    check(len(document["branches"]) == branches, "число веток совпадает с git", f"{len(document['branches'])} против {branches}")
    check(len(document["tags"]) == tags, "число тегов совпадает с git", f"{len(document['tags'])} против {tags}")
    check(
        len(document["files"]) == files,
        "число файлов совпадает с git (без имён вне UTF-8)",
        f"{len(document['files'])} против {files}",
    )
    check(len(document["repositories"]) == 1, "репозиторий собран")
    check(document["repositories"][0]["default_branch"] == "main", "ветка по умолчанию определена")
    check(
        document["repositories"][0]["head_commit_id"] == f"commit:{git(repository, 'rev-parse', 'HEAD')}",
        "HEAD совпадает с git",
    )
    check(
        sum(1 for item in document["branches"] if item["is_default"]) == 1,
        "ровно одна ветка помечена как ветка по умолчанию",
    )


def check_history(document: dict) -> None:
    """Разбор истории: переименование, слияние, участники, изменения файлов."""
    by_subject = {item["subject"]: item for item in document["commits"]}

    renamed = by_subject["DEMO-002: переименование документа"]["changes"]
    check(
        any(change["change_type"] == "renamed" and change.get("previous_path") == "docs/readme.md" for change in renamed),
        "переименование распознано с previous_path",
        json.dumps(renamed, ensure_ascii=False),
    )

    merge = by_subject["DEMO-004: слияние feature"]
    check(merge["is_merge"] and len(merge["parent_ids"]) == 2, "слияние помечено и имеет два родителя")
    check(
        len(merge["changes"]) >= 1,
        "изменения слияния посчитаны относительно первого родителя",
        str(len(merge["changes"])),
    )

    first = by_subject["DEMO-001: первый коммит"]
    check(first["parent_ids"] == [], "у корневого коммита нет родителей")
    check("тело сообщения" in first["message"], "тело сообщения сохранено целиком")
    check(first["message"].startswith(first["subject"]), "тема — первая строка сообщения")
    check(
        first["authored_at"].endswith("Z") and len(first["authored_at"]) == 20,
        "дата приведена к UTC по ISO 8601",
        first["authored_at"],
    )

    check(len(document["people"]) == 1, "участник сведён по адресу почты", str(len(document["people"])))
    person = document["people"][0]
    check(person["name"] == "Maria Orlova", "имя взято из самого позднего коммита", person["name"])
    check(
        {alias["name"] for alias in person["aliases"]} == {"Мария Орлова", "Maria Orlova"},
        "оба написания имени сохранены",
        json.dumps(person["aliases"], ensure_ascii=False),
    )

    modifies = [item for item in document["relationships"] if item["type"] == "MODIFIES"]
    check(bool(modifies), "связи MODIFIES построены")
    check(
        all(not item["to_id"].endswith("src/obsolete.py") for item in modifies),
        "удалённый файл не получает связь MODIFIES",
    )
    check(
        any(item["attributes"].get("insertions", 0) > 0 for item in modifies),
        "счётчики строк заполнены",
    )


def check_working_copy(document: dict) -> None:
    """Рабочая копия: фильтрация, роли, хэши, каталоги."""
    paths = {item["path"] for item in document["files"]}
    check(".env" not in paths and "run.log" not in paths, "игнорируемые файлы не собраны")
    check("src/app.py" in paths, "файлы рабочей копии собраны")

    roles = {item["path"]: item["role"] for item in document["files"]}
    check(roles.get("pyproject.toml") == "manifest", "манифест распознан", roles.get("pyproject.toml", ""))
    check(roles.get("docs/README.md") == "documentation", "документ распознан", roles.get("docs/README.md", ""))
    check(roles.get("migrations/0001_init.sql") == "migration", "миграция распознана", roles.get("migrations/0001_init.sql", ""))
    check(roles.get("src/app.py") == "source", "исходный файл распознан", roles.get("src/app.py", ""))

    sample = next(item for item in document["files"] if item["path"] == "src/app.py")
    check(len(sample["sha256"]) == 64, "хэш содержимого посчитан")
    check(sample["size_bytes"] > 0, "размер файла заполнен")
    check(sample["is_binary"] is False, "текстовый файл не помечен бинарным")
    check(sample["extension"] == "py", "расширение записано без точки")

    non_ascii = "шаблоны/Блок на 4 ссылки.snp"
    check(non_ascii in paths, "файл с не-ASCII именем собран", str(sorted(paths)))
    check(roles.get("index.php~") is not None, "файл с расширением php~ собран")
    check(
        next(item["extension"] for item in document["files"] if item["path"] == "index.php~") == "php~",
        "расширение с необычным символом сохранено",
    )
    modifies = {item["to_id"] for item in document["relationships"] if item["type"] == "MODIFIES"}
    check(
        any(item.endswith(non_ascii) for item in modifies),
        "коммит связан с файлом, имя которого git экранирует",
    )

    check(
        all(is_writable(item["path"]) for item in document["files"]),
        "в выгрузке нет имён вне UTF-8",
    )

    directories = {item["path"] for item in document["directories"]}
    check({"src", "docs", "migrations"} <= directories, "каталоги собраны", str(sorted(directories)))
    check(any(item["path"] == "." for item in document["directories"]), "корневой каталог собран")


def check_evidence(document: dict) -> None:
    """Каждый факт подтверждён источником нужного вида."""
    kinds = {item["source_kind"] for item in document["evidence"]}
    check({"git", "filesystem"} <= kinds, "подтверждения обоих видов источника", str(sorted(kinds)))
    git_records = [item for item in document["evidence"] if item["source_kind"] == "git"]
    check(all(item["source_commit_id"] for item in git_records), "у git-подтверждений указан коммит")
    file_records = [
        item for item in document["evidence"]
        if item["source_kind"] == "filesystem" and item["subject_type"] == "file"
    ]
    check(all(item["source_file_id"] for item in file_records), "у файловых подтверждений указан файл")
    check(
        all(item["analyzer"] in {"core", "filesystem", "vcs.git"} for item in document["evidence"]),
        "анализаторы названы в подтверждениях",
    )


def check_reproducibility(repository: Path, workspace: Path) -> None:
    first, second = workspace / "first.json", workspace / "second.json"
    main(["export", str(repository), "-o", str(first)])
    main(["export", str(repository), "-o", str(second)])
    left = json.loads(first.read_text(encoding="utf-8"))
    right = json.loads(second.read_text(encoding="utf-8"))
    for document in (left, right):
        document["generated_at"] = ""
        for record in document["evidence"]:
            record["collected_at"] = ""
    check(left == right, "повторный прогон даёт ту же выгрузку")

    inside = repository / "project-knowledge.json"
    main(["export", str(repository), "-o", str(inside)])
    document = json.loads(inside.read_text(encoding="utf-8"))
    check(
        all(item["path"] != "project-knowledge.json" for item in document["files"]),
        "файл выгрузки не попадает в собственную выгрузку",
    )
    inside.unlink()


def check_exclusions(repository: Path, workspace: Path) -> None:
    """Исключения задаются флагом и файлом конфигурации и складываются."""
    output = workspace / "excluded.json"
    code = main(["export", str(repository), "-o", str(output), "--quiet", "--exclude", "migrations/"])
    check(code == EXIT_OK, "export с флагом --exclude завершается кодом 0", f"код {code}")
    document = json.loads(output.read_text(encoding="utf-8"))
    paths = {item["path"] for item in document["files"]}
    check(
        not any(item.startswith("migrations/") for item in paths),
        "флаг --exclude убирает каталог из выгрузки",
        str(sorted(paths)),
    )
    check("src/app.py" in paths, "остальные файлы на месте")

    (repository / "agent.yml").write_text("exclude:\n  - docs/\n", encoding="utf-8")
    code = main(["export", str(repository), "-o", str(output), "--quiet", "--exclude", "migrations/"])
    check(code == EXIT_OK, "конфигурация читается без PyYAML", f"код {code}")
    document = json.loads(output.read_text(encoding="utf-8"))
    paths = {item["path"] for item in document["files"]}
    check(
        not any(item.startswith(("docs/", "migrations/")) for item in paths),
        "исключения флага и конфигурации складываются",
        str(sorted(paths)),
    )
    (repository / "agent.yml").unlink()


def check_author_filter(repository: Path, workspace: Path) -> None:
    """Выборка по автору: только его коммиты, целостность и достижимость сохраняются."""
    git(repository, "config", "user.name", "Gaibovich Alexey")
    git(repository, "config", "user.email", "a.gaibovich@tradeium.digital")
    (repository / "src" / "billing.py").write_text("RATE = 2\n", encoding="utf-8")
    git(repository, "add", "-A")
    commit(repository, "-m", "DEMO-010: коммит второго автора")

    output = workspace / "by-author.json"
    code = main(
        ["export", str(repository), "-o", str(output), "--quiet", "--author", "a.gaibovich@tradeium.digital"]
    )
    check(code == EXIT_OK, "export с --author завершается кодом 0", f"код {code}")
    document = json.loads(output.read_text(encoding="utf-8"))
    check(main(["validate", str(output)]) == EXIT_OK, "выгрузка по автору проходит валидацию")
    check(
        {item["email"] for item in document["people"]} == {"a.gaibovich@tradeium.digital"},
        "в выгрузке только запрошенный автор",
        str({item["email"] for item in document["people"]}),
    )
    check(
        document["filters"]["authors"] == ["a.gaibovich@tradeium.digital"],
        "применённая выборка записана в выгрузку",
        str(document["filters"]),
    )
    check(
        len(document["relationships"]) > 0
        and any(item["type"] == "HAS_COMMIT" for item in document["relationships"]),
        "коммиты связаны с репозиторием напрямую",
    )
    check(len(document["files"]) > len(document["commits"]), "рабочая копия собрана целиком")

    by_name = workspace / "by-name.json"
    main(["export", str(repository), "-o", str(by_name), "--quiet", "--author", "Gaibovich Alexey"])
    check(
        json.loads(by_name.read_text(encoding="utf-8"))["commits"],
        "выборка по имени автора находит коммиты",
    )

    both = workspace / "both.json"
    main(
        ["export", str(repository), "-o", str(both), "--quiet", "--author", "Gaibovich", "--author", "Orlova"]
    )
    check(
        len(json.loads(both.read_text(encoding="utf-8"))["people"]) == 2,
        "несколько --author складываются по «или»",
    )

    empty = workspace / "empty-author.json"
    code = main(["export", str(repository), "-o", str(empty), "--quiet", "--author", "nobody@example.com"])
    check(code == EXIT_OK, "выборка без совпадений завершается кодом 0", f"код {code}")
    document = json.loads(empty.read_text(encoding="utf-8"))
    check(document["commits"] == [] and document["branches"] == [], "история пуста при отсутствии совпадений")
    check(document["repositories"][0]["head_commit_id"] is None, "HEAD не ссылается на несобранный коммит")
    check(main(["validate", str(empty)]) == EXIT_OK, "пустая история проходит валидацию")


def check_identity_map(repository: Path, workspace: Path) -> None:
    """Карта тождества: несколько учётных записей сводятся в одного человека."""
    git(repository, "config", "user.name", "Gaibovich Alexey")
    git(repository, "config", "user.email", "alexgaib@mail.ru")
    (repository / "src" / "personal.py").write_text("VALUE = 3\n", encoding="utf-8")
    git(repository, "add", "-A")
    commit(repository, "-m", "DEMO-011: коммит со второй учётной записи")

    plain = workspace / "no-identity.json"
    main(["export", str(repository), "-o", str(plain), "--quiet"])
    accounts = json.loads(plain.read_text(encoding="utf-8"))["people"]
    check(
        len({item["email"] for item in accounts} & {"a.gaibovich@tradeium.digital", "alexgaib@mail.ru"}) == 2,
        "без карты учётные записи одного человека раздельны",
        str([item["email"] for item in accounts]),
    )

    identity = "Гайбович Алексей=a.gaibovich@tradeium.digital,alexgaib@mail.ru"
    merged_path = workspace / "identity.json"
    code = main(["export", str(repository), "-o", str(merged_path), "--quiet", "--identity", identity])
    check(code == EXIT_OK, "export с --identity завершается кодом 0", f"код {code}")
    document = json.loads(merged_path.read_text(encoding="utf-8"))
    check(main(["validate", str(merged_path)]) == EXIT_OK, "выгрузка с картой проходит валидацию")

    merged = [item for item in document["people"] if item["email"] == "a.gaibovich@tradeium.digital"]
    check(len(merged) == 1, "учётные записи сведены в одну сущность", str(len(merged)))
    check(
        "alexgaib@mail.ru" not in {item["email"] for item in document["people"]},
        "второй адрес не создаёт отдельного участника",
    )
    check(merged[0]["name"] == "Гайбович Алексей", "каноническое имя взято из карты", merged[0]["name"])
    check(
        {alias["email"] for alias in merged[0]["aliases"]} >= {"a.gaibovich@tradeium.digital", "alexgaib@mail.ru"},
        "оба адреса сохранены как написания",
        str(merged[0]["aliases"]),
    )

    selected = workspace / "identity-author.json"
    main([
        "export", str(repository), "-o", str(selected), "--quiet",
        "--identity", identity, "--author", "a.gaibovich@tradeium.digital",
    ])
    picked = json.loads(selected.read_text(encoding="utf-8"))
    subjects = {item["subject"] for item in picked["commits"]}
    check(
        any("DEMO-011" in item for item in subjects) and any("DEMO-010" in item for item in subjects),
        "отбор по одному адресу собирает коммиты всех учётных записей человека",
        str(sorted(subjects)),
    )
    check(len(picked["people"]) == 1, "в выборке остаётся один человек", str(len(picked["people"])))

    # Короткое имя личности и адрес, которого нет в истории: сведение идёт по написанию имени.
    short = workspace / "short-identity.json"
    main([
        "export", str(repository), "-o", str(short), "--quiet",
        "--identity", "Alexey=a.gaibovich@lenvendo.ru", "--author", "Alexey",
    ])
    document = json.loads(short.read_text(encoding="utf-8"))
    check(len(document["people"]) == 1, "короткое имя карты сводит учётные записи", str(len(document["people"])))
    check(
        document["people"][0]["email"] == "a.gaibovich@lenvendo.ru",
        "канонический адрес берётся из карты, даже если его нет в истории",
        document["people"][0]["email"],
    )
    check(
        {alias["email"] for alias in document["people"][0]["aliases"]}
        >= {"a.gaibovich@tradeium.digital", "alexgaib@mail.ru"},
        "исходные учётные записи сохранены как написания",
    )
    check(
        len(document["commits"]) >= 2,
        "отбор по имени личности собирает коммиты всех её учётных записей",
        str(len(document["commits"])),
    )

    git(repository, "config", "user.email", "a.gaibovich@tradeium.digital")


def check_without_git(workspace: Path) -> None:
    """Каталог без .git: репозитория нет, файлы и каталоги собраны."""
    plain = workspace / "plain"
    (plain / "src").mkdir(parents=True)
    (plain / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (plain / "README.md").write_text("# Без репозитория\n", encoding="utf-8")

    output = workspace / "plain.json"
    code = main(["export", str(plain), "-o", str(output)])
    check(code == EXIT_OK, "export на каталоге без .git завершается кодом 0", f"код {code}")
    document = json.loads(output.read_text(encoding="utf-8"))
    check(document["repositories"] == [], "массив repositories пуст")
    check(len(document["files"]) == 2, "файлы собраны без репозитория", str(len(document["files"])))
    check(document["files"][0]["repository_id"] is None, "repository_id у файла пуст")
    check(main(["validate", str(output)]) == EXIT_OK, "выгрузка без репозитория проходит валидацию")


def main_script() -> int:
    if subprocess.run(["git", "--version"], capture_output=True, check=False).returncode != 0:
        print("git недоступен: проверки шага 2 пропущены")
        return EXIT_OK

    with tempfile.TemporaryDirectory(prefix="step2-") as directory:
        workspace = Path(directory)
        repository = workspace / "repo"
        build_repository(repository)

        document = run_repository(repository, workspace / "repo.json")
        check_against_git(document, repository)
        check_history(document)
        check_working_copy(document)
        check_evidence(document)
        check_reproducibility(repository, workspace)
        check_exclusions(repository, workspace)
        check_author_filter(repository, workspace)
        check_identity_map(repository, workspace)
        check_without_git(workspace)

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
