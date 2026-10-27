"""Тесты главного окна: позиция, перетаскивание, группы, сообщения (П1–П3, П8)."""

from __future__ import annotations

import threading
import wave
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QGuiApplication

from konverter import paths
from konverter.converters.base import REGISTRY, Category, Preset
from konverter.core.settings import Settings, load_settings, save_settings
from konverter.strings import tr
from konverter.ui import main_window as main_window_module
from konverter.ui.common import files_count
from konverter.ui.drop_zone import local_paths_from_mime
from konverter.ui.main_window import SCREEN_MARGIN, MainWindow

# Допуск при проверке прижатия к углу, px (рамки и масштабирование DPI).
CORNER_TOLERANCE = 8
# Ожидание фонового разбора, мс (первая загрузка magika бывает небыстрой).
DETECT_TIMEOUT = 30000


# --- Вспомогательное --------------------------------------------------------


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
def make_window(qtbot):
    """Фабрика окон; каждое окно корректно останавливается после теста."""
    created: list[MainWindow] = []

    def factory(**kwargs) -> MainWindow:
        kwargs.setdefault("warm_up_detector", False)
        win = MainWindow(**kwargs)
        qtbot.addWidget(win)
        created.append(win)
        return win

    yield factory
    for win in created:
        win.shutdown()


def _png(path: Path, color=(200, 30, 30)) -> Path:
    Image.new("RGB", (8, 6), color).save(path, "PNG")
    return path


def _jpg(path: Path) -> Path:
    Image.new("RGB", (8, 6), (10, 120, 200)).save(path, "JPEG")
    return path


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return path


def _drop_and_wait(qtbot, win: MainWindow, items: list[Path], groups: int) -> None:
    win.add_paths(items)
    qtbot.waitUntil(
        lambda: not win.is_busy_detecting and len(win.group_panels) >= groups,
        timeout=DETECT_TIMEOUT,
    )


def _wait_detect_done(qtbot, win: MainWindow) -> None:
    qtbot.waitUntil(lambda: not win.is_busy_detecting, timeout=DETECT_TIMEOUT)


# --- Окно и позиция (Ф1, П1) ------------------------------------------------


def test_window_basics(make_window):
    win = make_window()
    assert win.windowTitle() == tr("app_title")
    assert win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert win.acceptDrops()
    assert win.hint_label.text() == tr("drop_hint")
    assert win.drop_zone.browse_button.text() == tr("choose_files")


def test_position_saved_and_restored(qtbot, make_window):
    available = QGuiApplication.primaryScreen().availableGeometry()
    target = QPoint(available.x() + 40, available.y() + 50)

    first = make_window()
    first.show()
    qtbot.waitExposed(first)
    first.move(target)
    qtbot.waitUntil(lambda: first.pos() == target, timeout=3000)
    first.close()
    assert load_settings().window_pos == (target.x(), target.y())

    second = make_window()
    second.show()
    qtbot.waitExposed(second)
    assert second.pos() == target


def test_position_outside_screens_goes_to_corner(qtbot, make_window):
    assert save_settings(Settings(window_pos=(-90000, -90000)))
    win = make_window()
    win.show()
    qtbot.waitExposed(win)

    available = QGuiApplication.primaryScreen().availableGeometry()
    frame = win.frameGeometry()
    assert available.contains(frame), (available, frame)
    right_gap = (available.x() + available.width()) - (frame.x() + frame.width())
    bottom_gap = (available.y() + available.height()) - (frame.y() + frame.height())
    assert abs(right_gap - SCREEN_MARGIN) <= CORNER_TOLERANCE, right_gap
    assert abs(bottom_gap - SCREEN_MARGIN) <= CORNER_TOLERANCE, bottom_gap


def test_data_not_writable_notice_and_no_saving(make_window, monkeypatch, isolated_data):
    monkeypatch.setattr(paths, "is_data_writable", lambda: False)
    win = make_window()
    assert [n.text() for n in win.notices] == [tr("data_not_writable")]
    win.shutdown()
    assert not (isolated_data / "data" / "settings" / "settings.json").exists()


def test_warm_up_runs_in_background(qtbot, make_window, monkeypatch):
    called = threading.Event()
    threads: list[str] = []

    def fake_warm_up() -> bool:
        threads.append(threading.current_thread().name)
        called.set()
        return True

    monkeypatch.setattr(main_window_module, "warm_up", fake_warm_up)
    make_window(warm_up_detector=True)
    qtbot.waitUntil(called.is_set, timeout=5000)
    assert threads and threads[0] != threading.main_thread().name


