"""Очередь задач конвертации, выполняемых в фоне (Ф8–Ф10, Ф13). Без Qt.

Как устроено:
- обычные задачи выполняют N = max(1, os.cpu_count() // 2) рабочих потоков;
- «тяжёлые» задачи (`target.is_long` и у конвертера задан атрибут
  `queue` — имя очереди: "media" для видео/аудио, "pdf" для PDF) выполняет
  ОТДЕЛЬНАЯ для каждой очереди группа из `max_heavy` потоков (по умолчанию
  1). Это то же самое, что семафор «не больше max_heavy одновременно» на
  каждый вид тяжёлой работы, но без блокировки обычных потоков ожиданием
  (иначе десяток видео или PDF в очереди занял бы все потоки и остановил
  лёгкие задачи). Очереди разных видов друг друга не ждут: PDF
  выполняется параллельно с видео (у них и в `_isolated` разные счётчики
  мест: "long" и "pdf"). Очереди из `HEAVY_QUEUES` создаются сразу,
  прочие имена — при первой задаче;
- рабочие потоки — свои, ФОНОВЫЕ (daemon), а не ThreadPoolExecutor: потоки
  пула не фоновые, и интерпретатор при выходе ждёт их бесконечно, поэтому
  один зависший конвертер не давал закрыть программу. Фоновый поток при
  выходе просто останавливается; опасные долгие декодеры (SVG, RAW) к тому
  же работают в дочерних процессах, которые убиваются при отмене;
- задача: проверка отмены → временный файл/папка в `output_dir` → конвертер
  с колбэком прогресса и кодами форматов из detect → перенос в конечное
  имя (`naming.finalize`) → done. При отмене/ошибке временный объект
  удаляется; исходник не трогается никогда (конвертер пишет только во
  временный путь, `finalize` не перезаписывает существующие файлы).

Служебные задачи (`submit_runner`): та же очередь, статусы, прогресс и
отмена, но вместо конвертера — функция `runner(progress, cancel)` без
временного файла и без результата (так идёт скачивание модуля LibreOffice —
в очереди "office", перед офисными конвертациями, которые его ждут).
Очередь обычной задачи выбирает конвертер: необязательный метод
`queue_for(target_id, formats)` (например, вариант через LibreOffice — в
"office"), иначе атрибут `queue`.

Остановка (`shutdown(wait, timeout, cancel)`): отменяет задачи, ждёт рабочие
потоки не дольше `timeout` и возвращает задачи, которые так и не
завершились. Для них временные объекты удаляются сразу (запись в журнале
временных файлов остаётся — если зависший поток пересоздаст файл, его
уберёт следующий запуск), а дальнейшие уведомления менеджера подавляются:
поздняя ошибка такого потока пользователю не показывается (только в лог).

ВАЖНО для интерфейса: колбэк `on_update(job)` вызывается ИЗ РАБОЧИХ ПОТОКОВ
(и из потока, вызвавшего `cancel`, если задача ещё в очереди). В Qt его
нужно переправить в главный поток, например через сигнал с
`Qt.QueuedConnection`. Внутри колбэка не делайте долгой работы. Поля `Job`
меняются рабочим потоком под блокировкой менеджера; для показа берите
`job.snapshot()` — копию, снятую под той же блокировкой.
Уведомления о прогрессе прореживаются: не чаще `PROGRESS_INTERVAL` секунд
(смена статуса уведомляется всегда).
"""

from __future__ import annotations

import itertools
import logging
import math
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable

from konverter.converters.base import (
    CancelToken,
    Cancelled,
    ConversionError,
    Converter,
    Preset,
    Target,
)
from konverter.core import naming
from konverter.strings import tr

log = logging.getLogger(__name__)

# Минимальный интервал между уведомлениями о прогрессе, с (≈10 раз в секунду).
PROGRESS_INTERVAL = 0.1

# Очереди тяжёлых задач, создаваемые сразу (см. `Converter.queue`).
HEAVY_QUEUES = ("media", "pdf")

