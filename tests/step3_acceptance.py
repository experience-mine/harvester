"""Приёмка шага 3: подготовка выгрузки к хранению в объектном хранилище.

Скрипт создаёт временный git-репозиторий известного содержания, прогоняет ``export``
и разбирает результат. Запуск: ``python tests/step3_acceptance.py``. Код возврата ``0`` —
все проверки пройдены; при отсутствии git проверки пропускаются.

Сценарий наращивается вместе с изменением формата выгрузки: разбор результата вынесен
отдельно от проверок, чтобы новые проверки опирались на уже разобранный документ.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.analyzers.vcs_git import git_available
from agent.cli import EXIT_INVALID, EXIT_OK, main
from agent.core.exporter import build_document
from agent.core.ids import code_unit_id, evidence_id, finding_id
from agent.core.model import AGENT_VERSION, Evidence, FactCollector
from agent.core.paths import analysis_file_id
from agent.core.validator import load_schema, validate_document

TIMESTAMP = "2026-08-30T12:00:00Z"

_results: list[tuple[bool, str, str]] = []

#: Секунды от начала суток, на которые сдвигается дата очередного коммита.
_moment = 0


def check(condition: bool, title: str, detail: str = "") -> None:
    _results.append((bool(condition), title, detail))


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=False
    )
    return completed.stdout.strip()


def commit(root: Path, *arguments: str) -> str:
    """Коммит с фиксированной датой: история репозитория остаётся детерминированной."""
    global _moment
    _moment += 60
    moment = f"2026-08-30T{10 + _moment // 3600:02d}:{_moment % 3600 // 60:02d}:00+00:00"
    completed = subprocess.run(
        ["git", "-C", str(root), "commit", "-q", *arguments],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_AUTHOR_DATE": moment, "GIT_COMMITTER_DATE": moment},
    )
    return completed.stdout.strip()


def build_repository(root: Path) -> None:
    """Репозиторий известного содержания: исходники, документ и миграция в двух коммитах."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main", ".")
    git(root, "config", "user.email", "m.orlova@acme.dev")
    git(root, "config", "user.name", "Мария Орлова")

    (root / "src").mkdir()
    (root / "docs").mkdir()
    (root / "src" / "app.py").write_text("print('привет')\n", encoding="utf-8")
    (root / "docs" / "readme.md").write_text("# Демонстрация\n", encoding="utf-8")
    git(root, "add", "-A")
    commit(root, "-m", "первый коммит")

    (root / "migrations").mkdir()
    (root / "migrations" / "0001_init.sql").write_text(
        "create table orders ();\n", encoding="utf-8"
    )
    (root / "src" / "app.py").write_text("print('привет, мир')\n", encoding="utf-8")
    git(root, "add", "-A")
    commit(root, "-m", "второй коммит")


def run_without_documents(workspace: Path) -> None:
    """Отсутствие документов анализа кода не мешает прогону: секции остаются пустыми."""
    project = workspace / "empty-analysis"
    build_repository(project)
    output = workspace / "empty-analysis.json"
    absent = workspace / "нет-такого-каталога"

    code = main(
        ["export", str(project), "-o", str(output), "--code-analysis", str(absent), "--quiet"]
    )
    check(code == EXIT_OK, "прогон без документов анализа завершается кодом 0", f"код {code}")
    if code != EXIT_OK:
        return

    document = json.loads(output.read_text(encoding="utf-8"))
    check(document.get("code_units") == [], "секция определений кода пуста")
    check(document.get("findings") == [], "секция находок пуста")
    check(document.get("analysis_scope") == {}, "охват анализа пуст, но присутствует")

    # Неуспех всех разделов отличается от прогона без анализа: каталог анализа создан,
    # но ни один документ не получен. Охват здесь обязан присутствовать — по нему
    # потребитель отличает файл вне области анализа от файла без определений.
    failed = workspace / "провалившийся-анализ"
    failed.mkdir()
    output_failed = workspace / "export-failed.json"
    code = main(
        ["export", str(project), "-o", str(output_failed), "--code-analysis", str(failed), "--quiet"]
    )
    check(code == EXIT_OK, "прогон с неуспешным анализом завершается кодом 0", f"код {code}")
    if code != EXIT_OK:
        return
    scope = json.loads(output_failed.read_text(encoding="utf-8")).get("analysis_scope") or {}
    check(bool(scope), "охват присутствует при неуспехе всех разделов анализа", str(scope))
    check(scope.get("documents") == [], "охват фиксирует отсутствие документов анализа")
    check(scope.get("sections") == {}, "охват фиксирует, что ни один раздел не выполнен")


