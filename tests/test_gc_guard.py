"""Сборка циклического мусора только в главном потоке (konverter/ui/gc_guard.py).

Авария, которую это предотвращает: автоматический сборщик срабатывал в
фоновом потоке (запуск дочернего процесса spawn) и удалял там объект Qt,
попавший в цикл ссылок, — access violation в тестах окна «Фрагмент для GIF».
Тесты ниже проверяют сам механизм детерминированно: объекты Qt в циклах
создаются, фоновые потоки активно выделяют память (с включённым
автоматическим сборщиком это запускало бы сборку в них), а объекты должны
удаляться только в главном потоке.
"""

from __future__ import annotations

import gc
import logging
import threading
import time
import weakref

import pytest
from PySide6.QtCore import QObject, Qt

from konverter.ui import gc_guard
from konverter.ui.bridge import BackgroundTask


class _Cyclic(QObject):
    """QObject в цикле ссылок с самим собой (освобождается только сборщиком)."""

    def __init__(self, deaths: list) -> None:
        super().__init__()
        self.me = self
        self.destroyed.connect(
            lambda *_: deaths.append(threading.current_thread() is threading.main_thread()),
            Qt.ConnectionType.DirectConnection,
        )


def _churn(seconds: float, stop: threading.Event) -> None:
    """Фоновый поток: много циклического мусора — повод для автоматической сборки."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and not stop.is_set():
        for _ in range(2000):
            a: dict = {}
            a["self"] = a
        time.sleep(0.002)  # отдать GIL главному потоку


def test_guard_installed_with_qapp(qapp):
    assert gc_guard.is_installed()
    assert not gc.isenabled()  # автоматическая сборка выключена
    assert gc_guard._guard.timer.isActive()
    assert gc_guard._guard.timer.thread() is qapp.thread()


def test_cyclic_qt_objects_destroyed_only_in_main_thread(qtbot):
    deaths: list[bool] = []
    refs = []
    for _ in range(50):
        obj = _Cyclic(deaths)
        refs.append(weakref.ref(obj))
        del obj
    stop = threading.Event()
    workers = [threading.Thread(target=_churn, args=(0.5, stop)) for _ in range(3)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    # Фоновые потоки выделили много памяти, но ничего не собрали.
    assert deaths == []
    assert all(ref() is not None for ref in refs)
    # Таймер в главном потоке собирает.
    qtbot.waitUntil(lambda: len(deaths) == 50, timeout=gc_guard.INTERVAL_MS * 5)
    assert all(deaths)


def test_collect_if_needed_only_in_main_thread():
    result: list = []
    worker = threading.Thread(target=lambda: result.append(gc_guard.collect_if_needed()))
    worker.start()
    worker.join()
    assert result == [None]


@pytest.mark.stress
def test_stress_background_tasks_with_errors_and_cycles(qtbot, monkeypatch):
    """Стресс (≤ ~10 с): сотни фоновых задач со сбоями (traceback-циклы),
    параллельно — объекты Qt в циклах и фоновое выделение памяти. Все объекты
    Qt удаляются только в главном потоке, процесс не падает."""
    deaths: list[bool] = []
    done: list[int] = []

    class Receiver(QObject):
        def on_done(self, task, result, error) -> None:
            done.append(1)

    receiver = Receiver()
    # Сотни трассировок «Сбой фоновой задачи» в журнале только замедляли бы тест.
    monkeypatch.setattr(logging.getLogger("konverter.ui.bridge"), "disabled", True)
    stop = threading.Event()
    churner = threading.Thread(target=_churn, args=(8.0, stop), daemon=True)
    churner.start()
    tasks = []
    started = time.monotonic()
    try:
        for index in range(300):
            if index % 3 == 0:
                _Cyclic(deaths)  # сразу мусор (цикл)
            task = BackgroundTask(lambda: 1 / 0, receiver.on_done)
            tasks.append(task)
            task.start()
            if index % 25 == 0:
                qtbot.wait(10)
        qtbot.waitUntil(lambda: len(done) == 300, timeout=15000)
        tasks.clear()
        qtbot.waitUntil(lambda: len(deaths) == 100, timeout=gc_guard.INTERVAL_MS * 5)
    finally:
        stop.set()
        churner.join(10)
    assert all(deaths)
    assert time.monotonic() - started < 20