# Служебная функция задачи: runner(progress, cancel); исключения — как у конвертера.
Runner = Callable[[Callable[[float], None], CancelToken], None]


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


FINISHED_STATUSES = frozenset({JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED})


@dataclass(eq=False)
class Job:
    """Задача конвертации.

    `sources` — один файл или (для `target.many_to_one`) все файлы группы.
    `formats` — коды форматов из detect, по одному на каждый исходник.
    `output_stem` — стем имени результата (по умолчанию из первого исходника).
    Время — `time.time()` (секунды эпохи), None — ещё не было.
    """

    id: int
    sources: list[Path]
    converter: Converter
    target: Target
    preset: Preset
    output_dir: Path
    output_stem: str
    formats: list[str] = field(default_factory=list)
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    error_message: str | None = None
    result_path: Path | None = None
    cancel_token: CancelToken = field(default_factory=CancelToken)
    started_at: float | None = None
    finished_at: float | None = None
    # Служебная задача (`JobManager.submit_runner`): функция вместо конвертера,
    # имя для показа и явная очередь.
    runner: Runner | None = field(default=None, repr=False, compare=False)
    display_name: str | None = None
    queue_override: str | None = None
    # Дополнительные именованные аргументы `converter.convert` (например,
    # `fragment` для GIF из видео, Ф20). Пусто — конвертер вызывается как раньше.
    options: dict = field(default_factory=dict)
    # Блокировка, под которой менеджер меняет поля (общая с менеджером).
    _lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )

    @property
    def is_finished(self) -> bool:
        return self.status in FINISHED_STATUSES

    @property
    def queue_name(self) -> str | None:
        """Очередь тяжёлых задач или None — общий лёгкий пул.

        Явная очередь служебной задачи; иначе для долгого варианта —
        `converter.queue_for(target.id, formats)`, если такой метод есть,
        или атрибут `converter.queue`.
        """
        if self.queue_override:
            return self.queue_override
        if not self.target.is_long:
            return None
        name = getattr(self.converter, "queue", None)
        queue_for = getattr(self.converter, "queue_for", None)
        if callable(queue_for):
            try:
                name = queue_for(self.target.id, list(self.formats))
            except Exception:  # noqa: BLE001 — сбой конвертера не ломает очередь
                log.exception("Конвертер упал в queue_for()")
        return name if isinstance(name, str) and name else None

    @property
    def is_heavy(self) -> bool:
        return self.queue_name is not None

    def snapshot(self) -> "Job":
        """Неглубокая копия задачи, снятая под блокировкой (списки копируются)."""
        with self._lock:
            return replace(self, sources=list(self.sources), formats=list(self.formats))


def default_output_dir(source: Path) -> Path | None:
    """Папка исходника, если в неё можно писать; иначе None (Ф12 — спросить)."""
    folder = Path(source).absolute().parent
    if not folder.is_dir():
        return None
    return folder if naming.can_write(folder) else None


UpdateCallback = Callable[[Job], None]

class _RunnerDone(Exception):
    """Служебная задача выполнена (результата-файла у неё нет)."""


# Метка «потоку пора выйти» в очереди задач.
_STOP = object()


