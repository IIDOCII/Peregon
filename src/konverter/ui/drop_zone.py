"""Зона «Перетащите файлы сюда» и разбор перетаскиваемых данных (Ф2).

Сами события перетаскивания принимает главное окно целиком (бросить можно
в любое его место), а зона только показывает подсказку, подсвечивается при
наведении с файлами и даёт кнопку «Выбрать файлы…».

Пока окно пустое, зона крупная: подсказка, пунктир (контраст ≥ 3:1 к фону
в обеих темах) и кнопка. Когда в окне есть группы, задачи или сообщения,
зона сворачивается в узкую строку с кнопкой «+ Добавить файлы»
(`set_compact`): место отдаётся содержимому, бросать файлы можно по-прежнему
на всё окно.
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, QUrl, Signal
from PySide6.QtWidgets import QBoxLayout, QFrame, QLabel, QPushButton, QWidget

from konverter.strings import tr
from konverter.ui.common import MIN_DROP_BORDER_CONTRAST, apply_style, border_color


def _style() -> str:
    """Стили зоны для текущей темы: цвет пунктира берётся из палитры с запасом контраста."""
    dashed = border_color(ratio=MIN_DROP_BORDER_CONTRAST).name()
    return f"""
DropZone {{
    border: 2px dashed {dashed};
    border-radius: 8px;
    background: palette(base);
}}
DropZone[compact="true"] {{
    border: 2px solid transparent;
    background: transparent;
}}
DropZone[active="true"] {{
    border: 2px solid palette(highlight);
    background: palette(alternate-base);
}}
"""


def local_paths_from_mime(mime: QMimeData | None) -> list[Path]:
    """Локальные пути из перетаскиваемых данных (не-файловые URL отбрасываются)."""
    if mime is None or not mime.hasUrls():
        return []
    result: list[Path] = []
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        local = url.toLocalFile()
        if local:
            result.append(Path(local))
    return result


# Текст из буфера: сколько строк смотреть и наибольшая длина строки-пути.
MAX_CLIPBOARD_LINES = 1000
MAX_CLIPBOARD_LINE = 32767

# Локальный путь с буквой диска: «C:\…» или «C:/…».
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _local_drive_path(line: str) -> Path | None:
    """Строка текста → локальный путь с буквой диска или None.

    Сетевые пути (`\\\\host\\share`, `//host/…`, `\\\\?\\UNC\\…`, вообще всё,
    что начинается с двух косых) и `file://` с непустым хостом отбрасываются
    ДО обращения к файловой системе: проверка существования такого пути —
    это обращение по SMB к чужому хосту (утечка NTLM-хеша, зависание окна).
    """
    if len(line) > MAX_CLIPBOARD_LINE or "\x00" in line:
        return None
    if line.lower().startswith("file:"):
        url = QUrl(line)
        if not url.isValid() or url.scheme().lower() != "file" or url.host():
            return None
        line = url.toLocalFile()
    if line[:2] in ("\\\\", "//", "\\/", "/\\") or not _DRIVE_PATH.match(line):
        return None
    return Path(line)


def paths_from_clipboard(mime: QMimeData | None) -> list[Path]:
    """Файлы из буфера обмена: скопированные в Проводнике (URL файлов — как
    при броске) или текст, где каждая строка — локальный путь с буквой диска
    или file:///-адрес существующего файла/папки (сетевые пути — нет)."""
    found = local_paths_from_mime(mime)
    if found or mime is None or not mime.hasText():
        return found
    result: list[Path] = []
    for line in mime.text().splitlines()[:MAX_CLIPBOARD_LINES]:
        path = _local_drive_path(line.strip().strip('"'))
        if path is None:
            continue
        try:
            if path.exists():
                result.append(path)
        except (OSError, ValueError):
            continue  # не путь (запрещённые символы и т.п.)
    return result


class DropZone(QFrame):
    """Подсказка для перетаскивания, кнопка выбора файлов и строка состояния."""

    browse_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("active", False)
        self.setProperty("compact", False)
        apply_style(self, _style)

        self.hint_label = QLabel(tr("drop_hint"), self)
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label.setWordWrap(True)
        font = self.hint_label.font()
        font.setPointSizeF(font.pointSizeF() * 1.15)
        self.hint_label.setFont(font)

        self.browse_button = QPushButton(tr("choose_files"), self)
        self.browse_button.setToolTip(tr("add_files_tip"))
        self.browse_button.clicked.connect(self.browse_requested)

        # Строка состояния: «Определяю форматы…» во время разбора.
        self.status_label = QLabel("", self)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setVisible(False)

        self._layout = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        self._layout.addWidget(self.hint_label)
        self._layout.addWidget(self.browse_button)
        self._layout.addWidget(self.status_label, 1)
        self._apply_layout()

    def _apply_layout(self) -> None:
        compact = self.is_compact()
        layout = self._layout
        self.hint_label.setVisible(not compact)
        if compact:
            layout.setDirection(QBoxLayout.Direction.LeftToRight)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setAlignment(self.browse_button, Qt.AlignmentFlag.AlignLeft)
            self.status_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            self.browse_button.setText(tr("add_files"))
        else:
            layout.setDirection(QBoxLayout.Direction.TopToBottom)
            layout.setContentsMargins(12, 12, 12, 12)
            layout.setAlignment(self.browse_button, Qt.AlignmentFlag.AlignHCenter)
            self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.browse_button.setText(tr("choose_files"))

    def _repolish(self) -> None:
        # Перерисовка с учётом нового значения свойства в стилях.
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def set_active(self, active: bool) -> None:
        """Подсветка: над окном перетаскивают файлы."""
        if bool(self.property("active")) == active:
            return
        self.setProperty("active", active)
        self._repolish()

    def is_active(self) -> bool:
        return bool(self.property("active"))

    def set_compact(self, compact: bool) -> None:
        """Свернуть в строку «+ Добавить файлы» (True) или развернуть (False)."""
        if self.is_compact() == compact:
            return
        self.setProperty("compact", compact)
        self._apply_layout()
        self._repolish()
        self.updateGeometry()

    def is_compact(self) -> bool:
        return bool(self.property("compact"))

    def set_status(self, text: str) -> None:
        """Показывает (или скрывает при пустом тексте) строку состояния."""
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))
