"""Манифест прогона: состав результатов, их происхождение и полнота.

Манифест делает каталог прогона самодостаточным: по нему потребитель узнаёт, из чего
состоит результат, чем и когда он получен и что в нём неполно, не открывая ни одного
из остальных объектов. Перечень объектов формирует записывающая сторона — тот, кто их
создал, знает состав точно, тогда как обзор каталога зависит от его прежнего содержимого.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"

#: Тип содержимого по расширению объекта.
CONTENT_TYPES: dict[str, str] = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".gz": "application/gzip",
    ".err": "text/plain",
}

DEFAULT_CONTENT_TYPE = "application/octet-stream"


def describe_object(path: Path, base: Path, records: int | None = None) -> dict[str, Any]:
    """Описание одного объекта прогона: имя, размер, контрольная сумма, тип, число записей."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    description: dict[str, Any] = {
        "name": path.relative_to(base).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "content_type": CONTENT_TYPES.get(path.suffix, DEFAULT_CONTENT_TYPE),
    }
    if records is not None:
        description["records"] = records
    return description


def section_records(document: dict[str, Any]) -> dict[str, int]:
    """Число записей по секциям выгрузки: объём секции не выводится из размера объекта."""
    return {key: len(value) for key, value in document.items() if isinstance(value, list)}


def build_manifest(
    started_at: str,
    finished_at: str,
    agent_version: str,
    filters: dict[str, Any],
    objects: list[dict[str, Any]],
    analysis_scope: dict[str, Any],
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Собрать манифест прогона из описаний объектов и сведений о самом прогоне."""
    manifest: dict[str, Any] = {
        "started_at": started_at,
        "finished_at": finished_at,
        "agent_version": agent_version,
        "filters": filters,
        "objects": sorted(objects, key=lambda item: item["name"]),
    }
    if analysis_scope:
        manifest["analysis_scope"] = analysis_scope
    if provenance:
        manifest["provenance"] = provenance
    return manifest


def write_manifest(manifest: dict[str, Any], directory: Path) -> Path:
    """Записать манифест в каталог прогона и вернуть путь к нему."""
    path = directory / MANIFEST_NAME
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def load_run_metadata(path: Path | None) -> dict[str, Any]:
    """Прочитать сведения о прогоне, собранные обвязкой; недоступный файл даёт пустой набор."""
    if path is None or not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
