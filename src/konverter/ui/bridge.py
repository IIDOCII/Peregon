"""Связь фоновых потоков с главным потоком Qt.

- `JobBridge` переправляет уведомления `JobManager.on_update` (они приходят
  из рабочих потоков) в главный поток сигналом с `QueuedConnection`.
- `BackgroundTask` выполняет функцию в потоке-демоне и сообщает результат
  (или исключение) сигналом в главный поток.

Если окно уже уничтожено, а фоновый поток ещё работает, испускание сигнала
у удалённого объекта даёт RuntimeError — она здесь глушится: результат
никому уже не нужен.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, Signal

from konverter.core.jobs import Job

log = logging.getLogger(__name__)


class JobBridge(QObject):
    """Переправляет обновления задач в главный поток."""

    # Аргумент — снимок задачи (`Job.snapshot()`), безопасный для чтения.
    job_updated = Signal(object)

    def connect_to(self, slot: Callable[[Job], None]) -> None:
        """Подключает обработчик; он всегда выполняется в главном потоке."""
        self.job_updated.connect(slot, Qt.ConnectionType.QueuedConnection)

    def notify(self, job: Job) -> None:
        """Колбэк для `JobManager(on_update=...)`; вызывается из любого потока."""
        try:
            self.job_updated.emit(job.snapshot())
        except RuntimeError:
            # Объект Qt уже удалён (окно закрыто) — уведомлять некого.
            pass


class _TaskSignals(QObject):
    # (задача, результат, исключение); из результата и исключения одно — None.
    finished = Signal(object, object, object)


class BackgroundTask:
    """Фоновая работа в потоке-демоне: выполняет `func()` и сообщает итог сигналом.

    Поток — `threading.Thread(daemon=True)`, а не `QThreadPool`: деструктор
    пула ждёт ВСЕ задачи без предела, и зависшее чтение файла держало бы
    выход из программы (ревью 2, п.4). Поток-демон не мешает завершению
    процесса.

    Объект сигналов создаётся в главном потоке (при создании задачи), поэтому
    обработчик `on_done(task, result, error)` выполняется в главном потоке
    (`QueuedConnection`; обработчик должен быть методом объекта Qt из главного
    потока). Вызывающий хранит ссылку на задачу, пока не придёт `on_done`.
    `slots` — необязательный семафор: ограничивает число одновременно
    работающих задач (поток ждёт свободного места до запуска `func`).
    """

    def __init__(
        self,
        func: Callable[[], Any],
        on_done: Callable[["BackgroundTask", Any, BaseException | None], None] | None = None,
        *,
        slots: threading.Semaphore | None = None,
        name: str = "konverter-background",
    ) -> None:
        self._func = func
        self._slots = slots
        self._name = name
        self.thread: threading.Thread | None = None
        self.signals = _TaskSignals()
        if on_done is not None:
            self.signals.finished.connect(on_done, Qt.ConnectionType.QueuedConnection)
        # Объект сигналов удаляется в главном потоке после доставки итога (сразу
        # за on_done), а не там, где пропала последняя ссылка на задачу: иначе
        # C++-объект Qt мог бы удаляться в фоновом потоке.
        self.signals.finished.connect(
            self.signals.deleteLater, Qt.ConnectionType.QueuedConnection
        )

    def start(self) -> None:
        """Запускает задачу в новом потоке-демоне."""
        self.thread = threading.Thread(target=self.run, name=self._name, daemon=True)
        self.thread.start()

    def run(self) -> None:  # выполняется в фоновом потоке
        result: Any = None
        error: BaseException | None = None
        if self._slots is not None:
            self._slots.acquire()
        try:
            result = self._func()
        except Exception as exc:  # noqa: BLE001 — сбой фоновой работы не должен ронять программу
            log.exception("Сбой фоновой задачи")
            error = exc
        finally:
            if self._slots is not None:
                self._slots.release()
        try:
            self.signals.finished.emit(self, result, error)
        except RuntimeError:
            pass  # объект сигналов уже удалён — результат никому не нужен
        finally:
            # Разрыв цикла «исключение → traceback → этот кадр → error»: иначе
            # кадр с `self` (задача и её объект Qt) — циклический мусор, и сборщик
            # мусора может удалить объект Qt в любом потоке (аварии access
            # violation в дочернем процессе spawn). Без цикла всё освобождается
            # подсчётом ссылок.
            error = None
            result = None
