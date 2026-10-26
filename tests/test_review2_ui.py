"""Ревью 2, п.4 и п.7: фоновые разборы не держат выход; кнопки диалогов по-русски."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from konverter import paths
from konverter.strings import tr
from konverter.ui import main_window as main_window_module
from konverter.ui.main_window import MainWindow


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.setattr(paths, "app_root", lambda: app_dir)
    return app_dir


# --- п.4 ---------------------------------------------------------------------


def test_close_does_not_wait_for_hung_grouping(qtbot, monkeypatch, tmp_path):
    """Разбор файлов «завис» на 8 с: закрытие и уничтожение окна его не ждут."""
    release = threading.Event()
    entered = threading.Event()

    def hung_build_groups(paths_, detect_func=None):
        entered.set()
        release.wait(8)  # «зависшее» чтение файла
        raise RuntimeError("не должно никуда дойти")

    monkeypatch.setattr(main_window_module, "build_groups", hung_build_groups)
    window = MainWindow(warm_up_detector=False)
    try:
        window.add_paths([tmp_path / "файл.png"])
        assert entered.wait(5)
        started = time.monotonic()
        assert window.close() is True
        window.deleteLater()
        window = None
        from PySide6.QtCore import QCoreApplication, QEvent

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qtbot.wait(10)
        elapsed = time.monotonic() - started
        assert elapsed < 1.5, elapsed
    finally:
        release.set()


def test_background_task_threads_are_daemons(qtbot, monkeypatch, tmp_path):
    names: list[threading.Thread] = []
    release = threading.Event()

    def hung_build_groups(paths_, detect_func=None):
        names.append(threading.current_thread())
        release.wait(5)
        raise RuntimeError("стоп")

    monkeypatch.setattr(main_window_module, "build_groups", hung_build_groups)
    window = MainWindow(warm_up_detector=False)
    qtbot.addWidget(window)
    try:
        window.add_paths([tmp_path / "a.png"])
        qtbot.waitUntil(lambda: bool(names), timeout=5000)
        assert names[0].daemon
        assert names[0] is not threading.main_thread()
    finally:
        release.set()
        window.shutdown()


def test_background_result_delivered_in_main_thread(qtbot):
    from konverter.ui.bridge import BackgroundTask

    got: list[tuple] = []

    class Receiver(main_window_module.QObject):
        def on_done(self, task, result, error):
            got.append((threading.current_thread() is threading.main_thread(), result, error))

    receiver = Receiver()
    task = BackgroundTask(lambda: 42, receiver.on_done)
    task.start()
    qtbot.waitUntil(lambda: bool(got), timeout=5000)
    assert got == [(True, 42, None)]


# --- п.7 ---------------------------------------------------------------------


def test_close_dialog_buttons_in_russian(qtbot, monkeypatch):
    """Диалог «Закрыть программу?»: кнопки-действия по-русски (а не Yes/No)."""
    seen: list[list[str]] = []

    def fake_exec(box: QMessageBox) -> int:
        seen.append([b.text() for b in box.buttons()])
        # Пользователь нажал «Продолжить работу».
        for button in box.buttons():
            if button.text() == tr("confirm_close_no"):
                button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    window = MainWindow(warm_up_detector=False)
    qtbot.addWidget(window)
    monkeypatch.setattr(window, "active_jobs", lambda: [object()])
    try:
        assert window.close() is False
        assert len(seen) == 1
        assert sorted(seen[0]) == sorted(["&Отменить задачи и закрыть", "&Продолжить работу"])
    finally:
        window.shutdown()


def test_ask_yes_no_returns_choice(qtbot, monkeypatch):
    from konverter.ui.common import ask_yes_no

    def clicker(text):
        def fake_exec(box: QMessageBox) -> int:
            for button in box.buttons():
                if button.text() == text:
                    button.click()
            return 0
        return fake_exec

    monkeypatch.setattr(QMessageBox, "exec", clicker("Да"))
    assert ask_yes_no(None, "Заголовок", "Текст") is True
    monkeypatch.setattr(QMessageBox, "exec", clicker("Нет"))
    assert ask_yes_no(None, "Заголовок", "Текст") is False
    # Закрыли крестиком / Esc — ничего не нажато → «Нет».
    monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)
    assert ask_yes_no(None, "Заголовок", "Текст") is False


def test_standard_button_labels_in_strings():
    for key, text in (("button_yes", "Да"), ("button_no", "Нет"),
                      ("button_ok", "ОК"), ("button_cancel", "Отмена")):
        assert tr(key) == text
