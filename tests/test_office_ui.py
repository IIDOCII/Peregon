"""Модуль LibreOffice в интерфейсе и очереди (Ф15, Ф16, Ф19; П20).

Настоящая сеть и LibreOffice не нужны: установка подменяется, `data/` — во
временной папке (модуля там нет).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from konverter import paths
from konverter.converters.base import (
    REGISTRY,
    CancelToken,
    Cancelled,
    Category,
    ConversionError,
    DetectedFormat,
    Preset,
    Target,
)
from konverter.converters.documents import DocumentConverter
from konverter.core.grouping import Group
from konverter.core.jobs import Job, JobManager, JobStatus
from konverter.libreoffice import download, module
from konverter.strings import tr
from konverter.ui import main_window as main_window_module
from konverter.ui.main_window import MainWindow

JOB_TIMEOUT = 30000


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.setattr(paths, "app_root", lambda: app_dir)
    # Никакой сети ни при каких условиях.
    monkeypatch.setattr(download, "make_opener", lambda *a: pytest.fail("сетевой запрос"))
    return app_dir


@pytest.fixture
def window(qtbot):
    win = MainWindow(warm_up_detector=False)
    qtbot.addWidget(win)
    yield win
    win.shutdown()


class Asker:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def __call__(self, parent, title, text, accept_text, reject_text, **kwargs) -> bool:
        if title == tr("office_ask_title"):
            # Кнопки — действия, а не «Да/Нет»; Enter — «Скачать», Esc — «Не сейчас».
            assert (accept_text, reject_text) == (
                tr("office_ask_download"), tr("office_ask_not_now"))
            assert kwargs.get("default_accept") is True
            assert kwargs.get("informative") == tr("office_ask_info")
            assert kwargs.get("detailed") == tr("office_ask_details")
        # Вопрос «Закрыть программу?» при закрытии окна теста тоже идёт сюда.
        self.calls.append(title)
        return self.answer


def _office_group(win: MainWindow, folder: Path, name: str, fmt: str = "doc"):
    path = folder / name
    path.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 100)
    group = Group(Category.DOCUMENT, [(path, DetectedFormat(fmt, Category.DOCUMENT, fmt.upper()))])
    panel = win._add_group(group)
    assert panel.set_target("pdf")
    return panel


def _wait_row_running(qtbot, win: MainWindow, job_id: int):
    def ready() -> bool:
        row = win.job_list.row(job_id)
        return row is not None and row.job.status == JobStatus.RUNNING

    qtbot.waitUntil(ready, timeout=JOB_TIMEOUT)
    return win.job_list.row(job_id)


def _wait_finished(qtbot, win: MainWindow, jobs: list[Job]) -> None:
    qtbot.waitUntil(lambda: all(win.job_manager.get(j.id).is_finished for j in jobs),
                    timeout=JOB_TIMEOUT)


def test_refusal_downloads_nothing_and_marks_jobs(qtbot, window, tmp_path, monkeypatch):
    """П20: отказ — ничего не скачивается, задачи помечены понятным сообщением."""
    asker = Asker(False)
    monkeypatch.setattr(main_window_module, "ask_choice", asker)
    monkeypatch.setattr(module, "install_job", lambda *a: pytest.fail("скачивание после отказа"))
    jobs = window.convert_group(_office_group(window, tmp_path, "a.doc"))
    assert asker.calls == [tr("office_ask_title")]
    _wait_finished(qtbot, window, jobs)
    job = window.job_manager.get(jobs[0].id)
    assert job.status == JobStatus.ERROR
    assert job.error_message == module.MSG_NOT_INSTALLED == tr("office_refused")
    assert window.office_download_job() is None
    assert not (tmp_path / "app" / "data" / "temp" / module.DOWNLOAD_DIR_NAME).exists()
    # Отказ не запоминается: следующая офисная конвертация спрашивает снова.
    jobs2 = window.convert_group(_office_group(window, tmp_path, "b.doc"))
    assert len(asker.calls) == 2
    _wait_finished(qtbot, window, jobs2)


def test_consent_one_download_for_several_groups(qtbot, window, tmp_path, monkeypatch):
    asker = Asker(True)
    monkeypatch.setattr(main_window_module, "ask_choice", asker)
    release = threading.Event()
    calls: list[int] = []

    def fake_install(progress, cancel) -> None:
        calls.append(1)
        progress(0.5)
        while not release.wait(0.02):
            cancel.raise_if_cancelled()

    monkeypatch.setattr(module, "install_job", fake_install)
    first = window.convert_group(_office_group(window, tmp_path, "a.doc"))
    download_job = window.office_download_job()
    assert download_job is not None and download_job.queue_name == "office"
    row = _wait_row_running(qtbot, window, download_job.id)
    assert tr("office_download_name") in row.name_label.text()
    qtbot.waitUntil(lambda: row.progress_bar.value() == 50, timeout=JOB_TIMEOUT)
    assert not row.cancel_button.isHidden()

    # Вторая группа во время скачивания: без второго вопроса и второго скачивания.
    second = window.convert_group(_office_group(window, tmp_path, "b.odt", "odt"))
    assert asker.calls == [tr("office_ask_title")]
    for job in first + second:
        assert job.queue_name == "office"
        assert window.job_manager.get(job.id).status == JobStatus.QUEUED  # ждут скачивания

    release.set()
    _wait_finished(qtbot, window, [download_job] + first + second)
    assert window.job_manager.get(download_job.id).status == JobStatus.DONE
    assert calls == [1]
    # Модуль в этом тесте не появился — офисные задачи завершаются объяснением.
    for job in first + second:
        assert window.job_manager.get(job.id).error_message == module.MSG_NOT_INSTALLED


def test_cancel_download_job(qtbot, window, tmp_path, monkeypatch):
    monkeypatch.setattr(main_window_module, "ask_choice", Asker(True))
    seen: list[str] = []

    def fake_install(progress, cancel) -> None:
        while True:
            try:
                cancel.raise_if_cancelled()
            except Cancelled:
                seen.append("cancelled")
                raise
            threading.Event().wait(0.02)

    monkeypatch.setattr(module, "install_job", fake_install)
    window.convert_group(_office_group(window, tmp_path, "a.doc"))
    job = window.office_download_job()
    _wait_row_running(qtbot, window, job.id).cancel_button.click()
    qtbot.waitUntil(lambda: window.job_manager.get(job.id).status == JobStatus.CANCELLED,
                    timeout=JOB_TIMEOUT)
    assert seen == ["cancelled"]


def test_unwritable_data_no_question(qtbot, window, tmp_path, monkeypatch):
    """Ф19: data/ недоступна — вопрос не задаётся, задача объясняет почему."""
    asker = Asker(True)
    monkeypatch.setattr(main_window_module, "ask_choice", asker)
    monkeypatch.setattr(paths, "is_data_writable", lambda: False)
    jobs = window.convert_group(_office_group(window, tmp_path, "a.doc"))
    assert asker.calls == []
    _wait_finished(qtbot, window, jobs)
    assert window.job_manager.get(jobs[0].id).error_message == module.MSG_NOT_WRITABLE


def test_non_office_variant_does_not_ask(qtbot, window, tmp_path, monkeypatch):
    asker = Asker(True)
    monkeypatch.setattr(main_window_module, "ask_choice", asker)
    path = tmp_path / "a.txt"
    path.write_text("Привет", encoding="utf-8")
    panel = window._add_group(Group(Category.DOCUMENT,
                                    [(path, DetectedFormat("txt", Category.DOCUMENT, "TXT"))]))
    assert panel.set_target("pdf")
    jobs = window.convert_group(panel)
    assert asker.calls == []
    assert jobs[0].queue_name == "docs"
    _wait_finished(qtbot, window, jobs)


def test_installed_module_no_question(qtbot, window, tmp_path, monkeypatch):
    asker = Asker(True)
    monkeypatch.setattr(main_window_module, "ask_choice", asker)
    monkeypatch.setattr(module, "is_installed", lambda: True)
    monkeypatch.setattr(main_window_module.lo_module, "availability", lambda: "installed")

    def fake_convert(*args, **kwargs):
        raise ConversionError("подменено")

    import konverter.converters._office as office

    monkeypatch.setattr(office, "convert", fake_convert)
    jobs = window.convert_group(_office_group(window, tmp_path, "a.doc"))
    assert asker.calls == []
    _wait_finished(qtbot, window, jobs)
    assert window.job_manager.get(jobs[0].id).error_message == "подменено"


# --- Очередь -------------------------------------------------------------------


def test_queue_for_routes_office_variants():
    converter = REGISTRY[Category.DOCUMENT]
    assert isinstance(converter, DocumentConverter)
    manager = JobManager(max_workers=1)
    try:
        pdf = next(t for t in converter.targets("docx") if t.id == "pdf")
        assert Job(1, [Path("a")], converter, pdf, Preset.MEDIUM, Path("."), "a",
                   formats=["docx"]).queue_name == "office"
        assert Job(2, [Path("a")], converter, pdf, Preset.MEDIUM, Path("."), "a",
                   formats=["txt"]).queue_name == "docs"
        presentations = REGISTRY[Category.PRESENTATION]
        assert presentations.queue == "office"
    finally:
        manager.shutdown(timeout=5)


def test_submit_runner_progress_and_result():
    events: list = []
    manager = JobManager(max_workers=1, on_update=lambda job: events.append(job.status))
    try:
        target = Target("svc", "служебная", "", uses_preset=False, is_long=True)
        done = threading.Event()

        def runner(progress, cancel: CancelToken) -> None:
            progress(0.3)
            done.set()

        job = manager.submit_runner("Задача", target, runner, "office")
        assert done.wait(10)
        for _ in range(200):
            if manager.get(job.id).is_finished:
                break
            threading.Event().wait(0.02)
        snap = manager.get(job.id).snapshot()
        assert snap.status == JobStatus.DONE and snap.result_path is None
        assert snap.display_name == "Задача" and snap.queue_name == "office"

        def failing(progress, cancel) -> None:
            raise ConversionError("не вышло")

        bad = manager.submit_runner("Задача 2", target, failing, "office")
        for _ in range(200):
            if manager.get(bad.id).is_finished:
                break
            threading.Event().wait(0.02)
        assert manager.get(bad.id).status == JobStatus.ERROR
        assert manager.get(bad.id).error_message == "не вышло"
    finally:
        manager.shutdown(timeout=5)
