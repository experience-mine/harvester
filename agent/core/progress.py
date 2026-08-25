"""Вывод прогресса прогона в стандартный поток ошибок.

Файл выгрузки записывается одной операцией в конце, поэтому на большом проекте
без прогресса непонятно, идёт ли работа. Сообщения печатаются в поток ошибок,
чтобы не смешиваться с выгрузкой при ``--output -``.
"""

from __future__ import annotations

import sys
import time


class Progress:
    """Этапы и счётчики прогона; при ``enabled=False`` не печатает ничего."""

    def __init__(self, enabled: bool = True, interval: float = 1.0, stream=None) -> None:
        self.enabled = enabled
        self.interval = interval
        self._stream = stream or sys.stderr
        self._started = time.monotonic()
        self._last = 0.0
        self._stage = ""

    def stage(self, message: str) -> None:
        """Объявить новый этап прогона."""
        self._stage = message
        self._last = 0.0
        self._write(message)

    def tick(self, done: int, total: int | None = None, unit: str = "") -> None:
        """Сообщить о продвижении внутри этапа не чаще, чем раз в ``interval`` секунд."""
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self._last < self.interval:
            return
        self._last = now
        share = f" из {total}" if total else ""
        self._write(f"{self._stage}: {done}{share} {unit}".rstrip())

    def done(self, message: str) -> None:
        """Завершить этап итоговой строкой."""
        self._last = 0.0
        self._write(message)

    def _write(self, message: str) -> None:
        if not self.enabled:
            return
        elapsed = time.monotonic() - self._started
        text = f"[{elapsed:6.1f}s] {message}"
        print(text.encode("utf-8", "replace").decode("utf-8"), file=self._stream, flush=True)