# --- Перетаскивание (Ф2) ----------------------------------------------------


def _mime_with(paths_: list[Path], extra_urls: list[str] = ()) -> QMimeData:
    mime = QMimeData()
    urls = [QUrl.fromLocalFile(str(p)) for p in paths_] + [QUrl(u) for u in extra_urls]
    mime.setUrls(urls)
    return mime


def test_local_paths_from_mime(files_dir):
    a = _png(files_dir / "а б.png")
    mime = _mime_with([a, files_dir], ["https://example.com/x.png"])
    assert local_paths_from_mime(mime) == [a, files_dir]
    assert local_paths_from_mime(QMimeData()) == []
    assert local_paths_from_mime(None) == []


def test_drop_event_passes_local_paths(make_window, files_dir, monkeypatch):
    win = make_window()
    received: list[list[Path]] = []
    monkeypatch.setattr(win, "add_paths", lambda items: received.append(list(items)))
    a = _png(files_dir / "фото.png")
    mime = _mime_with([a], ["https://example.com/y.png"])

    enter = QDragEnterEvent(
        QPointF(10, 10), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    win.dragEnterEvent(enter)
    assert enter.isAccepted()
    assert win.drop_zone.is_active()

    drop = QDropEvent(
        QPointF(10, 10), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    win.dropEvent(drop)
    assert received == [[a]]
    assert not win.drop_zone.is_active()


def test_drag_without_files_ignored(make_window):
    win = make_window()
    mime = QMimeData()
    mime.setText("просто текст")
    enter = QDragEnterEvent(
        QPointF(5, 5), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    win.dragEnterEvent(enter)
    assert not enter.isAccepted()
    assert not win.drop_zone.is_active()


def test_browse_files_dialog(make_window, files_dir, monkeypatch, qtbot):
    from PySide6.QtWidgets import QFileDialog

    a = _png(files_dir / "a.png")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames", staticmethod(lambda *args, **kw: ([str(a)], ""))
    )
    win = make_window()
    win.drop_zone.browse_button.click()
    qtbot.waitUntil(lambda: len(win.group_panels) == 1, timeout=DETECT_TIMEOUT)
    assert win.group_panels[0].group.paths == [a]


# --- Группы (Ф3, Ф5–Ф7, П2, П3) --------------------------------------------


def test_extension_mismatch_shown(qtbot, make_window, files_dir):
    fake = _png(files_dir / "фото.jpg")  # PNG под видом JPG
    win = make_window()
    _drop_and_wait(qtbot, win, [fake], groups=1)
    panel = win.group_panels[0]
    assert not panel.mismatch_label.isHidden()
    assert panel.mismatch_label.text() == tr("mismatch").format(name="фото.jpg", label="PNG")


def test_no_mismatch_label_for_correct_extension(qtbot, make_window, files_dir):
    win = make_window()
    _drop_and_wait(qtbot, win, [_png(files_dir / "a.png")], groups=1)
    assert win.group_panels[0].mismatch_label.isHidden()


def test_images_and_audio_split_into_groups(qtbot, make_window, files_dir):
    items = [
        _png(files_dir / "1.png"),
        _wav(files_dir / "a.wav"),
        _png(files_dir / "2.png"),
        _png(files_dir / "3.png"),
        _wav(files_dir / "b.wav"),
    ]
    win = make_window()
    _drop_and_wait(qtbot, win, items, groups=2)
    images, audio = win.group_panels
    assert images.group.category == Category.IMAGE
    assert images.title_label.text() == tr("group_title").format(
        category=tr("category_image"), count=files_count(3)
    )
    assert images.has_targets
    assert images.target_combo.count() > 0
    assert not images.convert_button.isHidden()

    # С этапа 2 аудио поддерживается: у группы свои варианты (без WAV для WAV).
    assert audio.group.category == Category.AUDIO
    assert "2 файла" in audio.title_label.text()
    assert audio.has_targets
    assert not audio.convert_button.isHidden()
    audio_ids = {t.id for t in audio.targets}
    assert "mp3" in audio_ids and "wav" not in audio_ids


def test_group_without_converter_not_supported(qtbot, make_window, files_dir, monkeypatch):
    """Категория без конвертера (здесь: аудио убрано из реестра) — «пока не поддерживаются»."""
    monkeypatch.delitem(REGISTRY, Category.AUDIO)
    win = make_window()
    _drop_and_wait(qtbot, win, [_png(files_dir / "1.png"), _wav(files_dir / "a.wav")], groups=2)
    audio = win.group_panels[1]
    assert audio.group.category == Category.AUDIO
    assert not audio.has_targets
    assert audio.unsupported_label.text() == tr("group_not_supported")
    assert not audio.unsupported_label.isHidden()
    assert audio.convert_button.isHidden()


def test_remove_group(qtbot, make_window, files_dir):
    win = make_window()
    _drop_and_wait(qtbot, win, [_png(files_dir / "a.png")], groups=1)
    win.group_panels[0].remove_button.click()
    assert win.group_panels == []


def test_preset_visible_only_when_used(qtbot, make_window, files_dir):
    win = make_window()
    _drop_and_wait(
        qtbot, win, [_png(files_dir / "a.png"), _wav(files_dir / "w.wav")], groups=2
    )
    png_panel = win.group_panels[0]
    assert png_panel.set_target("jpg")
    assert png_panel.preset_enabled()
    assert not png_panel.preset_combo.isHidden()
    assert png_panel.preset_combo.isEnabled()

    win.add_paths([_jpg(files_dir / "b.jpg")])
    qtbot.waitUntil(lambda: len(win.group_panels) == 3, timeout=DETECT_TIMEOUT)
    jpg_panel = win.group_panels[2]
    assert jpg_panel.set_target("png")
    assert not jpg_panel.preset_enabled()
    assert jpg_panel.preset_combo.isHidden()
    assert jpg_panel.preset_caption.isHidden()


def test_default_preset_from_settings_and_remembered(qtbot, make_window, files_dir):
    assert save_settings(Settings(last_preset=Preset.SMALL))
    win = make_window()
    _drop_and_wait(qtbot, win, [_png(files_dir / "a.png")], groups=1)
    panel = win.group_panels[0]
    assert panel.selected_preset() == Preset.SMALL
    panel.set_preset(Preset.HIGH)
    win.shutdown()
    assert load_settings().last_preset == Preset.HIGH


def test_default_preset_is_medium(qtbot, make_window, files_dir):
    win = make_window()
    _drop_and_wait(qtbot, win, [_png(files_dir / "a.png")], groups=1)
    assert win.group_panels[0].selected_preset() == Preset.MEDIUM
    labels = [win.group_panels[0].preset_combo.itemText(i) for i in range(3)]
    assert labels == [tr("preset_high"), tr("preset_medium"), tr("preset_small")]


# --- Неподдерживаемые файлы (Ф4, П8) ----------------------------------------


def test_unsupported_files_listed_and_app_keeps_working(qtbot, make_window, files_dir):
    empty = files_dir / "пусто.png"
    empty.write_bytes(b"")
    garbage = files_dir / "мусор.bin"
    garbage.write_bytes(bytes(range(256)) * 8)
    weird = files_dir / "ёжик 😀 #%&;'!.dat"
    weird.write_bytes(b"\x00\xff\x13\x37\x99" * 500)
    missing = files_dir / "нет такого.png"
    good = _png(files_dir / "фото №1 😀 & (копия).png")

    win = make_window()
    win.add_paths([empty, garbage, weird, missing, good])
    _wait_detect_done(qtbot, win)

    assert len(win.group_panels) == 1
    assert win.group_panels[0].group.paths == [good]
    assert len(win.notices) == 1
    text = win.notices[0].text()
    assert tr("unsupported_title").format(n=4) in text
    assert tr("unsupported_item").format(name=empty.name, reason=tr("unsupported_empty")) in text
    for path in (garbage, weird, missing):
        assert path.name in text

    # Сообщение закрывается, программа продолжает принимать файлы.
    win.notices[0].close_button.click()
    assert win.notices == []
    _drop_and_wait(qtbot, win, [_png(files_dir / "ещё.png")], groups=2)


def test_window_grows_with_content(qtbot, make_window, files_dir):
    win = make_window()
    win.show()
    qtbot.waitExposed(win)
    start_height = win.height()
    items = [_png(files_dir / f"{i}.png") for i in range(2)] + [_wav(files_dir / "a.wav")]
    _drop_and_wait(qtbot, win, items, groups=2)
    win.add_paths([files_dir / "нет1.png", files_dir / "нет2.png"])
    _wait_detect_done(qtbot, win)
    qtbot.waitUntil(lambda: win.height() > start_height, timeout=3000)
    available = win.screen().availableGeometry()
    assert win.frameGeometry().bottom() <= available.bottom()


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1, "1 файл уже в формате PNG — пропущен"),
        (2, "2 файла уже в формате PNG — пропущены"),
        (5, "5 файлов уже в формате PNG — пропущены"),
        (11, "11 файлов уже в формате PNG — пропущены"),
        (21, "21 файл уже в формате PNG — пропущен"),
        (22, "22 файла уже в формате PNG — пропущены"),
        (111, "111 файлов уже в формате PNG — пропущены"),
    ],
)
def test_skipped_same_format_text(n, expected):
    from konverter.ui.common import skipped_same_format_text

    assert skipped_same_format_text(n, "PNG") == expected


