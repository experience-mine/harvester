"""Карта тождества участников: несколько учётных записей — один человек.

Нормализация по адресу почты сводит только полные совпадения, поэтому один человек,
коммитивший с рабочего и личного адреса, попадает в выгрузку несколькими сущностями.
Карта задаёт явное соответствие «канонический человек — его адреса и написания имени».
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Identity:
    """Один человек: каноническое имя, канонический адрес и все известные написания."""

    name: str
    emails: tuple[str, ...]
    names: tuple[str, ...] = field(default=())

    @property
    def primary_email(self) -> str:
        """Адрес, под которым человек попадает в выгрузку."""
        return self.emails[0] if self.emails else ""


class IdentityMap:
    """Разрешение встреченной пары «имя, адрес» в каноническую личность."""

    def __init__(self, identities: list[Identity] | None = None) -> None:
        self._identities = list(identities or [])
        self._by_email: dict[str, Identity] = {}
        self._by_name: dict[str, Identity] = {}
        for identity in self._identities:
            for email in identity.emails:
                self._by_email[email.strip().lower()] = identity
            for name in (identity.name, *identity.names):
                self._by_name[name.strip().lower()] = identity

    def __bool__(self) -> bool:
        return bool(self._identities)

    def resolve(self, name: str, email: str) -> Identity | None:
        """Найти личность по адресу, иначе по написанию имени; иначе ``None``.

        Адрес сравнивается целиком, имя — подстрокой: в карте удобно писать короткое имя
        личности (``Alexey``), тогда как в истории стоит полное написание
        (``Gaibovich Alexey``). Обратная сторона — слишком короткое имя в карте сводит
        и посторонних однофамильцев, поэтому надёжнее перечислять адреса.
        """
        identity = self._by_email.get(email.strip().lower())
        if identity is not None:
            return identity

        needle = name.strip().lower()
        if not needle:
            return None
        exact = self._by_name.get(needle)
        if exact is not None:
            return exact
        for known, identity in self._by_name.items():
            if known and known in needle:
                return identity
        return None

    def expand(self, pattern: str) -> list[str]:
        """Раскрыть образец отбора в перечень адресов и имён той же личности.

        Отбор по одному адресу человека обязан находить и остальные его учётные записи,
        иначе карта тождества не влияла бы на выборку истории.
        """
        needle = pattern.strip().lower()
        for identity in self._identities:
            haystack = [item.lower() for item in (identity.name, *identity.names, *identity.emails)]
            if any(needle in item for item in haystack):
                return [*identity.emails, identity.name, *identity.names]
        return [pattern]


def parse_identities(raw: object) -> list[Identity]:
    """Разобрать карту тождества из конфигурации.

    Поддерживаются две формы: отображение «имя: адреса через запятую» и список записей
    с ключами ``name``, ``emails`` и необязательным ``names``.
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [Identity(name=name, emails=tuple(_as_items(value))) for name, value in raw.items()]
    if isinstance(raw, list):
        identities = []
        for item in raw:
            if not isinstance(item, dict) or "name" not in item:
                raise ValueError("запись карты тождества требует ключ name")
            identities.append(
                Identity(
                    name=str(item["name"]),
                    emails=tuple(_as_items(item.get("emails"))),
                    names=tuple(_as_items(item.get("names"))),
                )
            )
        return identities
    raise ValueError("карта тождества должна быть отображением либо списком записей")


def _as_items(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("перечень адресов задаётся строкой через запятую либо списком строк")
