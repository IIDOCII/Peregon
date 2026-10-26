"""Сборка циклического мусора только в главном потоке Qt.

Почему. Автоматический сборщик циклического мусора CPython запускается в
том потоке, который в этот момент выделяет память, — например в фоновом
потоке `BackgroundTask`, запускающем дочерний процесс (multiprocessing
spawn). Если в цикле ссылок оказалась Python-обёртка живого объекта Qt
(QObject, QPixmap…), которой владеет Python, сборщик удаляет C++-объект в
этом чужом потоке, пока главный поток работает с ним же или с его
событиями, — авария access violation (так падали тесты окна «Фрагмент для
GIF»). Отдельные циклы можно разорвать, но гарантии, что новых не появится,
нет.

Как (стандартный приём для PySide/PyQt). При старте приложения
автоматическая сборка выключается (`gc.disable()`), а её работу делает
таймер в главном потоке: раз в `INTERVAL_MS` он смотрит счётчики
`gc.get_count()` и, как это сделал бы сам сборщик, собирает нужное
поколение (`gc.collect(n)`). Плюс полная сборка при выходе. Подсчёт ссылок
(основной способ освобождения памяти в CPython) работает как обычно —
отложена только сборка циклов, не дольше чем на `INTERVAL_MS`. Таймер
срабатывает и во вложенных циклах событий модальных окон.

Дочерние процессы (декодирование, предпросмотр) — отдельные интерпретаторы
без Qt; их это не касается: в них сборщик включён как обычно.
"""

from __future__ import annotations

import gc
import threading

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

# Как часто проверять счётчики сборщика, мс.
INTERVAL_MS = 1000

_guard: "_MainThreadGc | None" = None


def collect_if_needed() -> int | None:
    """Сборка поколения, которое собрал бы автоматический сборщик (только
    в главном потоке). Возвращает собранное поколение или None."""
    if threading.current_thread() is not threading.main_thread():
        return None
    count0, count1, count2 = gc.get_count()
    threshold0, threshold1, threshold2 = gc.get_threshold()
    if threshold0 <= 0 or count0 < threshold0:
        return None
    generation = 0
    if count1 >= threshold1:
        generation = 1
        if count2 >= threshold2:
            generation = 2
    gc.collect(generation)
    return generation


def collect_now() -> None:
    """Полная сборка (выход из программы, конец теста) — только в главном потоке."""
    if threading.current_thread() is threading.main_thread():
        gc.collect()


class _MainThreadGc(QObject):
    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self.timer = QTimer(self)
        self.timer.setInterval(INTERVAL_MS)
        self.timer.timeout.connect(collect_if_needed)
        self.timer.start()


def install(app: QApplication | None = None) -> None:
    """Выключает автоматическую сборку и ставит таймер в главном потоке (один раз).

    Вызывается из главного потока после создания QApplication.
    """
    global _guard
    app = app or QApplication.instance()
    if app is None or _guard is not None:
        return
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("gc_guard.install() — только из главного потока")
    gc.disable()
    _guard = _MainThreadGc(app)
    app.aboutToQuit.connect(collect_now)


def is_installed() -> bool:
    return _guard is not None