def write_structure(directory: Path, root: Path) -> int:
    """Документ анализа кода известного содержания; возвращает число определений в нём."""
    document = {
        "root": str(root / "src"),
        "language": "php",
        "files": [
            {
                "path": "app.php",
                "definitions": [
                    {
                        "name": "Service",
                        "kind": "class",
                        "line_start": 3,
                        "line_end": 20,
                        "signature": "final class Service",
                    },
                    {
                        "name": "__construct",
                        "kind": "method",
                        "line_start": 5,
                        "line_end": 8,
                        "signature": "public function __construct(",
                    },
                    {
                        "name": "run",
                        "kind": "method",
                        "line_start": 10,
                        "line_end": 19,
                        "signature": "public function run(): void",
                    },
                    {
                        "name": "helper",
                        "kind": "function",
                        "line_start": 22,
                        "line_end": 25,
                        "signature": "function helper(): int",
                    },
                ],
            },
            {
                "path": "helper.php",
                "definitions": [
                    {
                        "name": "support",
                        "kind": "function",
                        "line_start": 3,
                        "line_end": 9,
                        "signature": "function support(): void",
                    }
                ],
            },
        ],
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tldr-structure.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    # Одна пара определений вызывается дважды с разными видами вызова: связь должна
    # остаться одной, а оба вида — сохраниться.
    calls = {
        "root": str(root / "src"),
        "language": "php",
        "nodes": ["app.php:Service.run", "app.php:Service.__construct"],
        "edges": [
            {
                "src_file": "app.php",
                "src_func": "Service.run",
                "dst_file": "app.php",
                "dst_func": "Service.__construct",
                "call_type": "intra",
            },
            {
                "src_file": "app.php",
                "src_func": "Service.run",
                "dst_file": "app.php",
                "dst_func": "Service.__construct",
                "call_type": "attr",
            },
            {
                "src_file": "app.php",
                "src_func": "Service.run",
                "dst_file": "vendor/Unknown.php",
                "dst_func": "Unknown.method",
                "call_type": "direct",
            },
        ],
        "truncated": False,
        "total_edges": 3,
        "shown_edges": 3,
    }
    (directory / "tldr-calls.json").write_text(
        json.dumps(calls, ensure_ascii=False), encoding="utf-8"
    )

    health = {
        "wrapper": "health",
        "root": str(root / "src"),
        "language": "php",
        "total_elapsed_ms": 1234.5678,
        "summary": {"files_analyzed": 1},
        "details": {
            "complexity": {
                "name": "complexity",
                "success": True,
                "elapsed_ms": 12.5,
                "findings_count": 1,
                "details": {
                    "functions_analyzed": 3,
                    "hotspots": [
                        {
                            "name": "Service.run",
                            "file": str(root / "src" / "app.php"),
                            "line": 10,
                            "cyclomatic": 17,
                            "cognitive": 24,
                            "loc": 9,
                            "rank": 1,
                        }
                    ],
                },
            },
            "cohesion": {
                "name": "cohesion",
                "success": True,
                "elapsed_ms": 8.25,
                "findings_count": 1,
                "details": {
                    "classes_analyzed": 1,
                    "classes": [
                        {
                            "name": "Service",
                            "file": str(root / "src" / "app.php"),
                            "line": 3,
                            "method_count": 2,
                            "field_count": 1,
                            "lcom4": 2,
                            "verdict": "split_candidate",
                            "split_suggestion": "Consider splitting into 2 classes",
                            "components": [{"methods": ["run"], "fields": []}],
                        }
                    ],
                },
            },
            "coupling": {
                "name": "coupling",
                "success": True,
                "findings_count": 1,
                "details": {
                    "tight_coupling_count": 1,
                    "top_pairs": [
                        {
                            "source": "app.php",
                            "target": "helper.php",
                            "import_count": 0,
                            "call_count": 12,
                            "score": 0.87,
                            "verdict": "tight",
                        },
                        {
                            "source": "app.php",
                            "target": "other.php",
                            "import_count": 1,
                            "call_count": 2,
                            "score": 0.2,
                            "verdict": "moderate",
                        },
                    ],
                },
            },
            "similar": {
                "name": "similar",
                "success": True,
                "findings_count": 2,
                "details": {
                    "clone_pairs": [
                        {
                            "id": 1,
                            "clone_type": "Type-1",
                            "similarity": 1.0,
                            "fragment1": {"file": str(root / "src" / "app.php"), "start_line": 10},
                            "fragment2": {"file": str(root / "src" / "helper.php"), "start_line": 4},
                        },
                        {
                            "id": 2,
                            "clone_type": "Type-2",
                            "similarity": 0.8,
                            "fragment1": {"file": str(root / "src" / "app.php"), "start_line": 12},
                            "fragment2": {"file": str(root / "src" / "helper.php"), "start_line": 6},
                        },
                    ],
                },
            },
        },
    }
    (directory / "tldr-health.json").write_text(
        json.dumps(health, ensure_ascii=False), encoding="utf-8"
    )
    return sum(len(item["definitions"]) for item in document["files"])


def run_definitions(workspace: Path) -> None:
    """Определения из документа анализа попадают в выгрузку вместе со связями."""
    project = workspace / "with-analysis"
    build_repository(project)
    (project / "src" / "app.php").write_text("<?php\n", encoding="utf-8")
    (project / "src" / "helper.php").write_text("<?php\n", encoding="utf-8")
    git(project, "add", "-A")
    commit(project, "-m", "исходник для анализа")

    analysis = workspace / "analysis"
    expected = write_structure(analysis, project)
    output = workspace / "with-analysis.json"

    code = main(
        ["export", str(project), "-o", str(output), "--code-analysis", str(analysis), "--quiet"]
    )
    check(code == EXIT_OK, "прогон с документами анализа завершается кодом 0", f"код {code}")
    if code != EXIT_OK:
        return

    document = json.loads(output.read_text(encoding="utf-8"))
    units = document["code_units"]
    check(
        len(units) == expected,
        "число определений совпадает с исходным документом",
        f"{len(units)} != {expected}",
    )

    by_id = {item["id"]: item for item in units}
    relationships = document["relationships"]
    defines = [item for item in relationships if item["type"] == "DEFINES" and item["to_id"] in by_id]
    check(len(defines) == expected, "каждое определение связано с файлом", str(len(defines)))

    contains = [item for item in relationships if item["type"] == "CONTAINS" and item["to_id"] in by_id]
    check(len(contains) == 2, "члены класса связаны с объемлющим классом", str(len(contains)))
    owners = {item["to_id"] for item in contains}
    check(len(owners) == len(contains), "член связан ровно с одним классом")

    qualified = {item.get("qualified_name") for item in units}
    check("Service.run" in qualified, "имя члена квалифицировано классом", str(sorted(qualified)))
    check("helper" in qualified, "определение вне класса не квалифицируется", str(sorted(qualified)))

    calls = [item for item in relationships if item["type"] == "CALLS"]
    check(len(calls) == 1, "повторный вызов той же пары даёт одну связь", str(len(calls)))
    if calls:
        check(
            calls[0]["attributes"].get("call_types") == ["attr", "intra"],
            "оба вида вызова сохранены в связи",
            str(calls[0]["attributes"]),
        )
    check(
        all(item["to_id"] in by_id for item in calls),
        "вызов несобранного определения отброшен",
    )

    method = next(
        (item for item in units if item.get("qualified_name") == "Service.run"), {}
    )
    check(method.get("cyclomatic") == 17, "измерение сложности записано полем определения", str(method.get("cyclomatic")))
    check(method.get("cognitive") == 24, "когнитивная сложность записана полем определения")
    check("rank" not in method, "суждение анализатора не попало в поля определения", str(sorted(method)))

    klass = next((item for item in units if item.get("qualified_name") == "Service"), {})
    check(klass.get("lcom4") == 2, "измерение связности записано полем класса", str(klass.get("lcom4")))
    check(klass.get("method_count") == 2, "число методов записано полем класса")
    check("components" not in klass, "разбивка связности в поля не переносится", str(sorted(klass)))

    findings = document["findings"]
    check(len(findings) == 2, "находки собраны из разделов анализа", str(len(findings)))
    kinds = {item["kind"] for item in findings}
    check(kinds == {"hotspot", "low_cohesion"}, "виды находок соответствуют разделам", str(kinds))

    hotspot = next((item for item in findings if item["kind"] == "hotspot"), {})
    check(hotspot.get("rank") == 1, "суждение несёт собственные атрибуты", str(hotspot.get("rank")))

    about = [item for item in document["relationships"] if item["type"] == "HAS_FINDING"]
    check(len(about) == len(findings), "каждая находка связана с предметом", str(len(about)))
    subjects = {item["from_id"] for item in about}
    check(subjects <= set(by_id), "предмет находки — собранное определение", str(subjects - set(by_id)))
    check(
        {item["to_id"] for item in about} == {item["id"] for item in findings},
        "связь ведёт от предмета к находке",
    )

    coupled = [item for item in document["relationships"] if item["type"] == "COUPLED_WITH"]
    check(len(coupled) == 1, "тесно связанная пара стала связью", str(len(coupled)))
    if coupled:
        check(
            coupled[0]["attributes"].get("call_count") == 12,
            "показатели связанности записаны атрибутами",
            str(coupled[0]["attributes"]),
        )

    similar = [item for item in document["relationships"] if item["type"] == "SIMILAR_TO"]
    check(len(similar) == 1, "два клона одной пары файлов дают одну связь", str(len(similar)))
    if similar:
        check(
            similar[0]["attributes"].get("clone_count") == 2,
            "число клонов сведено в атрибут связи",
            str(similar[0]["attributes"]),
        )
        check(
            similar[0]["attributes"].get("max_similarity") == 1.0,
            "наибольшее сходство сохранено",
        )

    check(
        all(item["kind"] not in ("tight_coupling", "clone") for item in findings),
        "парные суждения сущностей находок не создают",
        str({item["kind"] for item in findings}),
    )

    scope = document.get("analysis_scope") or {}
    check(scope.get("root") == "src", "охват называет анализируемый подкаталог", str(scope.get("root")))
    check(scope.get("language") == "php", "охват называет язык", str(scope.get("language")))
    check(
        scope.get("calls_truncated") is False,
        "охват фиксирует полноту графа вызовов",
        str(scope.get("calls_truncated")),
    )
    check(
        set(scope.get("sections") or {}) == {"complexity", "cohesion", "coupling", "similar"},
        "охват перечисляет выполненные разделы анализа",
        str(sorted(scope.get("sections") or {})),
    )

    serialized = json.dumps(document, ensure_ascii=False)
    check("elapsed_ms" not in serialized, "длительность анализа в выгрузку не попадает")


def normalize_time(document: dict) -> str:
    """Текст выгрузки без полей времени прогона: остальное обязано совпадать."""
    document = json.loads(json.dumps(document))
    document["generated_at"] = "—"
    for record in document.get("evidence", []):
        record["collected_at"] = "—"
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


def run_determinism(workspace: Path) -> None:
    """Повторный прогон по неизменной копии даёт ту же выгрузку с точностью до времени."""
    project = workspace / "repeatable"
    build_repository(project)
    (project / "src" / "app.php").write_text("<?php\n", encoding="utf-8")
    (project / "src" / "helper.php").write_text("<?php\n", encoding="utf-8")
    git(project, "add", "-A")
    commit(project, "-m", "исходники")

    analysis = workspace / "repeatable-analysis"
    write_structure(analysis, project)

    documents = []
    for index in (1, 2):
        output = workspace / f"repeatable-{index}.json"
        code = main(
            ["export", str(project), "-o", str(output), "--code-analysis", str(analysis), "--quiet"]
        )
        if code != EXIT_OK:
            check(False, "повторный прогон завершается кодом 0", f"код {code}")
            return
        documents.append(json.loads(output.read_text(encoding="utf-8")))

    check(
        normalize_time(documents[0]) == normalize_time(documents[1]),
        "две выгрузки совпадают с точностью до полей времени прогона",
    )
    check(
        documents[0]["generated_at"] != "" and documents[1]["generated_at"] != "",
        "время прогона в выгрузке заполнено",
    )


def run_dropped_facts(workspace: Path) -> None:
    """Ссылки на файлы вне выгрузки отбрасываются, а не превращаются в висячие сущности."""
    project = workspace / "filtered"
    build_repository(project)
    (project / "src" / "app.php").write_text("<?php\n", encoding="utf-8")
    (project / "src" / "helper.php").write_text("<?php\n", encoding="utf-8")
    git(project, "add", "-A")
    commit(project, "-m", "исходники")

    analysis = workspace / "filtered-analysis"
    write_structure(analysis, project)
    output = workspace / "filtered.json"

    # helper.php исключён из обхода, поэтому все факты анализа о нём неразрешимы
    code = main(
        [
            "export",
            str(project),
            "-o",
            str(output),
            "--code-analysis",
            str(analysis),
            "--exclude",
            "src/helper.php",
            "--quiet",
        ]
    )
    check(code == EXIT_OK, "прогон с фильтром завершается кодом 0", f"код {code}")
    if code != EXIT_OK:
        return

    document = json.loads(output.read_text(encoding="utf-8"))
    identifiers = {item["id"] for _, items in document.items() if isinstance(items, list) for item in items if isinstance(item, dict) and "id" in item}
    identifiers.add(document["project"]["id"])

    dangling = [
        item
        for item in document["relationships"]
        if item["from_id"] not in identifiers or item["to_id"] not in identifiers
    ]
    check(not dangling, "висячих ссылок в выгрузке нет", str(dangling[:1]))

    excluded = [item for item in document["code_units"] if "helper.php" in item["id"]]
    check(not excluded, "определения исключённого файла не собраны", str(excluded[:1]))

    kept = [item for item in document["code_units"] if "app.php" in item["id"]]
    check(bool(kept), "определения собранного файла на месте", str(len(kept)))

    scope = document.get("analysis_scope") or {}
    check(
        scope.get("dropped_facts", 0) > 0,
        "счётчик отброшенных фактов ненулевой",
        str(scope.get("dropped_facts")),
    )


def run_run_directory(workspace: Path) -> None:
    """Каталог прогона: изоляция прогонов, исходные документы и диагностика рядом."""
    project = workspace / "run-layout"
    build_repository(project)
    (project / "src" / "app.php").write_text("<?php\n", encoding="utf-8")
    (project / "src" / "helper.php").write_text("<?php\n", encoding="utf-8")
    git(project, "add", "-A")
    commit(project, "-m", "исходники")

    analysis = workspace / "run-analysis"
    write_structure(analysis, project)
    (analysis / "tldr-complexity.err").write_text("не удалось\n", encoding="utf-8")

    results = workspace / "results"
    results.mkdir()
    for _ in (1, 2):
        code = main(
            [
                "export",
                str(project),
                "--versioned",
                "-o",
                str(results),
                "--code-analysis",
                str(analysis),
                "--quiet",
            ]
        )
        if code != EXIT_OK:
            check(False, "прогон в каталог результатов завершается кодом 0", f"код {code}")
            return

    runs = sorted(results.glob("run-layout/*/core.json.gz"))
    check(len(runs) == 2, "каждый прогон получил свой каталог", str(len(runs)))
    if len(runs) != 2:
        return
    check(
        runs[0].parent != runs[1].parent,
        "прогоны не перезаписывают друг друга",
    )

    preserved = sorted((runs[0].parent / "code-analysis").glob("*.gz"))
    names = {item.name for item in preserved}
    check(
        "tldr-structure.json.gz" in names,
        "исходный документ анализа сохранён в каталоге прогона",
        str(sorted(names)),
    )
    check(
        "tldr-complexity.err.gz" in names,
        "диагностика анализатора перенесена в каталог прогона",
        str(sorted(names)),
    )

    archive = runs[0].parent / "code-analysis" / "tldr-structure.json.gz"
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        restored = json.load(handle)
    check(
        restored.get("language") == "php",
        "сохранённый документ читается и совпадает с исходным",
        str(restored.get("language")),
    )


def run_manifest(workspace: Path) -> None:
    """Манифест прогона: состав, контрольные суммы, провенанс и полнота."""
    project = workspace / "manifested"
    build_repository(project)
    (project / "src" / "app.php").write_text("<?php\n", encoding="utf-8")
    (project / "src" / "helper.php").write_text("<?php\n", encoding="utf-8")
    git(project, "add", "-A")
    commit(project, "-m", "исходники")

    analysis = workspace / "manifested-analysis"
    write_structure(analysis, project)

    metadata = workspace / "run-metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "tldr_version": "tldr 0.4.0",
                "image": "ghcr.io/experience-mine/harvester",
                "image_tag": "v1.1.0",
                "image_revision": "e3f1c2d",
                "image_digest": "sha256:0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
                "code_analysis": {"structure": True, "calls": True, "health": False},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    results = workspace / "manifest-results"
    results.mkdir()
    # В каталоге результатов уже лежит посторонний файл: в перечень он попасть не должен.
    (results / "посторонний.txt").write_text("не мой\n", encoding="utf-8")

    code = main(
        [
            "export",
            str(project),
            "--versioned",
            "-o",
            str(results),
            "--code-analysis",
            str(analysis),
            "--run-metadata",
            str(metadata),
            "--quiet",
        ]
    )
    check(code == EXIT_OK, "прогон с манифестом завершается кодом 0", f"код {code}")
    if code != EXIT_OK:
        return

    run = next(iter(sorted(results.glob("manifested/*/"))), None)
    check(run is not None, "каталог прогона создан")
    if run is None:
        return

    manifest_path = run / "manifest.json"
    check(manifest_path.is_file(), "манифест лежит в каталоге прогона")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    names = {item["name"] for item in manifest["objects"]}
    check("core.json.gz" in names, "скалярная часть перечислена в манифесте", str(sorted(names)))
    check(
        "code_units.jsonl.gz" in names and "evidence.jsonl.gz" in names,
        "секции перечислены в манифесте",
        str(sorted(names)),
    )
    check(
        "посторонний.txt" not in names,
        "посторонний файл в перечень не попал",
        str(sorted(names)),
    )

    for item in manifest["objects"]:
        target = run / item["name"]
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        check(
            digest == item["sha256"] and target.stat().st_size == item["size_bytes"],
            f"сумма и размер сходятся: {item['name']}",
            f"{digest} != {item['sha256']}",
        )

    with gzip.open(run / "code_units.jsonl.gz", "rt", encoding="utf-8") as handle:
        units = [line for line in handle.read().splitlines() if line]
    check(
        manifest["sections"]["code_units"] == len(units),
        "число записей секции совпадает с объектом секции",
        f"{manifest['sections']['code_units']} != {len(units)}",
    )
    with gzip.open(run / "core.json.gz", "rt", encoding="utf-8") as handle:
        core = json.load(handle)
    check(
        manifest["filters"] == core["filters"],
        "состав фильтров восстанавливается из манифеста полностью",
        str(manifest["filters"]),
    )

    provenance = manifest.get("provenance") or {}
    check(provenance.get("tldr_version") == "tldr 0.4.0", "провенанс несёт версию внешней утилиты")
    check(
        provenance.get("image_tag") == "v1.1.0",
        "провенанс несёт координату и версию образа",
        str(provenance.get("image_tag")),
    )
    # Версия агента и тег образа две сборки не различают: первая задана константой
    # в коде, второй переставляется на новую сборку. Различает неизменяемый идентификатор.
    check(
        provenance.get("image_revision") == "e3f1c2d",
        "провенанс несёт неизменяемую ревизию сборки образа",
        str(provenance.get("image_revision")),
    )
    check(
        str(provenance.get("image_digest", "")).startswith("sha256:"),
        "провенанс несёт digest образа",
        str(provenance.get("image_digest")),
    )
    check(
        provenance.get("code_analysis", {}).get("health") is False,
        "манифест фиксирует неуспех раздела анализа",
        str(provenance.get("code_analysis")),
    )
    # Один вызов в сценарии ведёт в файл вне выгрузки и потому отбрасывается.
    check(
        manifest.get("analysis_scope", {}).get("dropped_facts") == 1,
        "манифест несёт число отброшенных фактов",
        str(manifest.get("analysis_scope", {}).get("dropped_facts")),
    )


def run_sections(workspace: Path) -> None:
    """Посекционная запись: отдельные объекты, построчный формат, независимое сжатие."""
    project = workspace / "sectioned"
    build_repository(project)
    (project / "src" / "app.php").write_text("<?php\n", encoding="utf-8")
    (project / "src" / "helper.php").write_text("<?php\n", encoding="utf-8")
    git(project, "add", "-A")
    commit(project, "-m", "исходники")

    analysis = workspace / "sectioned-analysis"
    write_structure(analysis, project)
    results = workspace / "sectioned-results"
    results.mkdir()

    code = main(
        [
            "export",
            str(project),
            "--versioned",
            "-o",
            str(results),
            "--code-analysis",
            str(analysis),
            "--quiet",
        ]
    )
    check(code == EXIT_OK, "посекционный прогон завершается кодом 0", f"код {code}")
    if code != EXIT_OK:
        return

    run = next(iter(sorted(results.glob("sectioned/*/"))), None)
    if run is None:
        check(False, "каталог прогона создан")
        return

    with gzip.open(run / "core.json.gz", "rt", encoding="utf-8") as handle:
        core = json.load(handle)
    check("project" in core and "filters" in core, "скалярная часть в отдельном объекте", str(sorted(core)))
    check(
        not any(key in core for key in ("files", "relationships", "evidence")),
        "массивные секции в скалярную часть не попали",
        str(sorted(core)),
    )

    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    sections = manifest["sections"]

    # Выборочное чтение: нужна одна секция, остальные объекты не открываются.
    with gzip.open(run / "files.jsonl.gz", "rt", encoding="utf-8") as handle:
        lines = [line for line in handle.read().splitlines() if line]
    check(
        len(lines) == sections["files"],
        "число строк секции совпадает с числом записей",
        f"{len(lines)} != {sections['files']}",
    )
    check(
        all(json.loads(line).get("id", "").startswith("file:") for line in lines),
        "каждая строка секции — самостоятельная запись",
    )
    check(
        lines == sorted(lines, key=lambda item: json.loads(item)["id"]),
        "записи секции упорядочены по идентификатору",
    )
    check(
        "\n  " not in "".join(lines),
        "форматирующие отступы в секции отсутствуют",
    )

    empty = run / "requirements.jsonl.gz"
    check(empty.is_file(), "пустая секция записана объектом")
    with gzip.open(empty, "rt", encoding="utf-8") as handle:
        check(handle.read().strip() == "", "пустая секция содержит ноль записей")
    check(
        any(item["name"] == "requirements.jsonl.gz" for item in manifest["objects"]),
        "пустая секция перечислена в манифесте",
    )

    for item in manifest["objects"]:
        target = run / item["name"]
        if not item["name"].endswith(".gz"):
            continue
        try:
            with gzip.open(target, "rb") as handle:
                handle.read()
        except OSError:
            check(False, f"объект распаковывается независимо: {item['name']}")
            return
    check(True, "каждый объект распаковывается независимо")


def run_validation(workspace: Path) -> None:
    """Проверка прогона: секции по своим схемам, ссылки между секциями, манифест."""
    project = workspace / "validated"
    build_repository(project)
    (project / "src" / "app.php").write_text("<?php\n", encoding="utf-8")
    (project / "src" / "helper.php").write_text("<?php\n", encoding="utf-8")
    git(project, "add", "-A")
    commit(project, "-m", "исходники")

    analysis = workspace / "validated-analysis"
    write_structure(analysis, project)
    results = workspace / "validated-results"
    results.mkdir()

    if main(
        [
            "export",
            str(project),
            "--versioned",
            "-o",
            str(results),
            "--code-analysis",
            str(analysis),
            "--quiet",
        ]
    ) != EXIT_OK:
        check(False, "прогон для проверки создан")
        return

    run = next(iter(sorted(results.glob("validated/*/"))), None)
    if run is None:
        check(False, "каталог прогона создан")
        return

    check(main(["validate", str(run)]) == EXIT_OK, "прогон нового формата проходит проверку")

    # Прежний формат: единый документ с прежней версией схемы.
    legacy = workspace / "legacy.json"
    legacy.write_text(
        json.dumps({"schema_version": "1.0", "generated_at": TIMESTAMP}, ensure_ascii=False),
        encoding="utf-8",
    )
    check(main(["validate", str(legacy)]) == EXIT_INVALID, "выгрузка прежнего формата отклоняется")

    # Ссылка на запись, которой нет в соседней секции.
    broken = workspace / "broken-results"
    shutil.copytree(run, broken / run.name)
    target = broken / run.name / "relationships.jsonl.gz"
    with gzip.open(target, "rt", encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle.read().splitlines() if line]
    if records:
        records[0]["to_id"] = "file:validated/несуществующий.php"
        with gzip.open(target, "wt", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        check(
            main(["validate", str(broken / run.name)]) == EXIT_INVALID,
            "висячая ссылка между секциями отклоняется",
        )

    # Манифест без контрольных сумм.
    without_sums = workspace / "sumless-results"
    shutil.copytree(run, without_sums / run.name)
    manifest_path = without_sums / run.name / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["objects"]:
        item.pop("sha256", None)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    check(
        main(["validate", str(without_sums / run.name)]) == EXIT_INVALID,
        "манифест без контрольных сумм отклоняется",
    )


def run_export(workspace: Path) -> dict | None:
    """Прогон на подготовленном репозитории; возвращает разобранный документ выгрузки."""
    project = workspace / "project"
    build_repository(project)
    output = workspace / "export.json"

    code = main(["export", str(project), "-o", str(output), "--quiet"])
    check(code == EXIT_OK, "export на подготовленном репозитории завершается кодом 0", f"код {code}")
    if code != EXIT_OK or not output.exists():
        return None
    return json.loads(output.read_text(encoding="utf-8"))


def run_analysis_paths() -> None:
    """Приведение ссылок анализатора кода к идентификаторам файлов выгрузки."""
    relative = analysis_file_id("backend", "/backend/src", "/backend", "Afisha/UseCase/X.php")
    absolute = analysis_file_id(
        "backend", "/backend/src", "/backend", "/backend/src/Afisha/UseCase/X.php"
    )
    check(
        relative == absolute,
        "обе формы записи пути дают один идентификатор",
        f"{relative} != {absolute}",
    )
    check(
        relative == "file:backend/src/Afisha/UseCase/X.php",
        "идентификатор построен от корня репозитория",
        str(relative),
    )

    outside = analysis_file_id("backend", "/backend/src", "/backend", "../../etc/passwd")
    check(outside is None, "путь вне проекта не приводится к идентификатору", str(outside))

    empty = analysis_file_id("backend", "/backend/src", "/backend", "   ")
    check(empty is None, "пустой путь не приводится к идентификатору", str(empty))

    # Регистр значим: пути различаются, и сводить их к одному определению нельзя.
    other_case = analysis_file_id("backend", "/backend/src", "/backend", "afisha/usecase/x.php")
    check(
        other_case != relative,
        "путь другого регистра даёт другой идентификатор",
        f"{other_case} == {relative}",
    )

    same_root = analysis_file_id("backend", "/backend", "/backend", "src/app.py")
    check(
        same_root == "file:backend/src/app.py",
        "корень анализа, совпадающий с корнем проекта, обрабатывается",
        str(same_root),
    )

    nested = analysis_file_id("backend", "/backend/src/./Afisha/..", "/backend", "UseCase/X.php")
    check(
        nested == "file:backend/src/UseCase/X.php",
        "корень анализа нормализуется до сравнения",
        str(nested),
    )

    project_itself = analysis_file_id("backend", "/backend", "/backend", ".")
    check(project_itself is None, "корень проекта не является файлом", str(project_itself))


def add_project(collector: FactCollector) -> None:
    """Проект, удовлетворяющий схеме: без него выгрузка не собирается."""
    collector.add_entity(
        "project",
        "project:demo",
        {
            "name": "demo",
            "slug": "demo",
            "root_path": "/demo",
            "agent_version": AGENT_VERSION,
        },
        code_evidence("project:demo"),
    )


def code_evidence(subject_id: str) -> Evidence:
    """Подтверждение факта, полученного анализом кода."""
    return Evidence(
        id=evidence_id(subject_id, "filesystem", None, subject_id, "code"),
        subject_id=subject_id,
        subject_type=subject_id.split(":", 1)[0],
        source_kind="filesystem",
        analyzer="code",
        analyzer_version="1.0.0",
        collected_at=TIMESTAMP,
        source_locator=subject_id,
    )


def run_findings() -> None:
    """Находки анализа кода собираются в выгрузку, вид источника принимается моделью."""
    collector = FactCollector()
    add_project(collector)
    identifier = "finding:demo/hotspot-1"
    record = code_evidence(identifier)
    record.source_kind = "code_analysis"
    collector.add_entity(
        "finding",
        identifier,
        {"kind": "hotspot", "rank": 1, "subject_id": "code_unit:demo/a.php:A", "analyzer": "tldr.complexity"},
        record,
    )

    document = build_document(collector, TIMESTAMP)
    check("findings" in document, "секция находок присутствует в выгрузке")
    check(len(document.get("findings", [])) == 1, "находка попала в выгрузку")
    kinds = {item["source_kind"] for item in document["evidence"]}
    check("code_analysis" in kinds, "вид источника code_analysis принят накопителем", str(kinds))

    unknown = FactCollector()
    add_project(unknown)
    rejected = unknown.add_entity(
        "unknown_type", "unknown_type:x", {}, code_evidence("unknown_type:x")
    )
    check(rejected is None, "сущность неизвестного типа отклонена")
    check(bool(unknown.warnings), "отклонение неизвестного типа даёт предупреждение")


def run_relationship_ends() -> None:
    """Допустимые пары концов связей анализа кода и отклонение недопустимой пары."""
    collector = FactCollector()
    add_project(collector)
    caller = "code_unit:demo/src/a.php:A.run"
    callee = "code_unit:demo/src/b.php:B.handle"
    for identifier in (caller, callee):
        collector.add_entity(
            "code_unit",
            identifier,
            {"kind": "method", "name": identifier.rsplit(".", 1)[-1], "qualified_name": identifier.rsplit(":", 1)[-1]},
            code_evidence(identifier),
        )

    accepted = collector.add_relationship("CALLS", caller, callee, code_evidence(caller))
    check(accepted is not None, "связь вызова между определениями принята")

    schema = load_schema()
    errors = validate_document(build_document(collector, TIMESTAMP), schema)
    check(
        not any("не соединяет" in error for error in errors),
        "допустимая пара концов претензий не вызывает",
        str([error for error in errors if "не соединяет" in error][:1]),
    )

    collector.add_relationship("CALLS", "project:demo", callee, code_evidence(caller))
    errors = validate_document(build_document(collector, TIMESTAMP), schema)
    check(
        any("не соединяет" in error for error in errors),
        "связь вызова с недопустимой парой концов отклонена валидацией",
        str(errors[:1]),
    )


def run_identifiers() -> None:
    """Идентификаторы новых сущностей не зависят от порядка и времени прогона."""
    first = code_unit_id("demo", "src/a.php", "A.run")
    check(first == "code_unit:demo/src/a.php:A.run", "ключ определения собран из файла и имени", first)
    check(
        code_unit_id("demo", "src/a.php", "A.run") == first,
        "повторное построение даёт тот же ключ определения",
    )
    same_name_elsewhere = code_unit_id("demo", "src/b.php", "A.run")
    check(same_name_elsewhere != first, "одноимённые определения в разных файлах различаются")

    hotspot = finding_id("hotspot", first, "tldr.complexity")
    check(hotspot.startswith("finding:"), "ключ находки имеет префикс типа", hotspot)
    check(
        finding_id("hotspot", first, "tldr.complexity") == hotspot,
        "повторное построение даёт тот же ключ находки",
    )
    check(
        finding_id("low_cohesion", first, "tldr.complexity") != hotspot,
        "находки разного вида о том же предмете различаются",
    )


def run_code_units() -> None:
    """Определения кода собираются в выгрузку и упорядочены по идентификатору."""
    collector = FactCollector()
    add_project(collector)
    for identifier, name in (("code_unit:demo/b", "B"), ("code_unit:demo/a", "A")):
        collector.add_entity(
            "code_unit",
            identifier,
            {"name": name, "kind": "class", "qualified_name": name},
            code_evidence(identifier),
        )

    document = build_document(collector, TIMESTAMP)
    check("code_units" in document, "секция определений кода присутствует в выгрузке")
    collected = [item["id"] for item in document.get("code_units", [])]
    check(len(collected) == 2, "оба определения попали в выгрузку", str(collected))
    check(collected == sorted(collected), "определения упорядочены по идентификатору", str(collected))


def main_script() -> int:
    if not git_available():
        print("git недоступен, проверки пропущены")
        return EXIT_OK

    run_analysis_paths()
    run_code_units()
    run_findings()
    run_relationship_ends()
    run_identifiers()

    with tempfile.TemporaryDirectory(prefix="step3-") as directory:
        run_export(Path(directory))
        run_without_documents(Path(directory))
        run_definitions(Path(directory))
        run_determinism(Path(directory))
        run_dropped_facts(Path(directory))
        run_run_directory(Path(directory))
        run_manifest(Path(directory))
        run_sections(Path(directory))
        run_validation(Path(directory))

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
