"""Чтение файла конфигурации ``agent.yml``.

Файл необязателен: без него применяются значения по умолчанию, а имя проекта берётся
из имени корневого каталога. Неизвестный ключ конфигурации — ошибка запуска.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.core.identity import Identity, parse_identities

DEFAULT_CONFIG_NAME = "agent.yml"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_DOCUMENT_CHARS = 100_000

_ROOT_KEYS = {"project", "exclude", "technologies", "requirements", "history", "identities"}
_HISTORY_KEYS = {"authors"}
_PROJECT_KEYS = {"name", "description"}
_TECHNOLOGIES_KEYS = {"dictionary"}
_REQUIREMENTS_KEYS = {"enabled", "model", "max_document_chars"}


class ConfigError(Exception):
    """Ошибка конфигурации: неизвестный ключ, неверный тип значения, нечитаемый файл."""


@dataclass
class Config:
    """Значения конфигурации, применённые к прогону."""

    project_name: str | None = None
    project_description: str | None = None
    exclude: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    identities: list[Identity] = field(default_factory=list)
    technologies_dictionary: str | None = None
    requirements_enabled: bool = True
    requirements_model: str = DEFAULT_MODEL
    requirements_max_document_chars: int = DEFAULT_MAX_DOCUMENT_CHARS
    source_path: Path | None = None
    code_analysis_path: Path | None = None


def load_config(root: Path, explicit_path: Path | None = None) -> Config:
    """Загрузить конфигурацию: явный путь, иначе ``agent.yml`` в корне, иначе умолчания."""
    path = explicit_path or (root / DEFAULT_CONFIG_NAME)
    if explicit_path is not None and not path.exists():
        raise ConfigError(f"файл конфигурации не найден: {path}")
    if not path.exists():
        return Config()

    raw = _read_yaml(path)
    if not isinstance(raw, dict):
        raise ConfigError(f"конфигурация должна быть отображением ключей: {path}")

    _reject_unknown(raw, _ROOT_KEYS, "корень конфигурации")
    project = _section(raw, "project", _PROJECT_KEYS)
    technologies = _section(raw, "technologies", _TECHNOLOGIES_KEYS)
    requirements = _section(raw, "requirements", _REQUIREMENTS_KEYS)

    exclude = raw.get("exclude", [])
    if not isinstance(exclude, list) or any(not isinstance(item, str) for item in exclude):
        raise ConfigError("ключ exclude должен быть списком строк")

    try:
        identities = parse_identities(raw.get("identities"))
    except ValueError as error:
        raise ConfigError(f"ключ identities: {error}") from error

    history = _section(raw, "history", _HISTORY_KEYS)
    authors = history.get("authors", [])
    if authors is None:
        authors = []
    if not isinstance(authors, list) or any(not isinstance(item, str) for item in authors):
        raise ConfigError("ключ history.authors должен быть списком строк")

    enabled = requirements.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("ключ requirements.enabled должен быть логическим значением")

    max_chars = requirements.get("max_document_chars", DEFAULT_MAX_DOCUMENT_CHARS)
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ConfigError("ключ requirements.max_document_chars должен быть целым числом больше нуля")

    return Config(
        project_name=project.get("name"),
        project_description=project.get("description"),
        exclude=list(exclude),
        authors=list(authors),
        identities=identities,
        technologies_dictionary=technologies.get("dictionary"),
        requirements_enabled=enabled,
        requirements_model=str(requirements.get("model", DEFAULT_MODEL)),
        requirements_max_document_chars=max_chars,
        source_path=path,
    )


def _section(raw: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    section = raw.get(name, {})
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError(f"ключ {name} должен быть отображением ключей")
    _reject_unknown(section, allowed, f"секция {name}")
    return section


def _reject_unknown(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"неизвестные ключи конфигурации в {where}: {', '.join(unknown)}")


def _read_yaml(path: Path) -> Any:
    """Прочитать конфигурацию: PyYAML, если он установлен, иначе встроенный разбор.

    Встроенный разбор покрывает ровно ту форму, которую описывает конфигурация агента:
    ключ со значением, секция с вложенными ключами и список строк. Остальной YAML —
    якоря, многострочные блоки, вложенные списки — требует PyYAML.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"файл конфигурации нечитаем: {path}: {error}") from error

    try:
        import yaml
    except ImportError:
        return _parse_simple_yaml(raw, path)

    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise ConfigError(f"файл конфигурации не разобран: {path}: {error}") from error


def _parse_simple_yaml(raw: str, path: Path) -> dict[str, Any]:
    """Разбор подмножества YAML: ключи верхнего уровня, одна вложенная секция, списки строк."""
    document: dict[str, Any] = {}
    section: str | None = None
    nested: str | None = None

    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if stripped.startswith("- "):
            target = document.get(section) if section else None
            if isinstance(target, dict) and nested and isinstance(target.get(nested), list):
                target[nested].append(_scalar(stripped[2:]))
                continue
            if section is None or not isinstance(target, list):
                raise ConfigError(f"{path}, строка {number}: элемент списка вне списка")
            document[section].append(_scalar(stripped[2:]))
            continue

        if ":" not in stripped:
            raise ConfigError(f"{path}, строка {number}: ожидается «ключ: значение»")
        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()

        if indent == 0:
            nested = None
            if value:
                document[key] = _scalar(value)
                section = None
            else:
                document[key] = []
                section = key
        else:
            if section is None:
                raise ConfigError(f"{path}, строка {number}: вложенный ключ вне секции")
            if isinstance(document.get(section), list) and not document[section]:
                document[section] = {}
            if not isinstance(document.get(section), dict):
                raise ConfigError(f"{path}, строка {number}: секция {section} уже содержит список")
            if value:
                document[section][key] = _scalar(value)
                nested = None
            else:
                document[section][key] = []
                nested = key

    return {key: value for key, value in document.items()}


def _scalar(value: str) -> Any:
    """Скаляр YAML: строка в кавычках, логическое значение, целое число либо строка."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    lowered = text.lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    if lowered in {"null", "~", ""}:
        return None
    if text.lstrip("-").isdigit():
        return int(text)
    return text
