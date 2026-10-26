"""Общий бюджет мегапикселей на одновременно декодируемые изображения.

Зачем: пик памяти одной картинки 14000×14000 RGBA при перекодировании —
несколько гигабайт, а задач изображений выполняется N = ядра/2 параллельно.
Без общего ограничения несколько больших картинок одновременно приводят
к нехватке памяти. Поэтому каждая задача ПОСЛЕ чтения размеров (до
декодирования пикселей) резервирует в бюджете число пикселей, а по
окончании работы с ними — освобождает (контекстный менеджер `reserve`).

Правила:
- сумма резерваций всех потоков процесса не превышает `limit`;
- запрос больше `limit` урезается до `limit`: такая картинка ждёт, пока
  остальные освободят бюджет, и дальше работает одна;
- очередь честная (FIFO): большой запрос не «голодает», пока мелкие
  приходят один за другим; следующие в очереди ждут, даже если сами
  поместились бы;
- ожидание периодически проверяет отмену задачи (`CancelToken`) и при
  отмене бросает `Cancelled`, не оставляя за собой резервации.

Потокобезопасно (`threading.Condition`).
"""

from __future__ import annotations

import threading
from collections import deque
from contextlib import contextmanager
from typing import Iterator

from konverter.converters.base import CancelToken, Cancelled

# Бюджет по умолчанию на все одновременно декодируемые изображения, пикселей.
DEFAULT_LIMIT = 200_000_000

# Как часто ожидающий поток проверяет отмену, с.
POLL_INTERVAL = 0.1


class PixelBudget:
    """Бюджет пикселей с честной очередью и проверкой отмены."""

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        if limit <= 0:
            raise ValueError("limit должен быть положительным")
        self.limit = int(limit)
        self._used = 0
        self._cond = threading.Condition()
        self._queue: deque[object] = deque()

    @property
    def used(self) -> int:
        """Сколько пикселей сейчас зарезервировано (для тестов и логов)."""
        with self._cond:
            return self._used

    def acquire(self, pixels: int, cancel: CancelToken | None = None) -> int:
        """Резервирует `pixels` (не больше `limit`); возвращает зарезервированное.

        Ждёт своей очереди и свободного места; при отмене — `Cancelled`.
        Возвращённое значение нужно передать в `release`.
        """
        amount = max(0, min(int(pixels), self.limit))
        if amount == 0:
            return 0
        ticket = object()
        with self._cond:
            self._queue.append(ticket)
            try:
                while not (
                    self._queue[0] is ticket and self._used + amount <= self.limit
                ):
                    if cancel is not None and cancel.cancelled:
                        raise Cancelled()
                    self._cond.wait(POLL_INTERVAL)
            except BaseException:
                self._queue.remove(ticket)
                self._cond.notify_all()  # следующий в очереди мог стать первым
                raise
            self._queue.popleft()
            self._used += amount
            self._cond.notify_all()  # следующий в очереди тоже может поместиться
        return amount

    def release(self, amount: int) -> None:
        """Возвращает зарезервированное `acquire` в бюджет."""
        if amount <= 0:
            return
        with self._cond:
            self._used = max(0, self._used - amount)
            self._cond.notify_all()

    @contextmanager
    def reserve(self, pixels: int, cancel: CancelToken | None = None) -> Iterator[int]:
        """`with budget.reserve(w * h, cancel): …` — резервация на время блока."""
        amount = self.acquire(pixels, cancel)
        try:
            yield amount
        finally:
            self.release(amount)


# Общий бюджет процесса для конвертера изображений. Код берёт его через
# атрибут модуля (`budget.IMAGE_BUDGET`), поэтому в тестах его можно подменить.
IMAGE_BUDGET = PixelBudget(DEFAULT_LIMIT)
