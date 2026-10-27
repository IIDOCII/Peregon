"""Главное окно: небольшое, поверх всех окон, принимает перетаскиваемые файлы.

Что где:
- сверху — зона «Перетащите файлы сюда» с кнопкой «Выбрать файлы…»
  (`drop_zone.py`); когда в окне есть группы, задачи или сообщения, она
  сворачивается в строку «+ Добавить файлы»; бросить файлы можно в любое
  место окна; Ctrl+O — выбрать файлы, Ctrl+V — вставить файлы из буфера;
- ниже — прокручиваемая область: сообщения (неподдерживаемые файлы и т.п.,
  со значком), панели групп (`group_panel.py`) и под ними «Конвертировать
  всё» (если групп две и больше), список задач (`job_list.py`). Новая задача
  прокручивается в видимую часть.

Когда завершились все задачи «партии» (очередь опустела, и хотя бы одна
задача завершилась): уведомление Windows (сколько готово / с ошибками /
отменено), системный звук и Проводник с последним результатом — каждое
включается своим ключом настроек (`Settings.notify_on_finish` и т.д.;
Проводник по умолчанию выключен). Партия, где всё отменено, не сообщается;
пока открыт вопрос «Закрыть программу?», итог откладывается. Сами действия —
в `notify.py`.

Вся долгая работа — в фоне: разбор файлов на группы (`build_groups`) и
прогрев magika — в потоках-демонах (`bridge.BackgroundTask`), конвертация — в `JobManager`; уведомления
из рабочих потоков приходят в главный поток через `bridge.py`.

Модуль LibreOffice (Ф15–Ф19): если у выбранного варианта группы есть файлы,
которым нужен модуль (`converter.module_for`), а модуля нет, окно один раз
спрашивает разрешение на скачивание (после выбора папки результата: если
выбор папки отменён, задачи и скачивание не ставятся). Устаревший модуль
(версия ниже допустимой) — тот же вопрос с пояснением. Согласие — служебная задача
«Модуль LibreOffice» в очереди "office" (прогресс, «Отмена»), офисные
задачи встают в ту же очередь за ней. Отказ — ничего не скачивается, задачи
группы ставятся и сразу завершаются понятной ошибкой «модуль не скачан».
Отказ не запоминается: следующая офисная конвертация спросит снова. Пока
скачивание идёт, новые офисные группы просто встают в очередь за ним (один
вопрос и одно скачивание). Если `data/` недоступна для записи — вопрос не
задаётся, задачи завершаются объяснением (Ф19).

Видео → GIF (Ф20): при «Конвертировать» для каждого видео группы по очереди
открывается модальное окно «Фрагмент для GIF» (`gif_fragment.py`); выбранный
фрагмент уходит в задачу (`JobManager.submit(..., options={"fragment": …})`),
«Отмена» — это видео не конвертируется, «Применить ко всем» — остальные
видео группы ставятся с фрагментом 0..N без новых окон. Уже идущие задачи
окно не останавливает.

Изображения → «Для печати (PPI)»: так же по очереди окно «Подготовить к
печати» (`print_ppi.py`); PPI и сантиметры уходят в задачу
(`options={"print_ppi": (ppi, см)}`), «Применить ко всем» — остальные
картинки группы с теми же значениями без окон.

Окно растёт по высоте вместе с содержимым (до доли экрана, дальше —
прокрутка); если оно в нижней половине экрана, растёт вверх, чтобы
не уходить за край. Позиция запоминается в настройках (Ф1).
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, Iterable

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import (
    QCloseEvent,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QGuiApplication,
    QKeySequence,
    QResizeEvent,
    QShortcut,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import konverter.converters  # noqa: F401 — регистрирует конвертеры в REGISTRY
from konverter import paths
from konverter.converters.base import REGISTRY, Category, Converter, DetectedFormat, Preset, Target
from konverter.core import jobs as core_jobs
from konverter.core import naming
from konverter.core.detect import detect, warm_up
from konverter.core.grouping import (
    Group,
    GroupingResult,
    TargetSplit,
    build_groups,
    common_targets,
    split_for_target,
)
from konverter.core.jobs import Job, JobManager
from konverter.core.settings import load_settings, save_settings
from konverter.libreoffice import module as lo_module
from konverter.strings import tr
from konverter.ui import gc_guard, notify
from konverter.ui.bridge import BackgroundTask, JobBridge
from konverter.ui.common import (
    apply_base_font,
    ask_choice,
    category_name,
    files_count,
    limited_lines,
    load_app_icon,
    skipped_same_format_text,
)
from konverter.ui.drop_zone import DropZone, local_paths_from_mime, paths_from_clipboard
from konverter.ui.gif_fragment import CANCEL as GIF_CANCEL
from konverter.ui.gif_fragment import GifChoice, GifFragmentDialog
from konverter.ui.group_panel import NOTICE_INFO, NOTICE_WARNING, GroupPanel, NoticePanel
from konverter.ui.job_list import JobList, JobRow, reveal_in_explorer
from konverter.ui.print_ppi import CANCEL as PRINT_CANCEL
from konverter.ui.print_ppi import PrintChoice, PrintPpiDialog

log = logging.getLogger(__name__)

# Начальный размер окна (клиентская область), px; высота — она же минимальная
# для автоподбора.
WINDOW_WIDTH = 380
WINDOW_HEIGHT = 220
MIN_WIDTH = 300

# Отступ окна от краёв доступной области экрана, px.
SCREEN_MARGIN = 16

# Наибольшая высота окна при автоподборе — доля высоты экрана.
MAX_HEIGHT_FRACTION = 0.8

# Сколько сообщений держать одновременно (старые убираются).
MAX_NOTICES = 5

# Ожидание остановки задач при закрытии, с.
SHUTDOWN_TIMEOUT = 5.0
# Сколько фоновых разборов/прогревов выполняется одновременно.
BACKGROUND_SLOTS = 2

# Отступ при прокрутке к новой задаче, px.
SCROLL_MARGIN = 6


class _Batch:
    """Итоги «партии» задач: сколько завершилось и чем, последний результат."""

    def __init__(self) -> None:
        self.done = 0
        self.errors = 0
        self.cancelled = 0
        self.last_result: Path | None = None
        self.counted: set[int] = set()

    @property
    def total(self) -> int:
        return self.done + self.errors + self.cancelled

    def add(self, job: Job) -> None:
        if job.id in self.counted:
            return
        self.counted.add(job.id)
        if job.status == core_jobs.JobStatus.DONE:
            self.done += 1
            if job.result_path is not None:
                self.last_result = job.result_path
        elif job.status == core_jobs.JobStatus.ERROR:
            self.errors += 1
        elif job.status == core_jobs.JobStatus.CANCELLED:
            self.cancelled += 1

    def summary(self) -> str:
        """«Готово: 3, с ошибками: 1, отменено: 1» (нулевые части не пишутся)."""
        parts = []
        for count, key in ((self.done, "batch_done"), (self.errors, "batch_error"),
                           (self.cancelled, "batch_cancelled")):
            if count:
                parts.append(tr(key).format(n=count))
        text = ", ".join(parts)
        return text[:1].upper() + text[1:]


class _StopRequested(Exception):
    """Окно закрывается — разбор файлов прерывается."""


class _ContentArea(QScrollArea):
    """Прокручиваемая область, чья «желаемая» высота — высота содержимого.

    Так `sizeHint()` окна отражает реальное содержимое, и окно можно
    подогнать по высоте.
    """

    def sizeHint(self) -> QSize:  # noqa: N802 (имя из Qt)
        hint = super().sizeHint()
        widget = self.widget()
        if widget is not None:
            # Переносимые по словам подписи: высота зависит от ширины. Берём
            # высоту именно для текущей ширины области: sizeHint() считается
            # для «желаемой» ширины, которая может быть уже, и тогда переносов
            # больше, а под содержимым остаётся пустота.
            width = self.viewport().width()
            if widget.hasHeightForWidth() and width > 0:
                height = widget.heightForWidth(width)
            else:
                height = widget.sizeHint().height()
            hint.setHeight(height + 2 * self.frameWidth())
        return hint

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(super().minimumSizeHint().width(), 60)


class MainWindow(QWidget):
    """Главное окно программы."""

    def __init__(self, parent: QWidget | None = None, *, warm_up_detector: bool = True) -> None:
        super().__init__(parent)
        self._placed = False
        self._shutdown_done = False
        self._closing = threading.Event()
        self._tasks: set[BackgroundTask] = set()
        self._pending_groupings = 0
        self._fit_scheduled = False
        self._requested_height: int | None = None
        self._user_resized = False
        self._folder_cache: dict[str, Path | None] = {}
        # Служебная задача скачивания модуля LibreOffice (если запускалась).
        self._office_job_id: int | None = None
        # Итоги текущей партии задач; пока ставятся задачи группы (окна
        # фрагмента GIF, выбор папки) — партия не считается завершённой.
        self._batch = _Batch()
        self._submitting = 0
        self._user_cancelled = False
        # Открыт вопрос «Закрыть программу?» — итог партии откладывается.
        self._asking_close = False
        # Виджет, к которому прокрутить после подгонки высоты (новая задача).
        self._scroll_target: QWidget | None = None

        # Ф18/Ф19: без записи в data/ работаем без сохранения настроек.
        self._data_writable = paths.is_data_writable()
        self._settings = load_settings()

        self.group_panels: list[GroupPanel] = []
        self.notices: list[NoticePanel] = []

        # Фоновые разборы — в потоках-демонах (не QThreadPool: его деструктор
        # ждёт все задачи без предела, ревью 2, п.4); одновременно не больше
        # BACKGROUND_SLOTS.
        self._task_slots = threading.Semaphore(BACKGROUND_SLOTS)
        self._bridge = JobBridge(self)
        self._bridge.connect_to(self._on_job_updated)
        self.job_manager = JobManager(on_update=self._bridge.notify)

        self.setWindowTitle(tr("app_title"))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setAcceptDrops(True)
        self.setMinimumWidth(MIN_WIDTH)
        self._build_ui()
        self._build_shortcuts()
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self._fit_height()

        # Выход приложения не через закрытие окна (например, QApplication.quit())
        # тоже останавливает задачи и сохраняет настройки; повторной остановки
        # нет — shutdown() выполняется один раз.
        self._app = QApplication.instance()
        if self._app is not None:
            self._app.aboutToQuit.connect(self._on_about_to_quit)

        if not self._data_writable:
            self.add_notice(tr("data_not_writable"))

        # Предварительная расстановка; окончательная — при первом показе,
        # когда известны размеры рамки окна.
        self.place_bottom_right()

        if warm_up_detector:
            # Прогрев magika в фоне, чтобы первый бросок не тормозил.
            self._start_task(BackgroundTask(warm_up, self._on_task_done, slots=self._task_slots))

    # --- Построение интерфейса ---

    def _build_ui(self) -> None:
        self.drop_zone = DropZone(self)
        self.drop_zone.browse_requested.connect(self.browse_files)
        # Для совместимости: подсказка доступна как атрибут окна.
        self.hint_label = self.drop_zone.hint_label

        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(6)

        self._notices_layout = QVBoxLayout()
        self._notices_layout.setSpacing(4)
        self._groups_layout = QVBoxLayout()
        self._groups_layout.setSpacing(6)

        # «Конвертировать всё»: видна, когда групп, которые можно
        # конвертировать, две и больше.
        self.convert_all_row = QWidget(self._content)
        convert_all_layout = QHBoxLayout(self.convert_all_row)
        convert_all_layout.setContentsMargins(0, 0, 0, 0)
        self.convert_all_button = QPushButton(tr("convert_all"), self.convert_all_row)
        self.convert_all_button.setToolTip(tr("convert_all_tip"))
        self.convert_all_button.clicked.connect(self.convert_all)
        convert_all_layout.addStretch(1)
        convert_all_layout.addWidget(self.convert_all_button)
        self.convert_all_row.setVisible(False)

        self.jobs_header = QWidget(self._content)
        header_layout = QHBoxLayout(self.jobs_header)
        header_layout.setContentsMargins(0, 4, 0, 0)
        # Заголовок раздела крупнее заголовков карточек групп (те — обычного
        # размера, полужирные).
        self.jobs_title = jobs_title = QLabel(tr("jobs_title"), self.jobs_header)
        font = jobs_title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.25)
        jobs_title.setFont(font)
        self.clear_button = QPushButton(tr("clear_finished"), self.jobs_header)
        self.clear_button.clicked.connect(self.clear_finished)
        header_layout.addWidget(jobs_title)
        header_layout.addStretch(1)
        header_layout.addWidget(self.clear_button)
        self.jobs_header.setVisible(False)

        self.job_list = JobList(self._content)
        self.job_list.cancel_requested.connect(self.cancel_job)
        self.job_list.reveal_requested.connect(self.reveal_result)
        self.job_list.changed.connect(self._on_jobs_changed)

        content_layout.addLayout(self._notices_layout)
        content_layout.addLayout(self._groups_layout)
        content_layout.addWidget(self.convert_all_row)
        content_layout.addWidget(self.jobs_header)
        content_layout.addWidget(self.job_list)
        content_layout.addStretch(1)

        self.content_area = _ContentArea(self)
        self.content_area.setWidget(self._content)
        self.content_area.setWidgetResizable(True)
        self.content_area.setFrameShape(QFrame.Shape.NoFrame)
        self.content_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self.drop_zone)
        layout.addWidget(self.content_area, 1)

        # Подгонка высоты по факту пересчёта раскладки содержимого: Qt
        # показывает новые дочерние виджеты отложенно, и подгонка «сразу после
        # добавления» видела бы их скрытыми (ревью этапа 1). LayoutRequest
        # приходит и тогда, когда отложенно показанный виджет появился,
        # и когда изменилась высота строки задачи.
        self._content.installEventFilter(self)
        self.job_list.installEventFilter(self)

    def _build_shortcuts(self) -> None:
        """Ctrl+O — выбрать файлы, Ctrl+V — вставить файлы из буфера обмена."""
        self.open_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Open), self)
        self.open_shortcut.activated.connect(self.browse_files)
        self.paste_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Paste), self)
        self.paste_shortcut.activated.connect(self.paste_files)

    def _update_layout_state(self) -> None:
        """Свёрнутая зона броска и видимость «Конвертировать всё» — по содержимому."""
        if self._shutdown_done:
            return
        has_content = bool(self.group_panels or self.notices or self.job_list.rows())
        self.drop_zone.set_compact(has_content)
        convertible = sum(1 for panel in self.group_panels if panel.has_targets)
        self.convert_all_row.setVisible(convertible >= 2)

    # --- Позиция и размер окна (Ф1) ---

    def place_bottom_right(self) -> None:
        """Ставит окно в правый нижний угол доступной области основного экрана.

        Используется availableGeometry (без панели задач). Учитывается
        внешняя рамка окна (frameGeometry), чтобы окно целиком помещалось.
        """
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            # Экрана нет (экзотическое окружение) — оставляем позицию системе.
            return
        available = screen.availableGeometry()
        frame = self.frameGeometry()
        # move() задаёт позицию внешней рамки окна.
        x = available.x() + available.width() - frame.width() - SCREEN_MARGIN
        y = available.y() + available.height() - frame.height() - SCREEN_MARGIN
        self.move(max(x, available.x()), max(y, available.y()))

    def position_is_visible(self, pos: QPoint) -> bool:
        """Видна ли заметная часть заголовка окна на каком-либо экране.

        Проверяется полоса заголовка (верх окна): если за неё нельзя
        ухватиться мышью, окно потеряно для пользователя.
        """
        width = max(self.frameGeometry().width(), 100)
        strip = QRect(pos.x(), pos.y(), width, 32)
        for screen in QGuiApplication.screens():
            part = screen.availableGeometry().intersected(strip)
            if part.width() >= min(100, width) and part.height() >= 16:
                return True
        return False

    def _restore_position(self) -> None:
        saved = self._settings.window_pos
        if saved is not None:
            pos = QPoint(*saved)
            if self.position_is_visible(pos):
                self.move(pos)
                return
        self.place_bottom_right()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (имя из Qt)
        """При первом показе ставит окно на сохранённое место или в угол."""
        super().showEvent(event)
        if not self._placed:
            self._placed = True
            self._restore_position()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Размер, который задали не мы, — значит, пользователь потянул окно.
        if (
            self.isVisible()
            and self._requested_height is not None
            and event.size().height() != self._requested_height
        ):
            self._user_resized = True

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Раскладка содержимого пересчитывается — после неё подгоняем высоту."""
        if event.type() == QEvent.Type.LayoutRequest and not self._shutdown_done:
            self._schedule_fit()
        return super().eventFilter(watched, event)

    def _schedule_fit(self) -> None:
        """Подгоняет высоту окна после того, как раскладка пересчитается.

        Отложенный вызов срабатывает после уже стоящих в очереди событий
        (в том числе отложенного показа новых виджетов и LayoutRequest).
        """
        if not self._fit_scheduled:
            self._fit_scheduled = True
            QTimer.singleShot(0, self._fit_height)

    def _max_height(self) -> int:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return 800
        return max(WINDOW_HEIGHT, int(screen.availableGeometry().height() * MAX_HEIGHT_FRACTION))

    def _content_height(self) -> int:
        """Высота клиентской области, при которой всё видно без прокрутки.

        Считается явно: раскладка окна кэширует подсказку размера области
        прокрутки и не узнаёт, что внутри неё добавились виджеты.
        """
        layout = self.layout()
        margins = layout.contentsMargins()
        return (
            margins.top()
            + self.drop_zone.sizeHint().height()
            + layout.spacing()
            + self.content_area.sizeHint().height()
            + margins.bottom()
        )

    def _fit_height(self) -> None:
        """Высота по содержимому: не меньше WINDOW_HEIGHT, не больше доли экрана.

        Если пользователь сам менял размер, окно только растёт. После
        подгонки — прокрутка к новой задаче, если она есть.
        """
        self._fit_scheduled = False
        if self._shutdown_done:
            return
        self._resize_to_content()
        if self._scroll_target is not None:
            # После того как область прокрутки узнает новый размер содержимого.
            QTimer.singleShot(0, self._scroll_to_target)

    def _scroll_to_target(self) -> None:
        target, self._scroll_target = self._scroll_target, None
        if target is None or self._shutdown_done:
            return
        try:
            QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            self.content_area.ensureWidgetVisible(target, SCROLL_MARGIN, SCROLL_MARGIN)
        except RuntimeError:
            pass  # строку уже убрали («Очистить завершённые»)

    def scroll_to_widget(self, widget: QWidget) -> None:
        """Прокрутить содержимое к виджету (после ближайшей подгонки высоты)."""
        self._scroll_target = widget
        self._schedule_fit()

    def _resize_to_content(self) -> None:
        wanted = min(max(self._content_height(), WINDOW_HEIGHT), self._max_height())
        if self._user_resized:
            wanted = max(wanted, self.height())
        if wanted == self.height():
            return
        self._requested_height = wanted
        if not self.isVisible():
            self.resize(self.width(), wanted)
            return
        geo = self.geometry()
        frame = self.frameGeometry()
        screen = self.screen() or QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else frame
        if frame.center().y() > available.center().y():
            # Окно внизу экрана — растём вверх, нижний край на месте.
            title_height = geo.y() - frame.y()
            top = geo.y() + geo.height() - wanted
            top = max(top, available.y() + title_height)
            self.setGeometry(geo.x(), top, geo.width(), wanted)
        else:
            self.resize(geo.width(), wanted)

    # --- Перетаскивание и выбор файлов (Ф2) ---

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if local_paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
            self.drop_zone.set_active(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802
        if event.mimeData() is not None and event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:  # noqa: N802
        self.drop_zone.set_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self.drop_zone.set_active(False)
        dropped = local_paths_from_mime(event.mimeData())
        if not dropped:
            event.ignore()
            return
        event.acceptProposedAction()
        self.add_paths(dropped)

    def browse_files(self) -> None:
        """Кнопка «Выбрать файлы…» / «+ Добавить файлы» и Ctrl+O."""
        files, _filter = QFileDialog.getOpenFileNames(self, tr("choose_files_title"))
        if files:
            self.add_paths(files)

    def paste_files(self) -> None:
        """Ctrl+V: файлы, скопированные в Проводнике, или пути/file:///-адреса текстом."""
        clipboard = QGuiApplication.clipboard()
        found = paths_from_clipboard(clipboard.mimeData() if clipboard is not None else None)
        if found:
            self.add_paths(found)
        else:
            self.add_notice(tr("clipboard_no_files"), kind=NOTICE_INFO)

    def add_paths(self, items: Iterable[Path | str]) -> None:
        """Принимает файлы и папки (бросок или выбор) и разбирает их в фоне.

        Это тот же метод, что вызывает dropEvent. Результат появится в окне,
        когда фоновый разбор закончится.
        """
        dropped = [Path(item) for item in items if str(item)]
        if not dropped or self._closing.is_set():
            return
        closing = self._closing

        def detect_or_stop(path: Path) -> DetectedFormat | None:
            # При закрытии окна прерываем долгий разбор тысяч файлов.
            if closing.is_set():
                raise _StopRequested()
            return detect(path)

        self._pending_groupings += 1
        self._update_status()
        self._start_task(
            BackgroundTask(
                lambda: build_groups(dropped, detect_func=detect_or_stop),
                self._on_grouping_done,
                slots=self._task_slots,
            )
        )

    def _start_task(self, task: BackgroundTask) -> None:
        self._tasks.add(task)
        task.start()

    def _on_task_done(self, task: BackgroundTask, _result: Any, _error: BaseException | None) -> None:
        self._tasks.discard(task)

    def _on_grouping_done(
        self, task: BackgroundTask, result: GroupingResult | None, error: BaseException | None
    ) -> None:
        self._tasks.discard(task)
        self._pending_groupings = max(0, self._pending_groupings - 1)
        self._update_status()
        if self._closing.is_set():
            return
        if error is not None or result is None:
            self.add_notice(tr("grouping_failed").format(error=error))
            return
        for group in result.groups:
            self._add_group(group)
        if result.unsupported:
            lines = [
                tr("unsupported_item").format(name=path.name or str(path), reason=reason)
                for path, reason in result.unsupported
            ]
            text = "\n".join(
                [tr("unsupported_title").format(n=len(lines)), *limited_lines(lines)]
            )
            self.add_notice(text)
        if result.message:
            self.add_notice(result.message)

    def _update_status(self) -> None:
        self.drop_zone.set_status(tr("detecting") if self._pending_groupings else "")

    @property
    def is_busy_detecting(self) -> bool:
        """Идёт ли фоновый разбор брошенных файлов."""
        return self._pending_groupings > 0

    # --- Сообщения ---

    def add_notice(self, text: str, *, kind: str = NOTICE_WARNING) -> NoticePanel:
        """Показывает закрываемое сообщение над группами (значок — по `kind`)."""
        while len(self.notices) >= MAX_NOTICES:
            self._remove_notice(self.notices[0])
        notice = NoticePanel(text, self._content, kind=kind)
        notice.closed.connect(self._remove_notice)
        self.notices.append(notice)
        self._notices_layout.addWidget(notice)
        notice.show()  # сразу, а не отложенно: иначе подгонка его не учтёт
        self._update_layout_state()
        self._schedule_fit()
        return notice

    def _remove_notice(self, notice: NoticePanel) -> None:
        if notice in self.notices:
            self.notices.remove(notice)
        self._notices_layout.removeWidget(notice)
        notice.deleteLater()
        self._update_layout_state()
        self._schedule_fit()

    # --- Группы (Ф5–Ф7, Ф12) ---

    def _add_group(self, group: Group) -> GroupPanel:
        panel = GroupPanel(group, common_targets(group), self._settings.last_preset, self._content)
        panel.convert_requested.connect(self.convert_group)
        panel.remove_requested.connect(self.remove_group)
        panel.preset_changed.connect(self._remember_preset)
        self.group_panels.append(panel)
        self._groups_layout.addWidget(panel)
        panel.show()  # сразу, а не отложенно: иначе подгонка её не учтёт
        self._update_layout_state()
        self._schedule_fit()
        return panel

    def remove_group(self, panel: GroupPanel) -> None:
        if panel in self.group_panels:
            self.group_panels.remove(panel)
        self._groups_layout.removeWidget(panel)
        panel.deleteLater()
        self._update_layout_state()
        self._schedule_fit()

    def _remember_preset(self, preset: Preset) -> None:
        self._settings.last_preset = preset

    def _output_folder(self, source: Path) -> Path | None:
        """Папка результата рядом с исходником (с кэшем проверки записи)."""
        key = str(Path(source).absolute().parent).casefold()
        if key not in self._folder_cache:
            self._folder_cache[key] = core_jobs.default_output_dir(source)
        return self._folder_cache[key]

    def _ask_output_dir(self, group: Group) -> Path | None:
        """Ф12: спрашивает папку для результатов группы. None — отказ."""
        start = str(group.paths[0].parent) if group.paths else ""
        # Причина — сообщением в окне на время диалога (заголовок диалога
        # короткий); после ответа сообщение убирается, остаётся только итог.
        reason = self.add_notice(
            tr("output_dir_needed").format(group=category_name(group.category)),
            kind=NOTICE_INFO,
        )
        try:
            chosen = QFileDialog.getExistingDirectory(self, tr("choose_output_dir"), start)
        finally:
            self._remove_notice(reason)
        if not chosen:
            self.add_notice(
                tr("output_dir_cancelled").format(group=category_name(group.category))
            )
            return None
        folder = Path(chosen)
        if not naming.can_write(folder):
            self.add_notice(tr("output_dir_not_writable").format(folder=folder))
            return None
        return folder

    def _plan_outputs(
        self, group: Group, target: Target, split: TargetSplit
    ) -> list[tuple[list[Path], list[str], Path, str | None]] | None:
        """Список (исходники, форматы, папка, стем) для задач группы; None — отказ."""
        sources = split.convert_paths
        formats = split.convert_formats
        if target.many_to_one:
            units = [(sources, formats)]
            stem: str | None = naming.merged_stem(sources)
        else:
            units = [([path], [fmt]) for path, fmt in zip(sources, formats)]
            stem = None
        self._folder_cache.clear()  # права могли измениться с прошлого раза
        folders = [self._output_folder(unit[0][0]) for unit in units]
        if any(folder is None for folder in folders):
            # Один вопрос на всю группу, а не на каждый файл.
            chosen = self._ask_output_dir(group)
            if chosen is None:
                return None
            folders = [folder or chosen for folder in folders]
        return [(paths_, fmts, folder, stem) for (paths_, fmts), folder in zip(units, folders)]

    def convert_group(self, panel: GroupPanel) -> list[Job]:
        """Кнопка «Конвертировать»: ставит задачи группы в очередь."""
        self._submitting += 1
        try:
            return self._convert_group(panel)
        finally:
            self._submitting -= 1
            self._check_batch_done()

    def convert_all(self) -> list[Job]:
        """«Конвертировать всё»: группы по очереди — тем же путём, что их кнопки
        (окна фрагмента GIF, вопрос о LibreOffice, выбор папки).

        Отмена выбора папки или «Отмена» в окне фрагмента GIF прерывает весь
        проход: остальные группы не запускаются (и не задают своих вопросов).
        """
        created: list[Job] = []
        self._submitting += 1
        try:
            for panel in list(self.group_panels):
                if self._closing.is_set():
                    break
                if panel not in self.group_panels or not panel.has_targets:
                    continue
                created.extend(self.convert_group(panel))
                if self._user_cancelled:
                    log.info("«Конвертировать всё» прервано пользователем")
                    break
        finally:
            self._submitting -= 1
            self._check_batch_done()
        return created

    def _convert_group(self, panel: GroupPanel) -> list[Job]:
        # Пользователь что-то отменил (папку, окно GIF) — для «Конвертировать всё».
        self._user_cancelled = False
        group = panel.group
        target = panel.selected_target()
        converter = REGISTRY.get(group.category)
        if target is None or converter is None or not group.files:
            self.add_notice(tr("no_converter"))
            return []
        if target.uses_preset:
            preset = panel.selected_preset()
            self._remember_preset(preset)
        else:
            preset = self._settings.last_preset
        # Файлы, уже находящиеся в целевом формате, пропускаются (ревью, п.5).
        split = split_for_target(group, target, {group.category: converter})
        if not split.convert:
            self.add_notice(tr("no_converter"))
            return []
        plan = self._plan_outputs(group, target, split)
        if plan is None:
            self._user_cancelled = True
            return []  # выбор папки отменён — и скачивание модуля не ставится
        if self._needs_office(converter, target, split.convert_formats):
            self._prepare_office()
        if split.skipped:
            # Имя формата — как в списке вариантов, но без пояснений в скобках
            # («ICO», а не «ICO (значок)»): пропускаются только файлы, чей
            # формат совпадает с вариантом, т.е. это растровые форматы.
            format_name = target.extension.upper() if target.extension else target.label
            self.add_notice(
                skipped_same_format_text(len(split.skipped), format_name), kind=NOTICE_INFO
            )
        created: list[Job] = []
        # Ф20: видео → GIF — окно «Фрагмент для GIF» по очереди для каждого
        # видео; задача ставится сразу после ответа (предыдущие уже идут,
        # пока открыто следующее окно). «Применить ко всем» — остальные без окон.
        gif_mode = target.id == "gif" and group.category == Category.VIDEO
        apply_all: float | None = None
        skipped_gif = 0
        # Изображения → «Для печати (PPI)»: окно по очереди, «Применить ко всем».
        print_mode = target.id == "print_ppi" and group.category == Category.IMAGE
        apply_print: tuple[int, float] | None = None
        skipped_print = 0
        for index, (sources, formats, folder, stem) in enumerate(plan):
            options: dict[str, Any] = {}
            if print_mode:
                if apply_print is None:
                    choice = self._ask_print_ppi(sources[0], formats[0], index + 1, len(plan))
                    if self._closing.is_set():
                        break
                    if choice.kind not in ("one", "all"):
                        skipped_print += 1
                        self._user_cancelled = True
                        continue
                    if choice.kind == "all":
                        apply_print = (choice.ppi, choice.cm)
                    options["print_ppi"] = (choice.ppi, choice.cm)
                else:
                    options["print_ppi"] = apply_print
            if gif_mode:
                if apply_all is None:
                    choice = self._ask_gif_fragment(sources[0], formats[0], index + 1, len(plan))
                    if self._closing.is_set():
                        break
                    if choice.kind == "all":
                        apply_all = choice.end
                    elif choice.kind != "one":
                        skipped_gif += 1
                        self._user_cancelled = True
                        continue
                    options["fragment"] = (choice.start, choice.end)
                else:
                    options["fragment"] = (0.0, apply_all)
            try:
                created.append(
                    self.job_manager.submit(
                        sources, converter, target, preset, folder, stem, formats=formats,
                        options=options,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — граница с ядром
                log.exception("Не удалось поставить задачу")
                self.add_notice(tr("submit_failed").format(error=exc))
                break
        if skipped_gif and not self._closing.is_set():
            self.add_notice(
                tr("gif_skipped").format(count=files_count(skipped_gif)), kind=NOTICE_INFO
            )
        if skipped_print and not self._closing.is_set():
            self.add_notice(
                tr("print_skipped").format(count=files_count(skipped_print)), kind=NOTICE_INFO
            )
        if created:
            # Группа ушла в работу — её место занимают строки задач.
            self.remove_group(panel)
        return created

    def _ask_print_ppi(self, source: Path, fmt: str, index: int, total: int) -> PrintChoice:
        """Модальное окно «Подготовить к печати» для одной картинки."""
        dialog = PrintPpiDialog(source, fmt, index=index, total=total, parent=self)
        try:
            dialog.exec()
            return dialog.choice or PRINT_CANCEL
        except Exception:  # noqa: BLE001 — граница с окном: сбой = отмена этой картинки
            log.exception("Сбой окна «Подготовить к печати»")
            return PRINT_CANCEL
        finally:
            dialog.deleteLater()

    def _ask_gif_fragment(self, source: Path, fmt: str, index: int, total: int) -> GifChoice:
        """Модальное окно «Фрагмент для GIF» (Ф20) для одного видео.

        Очередь задач при этом работает (фоновые потоки, уведомления идут
        через вложенный цикл событий окна).
        """
        dialog = GifFragmentDialog(source, fmt, index=index, total=total, parent=self)
        try:
            dialog.exec()
            return dialog.choice or GIF_CANCEL
        except Exception:  # noqa: BLE001 — граница с окном: сбой = отмена этого видео
            log.exception("Сбой окна «Фрагмент для GIF»")
            return GIF_CANCEL
        finally:
            dialog.shutdown()
            dialog.deleteLater()

    # --- Модуль LibreOffice (Ф15–Ф19) ---

    @staticmethod
    def _needs_office(converter: Converter, target: Target, formats: list[str]) -> bool:
        module_for = getattr(converter, "module_for", None)
        if not callable(module_for):
            return False
        try:
            return any(module_for(target.id, fmt) == "libreoffice" for fmt in formats)
        except Exception:  # noqa: BLE001 — граница с конвертером
            log.exception("Конвертер упал в module_for()")
            return False

    def office_download_job(self) -> Job | None:
        """Незавершённая задача скачивания модуля или None."""
        if self._office_job_id is None:
            return None
        job = self.job_manager.get(self._office_job_id)
        return job if job is not None and not job.is_finished else None

    def _prepare_office(self) -> None:
        """Ф15: модуля нет — спросить и, если можно, поставить скачивание в очередь."""
        if self.office_download_job() is not None:
            return  # уже качается: задачи встанут в очередь за скачиванием
        if lo_module.availability() not in ("missing", "outdated"):
            return  # установлен, или data/ недоступна (Ф19) — без вопроса
        text = lo_module.ask_prefix() + tr("office_ask_text")
        consent = ask_choice(
            self, tr("office_ask_title"), text,
            tr("office_ask_download"), tr("office_ask_not_now"),
            default_accept=True,
            informative=tr("office_ask_info"),
            detailed=tr("office_ask_details"),
        )
        if not consent:
            log.info("Скачивание модуля LibreOffice: пользователь отказался")
            return
        target = Target(
            id="libreoffice_install", label=tr("office_download_target"), extension="",
            uses_preset=False, is_long=True,
        )
        try:
            job = self.job_manager.submit_runner(
                tr("office_download_name"), target, lo_module.install_job, lo_module.QUEUE
            )
        except Exception as exc:  # noqa: BLE001 — граница с ядром
            log.exception("Не удалось поставить скачивание модуля")
            self.add_notice(tr("submit_failed").format(error=exc))
            return
        self._office_job_id = job.id

    # --- Задачи (Ф9, Ф10, Ф14) ---

    def _on_job_updated(self, job: Job) -> None:
        """Обновление задачи (уже в главном потоке)."""
        before = self.job_list.row(job.id)
        if before is None and self.job_manager.get(job.id) is None:
            return  # задачу уже убрали из списка — запоздавшее уведомление
        was_finished = before is not None and before.job.is_finished
        self.job_list.update_job(job)
        row = self.job_list.row(job.id)
        if row is None:
            return
        if before is None:
            self.scroll_to_widget(row)  # новая задача — в видимую часть
        if row.job.is_finished and not was_finished:
            self._count_finished(row)
        self._check_batch_done()

    def _on_jobs_changed(self) -> None:
        self.jobs_header.setVisible(bool(self.job_list.rows()))
        self.clear_button.setEnabled(self.job_list.has_finished())
        self._update_layout_state()
        self._schedule_fit()

    # --- Завершение партии задач ---

    def _raise_window(self) -> None:
        """Щелчок по уведомлению: главное окно — на передний план."""
        if self._shutdown_done:
            return
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _count_finished(self, row: JobRow) -> None:
        job = row.job
        if job.display_name:
            return  # служебная задача (скачивание модуля) в итоги не входит
        self._batch.add(job)

    def _check_batch_done(self) -> None:
        """Все задачи завершены и в партии было завершение — итог один раз на партию."""
        if self._submitting or self._asking_close or self._closing.is_set() or self._shutdown_done:
            return
        if self._batch.total == 0:
            return
        for job in self.job_manager.jobs():
            row = self.job_list.row(job.id)
            # Нет строки или она ещё не знает о завершении — уведомления в пути.
            if row is None or not row.job.is_finished:
                return
        batch, self._batch = self._batch, _Batch()
        self._on_batch_finished(batch)

    def _on_batch_finished(self, batch: _Batch) -> None:
        """Уведомление, звук и Проводник — по ключам настроек.

        Партия, где всё отменено (ни готовых, ни ошибок), не сообщается.
        """
        if not batch.done and not batch.errors:
            return
        settings = self._settings
        if settings.notify_on_finish:
            notify.show_notification(
                tr("batch_title"), batch.summary(), error=bool(batch.errors),
                on_click=self._raise_window,
            )
        if settings.sound_on_finish:
            notify.play_sound(ok=not batch.errors)
        if settings.open_folder_on_finish and batch.last_result is not None:
            shown = naming.display_path(Path(batch.last_result))
            if not Path(naming.os_path(shown)).exists():
                log.info("Результат партии не найден, Проводник не открыт: %s", shown)
                return
            try:
                notify.reveal_result(shown)
            except OSError as exc:
                self.add_notice(tr("reveal_failed").format(error=exc.strerror or exc))

    def cancel_job(self, job_id: int) -> None:
        self.job_manager.cancel(job_id)

    def clear_finished(self) -> None:
        """Кнопка «Очистить завершённые»."""
        self.job_manager.clear_finished()
        self.job_list.remove_finished()

    def reveal_result(self, path: Path) -> None:
        """«Показать в папке»: Проводник с выделенным файлом."""
        shown = naming.display_path(Path(path))
        if not Path(naming.os_path(shown)).exists():
            self.add_notice(tr("result_missing").format(path=shown))
            return
        try:
            reveal_in_explorer(shown)
        except OSError as exc:
            self.add_notice(tr("reveal_failed").format(error=exc.strerror or exc))

    def active_jobs(self) -> list[Job]:
        return [job for job in self.job_manager.jobs() if not job.is_finished]

    # --- Закрытие ---

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._shutdown_done:
            event.accept()
            return
        active = self.active_jobs()
        if active:
            # Кнопки — действия; Enter и Esc — «Продолжить работу». Пока вопрос
            # открыт, итог партии откладывается; закрыли — не сообщается вовсе.
            self._asking_close = True
            try:
                close = ask_choice(
                    self,
                    tr("confirm_close_title"),
                    tr("confirm_close_text").format(n=len(active)),
                    tr("confirm_close_yes"),
                    tr("confirm_close_no"),
                )
            finally:
                self._asking_close = False
            if not close:
                event.ignore()
                self._check_batch_done()
                return
        self.shutdown()
        event.accept()

    def _on_about_to_quit(self) -> None:
        """Приложение завершается (не обязательно через закрытие окна)."""
        try:
            self.shutdown()
        except RuntimeError:
            pass  # объект Qt окна уже удалён — останавливать нечего

    def _disconnect_about_to_quit(self) -> None:
        if self._app is None:
            return
        try:
            self._app.aboutToQuit.disconnect(self._on_about_to_quit)
        except (RuntimeError, TypeError):
            pass  # уже отключено или приложение удалено
        self._app = None

    def shutdown(self, timeout: float = SHUTDOWN_TIMEOUT) -> bool:
        """Отменяет задачи, ждёт их остановки (не дольше `timeout`), сохраняет настройки.

        Выполняется один раз (повторные вызовы — из closeEvent, aboutToQuit,
        тестов — ничего не делают). Возвращает True, если все задачи успели
        остановиться; задачи зависшего конвертера остаются в фоновых
        потоках-демонах и не мешают выходу из программы.
        """
        if self._shutdown_done:
            return True
        self._shutdown_done = True
        self._closing.set()
        self._disconnect_about_to_quit()
        notify.shutdown()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            unfinished = self.job_manager.shutdown(wait=True, timeout=timeout, cancel=True)
            stopped = not unfinished
            if not stopped:
                log.warning(
                    "Задачи не остановились за %.1f с: %s", timeout, [j.id for j in unfinished]
                )
            # Фоновый разбор файлов прерывается флагом _closing; его не ждём:
            # потоки-демоны не держат выход, а их результат уже не нужен.
            busy = [t for t in self._tasks if t.thread is not None and t.thread.is_alive()]
            if busy:
                log.info("Закрытие без ожидания фоновых разборов: %d", len(busy))
        finally:
            QApplication.restoreOverrideCursor()
        if self._placed:
            self._settings.window_pos = (self.x(), self.y())
        if self._data_writable:
            save_settings(self._settings)
        return stopped


# Видимое название программы — Peregon (имя пакета Python остаётся konverter).
APP_USER_MODEL_ID = "XClassiK.Peregon"


def set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Свой AppUserModelID процесса (Windows): своя кнопка и значок на панели задач.

    Без него при запуске из исходников панель задач группирует окно с
    python.exe и показывает значок Python. Действует только на процесс,
    в реестр ничего не пишет. Не Windows или ошибка — False.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        shell32 = ctypes.WinDLL("shell32")
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        return shell32.SetCurrentProcessExplicitAppUserModelID(app_id) == 0
    except (OSError, AttributeError):
        log.warning("Не удалось задать AppUserModelID", exc_info=True)
        return False


def run() -> int:
    """Создаёт приложение, показывает главное окно и запускает цикл событий."""
    set_app_user_model_id()
    app = QApplication.instance() or QApplication(sys.argv)
    # Имя для стандартных диалогов Qt без заголовка (иначе — имя exe/python).
    # QSettings/QStandardPaths программа не использует, так что на диск это не влияет.
    app.setApplicationName(tr("app_title"))
    # Сборка циклического мусора — только в главном потоке (см. gc_guard).
    gc_guard.install(app)
    apply_base_font(app)
    icon = load_app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    window = MainWindow()
    window.show()
    return app.exec()