def test_target_combo_order_and_labels_for_mixed_group(qtbot, make_window, files_dir):
    """Ревью: «в JPG» не уезжает в конец списка у смешанной группы PNG+JPG."""
    win = make_window()
    _drop_and_wait(qtbot, win, [_png(files_dir / "a.png"), _jpg(files_dir / "b.jpg")], groups=1)
    combo = win.group_panels[0].target_combo
    ids = [combo.itemData(i) for i in range(combo.count())]
    assert ids == ["jpg", "png", "webp", "avif", "gif", "bmp", "tiff", "ico",
                   "pdf_each", "pdf_single", "print_ppi"]
    assert [combo.itemText(i) for i in range(combo.count())] == [
        tr(f"target_{tid}") for tid in ids
    ]


def test_group_panel_fits_window_width(qtbot, make_window, files_dir):
    """Длинные подписи вариантов не распирают панель шире окна (правый край
    с кнопками «×» и «Конвертировать» не обрезается)."""
    win = make_window()
    win.show()
    qtbot.waitExposed(win)
    _drop_and_wait(qtbot, win, [_png(files_dir / "a.png"), _jpg(files_dir / "b.jpg")], groups=1)
    qtbot.wait(300)
    viewport = win.content_area.viewport().width()
    assert win._content.width() <= viewport, (win._content.width(), viewport)
    panel = win.group_panels[0]
    for widget in (panel.convert_button, panel.remove_button, panel.target_combo):
        right = widget.mapTo(win.content_area.viewport(), widget.rect().topRight()).x()
        assert right < viewport, (widget, right, viewport)
    # Раскрытый список не уже самой длинной подписи; полная подпись — в подсказке.
    combo = panel.target_combo
    assert combo.view().minimumWidth() >= combo.view().sizeHintForColumn(0)
    assert panel.set_target("pdf_each")
    assert combo.toolTip() == tr("target_pdf_each")


