"""Что происходит, когда завершились все задачи: уведомление Windows, звук, Проводник.

Без новых пакетов: уведомление — через `QSystemTrayIcon.showMessage` (в
Windows 10/11 оно показывается как всплывающее уведомление системы). Для
этого значок программы появляется в области уведомлений только на время
показа (`TRAY_VISIBLE_MS`) и затем скрывается: без видимого значка Windows
сообщение не показывает. Звук — `winsound.MessageBeep` (разный для «всё
успешно» и «есть ошибки»). Проводник — тот же вызов, что у «Показать в папке».

Главное окно вызывает эти функции через модуль (`notify.show_notification`
и т.д.), поэтому тесты подменяют их целиком (tests/conftest.py): при прогоне
тестов Проводник не открывается и звуков нет.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QStyle, QSystemTrayIcon

from konverter.strings import tr
from konverter.ui.job_list import reveal_in_explorer

log = logging.getLogger(__name__)

# Сколько показывать уведомление, мс (Windows 10/11 может держать его по-своему).
MESSAGE_MS = 6000
# Сколько держать значок в области уведомлений, мс (чуть дольше сообщения).
TRAY_VISIBLE_MS = 8000

_tray: QSystemTrayIcon | None = None
_hide_timer: QTimer | None = None
# Что сделать при щелчке по уведомлению (главное окно поднимает себя).
_on_click: Callable[[], None] | None = None


def _hide_tray() -> None:
    if _hide_timer is not None:
        _hide_timer.stop()
    if _tray is not None:
        _tray.hide()


def _message_clicked() -> None:
    callback = _on_click
    _hide_tray()
    if callback is not None:
        try:
            callback()
        except RuntimeError:
            pass  # окно уже удалено


def shutdown() -> None:
    """Выход из программы: значок убирается, таймер останавливается."""
    global _on_click
    _on_click = None
    try:
        _hide_tray()
    except RuntimeError:
        pass  # объекты Qt уже удалены


def show_notification(title: str, text: str, *, error: bool = False,
                      on_click: Callable[[], None] | None = None) -> bool:
    """Всплывающее уведомление Windows. False — показать нельзя (нет области уведомлений).

    Значок держится, пока уведомление на экране (`TRAY_VISIBLE_MS`) или до
    щелчка по нему; щелчок вызывает `on_click`.
    """
    global _tray, _hide_timer, _on_click
    app = QApplication.instance()
    if app is None or not QSystemTrayIcon.isSystemTrayAvailable():
        return False
    try:
        if _tray is None:
            _tray = QSystemTrayIcon(app)
            _tray.messageClicked.connect(_message_clicked)
            _hide_timer = QTimer(_tray)
            _hide_timer.setSingleShot(True)
            _hide_timer.timeout.connect(_hide_tray)
        _on_click = on_click
        icon = app.windowIcon()
        if icon.isNull():
            icon = app.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
        _tray.setIcon(icon)
        _tray.setToolTip(tr("app_title"))
        _tray.show()
        kind = (
            QSystemTrayIcon.MessageIcon.Warning if error
            else QSystemTrayIcon.MessageIcon.Information
        )
        _tray.showMessage(title, text, kind, MESSAGE_MS)
        _hide_timer.start(TRAY_VISIBLE_MS)
        return True
    except RuntimeError:  # объект Qt уже удалён (выход из программы)
        log.warning("Не удалось показать уведомление", exc_info=True)
        return False


def play_sound(ok: bool) -> None:
    """Системный звук: обычный — всё успешно, «критическая ошибка» — есть ошибки."""
    if sys.platform != "win32":
        return
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
    except Exception:  # noqa: BLE001 — нет звука — не беда
        log.debug("Не удалось воспроизвести звук", exc_info=True)


def reveal_result(path: Path) -> None:
    """Проводник с выделенным файлом результата (ошибки запуска — OSError)."""
    reveal_in_explorer(path)
