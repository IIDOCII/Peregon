"""Окно «Подготовить к печати»: PPI и размер печати перед конвертацией.

В окне: PPI (целое, `images.PPI_MIN`–`images.PPI_MAX`, по умолчанию 300) и
длинная сторона в сантиметрах; вторая сторона считается по пропорциям картинки.
Размер картинки читается только по заголовку (`images.peek_size`, без
декодирования, с учётом EXIF-поворота); для SVG/RAW он неизвестен — параметры
всё равно можно задать, предел по пикселям проверит ядро. Неверные значения не
исправляются молча: «Сделать» блокируется, причина написана под полями (ядро
проверяет параметры ещё раз). Для группы из нескольких картинок есть
«Применить ко всем» — те же PPI и сантиметры для этой и остальных картинок.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from konverter.converters import images
from konverter.converters.base import ConversionError
from konverter.strings import tr
from konverter.ui.common import use_error_color

DEFAULT_PPI = 300
# PPI «по умолчанию» для файла без PPI (экранный).
SCREEN_PPI = 96.0
DEFAULT_CM = 30.0
# Предупреждать, если картинку придётся увеличить больше чем во столько раз.
UPSCALE_WARN = 2.0
CM_PER_INCH = 2.54


@dataclass(frozen=True)
class PrintChoice:
    """Ответ окна. `kind`: "one" — для этой картинки; "all" — для этой и всех
    остальных картинок группы; "cancel" — эту картинку не готовить."""

    kind: str
    ppi: int = 0
    cm: float = 0.0


CANCEL = PrintChoice("cancel")


def parse_number(text: str) -> float | None:
    """Число из текста (точка или запятая); None — не число."""
    try:
        value = float((text or "").strip().replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


class PrintPpiDialog(QDialog):
    """Модальное окно (см. докстринг модуля). Ответ — `choice` после закрытия."""

    def __init__(self, source: Path, fmt: str, *, index: int = 1, total: int = 1,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.source = Path(source)
        self.total = total
        self.choice: PrintChoice = CANCEL
        peeked = images.peek_size(self.source, fmt)
        self.size = peeked[:2] if peeked else None
        self.current_ppi = peeked[2] if peeked else None
        self.setWindowTitle(tr("print_dialog_title") if total <= 1 else
                            tr("print_dialog_title_n").format(index=index, total=total))
        self.setModal(True)
        self._build_ui()
        self.set_values(DEFAULT_PPI, self.as_is_cm() if self.size else DEFAULT_CM)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        name = QLabel(self.source.name)
        name.setWordWrap(True)
        font = name.font()
        font.setBold(True)
        name.setFont(font)
        layout.addWidget(name)
        layout.addWidget(QLabel(self._current_text()))

        form = QFormLayout()
        self.ppi_edit = QLineEdit()
        self.cm_edit = QLineEdit()
        self.ppi_edit.setToolTip(tr("print_ppi_tip"))
        self.cm_edit.setToolTip(tr("print_cm_tip"))
        for edit in (self.ppi_edit, self.cm_edit):
            edit.setMaximumWidth(120)
            edit.textChanged.connect(self._update)
        self.as_is_button = QPushButton(tr("print_as_is"))
        self.as_is_button.setToolTip(tr("print_as_is_tip"))
        self.as_is_button.setAutoDefault(False)
        self.as_is_button.setEnabled(self.size is not None)
        self.as_is_button.clicked.connect(
            lambda: self.cm_edit.setText(f"{self.as_is_cm():.2f}"))
        cm_row = QHBoxLayout()
        cm_row.addWidget(self.cm_edit)
        cm_row.addWidget(self.as_is_button)
        cm_row.addStretch(1)
        form.addRow(tr("print_ppi"), self.ppi_edit)
        form.addRow(tr("print_cm"), cm_row)
        layout.addLayout(form)

        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        self.warn_label = QLabel()
        self.warn_label.setWordWrap(True)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        use_error_color(self.error_label)
        for label in (self.result_label, self.warn_label, self.error_label):
            layout.addWidget(label)

        buttons = QHBoxLayout()
        self.make_button = QPushButton(tr("print_make"))
        self.cancel_button = QPushButton(tr("button_cancel"))
        self.make_button.setDefault(True)
        self.cancel_button.setAutoDefault(False)
        self.make_button.clicked.connect(lambda: self._answer("one"))
        self.cancel_button.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(self.make_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

        self.apply_all_button = None
        if self.total > 1:
            self.apply_all_button = QPushButton(tr("print_apply_all"))
            self.apply_all_button.setFlat(True)
            self.apply_all_button.setAutoDefault(False)
            self.apply_all_button.setToolTip(tr("print_apply_all_tip"))
            self.apply_all_button.clicked.connect(lambda: self._answer("all"))
            row = QHBoxLayout()
            row.addWidget(self.apply_all_button)
            row.addStretch(1)
            layout.addLayout(row)

    def _current_text(self) -> str:
        if self.size is None:
            return tr("print_unknown_size")
        width, height = self.size
        if self.current_ppi is None:
            return tr("print_current_no_ppi").format(width=width, height=height)
        return tr("print_current").format(width=width, height=height, ppi=f"{self.current_ppi:g}")

    def as_is_cm(self) -> float:
        """Текущий физический размер длинной стороны, см (без PPI в файле — по 96)."""
        return max(self.size) / (self.current_ppi or SCREEN_PPI) * CM_PER_INCH

    def set_values(self, ppi: int, cm: float) -> None:
        self.ppi_edit.setText(str(ppi))
        self.cm_edit.setText(f"{cm:.2f}")

    def values(self) -> tuple[int | float | None, float | None]:
        """(PPI, см) из полей; нечисловое — None, дробное PPI остаётся дробным (не пройдёт проверку)."""
        ppi = parse_number(self.ppi_edit.text())
        if ppi is not None and ppi.is_integer():
            ppi = int(ppi)
        return ppi, parse_number(self.cm_edit.text())

    def validation_error(self) -> str | None:
        """Почему сделать нельзя (None — можно)."""
        try:
            images.check_print(*self.values(), self.size)
        except ConversionError as exc:
            return str(exc)
        return None

    def _update(self) -> None:
        error = self.validation_error()
        self.error_label.setText(error or "")
        self.make_button.setEnabled(error is None)
        if self.apply_all_button is not None:
            self.apply_all_button.setEnabled(error is None)
        result = warn = ""
        if error is None and self.size is not None:
            ppi, cm = self.values()
            width, height = images.print_pixels(*self.size, cm, ppi)
            other = min(width, height) / ppi * CM_PER_INCH
            result = tr("print_result").format(width=width, height=height, other=f"{other:.1f}")
            factor = max(width, height) / max(self.size)
            if factor > UPSCALE_WARN:
                warn = tr("print_upscale").format(factor=f"{factor:.1f}")
        self.result_label.setText(result)
        self.warn_label.setText(warn)

    def _answer(self, kind: str) -> None:
        if self.validation_error() is not None:
            return
        ppi, cm = self.values()
        self.choice = PrintChoice(kind, ppi, cm)
        self.accept()