# Пауза, за которую успевают отработать отложенные показы виджетов и подгонка.
SETTLE_MS = 400


def test_window_fits_several_widgets_added_at_once(qtbot, make_window, files_dir):
    """Ревью, этап 1: сообщение и две группы за один бросок — окно растёт под все.

    Раньше высота подгонялась один раз, когда остальные новые виджеты были
    ещё скрыты (Qt показывает их отложенно), и часть содержимого была видна
    только прокруткой.
    """
    win = make_window()
    win.show()
    qtbot.waitExposed(win)
    empty = files_dir / "пусто.png"
    empty.write_bytes(b"")
    items = [
        *(_png(files_dir / f"{i}.png") for i in range(3)),
        _png(files_dir / "фото.jpg"),  # PNG под видом JPG — строка несовпадения
        _wav(files_dir / "звук.wav"),
        empty,
    ]
    _drop_and_wait(qtbot, win, items, groups=2)
    assert len(win.notices) == 1
    qtbot.wait(SETTLE_MS)

    assert all(p.isVisible() for p in win.group_panels)
    assert all(n.isVisible() for n in win.notices)
    if win._content_height() > win._max_height():
        pytest.skip("Экран слишком мал: содержимое не помещается без прокрутки")
    assert win.content_area.verticalScrollBar().maximum() == 0, (
        win.height(), win._content_height()
    )
    # И без лишней пустоты под последней панелью: допустимы только промежутки
    # раскладки (пустой список задач, растяжка) — около 26 px при 100 %;
    # раньше высота считалась для более узкой ширины и пустота была ~42 px.
    # Последний виджет содержимого: при двух группах под ними «Конвертировать всё».
    assert win.convert_all_row.isVisible()
    last = win.convert_all_row
    bottom = last.mapTo(win.content_area.viewport(), last.rect().bottomLeft()).y()
    spare = win.content_area.viewport().height() - bottom
    assert spare <= 32, spare
