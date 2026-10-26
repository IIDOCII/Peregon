"""Общие мелочи интерфейса: подписи категорий и пресетов, склонение числа,
вопросы с кнопками-действиями, цвета от темы (ошибка, вторичный текст, рамки),
базовый шрифт, иконка программы, системная настройка анимаций."""

from __future__ import annotations

import functools
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QSize, QTranslator
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QStyle, QWidget

from konverter.converters.base import Category, Preset
from konverter.strings import tr

# Порядок пресетов в выпадающем списке (Ф7).
PRESET_ORDER = (Preset.HIGH, Preset.MEDIUM, Preset.SMALL)

# Сколько строк показывать в списках (несовпадения, неподдерживаемые),
# остальное — «…и ещё N».
MAX_LIST_LINES = 8

# Базовый размер шрифта приложения, pt (семейство — системное, Segoe UI).
BASE_FONT_POINT_SIZE = 10.5

# Наименьший контраст текста к фону (WCAG AA для обычного текста).
MIN_TEXT_CONTRAST = 4.5
# Контраст пунктира зоны броска к фону (WCAG для значимых границ — 3:1).
MIN_DROP_BORDER_CONTRAST = 3.0
# Контраст рамки карточек (групп, задач, сообщений) — заметная, но спокойная.
MIN_CARD_BORDER_CONTRAST = 1.6

# Цвета ошибки Windows 11 (SystemFillColorCritical) для светлой и тёмной темы;
# если к фону палитры их контраста не хватает, яркость подстраивается.
_ERROR_LIGHT = "#c42b1c"
_ERROR_DARK = "#ff99a4"

# Папка иконки программы (саму иконку рисуют отдельно).
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ICON_NAMES = ("icon.svg", "icon.ico")

# Размер значков у статусов и сообщений, px (логические).
ICON_SIZE = 16


def category_name(category: Category) -> str:
    """Название категории по-русски."""
    return tr(f"category_{category.value}")


def preset_name(preset: Preset) -> str:
    """Название пресета по-русски."""
    return tr(f"preset_{preset.value}")


def plural_form(n: int) -> str:
    """Форма русского слова для числа n: "one" (1, 21), "few" (2–4, 22) или "many"."""
    last_two = abs(n) % 100
    last = abs(n) % 10
    if last == 1 and last_two != 11:
        return "one"
    if 2 <= last <= 4 and not 12 <= last_two <= 14:
        return "few"
    return "many"


def files_count(n: int) -> str:
    """«1 файл», «3 файла», «5 файлов» — с правильным склонением."""
    return tr(f"files_{plural_form(n)}").format(n=n)


def skipped_same_format_text(n: int, format_name: str) -> str:
    """«2 файла уже в формате PNG — пропущены» (причастие согласуется с числом)."""
    key = "skipped_same_format_one" if plural_form(n) == "one" else "skipped_same_format_many"
    return tr(key).format(count=files_count(n), format=format_name)


def limited_lines(lines: list[str], limit: int = MAX_LIST_LINES) -> list[str]:
    """Первые `limit` строк и, если есть ещё, строка «…и ещё N»."""
    if len(lines) <= limit:
        return list(lines)
    return [*lines[:limit], tr("and_more").format(n=len(lines) - limit)]


# --- Вопросы ----------------------------------------------------------------

# Строки самого Qt (контекст, исходный текст) → ключ strings.py. Без них
# кнопка подробностей QMessageBox подписана по-английски («Show Details...»).
_QT_STRINGS = {
    ("QMessageBox", "Show Details..."): "button_show_details",
    ("QMessageBox", "Hide Details..."): "button_hide_details",
}


class _QtStringsTranslator(QTranslator):
    """Переводит несколько встроенных строк Qt подписями из strings.py."""

    def translate(self, context, source_text, disambiguation=None, n=-1):  # noqa: D401
        key = _QT_STRINGS.get((context, source_text))
        return tr(key) if key else ""

    def isEmpty(self) -> bool:  # noqa: N802 (имя из Qt)
        return False


_qt_translator: _QtStringsTranslator | None = None


def install_qt_translations() -> None:
    """Ставит переводчик встроенных строк Qt (один раз на приложение)."""
    global _qt_translator
    app = QApplication.instance()
    if app is None or _qt_translator is not None:
        return
    _qt_translator = _QtStringsTranslator(app)
    app.installTranslator(_qt_translator)