class JobManager:
    """Очередь задач с фоновыми рабочими потоками, прогрессом и отменой."""

    def __init__(
        self,
        max_workers: int | None = None,
        on_update: UpdateCallback | None = None,
        max_heavy: int = 1,
    ) -> None:
        if max_workers is None:
            max_workers = max(1, (os.cpu_count() or 2) // 2)
        self._on_update = on_update
        self._lock = threading.RLock()
        self._jobs: dict[int, Job] = {}
        self._temps: dict[int, Path] = {}  # временные объекты выполняющихся задач
        self._ids = itertools.count(1)
        self._closed = False
        self._silenced = False  # после shutdown по таймауту — без уведомлений
        self._max_heavy = max(1, int(max_heavy))
        self._light_queue: queue.Queue = queue.Queue()
        # Очереди тяжёлых задач по имени (`Job.queue_name`).
        self._heavy_queues: dict[str, queue.Queue] = {}
        self._threads: list[threading.Thread] = []
        # Очередь, которую слушает каждый поток (для меток остановки).
        self._thread_queues: list[tuple[threading.Thread, queue.Queue]] = []
        for index in range(max(1, max_workers)):
            self._start_worker(self._light_queue, f"konverter-job-{index + 1}")
        with self._lock:
            for name in HEAVY_QUEUES:
                self._heavy_queue(name)

    def _start_worker(self, jobs_queue: queue.Queue, name: str) -> None:
        thread = threading.Thread(
            target=self._worker, args=(jobs_queue,), name=name, daemon=True
        )
        thread.start()
        self._threads.append(thread)
        self._thread_queues.append((thread, jobs_queue))

    def _heavy_queue(self, name: str) -> queue.Queue:
        """Очередь тяжёлых задач `name` (создаётся с потоками при первом обращении).

        Вызывать под `self._lock` и только пока менеджер не остановлен.
        """
        jobs_queue = self._heavy_queues.get(name)
        if jobs_queue is None:
            jobs_queue = queue.Queue()
            self._heavy_queues[name] = jobs_queue
            for index in range(self._max_heavy):
                self._start_worker(jobs_queue, f"konverter-heavy-{name}-{index + 1}")
        return jobs_queue

    def _queue_for(self, job: Job) -> queue.Queue:
        name = job.queue_name
        return self._light_queue if name is None else self._heavy_queue(name)

    # --- Публичный интерфейс ---

    def submit(
        self,
        sources: list[Path],
        converter: Converter,
        target: Target,
        preset: Preset,
        output_dir: Path,
        output_stem: str | None = None,
        *,
        formats: list[str],
        options: dict | None = None,
    ) -> Job:
        """Ставит задачу в очередь и возвращает её (статус queued).

        `formats` — коды форматов из detect (`DetectedFormat.fmt`) той же
        длины, что `sources`; передаются конвертеру без изменений.
        `options` — дополнительные именованные аргументы `convert` (например,
        `{"fragment": (начало, конец)}` для GIF); None/пусто — без них.
        """
        sources = [Path(s) for s in sources]
        formats = [str(f) for f in formats]
        if not sources:
            raise ValueError("Нет исходных файлов")
        if len(formats) != len(sources):
            raise ValueError("Число форматов не совпадает с числом исходных файлов")
        if output_stem is None:
            output_stem = (
                naming.merged_stem(sources) if target.many_to_one
                else naming.source_stem(sources[0])
            )
        with self._lock:
            if self._closed:
                raise RuntimeError("JobManager уже остановлен")
            job = Job(
                id=next(self._ids),
                sources=sources,
                converter=converter,
                target=target,
                preset=preset,
                output_dir=Path(output_dir),
                output_stem=output_stem,
                formats=formats,
                options=dict(options or {}),
                _lock=self._lock,
            )
            self._jobs[job.id] = job
        # Первое уведомление («в очереди») — ДО постановки в очередь: иначе
        # рабочий поток мог бы раньше прислать «выполняется», и интерфейс
        # получил бы статусы не по порядку. Уведомление — вне блокировки.
        self._notify(job)
        with self._lock:
            if job.status == JobStatus.QUEUED and not self._closed:
                self._queue_for(job).put(job)
                return job
            late_close = job.status == JobStatus.QUEUED
            if late_close:
                # Очередь остановили, пока шло уведомление: рабочих потоков
                # уже может не быть — задача не должна висеть «в очереди».
                job.cancel_token.cancel()
                job.status = JobStatus.CANCELLED
                job.finished_at = time.time()
        if late_close:
            self._notify(job)
        return job

    def submit_runner(self, name: str, target: Target, runner: Runner, queue: str) -> Job:
        """Ставит служебную задачу `runner(progress, cancel)` в очередь `queue`.

        Без исходников, временных файлов и результата; статусы, прогресс,
        отмена и уведомления — как у обычной задачи.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("JobManager уже остановлен")
            job = Job(
                id=next(self._ids), sources=[], converter=None, target=target,  # type: ignore[arg-type]
                preset=Preset.MEDIUM, output_dir=Path("."), output_stem="",
                runner=runner, display_name=name, queue_override=queue, _lock=self._lock,
            )
            self._jobs[job.id] = job
        self._notify(job)
        with self._lock:
            if job.status == JobStatus.QUEUED and not self._closed:
                self._queue_for(job).put(job)
                return job
            late_close = job.status == JobStatus.QUEUED
            if late_close:
                job.cancel_token.cancel()
                job.status = JobStatus.CANCELLED
                job.finished_at = time.time()
        if late_close:
            self._notify(job)
        return job

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        """Все задачи (в порядке постановки)."""
        with self._lock:
            return list(self._jobs.values())

    def cancel(self, job_id: int) -> bool:
        """Отменяет задачу. False — нет такой задачи или она уже завершена.

        Задача в очереди отменяется сразу (статус cancelled), выполняющаяся —
        при ближайшей проверке отмены конвертером.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_finished:
                return False
            job.cancel_token.cancel()
            queued = job.status == JobStatus.QUEUED
            if queued:
                # Рабочий поток, достав её из очереди, просто пропустит.
                job.status = JobStatus.CANCELLED
                job.finished_at = time.time()
        if queued:
            self._notify(job)
        return True

    def cancel_all(self) -> None:
        """Отменяет все незавершённые задачи."""
        for job in self.jobs():
            self.cancel(job.id)

    def clear_finished(self) -> None:
        """Забывает завершённые задачи (чтобы список не рос бесконечно)."""
        with self._lock:
            for job_id in [j.id for j in self._jobs.values() if j.is_finished]:
                self._jobs.pop(job_id, None)

    def shutdown(
        self, wait: bool = True, timeout: float | None = None, cancel: bool = True
    ) -> list[Job]:
        """Останавливает очередь и возвращает задачи, которые не завершились.

        `cancel` — сначала отменить все задачи (иначе очередь доработает
        до конца). `wait` — ждать рабочие потоки, но не дольше `timeout`
        секунд (None — без предела). Если по истечении ожидания остались
        незавершённые задачи, их временные объекты удаляются, а дальнейшие
        уведомления подавляются (см. докстринг модуля). Повторный вызов
        допустим.
        """
        with self._lock:
            first_call = not self._closed
            self._closed = True
        if cancel:
            self.cancel_all()
        with self._lock:
            # После _closed новые очереди и потоки не появляются.
            workers = list(self._thread_queues)
        if first_call:
            for _thread, queue_ in workers:
                queue_.put(_STOP)
        if wait:
            deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
            for thread, _queue in workers:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                thread.join(remaining)
        with self._lock:
            unfinished = [job for job in self._jobs.values() if not job.is_finished]
            if not wait or not unfinished:
                return unfinished
            self._silenced = True
            temps = [self._temps.get(job.id) for job in unfinished]
        log.warning("При остановке не завершились задачи: %s", [j.id for j in unfinished])
        for temp in temps:
            # Запись журнала оставляем: поток ещё может пересоздать файл.
            naming.cleanup(temp, forget=False)
        return unfinished

    # --- Выполнение ---

    def _worker(self, jobs_queue: queue.Queue) -> None:
        while True:
            item = jobs_queue.get()
            if item is _STOP:
                return
            try:
                self._run(item)
            except Exception:  # noqa: BLE001 — рабочий поток не должен умирать
                log.exception("Сбой рабочего потока на задаче %s", getattr(item, "id", "?"))

    def _notify(self, job: Job) -> None:
        if self._on_update is None or self._silenced:
            return
        try:
            self._on_update(job)
        except Exception:  # noqa: BLE001 — ошибка интерфейса не должна ломать очередь
            log.exception("Ошибка в колбэке on_update")

    def _set_finished(self, job: Job, status: JobStatus, message: str | None = None) -> None:
        with self._lock:
            job.status = status
            job.error_message = message
            job.finished_at = time.time()
            if status == JobStatus.DONE:
                job.progress = 1.0

    def _run(self, job: Job) -> None:
        with self._lock:
            if job.status == JobStatus.CANCELLED:
                return  # отменена в очереди, уведомление уже было
            if job.cancel_token.cancelled:
                job.status = JobStatus.CANCELLED
                job.finished_at = time.time()
                cancelled_early = True
            else:
                job.status = JobStatus.RUNNING
                job.started_at = time.time()
                cancelled_early = False
        self._notify(job)
        if cancelled_early:
            return

        temp: Path | None = None
        last_notify = 0.0

        def progress(fraction: float) -> None:
            nonlocal last_notify
            try:
                value = float(fraction)
            except (TypeError, ValueError):
                return
            if math.isnan(value):
                return
            with self._lock:
                job.progress = min(1.0, max(0.0, value))
            now = time.monotonic()
            if now - last_notify >= PROGRESS_INTERVAL:
                last_notify = now
                self._notify(job)

        try:
            job.cancel_token.raise_if_cancelled()
            if job.runner is not None:
                job.runner(progress, job.cancel_token)
                job.cancel_token.raise_if_cancelled()
                raise _RunnerDone()
            try:
                # Расширение временного файла — лишь метка: если настоящее другое,
                # его вернёт конвертер, и `finalize` даст результату верное имя.
                if job.target.extension:
                    temp = naming.make_temp_file(job.output_dir, job.target.extension)
                else:
                    temp = naming.make_temp_dir(job.output_dir)
            except OSError as exc:
                raise ConversionError(
                    tr("err_write_result").format(error=exc.strerror or exc)
                ) from exc
            with self._lock:
                self._temps[job.id] = temp

            extension = job.converter.convert(
                list(job.sources), temp, job.target, job.preset, progress, job.cancel_token,
                formats=list(job.formats), **job.options,
            )
            # Отмена, пришедшая в самом конце, — результат не сохраняем.
            job.cancel_token.raise_if_cancelled()

            try:
                # Конвертер может вернуть настоящее расширение (зависит от входа,
                # например «Для печати» у изображений); иначе — из варианта.
                if not (isinstance(extension, str) and re.fullmatch(r"[a-z0-9]{1,5}", extension)):
                    extension = job.target.extension
                result = naming.finalize(temp, job.output_dir, job.output_stem, extension)
            except OSError as exc:
                raise ConversionError(
                    tr("err_write_result").format(error=exc.strerror or exc)
                ) from exc
            temp = None
            with self._lock:
                job.result_path = result
            status, message = JobStatus.DONE, None
        except _RunnerDone:
            status, message = JobStatus.DONE, None
        except Cancelled:
            status, message = JobStatus.CANCELLED, None
        except ConversionError as exc:
            status, message = JobStatus.ERROR, str(exc) or tr("err_conversion_failed")
        except Exception as exc:  # noqa: BLE001 — любая ошибка конвертера → статус error
            log.exception("Внутренняя ошибка в задаче %s (%s)", job.id, job.sources)
            status, message = JobStatus.ERROR, tr("err_internal").format(error=exc)
        finally:
            # Уборка ДО смены статуса: увидев завершённую задачу, интерфейс
            # (и тесты) не застанут временных файлов.
            if temp is not None:
                naming.cleanup(temp)
            with self._lock:
                self._temps.pop(job.id, None)
        if self._silenced and status == JobStatus.ERROR:
            log.info("Задача %s завершилась после остановки: %s", job.id, message)
        self._set_finished(job, status, message)
        self._notify(job)
