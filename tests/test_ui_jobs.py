"""Тесты конвертации через интерфейс: задачи, отмена, папка результата, закрытие (П4–П7)."""

from __future__ import annotations

import hashlib
import os
import threading
import time
import wave
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from konverter import paths
from konverter.converters.base import REGISTRY, Category, Preset, Target
from konverter.core import jobs as core_jobs
from konverter.core.jobs import JobStatus
from konverter.core.naming import TEMP_PREFIX
from konverter.strings import tr
from konverter.ui import job_list as job_list_module
from konverter.ui.job_list import status_text
from konverter.ui.main_window import MainWindow

DETECT_TIMEOUT = 30000
JOB_TIMEOUT = 30000


# --- Вспомогательное --------------------------------------------------------


def _answer_dialogs(monkeypatch, answers: list[str], asked: list | None = None) -> None:
    """Подменяет показ QMessageBox: «нажимает» кнопку с подписью из `answers`."""

    def fake_exec(box: QMessageBox) -> int:
        texts = [b.text() for b in box.buttons()]
        if asked is not None:
            asked.append(texts)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        for button in box.buttons():
            if button.text() == answer:
                button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """Настройки и data/ — во временной папке, реальная data/ не трогается."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.setattr(paths, "app_root", lambda: app_dir)
    return app_dir


@pytest.fixture
def files_dir(tmp_path) -> Path:
    folder = tmp_path / "files"
    folder.mkdir()
    return folder


class SlowConverter:
    """Фиктивный долгий конвертер: работает, пока не отпустят или не отменят."""

    category = Category.IMAGE

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()

    def targets(self, fmt: str) -> list[Target]:
        return [Target("slow", "Медленно", "bin", uses_preset=False, is_long=True)]

    def convert(self, sources, dst, target, preset, progress, cancel, *, formats) -> None:
        self.started.set()
        step = 0
        while not self.release.is_set():
            cancel.raise_if_cancelled()
            progress(min(0.9, step / 100))
            step += 1
            time.sleep(0.01)  # рабочий поток, не главный
        Path(dst).write_bytes(b"done")


@pytest.fixture
def slow_converter(monkeypatch):
    converter = SlowConverter()
    monkeypatch.setitem(REGISTRY, Category.IMAGE, converter)
    yield converter
    converter.release.set()  # чтобы рабочие потоки точно завершились


@pytest.fixture
def window(qtbot):
    win = MainWindow(warm_up_detector=False)
    qtbot.addWidget(win)
    yield win
    win.shutdown()


def _png(path: Path, color=(200, 30, 30)) -> Path:
    Image.new("RGB", (8, 6), color).save(path, "PNG")
    return path


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _drop_and_wait(qtbot, win: MainWindow, items: list[Path], groups: int) -> None:
    win.add_paths(items)
    qtbot.waitUntil(
        lambda: not win.is_busy_detecting and len(win.group_panels) >= groups,
        timeout=DETECT_TIMEOUT,
    )


def _row_status(win: MainWindow, job_id: int) -> JobStatus | None:
    row = win.job_list.row(job_id)
    return row.job.status if row is not None else None


def _wait_status(qtbot, win: MainWindow, job_id: int, status: JobStatus) -> None:
    qtbot.waitUntil(lambda: _row_status(win, job_id) == status, timeout=JOB_TIMEOUT)


def _temp_leftovers(folder: Path) -> list[Path]:
    return [p for p in folder.iterdir() if p.name.startswith(TEMP_PREFIX)]


# --- Конвертация (П4, many_to_one, Ф14) -------------------------------------


def test_convert_png_to_webp_keeps_existing(qtbot, window, files_dir, monkeypatch):
    source = _png(files_dir / "x.png")
    existing = files_dir / "x.webp"
    existing.write_bytes(b"not really webp, but must stay untouched")
    source_hash, existing_hash = _sha(source), _sha(existing)

    _drop_and_wait(qtbot, window, [source], groups=1)
    panel = window.group_panels[0]
    assert panel.set_target("webp")
    panel.convert_button.click()
    assert window.group_panels == []  # группа ушла в работу

    jobs = window.job_manager.jobs()
    assert len(jobs) == 1
    _wait_status(qtbot, window, jobs[0].id, JobStatus.DONE)

    result = files_dir / "x (1).webp"
    assert result.is_file()
    with Image.open(result) as img:
        assert img.format == "WEBP"
    assert _sha(source) == source_hash
    assert _sha(existing) == existing_hash
    assert _temp_leftovers(files_dir) == []

    row = window.job_list.row(jobs[0].id)
    assert row.status_label.text() == status_text(JobStatus.DONE)
    assert not row.reveal_button.isHidden()
    assert row.cancel_button.isHidden()

    # «Показать в папке» — Проводник по полному пути, путь в кавычках, без оболочки.
    calls: list[tuple] = []
    monkeypatch.setattr(
        job_list_module.subprocess, "Popen", lambda *args, **kw: calls.append((args, kw))
    )
    row.reveal_button.click()
    assert len(calls) == 1
    args, kwargs = calls[0]
    explorer = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe")
    assert os.path.isabs(explorer)
    assert args[0] == f'"{explorer}" /select,"{result}"'
    assert not kwargs.get("shell")

    # «Очистить завершённые».
    assert window.clear_button.isEnabled()
    window.clear_button.click()
    assert window.job_list.rows() == []
    assert window.job_manager.jobs() == []


def test_many_to_one_single_job(qtbot, window, files_dir):
    items = [_png(files_dir / f"{name}.png") for name in ("первая", "вторая", "третья")]
    _drop_and_wait(qtbot, window, items, groups=1)
    panel = window.group_panels[0]
    assert panel.set_target("pdf_single")
    panel.convert_button.click()

    jobs = window.job_manager.jobs()
    assert len(jobs) == 1
    assert jobs[0].sources == items
    _wait_status(qtbot, window, jobs[0].id, JobStatus.DONE)
    pdfs = sorted(p.name for p in files_dir.glob("*.pdf"))
    assert pdfs == ["первая.pdf"]
    assert tr("job_name_many").format(name="первая.png", n=2) in (
        window.job_list.row(jobs[0].id).name_label.text()
    )


def test_mixed_group_skips_files_already_in_target(qtbot, window, files_dir):
    png = _png(files_dir / "a.png")
    jpgs = []
    for name in ("b.jpg", "c.jpg"):
        Image.new("RGB", (8, 6), (0, 0, 200)).save(files_dir / name, "JPEG")
        jpgs.append(files_dir / name)
    _drop_and_wait(qtbot, window, [png, *jpgs], groups=1)
    panel = window.group_panels[0]
    assert panel.set_target("jpg")
    panel.convert_button.click()
    jobs = window.job_manager.jobs()
    assert [job.sources for job in jobs] == [[png]]
    assert jobs[0].formats == ["png"]
    _wait_status(qtbot, window, jobs[0].id, JobStatus.DONE)
    assert (files_dir / "a.jpg").is_file()
    # Ревью: понятный текст с согласованием по числу.
    assert "2 файла уже в формате JPG — пропущены" in [n.text() for n in window.notices]


def test_error_text_shown(qtbot, window, files_dir, monkeypatch):
    from konverter.converters.base import ConversionError

    class FailingConverter(SlowConverter):
        def convert(self, *args, **kwargs):
            raise ConversionError("Сломалось нарочно")

    monkeypatch.setitem(REGISTRY, Category.IMAGE, FailingConverter())
    _drop_and_wait(qtbot, window, [_png(files_dir / "a.png")], groups=1)
    window.group_panels[0].convert_button.click()
    job = window.job_manager.jobs()[0]
    _wait_status(qtbot, window, job.id, JobStatus.ERROR)
    row = window.job_list.row(job.id)
    assert row.error_label.text() == "Сломалось нарочно"
    assert not row.error_label.isHidden()
    assert row.status_label.toolTip() == "Сломалось нарочно"
    assert _temp_leftovers(files_dir) == []


# --- Папка результата недоступна (Ф12, П5) ----------------------------------


def test_unwritable_folder_asks_once_per_group(qtbot, window, files_dir, tmp_path, monkeypatch):
    out = tmp_path / "выбранная папка"
    out.mkdir()
    items = [_png(files_dir / f"{i}.png") for i in range(3)]
    monkeypatch.setattr(core_jobs, "default_output_dir", lambda source: None)
    asked: list[tuple] = []

    def fake_dialog(*args, **kwargs):
        asked.append(args)
        return str(out)

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(fake_dialog))

    _drop_and_wait(qtbot, window, items, groups=1)
    panel = window.group_panels[0]
    assert panel.set_target("bmp")
    panel.convert_button.click()

    assert len(asked) == 1
    jobs = window.job_manager.jobs()
    assert len(jobs) == 3
    for job in jobs:
        _wait_status(qtbot, window, job.id, JobStatus.DONE)
    assert sorted(p.name for p in out.iterdir()) == ["0.bmp", "1.bmp", "2.bmp"]
    assert not list(files_dir.glob("*.bmp"))


def test_unwritable_folder_dialog_refused(qtbot, window, files_dir, monkeypatch):
    items = [_png(files_dir / f"{i}.png") for i in range(3)]
    monkeypatch.setattr(core_jobs, "default_output_dir", lambda source: None)
    asked: list[tuple] = []
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *args, **kw: asked.append(args) or ""),
    )

    _drop_and_wait(qtbot, window, items, groups=1)
    panel = window.group_panels[0]
    panel.convert_button.click()

    assert len(asked) == 1
    assert window.job_manager.jobs() == []
    assert window.group_panels == [panel]  # группа осталась, можно попробовать снова
    expected = tr("output_dir_cancelled").format(group=tr("category_image"))
    assert [n.text() for n in window.notices] == [expected]


# --- Долгие задачи: отзывчивость, отмена, закрытие (П6, П7) ------------------


def _start_slow_job(qtbot, win: MainWindow, files_dir: Path, slow: SlowConverter) -> int:
    _drop_and_wait(qtbot, win, [_png(files_dir / "долгий.png")], groups=1)
    win.group_panels[0].convert_button.click()
    job_id = win.job_manager.jobs()[0].id
    _wait_status(qtbot, win, job_id, JobStatus.RUNNING)
    assert slow.started.is_set()
    return job_id


def test_window_responsive_during_long_job(qtbot, window, files_dir, slow_converter):
    job_id = _start_slow_job(qtbot, window, files_dir, slow_converter)
    row = window.job_list.row(job_id)
    assert not row.progress_bar.isHidden()
    assert not row.cancel_button.isHidden()

    # Таймер главного потока тикает — цикл событий не заблокирован.
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    qtbot.waitUntil(lambda: len(ticks) >= 10, timeout=5000)
    timer.stop()

    # Прогресс доходит до интерфейса.
    qtbot.waitUntil(lambda: row.progress_bar.value() > 0, timeout=5000)

    # Новые файлы во время работы добавляются новыми группами.
    _drop_and_wait(
        qtbot, window, [_png(files_dir / "новый.png"), _wav(files_dir / "звук.wav")], groups=2
    )
    assert _row_status(window, job_id) == JobStatus.RUNNING
    categories = [p.group.category for p in window.group_panels]
    assert categories == [Category.IMAGE, Category.AUDIO]

    slow_converter.release.set()
    _wait_status(qtbot, window, job_id, JobStatus.DONE)
    assert (files_dir / "долгий.bin").read_bytes() == b"done"


def test_cancel_long_job(qtbot, window, files_dir, slow_converter):
    job_id = _start_slow_job(qtbot, window, files_dir, slow_converter)
    window.job_list.row(job_id).cancel_button.click()
    _wait_status(qtbot, window, job_id, JobStatus.CANCELLED)

    row = window.job_list.row(job_id)
    assert row.status_label.text() == tr("status_cancelled")
    assert row.cancel_button.isHidden()
    assert row.progress_bar.isHidden()
    assert _temp_leftovers(files_dir) == []
    assert not (files_dir / "долгий.bin").exists()


def test_close_with_active_job_asks_and_stops(qtbot, window, files_dir, slow_converter,
                                              monkeypatch, isolated_data):
    window.show()
    qtbot.waitExposed(window)
    job_id = _start_slow_job(qtbot, window, files_dir, slow_converter)

    answers = [tr("confirm_close_no")]
    asked: list[list[str]] = []
    _answer_dialogs(monkeypatch, answers, asked)

    # «Продолжить работу» — окно не закрывается, задача продолжается.
    assert window.close() is False
    assert len(asked) == 1
    assert window.isVisible()
    assert window.job_manager.get(job_id).status == JobStatus.RUNNING

    # «Отменить задачи и закрыть» — задачи отменяются, окно закрывается, настройки сохраняются.
    answers[:] = [tr("confirm_close_yes")]
    started = time.monotonic()
    assert window.close() is True
    assert time.monotonic() - started < 10
    assert len(asked) == 2
    assert window.job_manager.get(job_id).status == JobStatus.CANCELLED
    assert _temp_leftovers(files_dir) == []
    assert (isolated_data / "data" / "settings" / "settings.json").is_file()


def test_window_fits_several_job_rows(qtbot, window, files_dir):
    """Ревью, этап 1: несколько строк задач за раз — окно растёт под все строки.

    Строки добавляются в одном такте цикла событий (как когда пачка
    уведомлений ядра доставляется разом), без потоков — детерминированно.
    """
    from konverter.core.jobs import Job

    window.show()
    qtbot.waitExposed(window)
    target = Target("bmp", "BMP", "bmp", uses_preset=False)
    for index in range(4):
        source = _png(files_dir / f"{index}.png")
        job = Job(
            id=1000 + index, sources=[source], converter=REGISTRY[Category.IMAGE],
            target=target, preset=Preset.MEDIUM, output_dir=files_dir,
            output_stem=str(index), status=JobStatus.ERROR,
            error_message="Ошибка для проверки высоты строки",
        )
        window.job_list.update_job(job)
    qtbot.wait(400)

    assert len(window.job_list.rows()) == 4
    assert all(row.isVisible() for row in window.job_list.rows())
    if window._content_height() > window._max_height():
        pytest.skip("Экран слишком мал: строки не помещаются без прокрутки")
    assert window.content_area.verticalScrollBar().maximum() == 0, (
        window.height(), window._content_height()
    )


def test_late_queued_snapshot_does_not_roll_back_status(qtbot, window, files_dir):
    """Уведомление «в очереди» может прийти позже «выполняется» (ядро шлёт его
    после постановки в очередь, рабочий поток бывает быстрее) — строка не
    должна откатываться назад и «застревать» в очереди."""
    from dataclasses import replace

    from konverter.core.jobs import Job

    job = Job(
        id=7, sources=[_png(files_dir / "a.png")], converter=REGISTRY[Category.IMAGE],
        target=Target("bmp", "BMP", "bmp", uses_preset=False), preset=Preset.MEDIUM,
        output_dir=files_dir, output_stem="a", status=JobStatus.RUNNING,
    )
    window.job_list.update_job(job)
    window.job_list.update_job(replace(job, status=JobStatus.QUEUED))
    assert window.job_list.row(7).job.status == JobStatus.RUNNING
    assert window.job_list.row(7).status_label.text() == status_text(JobStatus.RUNNING)
    window.job_list.update_job(replace(job, status=JobStatus.DONE, result_path=files_dir / "a.bmp"))
    assert window.job_list.row(7).job.status == JobStatus.DONE


class HangingConverter(SlowConverter):
    """Фиктивный «зависший» конвертер: отмену не проверяет вовсе."""

    def convert(self, sources, dst, target, preset, progress, cancel, *, formats) -> None:
        self.started.set()
        self.release.wait(60)  # отмену игнорирует


@pytest.fixture
def hanging_converter(monkeypatch):
    converter = HangingConverter()
    monkeypatch.setitem(REGISTRY, Category.IMAGE, converter)
    yield converter
    converter.release.set()


def test_close_with_hanging_job_is_bounded(qtbot, window, files_dir, hanging_converter,
                                           monkeypatch, isolated_data):
    """Зависший конвертер не задерживает закрытие дольше ~6 с; настройки сохранены."""
    try:
        _close_with_hanging_job(qtbot, window, files_dir, hanging_converter, monkeypatch,
                                isolated_data)
    finally:
        # pytest-qt закрывает окна раньше фикстур; если тест упал до закрытия,
        # closeEvent показал бы модальный вопрос и прогон бы завис.
        hanging_converter.release.set()
        window.shutdown()


def _close_with_hanging_job(qtbot, window, files_dir, hanging_converter, monkeypatch,
                            isolated_data) -> None:
    window.show()
    qtbot.waitExposed(window)
    _drop_and_wait(qtbot, window, [_png(files_dir / "завис.png")], groups=1)
    window.group_panels[0].convert_button.click()
    job_id = window.job_manager.jobs()[0].id
    _wait_status(qtbot, window, job_id, JobStatus.RUNNING)
    assert hanging_converter.started.is_set()

    _answer_dialogs(monkeypatch, [tr("confirm_close_yes")])
    calls: list[dict] = []
    original = window.job_manager.shutdown

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(window.job_manager, "shutdown", spy)

    started = time.monotonic()
    assert window.close() is True
    elapsed = time.monotonic() - started
    assert elapsed < 6.5, elapsed
    assert len(calls) == 1
    assert calls[0].get("timeout") == pytest.approx(5.0)
    assert (isolated_data / "data" / "settings" / "settings.json").is_file()

    # Выход приложения после закрытия окна не останавливает задачи второй раз.
    from PySide6.QtWidgets import QApplication

    QApplication.instance().aboutToQuit.emit()
    assert len(calls) == 1


def test_about_to_quit_stops_jobs_and_saves(qtbot, window, isolated_data, monkeypatch):
    """Выход не через закрытие окна (aboutToQuit) тоже останавливает задачи и сохраняет."""
    from PySide6.QtWidgets import QApplication

    calls: list[dict] = []
    original = window.job_manager.shutdown

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(window.job_manager, "shutdown", spy)
    settings_file = isolated_data / "data" / "settings" / "settings.json"
    assert not settings_file.exists()

    QApplication.instance().aboutToQuit.emit()
    assert len(calls) == 1
    assert settings_file.is_file()

    # Повторно (закрытие окна, второй сигнал) — без второй остановки.
    window.close()
    QApplication.instance().aboutToQuit.emit()
    assert len(calls) == 1


def test_close_without_jobs_does_not_ask(qtbot, window, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Не должно спрашивать без активных задач")

    monkeypatch.setattr(QMessageBox, "exec", fail)
    window.show()
    qtbot.waitExposed(window)
    assert window.close() is True
