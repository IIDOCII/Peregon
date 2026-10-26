"""Окно «Фрагмент для GIF» (Ф20): выбор отрезка видео перед конвертацией в GIF.

Что в окне:
- начало и конец — ползунками (шаг 0,1 с) и полями времени (мм:сс или
  чч:мм:сс, можно с десятыми: «1:10.5»); длина фрагмента — не больше
  `media.GIF_MAX_SECONDS` (60 с), конец позже начала. Неверный фрагмент не
  исправляется молча: «Сделать GIF» и «Предпросмотр» блокируются, причина
  написана под полями (ядро проверяет фрагмент ещё раз);
- по умолчанию: видео ≤ 60 с — целиком, длиннее — 0–60 с. Длительность
  неизвестна — ползунки выключены, время вводится вручную (длина ≤ 60 с;
  конец позже фактической длины ядро просто обрежет);
- картинки первого и последнего кадра фрагмента: обновляются после
  изменения начала/конца с задержкой `FRAMES_DEBOUNCE_MS` (дребезг);
- «Предпросмотр» — маленький GIF (`media.PREVIEW_GIF_SETTINGS`), который
  проигрывается прямо в окне (QMovie из памяти); пока его нет, на его месте
  одна строка подсказки. Щелчок по предпросмотру — пауза/продолжение; если
  в Windows выключены эффекты анимации (`common.system_animations_enabled`),
  предпросмотр сам не проигрывается — показан первый кадр, щелчок запускает;
- «Сделать GIF» (основная кнопка) / «Отмена»; для группы из нескольких видео
  ниже — вторичная строка «Применить ко всем: первые N с» (этот и все
  остальные видео группы — с фрагментом 0..N без новых окон; видео короче
  N — целиком, ядро обрежет конец).

Безопасность и отзывчивость: длительность, кадры и предпросмотр готовит
ДОЧЕРНИЙ процесс (`media.fragment_frames`, `media.gif_preview` →
`_isolated.run_task`, счётчик мест "preview" — окно не ждёт идущих
конвертаций). Вызов идёт в потоке-демоне (`bridge.BackgroundTask`), окно не
зависает. Новый запрос кадров отменяет прежний (его процесс убивается), а
устаревшие ответы отбрасываются по номеру запроса. Закрытие окна (любой
кнопкой или крестиком) отменяет всё: процессы убиваются. Временный файл
предпросмотра создаётся в `data/temp` (`naming.make_temp_file`, с записью в
журнал временных файлов) и удаляется в том же фоновом потоке сразу после
чтения — и при ошибке, и при отмене; GIF показывается из памяти.
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QMovie, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from konverter import paths
from konverter.converters import media
from konverter.converters.base import CancelToken, Cancelled, ConversionError
from konverter.core import naming
from konverter.strings import tr
from konverter.ui import common
from konverter.ui.bridge import BackgroundTask
from konverter.ui.common import use_error_color

log = logging.getLogger(__name__)

# Задержка обновления кадров после изменения начала/конца, мс.
FRAMES_DEBOUNCE_MS = 350
# Размер области картинки кадра, px.
THUMB_BOX = QSize(200, 120)
# Шагов ползунка на секунду (десятые доли).
SLIDER_SCALE = 10

_TIME_RE = re.compile(r"^\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:[.,]\d*)?)\s*$")


def parse_time(text: str) -> float | None:
    """«ss», «мм:сс», «чч:мм:сс», с десятыми («1:10.5», «0:05,3»); None — не распознано.

    Минуты и секунды при старших частях — меньше 60.
    """
    match = _TIME_RE.match(text or "")
    if match is None:
        return None
    first, second, seconds_text = match.groups()
    try:
        seconds = float(seconds_text.replace(",", "."))
    except ValueError:
        return None
    if second is not None:  # чч:мм:сс
        hours, minutes = int(first), int(second)
    elif first is not None:  # мм:сс
        hours, minutes = 0, int(first)
    else:
        hours, minutes = 0, 0
    if (first is not None and seconds >= 60) or (second is not None and minutes >= 60):
        return None
    return hours * 3600 + minutes * 60 + seconds


def format_time(seconds: float) -> str:
    """Секунды → «мм:сс.д» (от часа — «ч:мм:сс.д»)."""
    tenths = max(0, int(round(float(seconds) * SLIDER_SCALE)))
    whole, tenth = divmod(tenths, SLIDER_SCALE)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{tenth}"
    return f"{minutes:02d}:{secs:02d}.{tenth}"


def _seconds_text(value: float) -> str:
    """Число секунд для сообщений: «60», «12.5»."""
    return f"{value:g}"


@dataclass(frozen=True)
class GifChoice:
    """Ответ окна. `kind`: "one" — этот фрагмент; "all" — фрагмент 0..end для
    этого и всех остальных видео группы; "cancel" — это видео не конвертировать."""

    kind: str
    start: float = 0.0
    end: float = 0.0


CANCEL = GifChoice("cancel")


def _frames_job(source: Path, fmt: str, start: float, end: float | None, token: CancelToken):
    return media.fragment_frames(source, fmt, start, end, token)


def _preview_job(source: Path, fmt: str, start: float, end: float, token: CancelToken) -> bytes:
    """Предпросмотр во временном файле `data/temp` (или системной временной папке);
    файл удаляется здесь же — после чтения, ошибки или отмены."""
    folder = paths.temp_dir() or Path(tempfile.gettempdir())
    dst = naming.make_temp_file(folder, "gif")
    try:
        return media.gif_preview(source, fmt, start, end, dst, token)
    finally:
        naming.cleanup(dst)


def _pixmap(image: media.FrameImage | None) -> QPixmap | None:
    if image is None:
        return None
    qimage = QImage(image.data, image.width, image.height, image.width * 3,
                    QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(qimage)


class GifFragmentDialog(QDialog):
    """Модальное окно выбора фрагмента (см. докстринг модуля).

    Ответ — `choice` (`GifChoice`) после закрытия; закрытие без «Сделать GIF»
    / «Применить ко всем» — `CANCEL`.
    """

    # Для тестов и подписчиков: кадры обновлены / ошибка чтения; предпросмотр
    # показан / не удался.
    frames_updated = Signal()
    preview_shown = Signal()
    preview_failed = Signal(str)

    def __init__(self, source: Path, fmt: str, *, index: int = 1, total: int = 1,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.source = Path(source)
        self.fmt = fmt
        self.total = total
        self.choice: GifChoice = CANCEL
        self.max_seconds = float(media.GIF_MAX_SECONDS)
        self.duration: float | None = None
        self.ready = False  # длительность прочитана (первый ответ ребёнка)
        self.read_error: str | None = None
        # Последний принятый ответ с кадрами (для проверки и отладки).
        self.frames_info: media.FragmentInfo | None = None
        self._start = 0.0
        self._end = self.max_seconds
        self._tasks: set[BackgroundTask] = set()
        self._frames_token: CancelToken | None = None
        self._frames_gen = 0
        self._preview_token: CancelToken | None = None
        self._preview_gen = 0
        self._movie: QMovie | None = None
        self._movie_buffer: QBuffer | None = None
        self._closed = False
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(FRAMES_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._request_frames)

        title = tr("gif_dialog_title") if total <= 1 else tr("gif_dialog_title_n").format(
            index=index, total=total)
        self.setWindowTitle(title)
        self.setModal(True)
        self._build_ui()
        self._set_busy_ui()
        self._request_frames(initial=True)

    # --- Интерфейс ---

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        # Имя видео — заголовком окна: полужирное и крупнее остального текста.
        self.name_label = QLabel(tr("gif_dialog_file").format(name=self.source.name))
        self.name_label.setWordWrap(True)
        self.name_label.setTextFormat(Qt.TextFormat.PlainText)
        font = self.name_label.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.2)
        self.name_label.setFont(font)
        layout.addWidget(self.name_label)
        self.duration_label = QLabel(tr("gif_reading"))
        self.duration_label.setWordWrap(True)
        layout.addWidget(self.duration_label)

        grid = QGridLayout()
        self.start_slider = QSlider(Qt.Orientation.Horizontal)
        self.end_slider = QSlider(Qt.Orientation.Horizontal)
        self.start_edit = QLineEdit()
        self.end_edit = QLineEdit()
        for row, (text, slider, edit) in enumerate((
            (tr("gif_start"), self.start_slider, self.start_edit),
            (tr("gif_end"), self.end_slider, self.end_edit),
        )):
            edit.setMaximumWidth(110)
            edit.setToolTip(tr("gif_time_hint"))
            grid.addWidget(QLabel(text), row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(edit, row, 2)
        layout.addLayout(grid)
        self.start_slider.valueChanged.connect(lambda v: self._slider_moved(True, v))
        self.end_slider.valueChanged.connect(lambda v: self._slider_moved(False, v))
        self.start_edit.editingFinished.connect(lambda: self._edit_finished(True))
        self.end_edit.editingFinished.connect(lambda: self._edit_finished(False))
        self.start_edit.textEdited.connect(lambda _t: self._validate())
        self.end_edit.textEdited.connect(lambda _t: self._validate())

        self.length_label = QLabel()
        layout.addWidget(self.length_label)
        self.hint_label = QLabel()
        self.hint_label.setWordWrap(True)
        use_error_color(self.hint_label)
        layout.addWidget(self.hint_label)

        thumbs = QHBoxLayout()
        self.first_label = self._thumb_label()
        self.last_label = self._thumb_label()
        for caption, label in ((tr("gif_first_frame"), self.first_label),
                               (tr("gif_last_frame"), self.last_label)):
            column = QVBoxLayout()
            column.addWidget(QLabel(caption))
            column.addWidget(label)
            thumbs.addLayout(column)
        layout.addLayout(thumbs)

        # Пока предпросмотра нет — одна строка подсказки, а не пустое поле
        # в полэкрана; с GIF метка растёт по размеру кадра.
        self.preview_label = QLabel(tr("gif_preview_hint"))
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setWordWrap(True)
        self.preview_label.installEventFilter(self)
        layout.addWidget(self.preview_label)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.preview_button = QPushButton(tr("gif_preview"))
        self.make_button = QPushButton(tr("gif_make"))
        self.cancel_button = QPushButton(tr("button_cancel"))
        self.make_button.setDefault(True)
        self.preview_button.setAutoDefault(False)
        self.cancel_button.setAutoDefault(False)
        self.preview_button.clicked.connect(self.start_preview)
        self.make_button.clicked.connect(self._make)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.preview_button)
        buttons.addStretch(1)
        buttons.addWidget(self.make_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

        # Вторичная строка под основными кнопками: заметна меньше «Сделать GIF».
        if self.total > 1:
            row = QHBoxLayout()
            self.apply_all_spin = QSpinBox()
            self.apply_all_spin.setRange(1, int(self.max_seconds))
            self.apply_all_spin.setValue(int(self.max_seconds))
            self.apply_all_spin.setSuffix(" с")
            # Стиль windows11 рисует стрелки рядом, а подсказка размера их не
            # учитывает — без запаса «60 с» обрезается.
            self.apply_all_spin.setMinimumWidth(
                self.apply_all_spin.sizeHint().width()
                + 2 * self.apply_all_spin.fontMetrics().height()
            )
            self.apply_all_button = QPushButton()
            self.apply_all_button.setFlat(True)
            self.apply_all_button.setAutoDefault(False)
            self.apply_all_button.setToolTip(tr("gif_apply_all_tip"))
            self.apply_all_spin.valueChanged.connect(self._update_apply_all_text)
            self.apply_all_button.clicked.connect(self._apply_all)
            row.addWidget(self.apply_all_button)
            row.addWidget(self.apply_all_spin)
            row.addStretch(1)
            layout.addLayout(row)
            self._update_apply_all_text()
        else:
            self.apply_all_spin = None
            self.apply_all_button = None

    @staticmethod
    def _thumb_label() -> QLabel:
        label = QLabel()
        label.setFixedSize(THUMB_BOX)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setFrameShape(QLabel.Shape.StyledPanel)
        return label

    def _update_apply_all_text(self) -> None:
        if self.apply_all_button is not None:
            self.apply_all_button.setText(tr("gif_apply_all").format(n=self.apply_all_spin.value()))

    def _set_busy_ui(self) -> None:
        for widget in (self.start_slider, self.end_slider, self.start_edit, self.end_edit,
                       self.preview_button, self.make_button):
            widget.setEnabled(False)
        self.first_label.setText(tr("gif_loading"))
        self.last_label.setText(tr("gif_loading"))

    # --- Фрагмент ---

    @property
    def fragment(self) -> tuple[float, float]:
        return self._start, self._end

    def set_fragment(self, start: float, end: float) -> None:
        """Ставит начало и конец (как ввод пользователя: с ограничением и дребезгом)."""
        self._start = self._clamp(start)
        self._end = self._clamp(end)
        self._sync_widgets()
        self._changed()

    def _clamp(self, value: float) -> float:
        """Ввод пользователя: до десятых, затем в пределах [0, длительность].

        Длительность НЕ округляется: конец «до конца видео» — точная длина
        (видео 0,04 с или 10,04 с — целиком, последний кадр не теряется).
        """
        return self._fit(round(max(0.0, float(value)) * SLIDER_SCALE) / SLIDER_SCALE)

    def _fit(self, value: float) -> float:
        """В пределах [0, длительность] без округления."""
        value = max(0.0, float(value))
        if self.duration is not None:
            value = min(value, self.duration)
        return value

    def _sync_widgets(self) -> None:
        for slider, edit, value in ((self.start_slider, self.start_edit, self._start),
                                    (self.end_slider, self.end_edit, self._end)):
            slider.blockSignals(True)
            slider.setValue(int(round(value * SLIDER_SCALE)))
            slider.blockSignals(False)
            edit.setText(format_time(value))

    def _slider_moved(self, is_start: bool, value: int) -> None:
        slider = self.start_slider if is_start else self.end_slider
        # Крайнее правое положение — точная длительность (а не её округление).
        seconds = self._fit(value / SLIDER_SCALE)
        if value >= slider.maximum() and self.duration is not None:
            seconds = self.duration
        if is_start:
            self._start = seconds
            self.start_edit.setText(format_time(seconds))
        else:
            self._end = seconds
            self.end_edit.setText(format_time(seconds))
        self._changed()

    def _edit_finished(self, is_start: bool) -> None:
        edit = self.start_edit if is_start else self.end_edit
        current = self._start if is_start else self._end
        if edit.text().strip() == format_time(current):
            self._validate()  # текст не правили (потеря фокуса) — значение не трогаем
            return
        value = parse_time(edit.text())
        if value is None:
            self._validate()
            return
        value = self._clamp(value)
        if is_start:
            if value == self._start:
                self._sync_widgets()
                self._validate()
                return
            self._start = value
        else:
            if value == self._end:
                self._sync_widgets()
                self._validate()
                return
            self._end = value
        self._sync_widgets()
        self._changed()

    def _changed(self) -> None:
        """Фрагмент изменился: проверка, старый предпросмотр не годится, кадры — с задержкой."""
        self._cancel_preview()
        self._clear_preview()
        if self._validate() is None:
            self.first_label.setText(tr("gif_loading"))
            self.last_label.setText(tr("gif_loading"))
            self._debounce.start()
        else:
            # Неверный фрагмент: кадров для него нет — ни «Загрузка…», ни старых картинок.
            self._debounce.stop()
            self._cancel_frames()
            self._frames_gen += 1  # ответ уже идущего запроса не нужен
            self.first_label.setText(tr("gif_frame_unavailable"))
            self.last_label.setText(tr("gif_frame_unavailable"))

    def validation_error(self) -> str | None:
        """Почему фрагмент нельзя сделать (None — можно)."""
        for edit in (self.start_edit, self.end_edit):
            if parse_time(edit.text()) is None:
                return tr("gif_err_time_format").format(text=edit.text())
        start, end = self._start, self._end
        if self.duration is not None and start >= self.duration - media.FRAGMENT_EPS:
            return tr("gif_err_start_after_end")
        if end <= start + media.FRAGMENT_EPS:
            return tr("gif_err_end_before_start")
        if end - start > self.max_seconds + media.FRAGMENT_EPS:
            return tr("gif_err_too_long").format(max=_seconds_text(self.max_seconds))
        return None

    def _validate(self) -> str | None:
        error = self.validation_error() if self.ready else (self.read_error or "")
        if self.ready:
            self.hint_label.setText(error or "")
            self.length_label.setText(tr("gif_length").format(
                length=format_time(max(0.0, self._end - self._start))))
        ok = self.ready and error is None
        self.make_button.setEnabled(ok)
        self.preview_button.setEnabled(ok)
        if self.apply_all_button is not None:
            self.apply_all_button.setEnabled(self.ready)
        return error

    # --- Кадры (дочерний процесс) ---

    def _start_task(self, func, on_done) -> BackgroundTask:
        task = BackgroundTask(func, on_done, name="konverter-gif-fragment")
        self._tasks.add(task)
        task.start()
        return task

    def _cancel_frames(self) -> None:
        if self._frames_token is not None:
            self._frames_token.cancel()
            self._frames_token = None

    def _request_frames(self, initial: bool = False) -> None:
        if self._closed:
            return
        self._cancel_frames()
        self._frames_gen += 1
        gen = self._frames_gen
        token = CancelToken()
        self._frames_token = token
        start, end = (0.0, None) if initial else (self._start, self._end)
        source, fmt = self.source, self.fmt
        self._start_task(
            lambda: _frames_job(source, fmt, start, end, token),
            lambda task, result, error: self._frames_done(task, gen, initial, result, error),
        )

    def _frames_done(self, task: BackgroundTask, gen: int, initial: bool, result, error) -> None:
        self._tasks.discard(task)
        if self._closed or gen != self._frames_gen:
            return  # устаревший ответ (фрагмент уже другой) или окно закрыто
        self._frames_token = None
        if isinstance(error, Cancelled):
            return
        if error is not None:
            text = str(error) if isinstance(error, ConversionError) else media.MSG_DAMAGED
            if initial:
                self.read_error = tr("gif_read_failed").format(error=text)
                self.duration_label.setText(self.read_error)
                self.first_label.setText(tr("gif_no_frame"))
                self.last_label.setText(tr("gif_no_frame"))
                self.status_label.setText(self.read_error)
                self._validate()
            else:
                self.status_label.setText(tr("gif_frames_failed").format(error=text))
                self.first_label.setText(tr("gif_no_frame"))
                self.last_label.setText(tr("gif_no_frame"))
            self.frames_updated.emit()
            return
        info: media.FragmentInfo = result
        self.frames_info = info
        if initial:
            self._apply_probe(info)
        self.status_label.setText("")
        self._show_thumb(self.first_label, info.first)
        self._show_thumb(self.last_label, info.last)
        self.frames_updated.emit()

    def _apply_probe(self, info: media.FragmentInfo) -> None:
        """Первый ответ: длительность и фрагмент по умолчанию."""
        self.duration = info.duration
        self.ready = True
        max_text = _seconds_text(self.max_seconds)
        if info.duration is not None:
            self.duration_label.setText(tr("gif_duration").format(
                duration=format_time(info.duration), max=max_text))
            top = int(round(info.duration * SLIDER_SCALE))
            for slider in (self.start_slider, self.end_slider):
                slider.setRange(0, max(1, top))
                slider.setEnabled(True)
        else:
            self.duration_label.setText(tr("gif_duration_unknown").format(max=max_text))
            for slider in (self.start_slider, self.end_slider):
                slider.setEnabled(False)
        self.start_edit.setEnabled(True)
        self.end_edit.setEnabled(True)
        # Фрагмент по умолчанию — как есть (без округления до десятых).
        self._start = self._fit(info.start)
        self._end = self._fit(info.end)
        self._sync_widgets()
        self._validate()

    @staticmethod
    def _show_thumb(label: QLabel, image: media.FrameImage | None) -> None:
        pixmap = _pixmap(image)
        if pixmap is None:
            label.setPixmap(QPixmap())
            label.setText(tr("gif_no_frame"))
            return
        label.setText("")
        # По размеру области внутри рамки (а не всей метки): иначе картинка
        # закрывает рамку и её правый край не виден. С учётом масштаба экрана.
        ratio = label.devicePixelRatioF()
        inner = label.contentsRect().size()
        scaled = pixmap.scaled(inner * ratio, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio)
        label.setPixmap(scaled)

    # --- Предпросмотр (дочерний процесс) ---

    def _cancel_preview(self) -> None:
        if self._preview_token is not None:
            self._preview_token.cancel()
            self._preview_token = None
            self._preview_gen += 1  # ответ отменённого запроса не нужен

    def _clear_preview(self) -> None:
        if self._movie is not None:
            self._movie.stop()
            self.preview_label.setMovie(None)
            self._movie.deleteLater()
            self._movie = None
        if self._movie_buffer is not None:
            self._movie_buffer.close()
            self._movie_buffer.deleteLater()
            self._movie_buffer = None
        self.preview_label.setToolTip("")
        self.preview_label.setText(tr("gif_preview_hint"))

    @property
    def movie(self) -> QMovie | None:
        return self._movie

    def toggle_preview(self) -> None:
        """Щелчок по предпросмотру: пауза или продолжение."""
        movie = self._movie
        if movie is None:
            return
        if movie.state() == QMovie.MovieState.Running:
            movie.setPaused(True)
        elif movie.state() == QMovie.MovieState.Paused:
            movie.setPaused(False)
        else:
            movie.start()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if (
            watched is self.preview_label
            and event.type() == QEvent.Type.MouseButtonRelease
            and self._movie is not None
        ):
            self.toggle_preview()
            return True
        return super().eventFilter(watched, event)

    def start_preview(self) -> None:
        """Кнопка «Предпросмотр»."""
        if self._closed or self._validate() is not None:
            return
        self._cancel_preview()
        self._clear_preview()
        self._preview_gen += 1
        gen = self._preview_gen
        token = CancelToken()
        self._preview_token = token
        start, end = self._start, self._end
        source, fmt = self.source, self.fmt
        self.preview_label.setText(tr("gif_preview_loading"))
        self._start_task(
            lambda: _preview_job(source, fmt, start, end, token),
            lambda task, result, error: self._preview_done(task, gen, result, error),
        )

    def _preview_done(self, task: BackgroundTask, gen: int, result, error) -> None:
        self._tasks.discard(task)
        if self._closed or gen != self._preview_gen:
            return
        self._preview_token = None
        if isinstance(error, Cancelled):
            return
        if error is not None or not isinstance(result, bytes):
            text = str(error) if isinstance(error, ConversionError) else media.MSG_DAMAGED
            message = tr("gif_preview_failed").format(error=text)
            self.preview_label.setText(message)
            self.preview_failed.emit(message)
            return
        data = QByteArray(result)
        buffer = QBuffer(self)
        buffer.setData(data)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        movie = QMovie(self)
        movie.setDevice(buffer)
        if not movie.isValid():
            buffer.close()
            buffer.deleteLater()
            movie.deleteLater()
            message = tr("gif_preview_failed").format(error=media.MSG_DAMAGED)
            self.preview_label.setText(message)
            self.preview_failed.emit(message)
            return
        self._movie_buffer = buffer
        self._movie = movie
        self.preview_label.setText("")
        self.preview_label.setMovie(movie)
        self.preview_label.setToolTip(tr("gif_preview_toggle_tip"))
        movie.start()
        if not common.system_animations_enabled():
            # Анимации в Windows выключены: первый кадр, без проигрывания.
            movie.setPaused(True)
        self.preview_shown.emit()

    # --- Ответ ---

    def _make(self) -> None:
        if self._validate() is not None:
            return
        self.choice = GifChoice("one", self._start, self._end)
        self.accept()

    def _apply_all(self) -> None:
        if not self.ready or self.apply_all_spin is None:
            return
        seconds = float(min(self.apply_all_spin.value(), self.max_seconds))
        self.choice = GifChoice("all", 0.0, seconds)
        self.accept()

    def shutdown(self) -> None:
        """Отменяет всё фоновое (процессы убиваются); повторный вызов безвреден."""
        self._closed = True
        self._debounce.stop()
        self._cancel_frames()
        self._cancel_preview()
        if self._movie is not None:
            self._movie.stop()

    def done(self, result: int) -> None:  # noqa: D401 — закрытие любым способом
        self.shutdown()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802 (имя из Qt)
        self.shutdown()
        super().closeEvent(event)
