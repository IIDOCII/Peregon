"""Панель одной группы файлов (Ф3, Ф5–Ф7) и панель сообщений (Ф4, Ф18).

Панель группы: заголовок «Изображения — 3 файла», метки несовпадения
расширения с содержимым, выбор варианта, выбор пресета (только если
вариант от него зависит), кнопки «Конвертировать» (основная кнопка) и
«Убрать группу» (и клавиша Del на карточке). Если вариантов нет — текст
«пока не поддерживаются» без кнопки.

Панель сообщения: значок (предупреждение или информация) слева, текст,
кнопка «Закрыть». Рамка у групп, сообщений и строк задач одна
(`common.card_style`).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from konverter.converters.base import Preset, Target
from konverter.core.grouping import Group
from konverter.strings import tr
from konverter.ui.common import (
    PRESET_ORDER,
    apply_style,
    card_style,
    category_name,
    files_count,
    limited_lines,
    preset_name,
    standard_icon_label,
)

# Рамка карточек — общая с сообщениями и строками задач.
_PANEL_STYLE = card_style("GroupPanel", "NoticePanel")

# Вид сообщения: значок стиля слева.
NOTICE_WARNING = "warning"
NOTICE_INFO = "info"
_NOTICE_ICONS = {
    NOTICE_WARNING: QStyle.StandardPixmap.SP_MessageBoxWarning,
    NOTICE_INFO: QStyle.StandardPixmap.SP_MessageBoxInformation,
}


def _close_button(parent: QWidget, tooltip: str) -> QToolButton:
    """Маленькая кнопка «×» со стандартной иконкой Qt."""
    button = QToolButton(parent)
    button.setIcon(parent.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton))
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setAutoRaise(True)
    return button


class GroupPanel(QFrame):
    """Группа файлов одной категории с выбором варианта конвертации."""

    convert_requested = Signal(object)  # аргумент — сама панель
    remove_requested = Signal(object)
    preset_changed = Signal(object)  # аргумент — выбранный Preset

    def __init__(
        self,
        group: Group,
        targets: list[Target],
        default_preset: Preset,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.group = group
        self.targets = list(targets)
        apply_style(self, _PANEL_STYLE)
        # Del на карточке убирает группу: карточка берёт фокус щелчком и Tab.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(4)

        # Заголовок и кнопка «убрать».
        header = QHBoxLayout()
        self.title_label = QLabel(
            tr("group_title").format(
                category=category_name(group.category),
                count=files_count(len(group.files)),
            ),
            self,
        )
        font = self.title_label.font()
        font.setBold(True)
        self.title_label.setFont(font)
        self.title_label.setWordWrap(True)
        self.remove_button = _close_button(self, tr("remove_group"))
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        header.addWidget(self.title_label, 1)
        header.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        # Несовпадения расширения с содержимым (Ф3).
        mismatches = [
            tr("mismatch").format(name=path.name, label=fmt.label)
            for path, fmt in group.files
            if fmt.extension_mismatch
        ]
        # Значок предупреждения + обычный цвет текста (не цвет ссылки).
        self.mismatch_row = QWidget(self)
        mismatch_layout = QHBoxLayout(self.mismatch_row)
        mismatch_layout.setContentsMargins(0, 0, 0, 0)
        self.mismatch_icon = standard_icon_label(
            self.mismatch_row, QStyle.StandardPixmap.SP_MessageBoxWarning
        )
        self.mismatch_label = QLabel("\n".join(limited_lines(mismatches)), self.mismatch_row)
        self.mismatch_label.setWordWrap(True)
        self.mismatch_label.setTextFormat(Qt.TextFormat.PlainText)
        mismatch_layout.addWidget(self.mismatch_icon, 0, Qt.AlignmentFlag.AlignTop)
        mismatch_layout.addWidget(self.mismatch_label, 1)
        self.mismatch_label.setVisible(bool(mismatches))
        self.mismatch_row.setVisible(bool(mismatches))
        layout.addWidget(self.mismatch_row)

        self.target_combo = QComboBox(self)
        self.preset_combo = QComboBox(self)
        for combo in (self.target_combo, self.preset_combo):
            # Минимальная ширина — не по самой длинной подписи («PDF (отдельный
            # файл на каждое изображение)»), иначе панель шире окна и правый
            # край (кнопки «×», «Конвертировать») обрезается. Список при
            # раскрытии всё равно показывает подписи целиком.
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(8)
        self.target_caption = QLabel(tr("target_label"), self)
        self.preset_caption = QLabel(tr("preset_label"), self)
        self.convert_button = QPushButton(tr("convert"), self)
        # Основная кнопка карточки: стиль рисует её акцентной (свои цвета не задаём).
        self.convert_button.setDefault(True)
        self.convert_button.setAutoDefault(True)
        self.unsupported_label = QLabel(tr("group_not_supported"), self)
        self.unsupported_label.setWordWrap(True)

        if not self.targets:
            # Ф5: вариантов нет — только сообщение, без кнопки.
            for widget in (
                self.target_combo, self.preset_combo, self.target_caption,
                self.preset_caption, self.convert_button,
            ):
                widget.setVisible(False)
            layout.addWidget(self.unsupported_label)
            return
        self.unsupported_label.setVisible(False)

        for target in self.targets:
            self.target_combo.addItem(target.label, target.id)
        for preset in PRESET_ORDER:
            self.preset_combo.addItem(preset_name(preset), preset.value)
        for combo in (self.target_combo, self.preset_combo):
            # Раскрытый список — по ширине самой длинной подписи (Qt не даёт
            # ему выйти за край экрана), а сама подпись видна и в подсказке.
            view = combo.view()
            view.setMinimumWidth(view.sizeHintForColumn(0) + 2 * view.frameWidth() + 8)
            combo.setToolTip(combo.currentText())
            combo.currentTextChanged.connect(combo.setToolTip)
        self.set_preset(default_preset)

        # «Во что:» и «Качество:» — в одну колонку, поля выровнены.
        self.form = QFormLayout()
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.target_caption.setBuddy(self.target_combo)
        self.preset_caption.setBuddy(self.preset_combo)
        self.form.addRow(self.target_caption, self.target_combo)
        self.form.addRow(self.preset_caption, self.preset_combo)
        layout.addLayout(self.form)
        layout.addWidget(self.convert_button, 0, Qt.AlignmentFlag.AlignRight)

        self.target_combo.currentIndexChanged.connect(self._update_preset_visibility)
        self.preset_combo.currentIndexChanged.connect(
            lambda _index: self.preset_changed.emit(self.selected_preset())
        )
        self.convert_button.clicked.connect(lambda: self.convert_requested.emit(self))
        self._update_preset_visibility()

    # --- Состояние выбора ---

    @property
    def has_targets(self) -> bool:
        return bool(self.targets)

    def selected_target(self) -> Target | None:
        index = self.target_combo.currentIndex()
        if 0 <= index < len(self.targets):
            return self.targets[index]
        return None

    def set_target(self, target_id: str) -> bool:
        """Выбирает вариант по коду. False — такого варианта нет."""
        index = self.target_combo.findData(target_id)
        if index < 0:
            return False
        self.target_combo.setCurrentIndex(index)
        return True

    def selected_preset(self) -> Preset:
        value = self.preset_combo.currentData()
        try:
            return Preset(value)
        except ValueError:
            return Preset.MEDIUM

    def set_preset(self, preset: Preset) -> None:
        index = self.preset_combo.findData(preset.value)
        if index >= 0:
            self.preset_combo.setCurrentIndex(index)

    def preset_enabled(self) -> bool:
        """Влияет ли пресет на выбранный вариант (тогда выбор пресета показан)."""
        target = self.selected_target()
        return bool(target and target.uses_preset)

    def _update_preset_visibility(self) -> None:
        # Ф7: выбор пресета виден только для вариантов, на которые он влияет.
        enabled = self.preset_enabled()
        self.form.setRowVisible(self.preset_combo, enabled)
        self.preset_combo.setVisible(enabled)
        self.preset_caption.setVisible(enabled)
        self.preset_combo.setEnabled(enabled)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (имя из Qt)
        """Del — убрать группу, только когда фокус на самой карточке.

        Del, не принятый списком или кнопкой карточки, тоже доходит сюда —
        его пропускаем: группа не должна пропадать из-за Del в списке.
        """
        focus = self.window().focusWidget()
        if (
            event.key() == Qt.Key.Key_Delete
            and event.modifiers() in (
                Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier
            )
            and (focus is None or focus is self)
        ):
            event.accept()
            self.remove_requested.emit(self)
            return
        super().keyPressEvent(event)


class NoticePanel(QFrame):
    """Закрываемое сообщение для пользователя (неподдерживаемые файлы и т.п.)."""

    closed = Signal(object)

    def __init__(self, text: str, parent: QWidget | None = None, *,
                 kind: str = NOTICE_WARNING) -> None:
        super().__init__(parent)
        self.kind = kind if kind in _NOTICE_ICONS else NOTICE_WARNING
        apply_style(self, _PANEL_STYLE)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 4, 6)
        self.icon_label = standard_icon_label(self, _NOTICE_ICONS[self.kind])
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        self.text_label = QLabel(text, self)
        self.text_label.setWordWrap(True)
        self.text_label.setTextFormat(Qt.TextFormat.PlainText)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.close_button = _close_button(self, tr("close_notice"))
        self.close_button.clicked.connect(lambda: self.closed.emit(self))
        layout.addWidget(self.text_label, 1)
        layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)

    def text(self) -> str:
        return self.text_label.text()
