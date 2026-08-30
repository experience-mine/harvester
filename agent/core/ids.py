"""Правила формирования идентификаторов сущностей, связей и подтверждений.

Идентификатор имеет вид ``{тип}:{ключ}``. Ключ вычисляется из содержания сущности,
поэтому не зависит ни от порядка обхода, ни от времени прогона: повторный запуск
на том же состоянии рабочей копии даёт те же идентификаторы.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(value: str) -> str:
    """Привести имя к виду ``[a-z0-9-]``: нижний регистр, транслитерация, дефисы."""
    lowered = unicodedata.normalize("NFKC", value).strip().lower()
    transliterated = "".join(_TRANSLIT.get(char, char) for char in lowered)
    ascii_only = (
        unicodedata.normalize("NFKD", transliterated)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = _NON_SLUG.sub("-", ascii_only).strip("-")
    return slug or "unnamed"


def normalize_text(value: str) -> str:
    """Нормализовать текст перед хэшированием: регистр, пробелы, края строки."""
    return _WHITESPACE.sub(" ", value.strip().lower())


def sha16(*parts: object) -> str:
    """Первые 16 символов sha256 от конкатенации частей через вертикальную черту."""
    payload = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def project_id(slug: str) -> str:
    return f"project:{slug}"


def repository_id(slug: str) -> str:
    return f"repository:{slug}"


def branch_id(repository_slug: str, name: str) -> str:
    return f"branch:{repository_slug}/{name}"


def tag_id(repository_slug: str, name: str) -> str:
    return f"tag:{repository_slug}/{name}"


def commit_id(sha: str) -> str:
    return f"commit:{sha}"


def person_id(email: str, name: str = "") -> str:
    """Ключ участника — адрес в нижнем регистре; без адреса — slug имени."""
    if email.strip():
        return f"person:{email.strip().lower()}"
    return f"person:name/{slugify(name)}"


def directory_id(repository_slug: str, path: str) -> str:
    return f"directory:{repository_slug}/{path}"


def file_id(repository_slug: str, path: str) -> str:
    return f"file:{repository_slug}/{path}"


def technology_id(slug: str) -> str:
    return f"technology:{slug}"


def dependency_id(ecosystem: str, name: str) -> str:
    return f"dependency:{ecosystem}/{name.strip().lower()}"


def migration_id(repository_slug: str, path: str) -> str:
    return f"migration:{repository_slug}/{path}"


def database_object_id(kind: str, name: str) -> str:
    return f"database_object:{kind}/{name.strip().lower()}"


def document_id(repository_slug: str, path: str) -> str:
    return f"document:{repository_slug}/{path}"


def requirement_id(statement: str) -> str:
    return f"requirement:{sha16(normalize_text(statement))}"


def relationship_id(from_id: str, relationship_type: str, to_id: str) -> str:
    return f"relationship:{sha16(from_id, relationship_type, to_id)}"


def evidence_id(
    subject_id: str,
    source_kind: str,
    source_file_id: str | None,
    source_locator: str | None,
    analyzer: str,
) -> str:
    return f"evidence:{sha16(subject_id, source_kind, source_file_id, source_locator, analyzer)}"


def entity_type_of(identifier: str) -> str:
    """Тип сущности по её идентификатору: часть до первого двоеточия."""
    return identifier.split(":", 1)[0]


def code_unit_id(repository_slug: str, path: str, qualified_name: str) -> str:
    """Ключ определения кода — файл и квалифицированное имя внутри него.

    Квалифицированное имя приходит от анализатора в форме ``Класс.метод`` для члена
    класса и ``Имя`` для самостоятельного определения, поэтому два определения с одним
    именем в разных файлах не сливаются, а одноимённые члены разных классов различаются.
    """
    return f"code_unit:{repository_slug}/{path}:{qualified_name}"


def finding_id(kind: str, subject_id: str, analyzer: str) -> str:
    """Ключ находки — вид суждения, его предмет и вынесший суждение анализатор.

    Порядковый номер находки в ключ не входит: он зависит от порядка обхода и менялся бы
    от прогона к прогону при неизменном содержании проекта.
    """
    return f"finding:{sha16(kind, subject_id, analyzer)}"


def filters_fingerprint(authors: list[str], exclude: list[str]) -> str:
    """Отпечаток применённой выборки: по нему различаются прогоны одного проекта.

    Порядок перечисления на отпечаток не влияет: наборы приводятся к упорядоченному виду,
    поэтому одна и та же выборка, записанная иначе, даёт то же значение. Отпечаток
    различает прогоны в имени каталога; полный состав фильтров хранится в самой выгрузке.
    """
    payload = "|".join(sorted(authors)) + "||" + "|".join(sorted(exclude))
    return sha16(payload)[:8]
