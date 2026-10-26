"""Дымовые тесты каркаса (этап 0)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

from konverter import paths
from konverter.strings import tr
from konverter.ui.main_window import SCREEN_MARGIN, MainWindow

# Допуск при проверке прижатия к углу, px (рамки и масштабирование DPI).
CORNER_TOLERANCE = 8


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    """Главное окно; qtbot закроет его после теста.

    Папка программы подменяется на tmp_path: окно читает и сохраняет
    настройки (позицию), реальная data/ не должна затрагиваться.
    """
    monkeypatch.setattr(paths, "app_root", lambda: tmp_path)
    win = MainWindow(warm_up_detector=False)
    qtbot.addWidget(win)
    yield win
    win.shutdown()


# --- Главное окно ---------------------------------------------------------


def test_window_created(window):
    assert window.windowTitle() == tr("app_title")
    assert window.hint_label.text() == tr("drop_hint")
    # Этап 1: окно принимает перетаскиваемые файлы.
    assert window.acceptDrops()


def test_window_stays_on_top(window):
    assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint


def test_window_in_bottom_right_corner(qtbot, window):
    window.show()
    qtbot.waitExposed(window)

    available = QGuiApplication.primaryScreen().availableGeometry()
    frame = window.frameGeometry()

    # Окно целиком внутри доступной области экрана.
    assert available.contains(frame), (available, frame)

    # Прижато к правому нижнему углу с отступом SCREEN_MARGIN (± допуск).
    right_gap = (available.x() + available.width()) - (frame.x() + frame.width())
    bottom_gap = (available.y() + available.height()) - (frame.y() + frame.height())
    assert abs(right_gap - SCREEN_MARGIN) <= CORNER_TOLERANCE, right_gap
    assert abs(bottom_gap - SCREEN_MARGIN) <= CORNER_TOLERANCE, bottom_gap


# --- Пути -----------------------------------------------------------------


def test_app_root_is_project_root_in_dev():
    root = paths.app_root()
    assert root.is_dir()
    assert (root / "pyproject.toml").is_file()
    assert (root / "src" / "konverter").is_dir()


def test_is_writable_true_for_tmp(tmp_path: Path):
    assert paths.is_writable(tmp_path) is True
    # Пробный файл не остаётся.
    assert list(tmp_path.iterdir()) == []


def test_is_writable_false_when_write_denied(tmp_path: Path, monkeypatch):
    """Недоступная для записи папка моделируется подменой создания файла."""

    def deny(*args, **kwargs):
        raise PermissionError("Отказано в доступе (смоделировано)")

    monkeypatch.setattr(paths.tempfile, "mkstemp", deny)
    assert paths.is_writable(tmp_path / "locked") is False


def test_data_subdirs_created_on_demand(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(paths, "app_root", lambda: tmp_path)

    assert not (tmp_path / "data").exists()
    settings = paths.settings_dir()
    assert settings == tmp_path / "data" / "settings"
    assert settings.is_dir()
    for func in (paths.logs_dir, paths.temp_dir, paths.modules_dir):
        assert func().is_dir()
    assert paths.is_data_writable() is True


def test_data_subdir_none_when_mkdir_fails(tmp_path: Path, monkeypatch):
    """Ф19: если папку создать нельзя — не падаем, возвращаем None."""
    monkeypatch.setattr(paths, "app_root", lambda: tmp_path)

    def deny(self, *args, **kwargs):
        raise PermissionError("Отказано в доступе (смоделировано)")

    monkeypatch.setattr(Path, "mkdir", deny)
    assert paths.settings_dir() is None
    assert paths.is_data_writable() is False
