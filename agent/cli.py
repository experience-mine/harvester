"""Командный интерфейс агента: операции ``export`` и ``validate``.

Коды возврата:

* ``0`` — файл записан либо проверка пройдена;
* ``1`` — ошибка запуска: каталог недоступен, конфигурация неверна, аргументы неполны;
* ``2`` — валидация не пройдена, файл не записан;
* ``3`` — требования не извлечены: Claude API недоступен либо ключ доступа отсутствует.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent.analyzers import register_default_analyzers
from agent.analyzers.vcs_git import collect_authors, git_available
from agent.config import Config, ConfigError, load_config
from agent.core import registry
from agent.core.exporter import build_document, write_document
from agent.core.identity import Identity, IdentityMap
from agent.core.ids import evidence_id, project_id, repository_id, slugify
from agent.core.model import AGENT_VERSION, Evidence, FactCollector
from agent.core.progress import Progress
from agent.core.registry import AnalyzerContext
from agent.core.validator import load_schema, validate_document

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INVALID = 2
EXIT_LLM_UNAVAILABLE = 3

DEFAULT_OUTPUT = "project-knowledge.json"
CACHE_DIRECTORY = ".agent-cache"
#: Шаблон имени версионированной выгрузки: метка времени прогона и slug проекта.
VERSIONED_NAME = "Version{timestamp}_{project}_export.json"
VERSIONED_GLOB = "Version*_export.json"
CORE_ANALYZER = "core"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "export":
        return _run_export(arguments)
    if arguments.command == "authors":
        return _run_authors(arguments)
    if arguments.command == "validate":
        return _run_validate(arguments)
    parser.print_usage(sys.stderr)
    return EXIT_USAGE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent", description="Сбор проектных знаний в файл project-knowledge.json"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="собрать знания о проекте в файл выгрузки")
    export.add_argument("path", help="путь к каталогу проекта")
    export.add_argument("--output", "-o", default=DEFAULT_OUTPUT, help="путь результата, - для stdout")
    export.add_argument(
        "--no-requirements", action="store_true", help="не обращаться к модели за требованиями"
    )
    export.add_argument("--config", default=None, help="путь к файлу конфигурации вместо agent.yml")
    export.add_argument(
        "--identity",
        action="append",
        default=[],
        metavar="ИМЯ=ПОЧТА,ПОЧТА",
        help="свести учётные записи в одного человека; флаг повторяется",
    )
    export.add_argument(
        "--versioned",
        action="store_true",
        help=(
            "именовать выгрузку по шаблону Version{ГГГГММДДЧЧММСС}_{проект}_export.json; "
            "--output задаёт каталог"
        ),
    )
    export.add_argument("--quiet", "-q", action="store_true", help="не выводить прогресс прогона")
    export.add_argument(
        "--author",
        action="append",
        default=[],
        metavar="ИМЯ_ИЛИ_ПОЧТА",
        help="оставить в истории только коммиты этого автора; флаг повторяется",
    )
    export.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="ШАБЛОН",
        help="исключить пути из обхода; флаг повторяется, значения дополняют конфигурацию",
    )

    authors = commands.add_parser("authors", help="перечень авторов истории без сборки выгрузки")
    authors.add_argument("path", help="путь к каталогу проекта")
    authors.add_argument("--config", default=None, help="путь к файлу конфигурации вместо agent.yml")
    authors.add_argument(
        "--identity",
        action="append",
        default=[],
        metavar="ИМЯ=ПОЧТА,ПОЧТА",
        help="свести учётные записи в одного человека; флаг повторяется",
    )
    authors.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="вид перечня: строки «коммиты имя <почта>» либо JSON с написаниями",
    )

    validate = commands.add_parser("validate", help="проверить готовый файл выгрузки")
    validate.add_argument("file", help="путь к файлу выгрузки")
    validate.add_argument("--schema", default=None, help="путь к схеме вместо поставляемой с агентом")
    return parser


def _run_export(arguments: argparse.Namespace) -> int:
    root = Path(arguments.path).expanduser()
    if not root.exists() or not root.is_dir():
        _error(f"каталог проекта недоступен: {root}")
        return EXIT_USAGE
    root = root.resolve()

    try:
        config = load_config(root, Path(arguments.config).expanduser() if arguments.config else None)
    except ConfigError as error:
        _error(str(error))
        return EXIT_USAGE
    if arguments.no_requirements:
        config.requirements_enabled = False
    config.exclude = [*config.exclude, *arguments.exclude]
    config.authors = [*config.authors, *arguments.author]
    try:
        config.identities = [*config.identities, *_parse_identity_flags(arguments.identity)]
    except ValueError as error:
        _error(f"флаг --identity: {error}")
        return EXIT_USAGE

    progress = Progress(enabled=not arguments.quiet)
    collected_at = _utc_now()
    collector = FactCollector()
    project = _collect_project(root, config, collector, collected_at)

    slug = slugify(_project_name(root, config))
    output = _resolve_output(arguments, slug, collected_at)
    if arguments.versioned:
        # Прежние версии выгрузки лежат рядом и не должны попадать в следующую.
        config.exclude = [*config.exclude, VERSIONED_GLOB]
    has_repository = (root / ".git").exists()
    context = AnalyzerContext(
        root=root,
        config=config,
        project_id=project,
        repository_slug=slug,
        collected_at=collected_at,
        has_repository=has_repository,
        repository_id=repository_id(slug) if has_repository else None,
        excluded_paths=_own_paths(root, output),
        progress=progress,
    )
    if not registry.analyzers():
        register_default_analyzers()
    for analyzer in registry.analyzers():
        analyzer.analyze(context, collector)

    progress.stage("сборка выгрузки")
    document = build_document(
        collector,
        generated_at=collected_at,
        filters={"authors": config.authors, "exclude": config.exclude},
    )

    try:
        schema = load_schema()
    except (OSError, json.JSONDecodeError) as error:
        _error(f"схема выгрузки недоступна: {error}")
        return EXIT_USAGE

    progress.stage("валидация выгрузки")
    errors = validate_document(document, schema)
    _report_warnings(collector.warnings)
    if errors:
        _report_errors(errors)
        return EXIT_INVALID

    progress.stage("запись файла")
    try:
        write_document(document, output)
    except OSError as error:
        _error(f"файл выгрузки не записан: {error}")
        return EXIT_USAGE
    progress.done(_summary(document, output))
    return EXIT_OK


def _run_authors(arguments: argparse.Namespace) -> int:
    """Вывести авторов истории: перечень для последующего отбора флагом ``--author``."""
    root = Path(arguments.path).expanduser()
    if not root.exists() or not root.is_dir():
        _error(f"каталог проекта недоступен: {root}")
        return EXIT_USAGE
    root = root.resolve()
    if not (root / ".git").exists():
        _error(f"каталог не является рабочей копией git: {root}")
        return EXIT_USAGE
    if not git_available():
        _error("исполняемый файл git недоступен")
        return EXIT_USAGE

    try:
        config = load_config(root, Path(arguments.config).expanduser() if arguments.config else None)
    except ConfigError as error:
        _error(str(error))
        return EXIT_USAGE
    try:
        identities = [*config.identities, *_parse_identity_flags(arguments.identity)]
    except ValueError as error:
        _error(f"флаг --identity: {error}")
        return EXIT_USAGE

    authors = collect_authors(root, IdentityMap(identities) if identities else None)
    if not authors:
        _error("история пуста: авторы не найдены")
        return EXIT_OK

    if arguments.format == "json":
        payload = [
            {
                "name": item.name,
                "email": item.email,
                "commits": item.commits,
                "aliases": [{"name": name, "email": email} for name, email in item.aliases],
            }
            for item in authors
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    width = max(len(str(item.commits)) for item in authors)
    for item in authors:
        print(f"{item.commits:>{width}}  {item.name} <{item.email}>")
    return EXIT_OK


def _run_validate(arguments: argparse.Namespace) -> int:
    path = Path(arguments.file).expanduser()
    try:
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as error:
        _error(f"файл выгрузки недоступен: {error}")
        return EXIT_USAGE
    except json.JSONDecodeError as error:
        _report_errors([f"$ — JSON — {error}"])
        return EXIT_INVALID

    try:
        schema = load_schema(Path(arguments.schema).expanduser() if arguments.schema else None)
    except (OSError, json.JSONDecodeError) as error:
        _error(f"схема выгрузки недоступна: {error}")
        return EXIT_USAGE

    errors = validate_document(document, schema)
    if errors:
        _report_errors(errors)
        return EXIT_INVALID
    return EXIT_OK


def _collect_project(root: Path, config: Config, collector: FactCollector, collected_at: str) -> str:
    """Создать сущность Project; её источник — корневой каталог рабочей копии."""
    name = _project_name(root, config)
    slug = slugify(name)
    identifier = project_id(slug)
    locator = str(config.source_path.name) if config.source_path else "."
    evidence = Evidence(
        id=evidence_id(identifier, "filesystem", None, locator, CORE_ANALYZER),
        subject_id=identifier,
        subject_type="project",
        source_kind="filesystem",
        analyzer=CORE_ANALYZER,
        analyzer_version=AGENT_VERSION,
        collected_at=collected_at,
        source_locator=locator,
    )
    data = {"name": name, "slug": slug, "root_path": str(root), "agent_version": AGENT_VERSION}
    if config.project_description:
        data["description"] = config.project_description
    collector.add_entity("project", identifier, data, evidence)
    return identifier


def _parse_identity_flags(values: list[str]) -> list[Identity]:
    """Разобрать флаги вида ``Имя=почта,почта`` в записи карты тождества."""
    identities: list[Identity] = []
    for value in values:
        name, separator, emails = value.partition("=")
        if not separator or not name.strip() or not emails.strip():
            raise ValueError(f"ожидается «Имя=почта,почта», получено {value!r}")
        identities.append(
            Identity(
                name=name.strip(),
                emails=tuple(item.strip() for item in emails.split(",") if item.strip()),
            )
        )
    return identities


def _resolve_output(arguments: argparse.Namespace, slug: str, collected_at: str) -> str:
    """Путь выгрузки: явный, либо версионированное имя в заданном каталоге.

    При ``--versioned`` значение ``--output`` понимается как каталог: имя файла формирует
    сам агент из метки времени прогона и slug проекта.
    """
    if not arguments.versioned:
        return arguments.output
    if arguments.output == "-":
        return "-"

    name = VERSIONED_NAME.format(timestamp=_timestamp(collected_at), project=slug)
    target = Path(arguments.output)
    if arguments.output == DEFAULT_OUTPUT:
        directory = Path.cwd()
    elif target.is_dir() or arguments.output.endswith("/"):
        directory = target
    else:
        directory = target.parent
    return str(directory / name)


def _timestamp(collected_at: str) -> str:
    """Метка времени имени файла: ГГГГММДДЧЧММСС из отметки прогона."""
    return "".join(character for character in collected_at if character.isdigit())


def _summary(document: dict, output: str) -> str:
    """Итоговая строка прогона: куда записано и что собрано."""
    counts = ", ".join(
        f"{key} {len(document[key])}"
        for key in ("commits", "people", "files", "directories", "relationships", "evidence")
        if document[key]
    )
    target = "стандартный вывод" if output == "-" else str(Path(output).resolve())
    return f"готово: {target} — {counts}" if counts else f"готово: {target}"


def _own_paths(root: Path, output: str) -> set[str]:
    """Пути собственных файлов прогона внутри проекта: выгрузка и каталог кэша."""
    own = {CACHE_DIRECTORY}
    if output == "-":
        return own
    try:
        resolved = Path(output).expanduser().resolve()
        own.add(resolved.relative_to(root).as_posix())
    except (OSError, ValueError):
        # Выгрузка лежит вне проекта: исключать нечего.
        pass
    return own


def _project_name(root: Path, config: Config) -> str:
    return config.project_name or root.name


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _report_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        _error(f"warning: {warning}")


def _report_errors(errors: list[str]) -> None:
    _error(f"валидация не пройдена, ошибок: {len(errors)}")
    for message in errors:
        _error(f"  {message}")


def _error(message: str) -> None:
    # Сообщение может содержать имя файла вне UTF-8: суррогаты заменяются, чтобы вывод не падал.
    print(message.encode("utf-8", "replace").decode("utf-8"), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