def ask_choice(
    parent,
    title: str,
    text: str,
    accept_text: str,
    reject_text: str,
    *,
    default_accept: bool = False,
    informative: str = "",
    detailed: str = "",
) -> bool:
    """Вопрос с двумя кнопками-действиями («Скачать (375 МБ)» / «Не сейчас»).

    True — нажата `accept_text`. Стандартные кнопки QMessageBox без
    QTranslator подписаны по-английски, поэтому кнопки свои, с ролями
    Yes/No (порядок — как у системного диалога). Esc и закрытие крестиком —
    всегда `reject_text` (безопасный вариант); кнопка по умолчанию (Enter) —
    по `default_accept`. `informative` — пояснение под вопросом,
    `detailed` — текст под кнопкой «Подробнее».
    """
    from PySide6.QtWidgets import QMessageBox

    install_qt_translations()
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    if detailed:
        box.setDetailedText(detailed)
    accept = box.addButton(accept_text, QMessageBox.ButtonRole.YesRole)
    reject = box.addButton(reject_text, QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(accept if default_accept else reject)
    box.setEscapeButton(reject)
    try:
        box.exec()
        return box.clickedButton() is accept
    finally:
        box.deleteLater()


def ask_yes_no(parent, title: str, text: str, *, default_yes: bool = False) -> bool:
    """Вопрос с кнопками «Да» / «Нет» (подписи из strings.py). True — «Да»."""
    return ask_choice(
        parent, title, text, tr("button_yes"), tr("button_no"), default_accept=default_yes
    )


# --- Цвета от темы ----------------------------------------------------------


def _luminance(color: QColor) -> float:
    """Относительная яркость (WCAG 2.x) непрозрачного цвета."""

    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.redF())
        + 0.7152 * channel(color.greenF())
        + 0.0722 * channel(color.blueF())
    )


def contrast_ratio(first: QColor, second: QColor) -> float:
    """Контраст двух непрозрачных цветов по WCAG: от 1 до 21."""
    a, b = _luminance(first), _luminance(second)
    if a < b:
        a, b = b, a
    return (a + 0.05) / (b + 0.05)


def blend(color: QColor, background: QColor) -> QColor:
    """Цвет с прозрачностью, наложенный на непрозрачный фон (каким его видно)."""
    alpha = color.alphaF()
    return QColor.fromRgbF(
        color.redF() * alpha + background.redF() * (1 - alpha),
        color.greenF() * alpha + background.greenF() * (1 - alpha),
        color.blueF() * alpha + background.blueF() * (1 - alpha),
    )


def _mix(first: QColor, second: QColor, t: float) -> QColor:
    """Цвет между first (t=0) и second (t=1)."""
    return QColor.fromRgbF(
        first.redF() + (second.redF() - first.redF()) * t,
        first.greenF() + (second.greenF() - first.greenF()) * t,
        first.blueF() + (second.blueF() - first.blueF()) * t,
    )


def _backgrounds(palette: QPalette) -> list[QColor]:
    """Фоны, на которых бывает текст: окно и поле ввода (как их видно)."""
    window = palette.color(QPalette.ColorRole.Window)
    window = blend(window, QColor("white")) if window.alpha() < 255 else window
    base = blend(palette.color(QPalette.ColorRole.Base), window)
    return [window, base]


def _min_contrast(color: QColor, backgrounds: list[QColor]) -> float:
    return min(contrast_ratio(color, bg) for bg in backgrounds)


def is_dark_palette(palette: QPalette) -> bool:
    """Тёмная ли тема: светлый текст на тёмном фоне."""
    backgrounds = _backgrounds(palette)
    return _luminance(backgrounds[0]) < 0.25


def _towards_contrast(color: QColor, target: QColor, backgrounds: list[QColor],
                      ratio: float) -> QColor:
    """Ближайший к `color` цвет на пути к `target` с контрастом не меньше `ratio`."""
    if _min_contrast(color, backgrounds) >= ratio:
        return color
    for step in range(1, 41):
        candidate = _mix(color, target, step / 40)
        if _min_contrast(candidate, backgrounds) >= ratio:
            return candidate
    return QColor(target)


def _app_palette() -> QPalette:
    app = QApplication.instance()
    return app.palette() if app is not None else QPalette()


def error_color(palette: QPalette | None = None) -> QColor:
    """Цвет текста ошибки: контраст ≥ 4,5:1 к фону окна и полей в любой теме."""
    palette = palette or _app_palette()
    backgrounds = _backgrounds(palette)
    if is_dark_palette(palette):
        return _towards_contrast(QColor(_ERROR_DARK), QColor("white"), backgrounds,
                                 MIN_TEXT_CONTRAST)
    return _towards_contrast(QColor(_ERROR_LIGHT), QColor("black"), backgrounds,
                             MIN_TEXT_CONTRAST)


def secondary_text_color(palette: QPalette | None = None) -> QColor:
    """Вторичный текст (статус задачи): цвет подсказок палитры, но читаемый (≥ 4,5:1)."""
    palette = palette or _app_palette()
    backgrounds = _backgrounds(palette)
    window = backgrounds[0]
    text = blend(palette.color(QPalette.ColorRole.WindowText), window)
    secondary = blend(palette.color(QPalette.ColorRole.PlaceholderText), window)
    return _towards_contrast(secondary, text, backgrounds, MIN_TEXT_CONTRAST)


def border_color(palette: QPalette | None = None, ratio: float = MIN_CARD_BORDER_CONTRAST) -> QColor:
    """Цвет рамки: смесь фона окна с цветом текста с контрастом ≥ `ratio` к фону."""
    palette = palette or _app_palette()
    window = _backgrounds(palette)[0]
    text = blend(palette.color(QPalette.ColorRole.WindowText), window)
    return _towards_contrast(window, text, [window], ratio)


