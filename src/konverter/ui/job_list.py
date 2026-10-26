"""Список задач конвертации (Ф9, Ф10, Ф14).

Строка задачи: имя файла (полужирным) и вариант, статус (вторичным цветом,
со значком у завершённых: «Готово» / «Ошибка» / «Отменено»), для долгих
выполняющихся задач — полоса прогресса вместо слова «Выполняется», кнопка
«Отмена» (для задач в очереди и выполняющихся), «Показать в папке» для
готовых, текст ошибки (цвет ошибки от темы) для упавших.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from konverter.core.jobs import Job, JobStatus
from konverter.core.naming import display_path
from konverter.strings import tr
from konverter.ui.common import (
    apply_style,
    card_style,
    set_standard_icon,
    use_error_color,
    use_secondary_color,
)

# Рамка строки задачи — та же, что у карточек групп и сообщений.
_ROW_STYLE = card_style("JobRow")

# Значок у статуса: завершённые статусы различимы не только словом.
_STATUS_ICONS = {
    JobStatus.DONE: QStyle.StandardPixmap.SP_DialogApplyButton,
    JobStatus.ERROR: QStyle.StandardPixmap.SP_MessageBoxCritical,
    JobStatus.CANCELLED: QStyle.StandardPixmap.SP_DialogCancelButton,
}

_STATUS_KEYS = {
    JobStatus.QUEUED: "status_queued",
    JobStatus.RUNNING: "status_running",
    JobStatus.DONE: "status_done",
    JobStatus.ERROR: "status_error",
    JobStatus.CANCELLED: "status_cancelled",
}

_ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)

# Порядок жизни задачи: в очереди → выполняется → завершена (любым исходом).
_STATUS_RANK = {
    JobStatus.QUEUED: 0,
    JobStatus.RUNNING: 1,
    JobStatus.DONE: 2,
    JobStatus.ERROR: 2,
    JobStatus.CANCELLED: 2,
}


def status_text(status: JobStatus) -> str:
    return tr(_STATUS_KEYS[status])


def job_display_name(job: Job) -> str:
    """«фото.png» или «фото.png и ещё 2» для задач «много → один»;
    у служебной задачи (скачивание модуля) — её имя."""
    if job.display_name:
        return job.display_name
    first = job.sources[0].name if job.sources else "?"
    if len(job.sources) > 1:
        return tr("job_name_many").format(name=first, n=len(job.sources) - 1)
    return first


def is_long_job(job: Job) -> bool:
    """Показывать ли прогресс: долгий вариант (Ф9).

    Все варианты видео, аудио и PDF — долгие (`is_long=True`); тяжёлая
    задача (`Job.is_heavy`) всегда долгая.
    """
    return job.target.is_long


def explorer_command(path: Path) -> str:
    """Строка запуска Проводника с выделенным файлом.

    Путь всегда в кавычках (`/select,"путь"`): Проводник сам разбирает свою
    командную строку и без кавычек режет путь по запятым (`a,b.png`).
    Кавычек в именах Windows не бывает; если они есть — OSError (иначе путь
    мог бы «вылезти» из кавычек). Префикс длинного пути `\\\\?\\` убирается.
    Проводник — по полному пути из %WINDIR%, а не по имени из PATH (чтобы не
    подхватить подложенный `explorer.exe` из текущей папки).
    """
    shown = str(Path(os.path.abspath(display_path(Path(path)))))
    if '"' in shown:
        raise OSError(f"Недопустимый символ в пути: {shown}")
    explorer = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe")
    return f'"{explorer}" /select,"{shown}"'


def reveal_in_explorer(path: Path) -> None:
    """Открывает Проводник с выделенным файлом (Ф14).

    Запуск без оболочки (`shell=False`, строка уходит прямо в CreateProcess):
    имя файла не может превратиться в команду. Ошибки запуска — OSError (их
    показывает вызывающий).
    """
    subprocess.Popen(explorer_command(path), shell=False)  # noqa: S603


class JobRow(QFrame):
    """Строка одной задачи."""

    cancel_requested = Signal(int)
    reveal_requested = Signal(object)  # Path результата

    def __init__(self, job: Job, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.job_id = job.id
        self.job: Job = job
        apply_style(self, _ROW_STYLE)

        self.name_label = QLabel(self)
        self.name_label.setTextFormat(Qt.TextFormat.PlainText)
        self.name_label.setWordWrap(True)
        font = self.name_label.font()
        font.setBold(True)
        self.name_label.setFont(font)
        self.status_icon = QLabel(self)
        self.status_icon.setVisible(False)
        self.status_label = QLabel(self)
        use_secondary_color(self.status_label)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.cancel_button = QPushButton(tr("cancel"), self)
        self.reveal_button = QPushButton(tr("show_in_folder"), self)
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        use_error_color(self.error_label)

        self.cancel_button.clicked.connect(lambda: self.cancel_requested.emit(self.job_id))
        self.reveal_button.clicked.connect(self._on_reveal)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)
        layout.addWidget(self.name_label)
        row = QHBoxLayout()
        row.addWidget(self.status_icon)
        row.addWidget(self.status_label)
        row.addWidget(self.progress_bar, 1)
        row.addStretch(0)
        row.addWidget(self.cancel_button)
        row.addWidget(self.reveal_button)
        layout.addLayout(row)
        layout.addWidget(self.error_label)

        self.update_job(job)

    def update_job(self, job: Job) -> None:
        """Обновляет строку по снимку задачи."""
        self.job = job
        name = job_display_name(job)
        self.name_label.setText(
            f"{name}  {tr('job_target').format(target=job.target.label)}"
        )
        self.name_label.setToolTip("\n".join(str(s) for s in job.sources[:20]))
        self.status_label.setText(status_text(job.status))
        set_standard_icon(self.status_icon, _STATUS_ICONS.get(job.status))

        active = job.status in _ACTIVE_STATUSES
        # Долгая выполняющаяся задача: полоса «42 %» вместо слова «Выполняется».
        show_progress = job.status == JobStatus.RUNNING and is_long_job(job)
        self.progress_bar.setVisible(show_progress)
        self.status_label.setVisible(not show_progress)
        if show_progress:
            self.progress_bar.setValue(round(job.progress * 100))
            self.progress_bar.setToolTip(status_text(job.status))
        self.cancel_button.setVisible(active)
        self.reveal_button.setVisible(job.status == JobStatus.DONE and job.result_path is not None)
        if job.result_path is not None:
            self.reveal_button.setToolTip(str(display_path(job.result_path)))

        error = job.error_message if job.status == JobStatus.ERROR else None
        self.error_label.setText(error or "")
        self.error_label.setVisible(bool(error))
        self.status_label.setToolTip(error or "")

    def _on_reveal(self) -> None:
        if self.job.result_path is not None:
            self.reveal_requested.emit(self.job.result_path)


class JobList(QWidget):
    """Вертикальный список строк задач."""

    cancel_requested = Signal(int)
    reveal_requested = Signal(object)
    changed = Signal()  # строки добавились/убрались/сменили статус

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[int, JobRow] = {}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)

    def rows(self) -> list[JobRow]:
        return list(self._rows.values())

    def row(self, job_id: int) -> JobRow | None:
        return self._rows.get(job_id)

    def update_job(self, job: Job) -> None:
        """Добавляет строку новой задачи или обновляет существующую."""
        row = self._rows.get(job.id)
        if row is None:
            row = JobRow(job, self)
            row.cancel_requested.connect(self.cancel_requested)
            row.reveal_requested.connect(self.reveal_requested)
            self._rows[job.id] = row
            self._layout.addWidget(row)
            # Сразу, а не отложенно (как сделал бы Qt в показанном окне):
            # иначе подгонка высоты окна не учтёт строку.
            row.show()
            self.changed.emit()
            return
        old_status = row.job.status
        if _STATUS_RANK[job.status] < _STATUS_RANK[old_status]:
            # Запоздавшее уведомление: статус не откатывается назад.
            # Завершённая задача не «оживает»; «в очереди» не затирает
            # «выполняется» (ядро шлёт «в очереди» уже после постановки
            # задачи, и рабочий поток иногда успевает сообщить о запуске раньше).
            return
        row.update_job(job)
        if old_status != job.status:
            self.changed.emit()

    def has_finished(self) -> bool:
        return any(r.job.is_finished for r in self._rows.values())

    def remove_finished(self) -> None:
        """Убирает строки завершённых задач."""
        for job_id in [i for i, r in self._rows.items() if r.job.is_finished]:
            row = self._rows.pop(job_id)
            self._layout.removeWidget(row)
            row.deleteLater()
        self.changed.emit()
