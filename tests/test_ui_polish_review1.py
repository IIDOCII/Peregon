"""Оформление, исправления ревью 1: буфер обмена без сетевых путей, прерывание
«Конвертировать всё», кавычки в команде Проводника, итог партии (всё отменено,
вопрос о закрытии), значок уведомления при выходе."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import QFileDialog, QMessageBox

from konverter.core import jobs as core_jobs
from konverter.core.jobs import JobStatus
from konverter.strings import tr
from konverter.ui import job_list as job_list_module
from konverter.ui import notify
from konverter.ui.drop_zone import MAX_CLIPBOARD_LINE, MAX_CLIPBOARD_LINES, paths_from_clipboard
from konverter.ui.gif_fragment import CANCEL as GIF_CANCEL
from konverter.ui.job_list import explorer_command
from konverter.ui.main_window import _Batch

from test_ui_polish import (  # noqa: F401 — фикстуры и помощники
    _image_group,
    _job,
    _png,
    files_dir,
    isolated_data,
    window,
)


# --- Буфер обмена: сетевые пути не трогаются ---------------------------------


@pytest.fixture
def fs_spy(monkeypatch):
    """Записывает все обращения к файловой системе (Path.exists, os.stat)."""
    touched: list[str] = []
    real_exists, real_stat = Path.exists, os.stat

    def exists(self, *args, **kwargs):
        touched.append(str(self))
        return real_exists(self, *args, **kwargs)

    def stat(path, *args, **kwargs):
        touched.append(str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(os, "stat", stat)
    return touched


def _text_mime(text: str) -> QMimeData:
    mime = QMimeData()
    mime.setText(text)
    return mime


def test_clipboard_text_never_touches_network_paths(fs_spy, tmp_path):
    local = _png(tmp_path / "свой.png")
    lines = [
        r"\\attacker\share\x.png",
        "//attacker/share/x.png",
        r"\\?\UNC\attacker\share\x.png",
        r"\\.\pipe\x",
        r"\/attacker/share/x.png",
        "file://attacker/share/x.png",
        "file:////attacker/share/x.png",
        "FILE://attacker/share/x.png",
        "relative.png",
        "C:",
        str(local),
    ]
    found = paths_from_clipboard(_text_mime("\n".join(lines)))
    assert found == [local]
    for touched in fs_spy:
        assert "attacker" not in touched, touched
        assert not touched.startswith(("\\\\", "//")), touched


def test_clipboard_text_limits(fs_spy, tmp_path):
    local = _png(tmp_path / "a.png")
    # Строка за пределом числа строк не смотрится.
    text = "\n".join(["нет"] * MAX_CLIPBOARD_LINES + [str(local)])
    assert paths_from_clipboard(_text_mime(text)) == []
    # Слишком длинная строка отбрасывается без обращения к диску.
    fs_spy.clear()
    long_line = "C:\\" + "a" * MAX_CLIPBOARD_LINE
    assert paths_from_clipboard(_text_mime(long_line)) == []
    assert fs_spy == []


def test_clipboard_explorer_copy_still_accepted(fs_spy):
    """Файлы, скопированные в Проводнике (URL), — как при броске, без проверок диска."""
    from PySide6.QtCore import QUrl

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(r"D:\папка\файл.png")])
    assert paths_from_clipboard(mime) == [Path(r"D:\папка\файл.png")]
    assert fs_spy == []


# --- «Конвертировать всё» прерывается отменой --------------------------------


def test_convert_all_stops_on_folder_cancel(window, files_dir, monkeypatch):
    first = _image_group(window, [_png(files_dir / "a.png")])
    second = _image_group(window, [_png(files_dir / "b.png")])
    asked: list = []
    monkeypatch.setattr(core_jobs, "default_output_dir", lambda source: None)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: asked.append(a) or ""))
    assert window.convert_all() == []
    assert len(asked) == 1  # вторая группа папку не спрашивала
    assert window.group_panels == [first, second]
    assert window.job_manager.jobs() == []
    expected = tr("output_dir_cancelled").format(group=tr("category_image"))
    assert [n.text() for n in window.notices] == [expected]  # одно сообщение


def test_convert_all_stops_on_gif_cancel(qtbot, window, files_dir, monkeypatch):
    from konverter.converters.base import Category, DetectedFormat
    from konverter.core.grouping import Group

    video = files_dir / "клип.mp4"
    video.write_bytes(b"\x00" * 64)
    video_panel = window._add_group(
        Group(Category.VIDEO, [(video, DetectedFormat("mp4", Category.VIDEO, "MP4"))]))
    assert video_panel.set_target("gif")
    image_panel = _image_group(window, [_png(files_dir / "фото.png")])
    monkeypatch.setattr(window, "_ask_gif_fragment", lambda *a: GIF_CANCEL)
    started: list = []
    original = window.convert_group
    monkeypatch.setattr(window, "convert_group",
                        lambda panel: started.append(panel) or original(panel))
    assert window.convert_all() == []
    assert started == [video_panel]
    assert window.group_panels == [video_panel, image_panel]
    assert window.job_manager.jobs() == []


# --- Проводник: путь в кавычках, без оболочки --------------------------------


def test_explorer_command_quotes_path_with_comma(tmp_path, monkeypatch):
    target = tmp_path / "отчёт,итог 1.png"
    explorer = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe")
    assert explorer_command(target) == f'"{explorer}" /select,"{target}"'
    calls: list = []
    monkeypatch.setattr(job_list_module.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)))
    job_list_module.reveal_in_explorer(target)
    assert calls == [((f'"{explorer}" /select,"{target}"',), {"shell": False})]
    with pytest.raises(OSError):
        explorer_command(Path('C:/a"b.png'))


# --- Итог партии ------------------------------------------------------------


def test_all_cancelled_batch_not_reported(window, files_dir, finish_actions):
    window._settings.open_folder_on_finish = True
    batch = _Batch()
    batch.add(_job(1, files_dir, JobStatus.CANCELLED))
    batch.add(_job(2, files_dir, JobStatus.CANCELLED))
    window._on_batch_finished(batch)
    assert finish_actions.notifications == finish_actions.sounds == finish_actions.revealed == []


def _finished_batch(window, files_dir) -> None:
    window._batch = _Batch()
    window._batch.add(_job(1, files_dir, JobStatus.DONE))


@pytest.mark.parametrize("close", [False, True])
def test_batch_deferred_while_close_question_open(window, files_dir, finish_actions,
                                                   monkeypatch, close):
    monkeypatch.setattr(window, "active_jobs", lambda: [object()])
    during: list = []

    def fake_exec(box: QMessageBox) -> int:
        # Партия завершилась, пока открыт вопрос: итог не показывается.
        _finished_batch(window, files_dir)
        window._check_batch_done()
        during.append(len(finish_actions.notifications))
        answer = tr("confirm_close_yes") if close else tr("confirm_close_no")
        for button in box.buttons():
            if button.text() == answer:
                button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    assert window.close() is close
    assert during == [0]
    # «Продолжить работу» — итог после вопроса; закрыли — не сообщается вовсе.
    assert len(finish_actions.notifications) == (0 if close else 1)
    assert len(finish_actions.sounds) == (0 if close else 1)


def test_notification_click_raises_window(window, files_dir, finish_actions):
    _finished_batch(window, files_dir)
    window._check_batch_done()
    assert len(finish_actions.notifications) == 1
    assert finish_actions.on_click == window._raise_window
    finish_actions.on_click()  # не падает; окно показывается и поднимается
    assert window.isVisible()


class _Fake:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def hide(self) -> None:
        self.calls.append("hide")

    def stop(self) -> None:
        self.calls.append("stop")


def test_notify_shutdown_hides_tray_and_click_calls_back(monkeypatch):
    tray, timer = _Fake(), _Fake()
    clicked: list = []
    monkeypatch.setattr(notify, "_tray", tray)
    monkeypatch.setattr(notify, "_hide_timer", timer)
    monkeypatch.setattr(notify, "_on_click", lambda: clicked.append(1))
    notify._message_clicked()
    assert clicked == [1] and tray.calls == ["hide"] and timer.calls == ["stop"]
    notify.shutdown()
    assert tray.calls == ["hide", "hide"] and timer.calls == ["stop", "stop"]
    assert notify._on_click is None


def test_window_shutdown_calls_notify_shutdown(window, monkeypatch):
    calls: list = []
    monkeypatch.setattr(notify, "shutdown", lambda: calls.append(1))
    window.shutdown()
    assert calls == [1]


# --- Объекты Qt не попадают в циклический мусор -------------------------------


def _live_qt_garbage() -> list[str]:
    """Живые объекты Qt, которыми владеет Python, в циклическом мусоре: их
    удалял бы сборщик мусора — в любом потоке (аварии access violation)."""
    import gc

    import shiboken6

    gc.collect()
    gc.set_debug(gc.DEBUG_SAVEALL)
    try:
        gc.collect()
        return [type(o).__name__ for o in gc.garbage
                if isinstance(o, shiboken6.Object) and shiboken6.isValid(o)
                and shiboken6.ownedByPython(o)]
    finally:
        gc.garbage.clear()
        gc.set_debug(0)


def test_background_task_error_leaves_no_qt_cycle(qtbot):
    """Сбой фоновой задачи: исключение → traceback → кадр run() с задачей —
    раньше цикл, и объект сигналов (QObject) удалял сборщик мусора в чужом потоке."""
    import shiboken6

    from konverter.ui.bridge import BackgroundTask

    class Receiver(job_list_module.QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.got: list = []

        def on_done(self, task, result, error) -> None:
            self.got.append(error)

    receiver = Receiver()
    task = BackgroundTask(lambda: 1 / 0, receiver.on_done)
    signals = task.signals
    task.start()
    qtbot.waitUntil(lambda: bool(receiver.got), timeout=5000)
    assert isinstance(receiver.got[0], ZeroDivisionError)
    task.thread.join(5)
    receiver.got.clear()
    del task
    # Объект сигналов удалён в главном потоке (deleteLater), а не сборщиком мусора.
    qtbot.waitUntil(lambda: not shiboken6.isValid(signals), timeout=5000)
    assert _live_qt_garbage() == []


def test_themed_widgets_not_in_cycles():
    """Наблюдатель темы не образует цикла с виджетом: виджет без родителя
    освобождается сразу подсчётом ссылок, а не сборщиком мусора (тот мог бы
    удалить объект Qt в любом потоке)."""
    import weakref

    from PySide6.QtWidgets import QLabel

    from konverter.ui.common import apply_style, card_style, use_error_color, use_secondary_color

    for setup in (use_error_color, use_secondary_color,
                  lambda w: apply_style(w, card_style("QLabel"))):
        label = QLabel("x")
        setup(label)
        ref = weakref.ref(label)
        del label
        assert ref() is None, setup
    assert _live_qt_garbage() == []