class _ThemeWatcher(QObject):
    """Вызывает `callback(widget)` при смене палитры (в том числе темы Windows на лету).

    Виджет передаётся аргументом, а не захватывается замыканием: иначе
    виджет → наблюдатель (дочерний объект) → замыкание → виджет — цикл
    ссылок, и такой виджет без родителя удалял бы сборщик мусора — в любом
    потоке. Поэтому `callback` не должен ссылаться на виджет сам.
    """

    _EVENTS = (QEvent.Type.ApplicationPaletteChange, QEvent.Type.PaletteChange)

    def __init__(self, widget: QWidget, callback: Callable[[QWidget], None]) -> None:
        super().__init__(widget)
        self._callback = callback
        self._busy = False
        widget.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() in self._EVENTS and not self._busy:
            self._busy = True  # своя же setPalette/setStyleSheet не зацикливает
            try:
                self._callback(watched)
            finally:
                self._busy = False
        return False


def watch_theme(widget: QWidget, callback: Callable[[QWidget], None]) -> None:
    """Сразу применяет `callback(widget)` и повторяет при каждой смене палитры.

    `callback` не должен ссылаться на `widget` (см. `_ThemeWatcher`)."""
    callback(widget)
    _ThemeWatcher(widget, callback)


def _set_text_color(widget: QWidget, color: QColor) -> None:
    palette = widget.palette()
    if palette.color(QPalette.ColorRole.WindowText) != color:
        palette.setColor(QPalette.ColorRole.WindowText, color)
        widget.setPalette(palette)


def _apply_error_color(widget: QWidget) -> None:
    _set_text_color(widget, error_color())


def _apply_secondary_color(widget: QWidget) -> None:
    _set_text_color(widget, secondary_text_color())


def use_error_color(label: QLabel) -> None:
    """Текст метки — цветом ошибки, с пересчётом при смене темы."""
    watch_theme(label, _apply_error_color)


def use_secondary_color(label: QLabel) -> None:
    """Текст метки — вторичным цветом, с пересчётом при смене темы."""
    watch_theme(label, _apply_secondary_color)


def card_style(*class_names: str) -> Callable[[], str]:
    """Функция, дающая единую таблицу стилей рамки карточек для текущей темы."""
    selector = ", ".join(class_names)

    def build() -> str:
        color = border_color().name()
        return f"{selector} {{ border: 1px solid {color}; border-radius: 6px; }}"

    return build


def _apply_sheet(widget: QWidget, build: Callable[[], str]) -> None:
    sheet = build()
    if widget.styleSheet() != sheet:
        widget.setStyleSheet(sheet)


def apply_style(widget: QWidget, build: Callable[[], str]) -> None:
    """Таблица стилей `build()` на виджете, с пересчётом при смене темы.

    `build` не должен ссылаться на виджет (см. `_ThemeWatcher`)."""
    watch_theme(widget, functools.partial(_apply_sheet, build=build))


# --- Значки -----------------------------------------------------------------


def standard_icon_label(parent: QWidget, pixmap: QStyle.StandardPixmap,
                        size: int = ICON_SIZE) -> QLabel:
    """Метка со стандартным значком стиля (предупреждение, информация…)."""
    label = QLabel(parent)
    set_standard_icon(label, pixmap, size)
    return label


def set_standard_icon(label: QLabel, pixmap: QStyle.StandardPixmap | None,
                      size: int = ICON_SIZE) -> None:
    """Ставит (или убирает при None) стандартный значок на метку."""
    if pixmap is None:
        label.clear()
        label.setVisible(False)
        return
    icon = label.style().standardIcon(pixmap, None, label)
    label.setPixmap(icon.pixmap(QSize(size, size)))
    label.setFixedSize(size, size)
    label.setVisible(True)


# --- Приложение ---------------------------------------------------------------


def apply_base_font(app: QApplication, point_size: float = BASE_FONT_POINT_SIZE) -> None:
    """Базовый шрифт приложения: системное семейство (Segoe UI), размер `point_size`."""
    font = app.font()
    if font.pointSizeF() != point_size:
        font.setPointSizeF(point_size)
        app.setFont(font)


def app_icon_path(folder: Path | None = None) -> Path | None:
    """Путь к иконке программы (icon.svg или icon.ico в ui/assets) или None."""
    folder = ASSETS_DIR if folder is None else Path(folder)
    for name in ICON_NAMES:
        path = folder / name
        if path.is_file():
            return path
    return None


def load_app_icon(folder: Path | None = None) -> QIcon | None:
    """Иконка программы из ui/assets; файла нет (или он не читается) — None."""
    path = app_icon_path(folder)
    if path is None:
        return None
    icon = QIcon(str(path))
    return None if icon.isNull() else icon


def system_animations_enabled() -> bool:
    """Включены ли анимации в Windows («Эффекты анимации» в параметрах).

    Читается SPI_GETCLIENTAREAANIMATION; не Windows или ошибка — True.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        spi_getclientareaanimation = 0x1042
        value = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            spi_getclientareaanimation, 0, ctypes.byref(value), 0
        )
        return bool(value.value) if ok else True
    except Exception:  # noqa: BLE001 — нет ответа системы: анимации считаем включёнными
        return True
