"""Доработка внешнего вида: свёрнутая зона броска, «Конвертировать всё»,
прокрутка к новой задаче, итог партии задач (уведомление, звук, Проводник),
подписи кнопок диалогов, Ctrl+O / Ctrl+V / Del, цвета от темы, значки статусов.

Уведомление, звук и Проводник подменены фикстурой `finish_actions`
(tests/conftest.py): ничего не открывается и не звучит.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QPalette
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton, QWidget

from konverter import paths
from konverter.converters.base import (
    REGISTRY,
    Category,
    ConversionError,
    DetectedFormat,
    Preset,
    Target,
)
from konverter.core import jobs as core_jobs
from konverter.core.grouping import Group
from konverter.core.jobs import Job, JobStatus
from konverter.libreoffice import module as lo_module
from konverter.strings import OFFICE_DOWNLOAD_MB, tr
from konverter.ui import common
from konverter.ui import main_window as main_window_module
from konverter.ui.common import (
    MIN_DROP_BORDER_CONTRAST,
    MIN_TEXT_CONTRAST,
    blend,
    border_color,
    contrast_ratio,
    error_color,
    secondary_text_color,
)
from konverter.ui.drop_zone import paths_from_clipboard
from konverter.ui.gif_fragment import GifChoice
from konverter.ui.group_panel import NOTICE_INFO, NOTICE_WARNING
from konverter.ui.job_list import JobRow
from konverter.ui.main_window import WINDOW_WIDTH, MainWindow

JOB_TIMEOUT = 30000
SETTLE_MS = 300


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


@pytest.fixture
def window(qtbot):
    win = MainWindow(warm_up_detector=False)
    qtbot.addWidget(win)
    yield win
    win.shutdown()


def _png(path: Path) -> Path:
    Image.new("RGB", (8, 6), (200, 30, 30)).save(path, "PNG")
    return path


def _image_group(win: MainWindow, files: list[Path], target: str = "bmp"):
    group = Group(Category.IMAGE, [(p, DetectedFormat("png", Category.IMAGE, "PNG")) for p in files])
    panel = win._add_group(group)
    assert panel.set_target(target)
    return panel


def _job(job_id: int, folder: Path, status: JobStatus, *, long: bool = False, **kw) -> Job:
    return Job(
        id=job_id, sources=[folder / f"{job_id}.png"], converter=REGISTRY[Category.IMAGE],
        target=Target("bmp", "BMP", "bmp", uses_preset=False, is_long=long),
        preset=Preset.MEDIUM, output_dir=folder, output_stem=str(job_id), status=status, **kw,
    )


def _wait_all_finished(qtbot, win: MainWindow) -> None:
    def done() -> bool:
        jobs = win.job_manager.jobs()
        return bool(jobs) and all(
            (row := win.job_list.row(j.id)) is not None and row.job.is_finished for j in jobs
        )

    qtbot.waitUntil(done, timeout=JOB_TIMEOUT)


# --- Зона броска -------------------------------------------------------------


def test_drop_zone_collapses_when_window_has_content(qtbot, window, files_dir):
    zone = window.drop_zone
    assert not zone.is_compact()
    assert not window.hint_label.isHidden()
    assert zone.browse_button.text() == tr("choose_files")
    empty_height = zone.sizeHint().height()

    panel = _image_group(window, [_png(files_dir / "a.png")])
    assert zone.is_compact()
    assert window.hint_label.isHidden()
    assert zone.browse_button.text() == tr("add_files")
    assert zone.sizeHint().height() < empty_height  # узкая строка

    window.remove_group(panel)
    assert not zone.is_compact()
    assert zone.browse_button.text() == tr("choose_files")

    notice = window.add_notice("Сообщение")
    assert zone.is_compact()
    window._remove_notice(notice)
    assert not zone.is_compact()

    window.job_list.update_job(_job(1, files_dir, JobStatus.QUEUED))
    assert zone.is_compact()


def test_drops_are_accepted_by_whole_window(window, files_dir):
    """Ни один вложенный виджет не перехватывает бросок: он доходит до окна."""
    _image_group(window, [_png(files_dir / "a.png")])
    window.add_notice("Сообщение")
    window.job_list.update_job(_job(1, files_dir, JobStatus.QUEUED))
    assert window.acceptDrops()
    takers = [w for w in window.findChildren(QWidget) if w.acceptDrops()]
    assert takers == []


def test_drop_zone_dashed_border_contrast():
    palette = QApplication.instance().palette()
    window_bg = blend(palette.color(QPalette.ColorRole.Window), QColor("white"))
    assert contrast_ratio(border_color(palette, MIN_DROP_BORDER_CONTRAST), window_bg) >= 3.0


# --- «Конвертировать всё» ---------------------------------------------------


def test_convert_all_visible_from_two_groups(window, files_dir):
    assert window.convert_all_row.isHidden()
    first = _image_group(window, [_png(files_dir / "a.png")])
    assert window.convert_all_row.isHidden()
    _image_group(window, [_png(files_dir / "b.png")])
    assert not window.convert_all_row.isHidden()
    assert window.convert_all_button.text() == tr("convert_all")
    window.remove_group(first)
    assert window.convert_all_row.isHidden()


def test_convert_all_runs_groups_in_order_one_batch(qtbot, window, files_dir, finish_actions):
    a = _image_group(window, [_png(files_dir / "a.png")])
    b = _image_group(window, [_png(files_dir / "b.png")])
    order: list = []
    original = window.convert_group

    def spy(panel):
        order.append(panel)
        return original(panel)

    window.convert_group = spy
    window.convert_all_button.click()
    assert order == [a, b]
    assert window.group_panels == []
    _wait_all_finished(qtbot, window)
    assert (files_dir / "a.bmp").is_file() and (files_dir / "b.bmp").is_file()
    qtbot.waitUntil(lambda: len(finish_actions.notifications) == 1, timeout=JOB_TIMEOUT)
    qtbot.wait(SETTLE_MS)
    # Обе группы — одна партия: одно уведомление на всё.
    assert finish_actions.notifications == [
        (tr("batch_title"), tr("batch_done").format(n=2).capitalize(), False)
    ]


def test_convert_all_goes_through_gif_office_and_folder(qtbot, window, files_dir, tmp_path,
                                                        monkeypatch):
    """«Конвертировать всё» — тем же путём, что кнопки групп: окно фрагмента GIF,
    вопрос о LibreOffice, выбор папки."""
    video = files_dir / "клип.mp4"
    video.write_bytes(b"\x00" * 64)
    video_panel = window._add_group(
        Group(Category.VIDEO, [(video, DetectedFormat("mp4", Category.VIDEO, "MP4"))]))
    assert video_panel.set_target("gif")
    doc = files_dir / "письмо.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 100)
    doc_panel = window._add_group(
        Group(Category.DOCUMENT, [(doc, DetectedFormat("doc", Category.DOCUMENT, "DOC"))]))
    assert doc_panel.set_target("pdf")
    image_panel = _image_group(window, [_png(files_dir / "фото.png")])

    out = tmp_path / "out"
    out.mkdir()
    folders: list = []
    monkeypatch.setattr(core_jobs, "default_output_dir", lambda source: None)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: folders.append(a) or str(out)))
    gif_asked: list = []
    monkeypatch.setattr(window, "_ask_gif_fragment",
                        lambda *a: gif_asked.append(a) or GifChoice("one", 0.0, 1.0))
    office_asked: list = []
    monkeypatch.setattr(main_window_module, "ask_choice",
                        lambda parent, title, *a, **k: office_asked.append(title) or False)

    window.convert_all()
    assert len(folders) == 3
    assert len(gif_asked) == 1 and gif_asked[0][0] == video
    assert office_asked == [tr("office_ask_title")]
    # Все три группы ушли в работу (отмена прерывала бы проход — см. review1).
    assert window.group_panels == []
    assert len(window.job_manager.jobs()) == 3
    _wait_all_finished(qtbot, window)


# --- Прокрутка к новой задаче -----------------------------------------------


def test_new_job_scrolled_into_view(qtbot, window, files_dir, monkeypatch):
    monkeypatch.setattr(window, "_max_height", lambda: 260)
    window.show()
    qtbot.waitExposed(window)
    for index in range(4):
        _image_group(window, [_png(files_dir / f"{index}.png")])
    qtbot.wait(SETTLE_MS)
    bar = window.content_area.verticalScrollBar()
    assert bar.maximum() > 0 and bar.value() == 0

    job = _job(77, files_dir, JobStatus.QUEUED)
    monkeypatch.setattr(window.job_manager, "get", lambda job_id: job)
    window._on_job_updated(job)
    row = window.job_list.row(77)
    viewport = window.content_area.viewport()

    def row_visible() -> bool:
        top = row.mapTo(viewport, QPoint(0, 0)).y()
        return top >= 0 and top + row.height() <= viewport.height()

    qtbot.waitUntil(row_visible, timeout=5000)
    assert bar.value() > 0


# --- Итог партии задач ------------------------------------------------------


def test_open_folder_off_by_default(qtbot, window, files_dir, finish_actions):
    """Решение владельца: Проводник по завершении по умолчанию не открывается."""
    assert window._settings.open_folder_on_finish is False
    _image_group(window, [_png(files_dir / "а.png")]).convert_button.click()
    qtbot.waitUntil(lambda: len(finish_actions.notifications) == 1, timeout=JOB_TIMEOUT)
    qtbot.wait(SETTLE_MS)
    assert finish_actions.sounds == [True]
    assert finish_actions.revealed == []


def test_batch_finish_actions_once_per_batch(qtbot, window, files_dir, finish_actions):
    window._settings.open_folder_on_finish = True  # явно включено
    sources = [_png(files_dir / f"{name}.png") for name in ("один", "два", "три")]
    _image_group(window, sources).convert_button.click()
    _wait_all_finished(qtbot, window)
    qtbot.waitUntil(lambda: len(finish_actions.notifications) == 1, timeout=JOB_TIMEOUT)
    qtbot.wait(SETTLE_MS)
    assert finish_actions.notifications == [
        (tr("batch_title"), tr("batch_done").format(n=3).capitalize(), False)
    ]
    assert finish_actions.sounds == [True]
    results = {Path(j.result_path) for j in window.job_manager.jobs()}
    assert len(finish_actions.revealed) == 1 and Path(finish_actions.revealed[0]) in results

    # Следующая партия — снова ровно один раз.
    _image_group(window, [_png(files_dir / "четыре.png")]).convert_button.click()
    qtbot.waitUntil(lambda: len(finish_actions.notifications) == 2, timeout=JOB_TIMEOUT)
    qtbot.wait(SETTLE_MS)
    assert len(finish_actions.notifications) == 2
    assert finish_actions.sounds == [True, True]
    assert Path(finish_actions.revealed[1]) == files_dir / "четыре.bmp"


def test_batch_finish_actions_disabled_by_settings(qtbot, window, files_dir, finish_actions):
    window._settings.notify_on_finish = False
    window._settings.sound_on_finish = False
    window._settings.open_folder_on_finish = False
    _image_group(window, [_png(files_dir / "а.png")]).convert_button.click()
    _wait_all_finished(qtbot, window)
    qtbot.wait(SETTLE_MS)
    assert finish_actions.notifications == []
    assert finish_actions.sounds == []
    assert finish_actions.revealed == []


class FailingConverter:
    category = Category.IMAGE

    def targets(self, fmt: str) -> list[Target]:
        return [Target("fail", "Сбой", "bin", uses_preset=False)]

    def convert(self, sources, dst, target, preset, progress, cancel, *, formats) -> None:
        raise ConversionError("Сломалось нарочно")


def test_batch_with_errors_other_sound_and_no_folder(qtbot, window, files_dir, finish_actions,
                                                     monkeypatch):
    monkeypatch.setitem(REGISTRY, Category.IMAGE, FailingConverter())
    _image_group(window, [_png(files_dir / "x.png")], target="fail").convert_button.click()
    _wait_all_finished(qtbot, window)
    qtbot.waitUntil(lambda: len(finish_actions.notifications) == 1, timeout=JOB_TIMEOUT)
    title, text, error = finish_actions.notifications[0]
    assert text == tr("batch_error").format(n=1).capitalize() and error is True
    assert finish_actions.sounds == [False]
    assert finish_actions.revealed == []  # успешных результатов нет — Проводник не открыт


def test_batch_summary_text():
    batch = main_window_module._Batch()
    for job_id, status in enumerate((JobStatus.DONE, JobStatus.DONE, JobStatus.ERROR,
                                     JobStatus.CANCELLED)):
        batch.add(_job(job_id, Path("."), status))
    batch.add(_job(0, Path("."), JobStatus.DONE))  # повтор не считается
    assert batch.summary() == "Готово: 2, с ошибками: 1, отменено: 1"


def test_finish_settings_roundtrip(tmp_path):
    from konverter.core.settings import Settings, load_settings, save_settings

    assert Settings().notify_on_finish and Settings().sound_on_finish
    assert Settings().open_folder_on_finish is False  # решение владельца
    path = tmp_path / "s.json"
    save_settings(Settings(sound_on_finish=False), path)
    loaded = load_settings(path)
    assert loaded.sound_on_finish is False and loaded.notify_on_finish is True
    # Не bool — значение по умолчанию.
    assert Settings.from_dict({"open_folder_on_finish": 1}).open_folder_on_finish is False
    assert Settings.from_dict({"open_folder_on_finish": True}).open_folder_on_finish is True


# --- Диалоги ----------------------------------------------------------------


def _record_box(monkeypatch, seen: dict, click: str | None):
    def fake_exec(box: QMessageBox) -> int:
        seen.update(
            text=box.text(), info=box.informativeText(), details=box.detailedText(),
            buttons=[b.text() for b in box.buttons()
                     if box.buttonRole(b) != QMessageBox.ButtonRole.ActionRole],
            extra=[b.text() for b in box.buttons()
                   if box.buttonRole(b) == QMessageBox.ButtonRole.ActionRole],
            default=box.defaultButton().text(), escape=box.escapeButton().text(),
        )
        for button in box.buttons():
            if click is not None and button.text() == click:
                button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)


def test_office_dialog_buttons_and_texts(window, monkeypatch):
    monkeypatch.setattr(lo_module, "availability", lambda: "missing")
    monkeypatch.setattr(lo_module, "ask_prefix", lambda: "")
    seen: dict = {}
    _record_box(monkeypatch, seen, tr("office_ask_not_now"))
    window._prepare_office()
    assert sorted(seen["buttons"]) == sorted([tr("office_ask_download"), tr("office_ask_not_now")])
    assert f"({OFFICE_DOWNLOAD_MB} МБ)" in tr("office_ask_download")
    assert f"{OFFICE_DOWNLOAD_MB} МБ" in seen["info"]
    assert seen["text"] == tr("office_ask_text") and "?" in seen["text"]
    assert "MPL-2.0" in seen["details"] and "товарный знак" in seen["details"]
    assert seen["extra"] == [tr("button_show_details")]  # «Подробнее…», а не «Show Details...»
    assert seen["default"] == tr("office_ask_download")  # как и раньше: Enter — скачать
    assert seen["escape"] == tr("office_ask_not_now")
    assert window.office_download_job() is None  # «Не сейчас» — ничего не скачивается


def test_close_dialog_default_and_escape_keep_working(window, monkeypatch):
    seen: dict = {}
    _record_box(monkeypatch, seen, None)  # закрыли крестиком / Esc
    monkeypatch.setattr(window, "active_jobs", lambda: [object()])
    assert window.close() is False
    assert sorted(seen["buttons"]) == sorted([tr("confirm_close_yes"), tr("confirm_close_no")])
    assert seen["default"] == seen["escape"] == tr("confirm_close_no")


def test_output_dir_dialog_short_title_reason_shown_before(window, files_dir, monkeypatch):
    monkeypatch.setattr(core_jobs, "default_output_dir", lambda source: None)
    shown: list = []

    def fake_dialog(parent, title, start):
        shown.append((title, [n.text() for n in window.notices]))
        return ""

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(fake_dialog))
    window.convert_group(_image_group(window, [_png(files_dir / "a.png")]))
    title, notices_during = shown[0]
    assert title == tr("choose_output_dir") and len(title) < 40
    reason = tr("output_dir_needed").format(group=tr("category_image"))
    assert reason in notices_during
    assert reason not in [n.text() for n in window.notices]  # после ответа убрано


# --- Клавиатура -------------------------------------------------------------


def test_ctrl_o_opens_file_choice(window, files_dir, monkeypatch):
    assert window.open_shortcut.key() == QKeySequence(QKeySequence.StandardKey.Open)
    assert window.open_shortcut.key().toString() == "Ctrl+O"
    chosen = str(_png(files_dir / "a.png"))
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([chosen], "")))
    added: list = []
    monkeypatch.setattr(window, "add_paths", lambda items: added.append(list(items)))
    window.open_shortcut.activated.emit()
    assert added == [[chosen]]


class _FakeClipboard:
    def __init__(self, mime: QMimeData) -> None:
        self.mime = mime

    def mimeData(self):  # noqa: N802 (имя из Qt)
        return self.mime


def test_ctrl_v_pastes_files(window, files_dir, monkeypatch):
    assert window.paste_shortcut.key().toString() == "Ctrl+V"
    a, b = _png(files_dir / "a.png"), _png(files_dir / "b.png")
    added: list = []
    monkeypatch.setattr(window, "add_paths", lambda items: added.append(list(items)))

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(a)), QUrl("https://example.com/x.png")])
    monkeypatch.setattr(QGuiApplication, "clipboard", staticmethod(lambda: _FakeClipboard(mime)))
    window.paste_shortcut.activated.emit()
    assert added == [[a]]

    text = QMimeData()
    text.setText(f'"{b}"\n{QUrl.fromLocalFile(str(a)).toString()}\nне путь\n')
    monkeypatch.setattr(QGuiApplication, "clipboard", staticmethod(lambda: _FakeClipboard(text)))
    window.paste_shortcut.activated.emit()
    assert added[1] == [b, a]

    empty = QMimeData()
    empty.setText("просто текст")
    monkeypatch.setattr(QGuiApplication, "clipboard", staticmethod(lambda: _FakeClipboard(empty)))
    window.paste_shortcut.activated.emit()
    assert len(added) == 2
    assert window.notices[-1].text() == tr("clipboard_no_files")
    assert window.notices[-1].kind == NOTICE_INFO


def test_paths_from_clipboard_ignores_missing(tmp_path):
    mime = QMimeData()
    mime.setText(str(tmp_path / "нет.png"))
    assert paths_from_clipboard(mime) == []
    assert paths_from_clipboard(None) == []


def test_delete_key_removes_group(qtbot, window, files_dir):
    panel = _image_group(window, [_png(files_dir / "a.png")])
    # Фокус на списке или кнопке карточки — Del группу не убирает.
    for child in (panel.target_combo, panel.convert_button):
        child.setFocus()
        assert window.focusWidget() is child
        qtbot.keyClick(child, Qt.Key.Key_Delete)
        assert window.group_panels == [panel]
    panel.setFocus()
    assert window.focusWidget() is panel
    qtbot.keyClick(panel, Qt.Key.Key_Delete)
    assert window.group_panels == []


def test_main_buttons_have_mnemonics():
    for key in ("choose_files", "add_files", "convert", "convert_all", "clear_finished",
                "office_ask_download", "office_ask_not_now", "confirm_close_yes",
                "confirm_close_no", "gif_make", "gif_preview"):
        assert "&" in tr(key), key


# --- Цвета от темы ----------------------------------------------------------


def _palette(window: str, text: str, base: str) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(window))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(text))
    palette.setColor(QPalette.ColorRole.Base, QColor(base))
    placeholder = QColor(text)
    placeholder.setAlpha(128)
    palette.setColor(QPalette.ColorRole.PlaceholderText, placeholder)
    return palette


@pytest.mark.parametrize("colors", [
    ("#f3f3f3", "#000000", "#ffffff"),  # светлая Windows 11
    ("#1e1e1e", "#ffffff", "#2d2d2d"),  # тёмная Windows 11
    ("#ffffff", "#000000", "#ffffff"),
    ("#000000", "#ffffff", "#101010"),
])
def test_error_and_secondary_colors_contrast(colors):
    palette = _palette(*colors)
    for color in (error_color(palette), secondary_text_color(palette)):
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base):
            assert contrast_ratio(color, palette.color(role)) >= MIN_TEXT_CONTRAST, (colors, role)
    assert contrast_ratio(border_color(palette, MIN_DROP_BORDER_CONTRAST),
                          palette.color(QPalette.ColorRole.Window)) >= 3.0


def test_error_color_follows_theme_change(qtbot, tmp_path):
    """Смена темы на лету (QStyleHints.setColorScheme): цвет ошибки пересчитывается."""
    app = QApplication.instance()
    hints = app.styleHints()
    if not hasattr(hints, "setColorScheme"):
        pytest.skip("Qt без QStyleHints.setColorScheme")
    row = JobRow(_job(1, tmp_path, JobStatus.ERROR, error_message="Сбой"))
    qtbot.addWidget(row)
    seen_dark = set()
    try:
        for scheme in (Qt.ColorScheme.Light, Qt.ColorScheme.Dark, Qt.ColorScheme.Light):
            hints.setColorScheme(scheme)
            qtbot.wait(50)
            palette = app.palette()
            seen_dark.add(common.is_dark_palette(palette))
            color = row.error_label.palette().color(QPalette.ColorRole.WindowText)
            assert color == error_color(palette)
            secondary = row.status_label.palette().color(QPalette.ColorRole.WindowText)
            for bg in common._backgrounds(palette):
                assert contrast_ratio(color, bg) >= MIN_TEXT_CONTRAST, scheme
                assert contrast_ratio(secondary, bg) >= MIN_TEXT_CONTRAST, scheme
    finally:
        hints.setColorScheme(Qt.ColorScheme.Unknown)
        qtbot.wait(50)
    if seen_dark != {True, False}:
        pytest.skip("Стиль не меняет палитру по теме — проверена только одна тема")


# --- Строки задач, сообщения, группы -----------------------------------------


def test_status_icons_and_no_duplicate_running_text(qtbot, tmp_path):
    rows = {}
    for job_id, status in enumerate((JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED)):
        row = JobRow(_job(job_id, tmp_path, status, error_message="x"))
        qtbot.addWidget(row)
        assert not row.status_icon.isHidden()
        rows[status] = row.status_icon.pixmap().toImage()
    images = list(rows.values())
    assert images[0] != images[1] and images[1] != images[2] and images[0] != images[2]

    running = JobRow(_job(10, tmp_path, JobStatus.RUNNING, long=True, progress=0.42))
    qtbot.addWidget(running)
    assert not running.progress_bar.isHidden() and running.progress_bar.value() == 42
    assert running.status_label.isHidden()  # не «Выполняется» рядом с «42 %»
    assert running.status_icon.isHidden()
    queued = JobRow(_job(11, tmp_path, JobStatus.QUEUED, long=True))
    qtbot.addWidget(queued)
    assert queued.progress_bar.isHidden() and not queued.status_label.isHidden()
    assert queued.name_label.font().bold()


def test_notices_have_icons(window):
    warning = window.add_notice("Предупреждение")
    info = window.add_notice("Сведения", kind=NOTICE_INFO)
    assert warning.kind == NOTICE_WARNING and info.kind == NOTICE_INFO
    for notice in (warning, info):
        assert not notice.icon_label.isHidden() and not notice.icon_label.pixmap().isNull()
    assert warning.icon_label.pixmap().toImage() != info.icon_label.pixmap().toImage()


def test_group_panel_layout_and_primary_button(window, files_dir):
    path = _png(files_dir / "фото.jpg")
    group = Group(Category.IMAGE, [(path, DetectedFormat("png", Category.IMAGE, "PNG", True))])
    panel = window._add_group(group)
    assert panel.convert_button.isDefault()
    assert panel.mismatch_label.styleSheet() == ""  # обычный цвет, не цвет ссылки
    assert not panel.mismatch_icon.pixmap().isNull()
    assert panel.set_target("jpg")  # вариант с пресетом — видны обе строки формы
    window.show()
    QApplication.processEvents()
    assert panel.target_combo.x() == panel.preset_combo.x()  # поля выровнены


def test_jobs_title_larger_than_group_title(window, files_dir):
    panel = _image_group(window, [_png(files_dir / "a.png")])
    assert window.jobs_title.font().pointSizeF() > panel.title_label.font().pointSizeF()


def test_base_font_and_nothing_clipped(qtbot, window, files_dir):
    assert QApplication.instance().font().pointSizeF() == common.BASE_FONT_POINT_SIZE
    _image_group(window, [_png(files_dir / "a.png")])
    _image_group(window, [_png(files_dir / "b.png")])
    window.add_notice("Пропущены файлы (1):\nмусор.xyz — Формат не поддерживается")
    for job_id, status in enumerate((JobStatus.DONE, JobStatus.ERROR, JobStatus.QUEUED)):
        window.job_list.update_job(_job(100 + job_id, files_dir, status,
                                        result_path=files_dir / "a.bmp",
                                        error_message="Не удалось прочитать файл"))
    window.show()
    qtbot.waitExposed(window)
    qtbot.wait(SETTLE_MS)
    assert window.width() >= WINDOW_WIDTH
    assert window.width() >= window.minimumSizeHint().width()
    for button in window.findChildren(QPushButton):
        if button.isVisible():
            assert button.width() >= button.sizeHint().width(), button.text()
            assert button.height() >= button.minimumSizeHint().height(), button.text()


# --- Иконка программы -------------------------------------------------------


def test_app_icon_loader(tmp_path):
    assert common.load_app_icon(tmp_path) is None
    (tmp_path / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">'
        '<rect width="16" height="16" fill="#2a7"/></svg>', encoding="utf-8")
    icon = common.load_app_icon(tmp_path)
    assert icon is not None and not icon.isNull()
    assert common.ASSETS_DIR.parts[-2:] == ("ui", "assets")
