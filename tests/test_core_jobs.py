"""Тесты очереди задач (core/jobs.py) на фиктивных конвертерах."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest

from konverter.converters.base import Category, ConversionError, Preset, Target
from konverter.core import naming
from konverter.core.jobs import JobManager, JobStatus, default_output_dir

TARGET = Target("txt", "Текст", "txt")
LONG_VIDEO = Target("mp4", "MP4", "mp4", is_long=True)


class FakeConverter:
    """Пишет содержимое исходника в результат; умеет «долго работать» и падать."""

    def __init__(self, category=Category.DOCUMENT, steps=5, delay=0.01, fail=None,
                 started: threading.Event | None = None, queue: str | None = None):
        self.category = category
        self.queue = queue
        self.steps = steps
        self.delay = delay
        self.fail = fail
        self.started = started
        self.running = 0
        self.max_running = 0
        self._lock = threading.Lock()

    def targets(self, fmt):
        return [TARGET]

    def convert(self, sources, dst, target, preset, progress, cancel, *, formats):
        assert len(formats) == len(sources)
        self.formats = list(formats)
        with self._lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            if self.started is not None:
                self.started.set()
            with open(dst, "wb") as out:
                for i in range(self.steps):
                    cancel.raise_if_cancelled()
                    time.sleep(self.delay)
                    progress((i + 1) / self.steps)
                if self.fail == "conversion":
                    raise ConversionError("Файл повреждён")
                if self.fail == "internal":
                    raise ValueError("бах")
                for src in sources:
                    out.write(Path(src).read_bytes())
        finally:
            with self._lock:
                self.running -= 1


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _temp_files(folder: Path) -> list[Path]:
    return [p for p in folder.iterdir() if naming.is_temp_name(p.name)]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "исходник.txt"
    path.write_text("содержимое", encoding="utf-8")
    return path


@pytest.fixture
def manager():
    updates = []
    mgr = JobManager(max_workers=2, on_update=lambda job: updates.append(job.status))
    mgr.updates = updates
    yield mgr
    mgr.shutdown(wait=True)


def test_job_done(manager, source, tmp_path):
    before = _hash(source)
    job = manager.submit([source], FakeConverter(), TARGET, Preset.MEDIUM, tmp_path, formats=["txt"])
    assert _wait(lambda: job.is_finished)
    assert job.status == JobStatus.DONE, job.error_message
    assert job.progress == 1.0
    # Исходник занят (то же имя и расширение) — результат с суффиксом (1).
    assert job.result_path == tmp_path / "исходник (1).txt"
    assert job.result_path.read_text(encoding="utf-8") == "содержимое"
    assert _hash(source) == before
    assert _temp_files(tmp_path) == []
    assert job.started_at is not None and job.finished_at >= job.started_at
    assert JobStatus.RUNNING in manager.updates and manager.updates[-1] == JobStatus.DONE


def test_cancel_running_job(manager, source, tmp_path):
    started = threading.Event()
    conv = FakeConverter(steps=10_000, delay=0.01, started=started)
    job = manager.submit([source], conv, TARGET, Preset.MEDIUM, tmp_path, formats=["txt"])
    assert started.wait(5)
    t0 = time.monotonic()
    assert manager.cancel(job.id)
    assert _wait(lambda: job.is_finished, timeout=2)
    assert time.monotonic() - t0 < 2
    assert job.status == JobStatus.CANCELLED
    assert _temp_files(tmp_path) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["исходник.txt"]


def test_cancel_queued_job(source, tmp_path):
    started = threading.Event()
    mgr = JobManager(max_workers=1)
    try:
        blocker = mgr.submit(
            [source], FakeConverter(steps=10_000, started=started), TARGET, Preset.MEDIUM, tmp_path, formats=["txt"]
        )
        assert started.wait(5)
        queued = mgr.submit([source], FakeConverter(), TARGET, Preset.MEDIUM, tmp_path, formats=["txt"])
        assert queued.status == JobStatus.QUEUED
        assert mgr.cancel(queued.id)
        assert queued.status == JobStatus.CANCELLED  # сразу, не дожидаясь очереди
        assert queued.started_at is None
        mgr.cancel(blocker.id)
        assert _wait(lambda: blocker.is_finished)
        assert mgr.cancel(queued.id) is False  # уже завершена
    finally:
        mgr.shutdown(wait=True)
    assert _temp_files(tmp_path) == []


@pytest.mark.parametrize("fail, expected", [
    ("conversion", "Файл повреждён"),
    ("internal", "Внутренняя ошибка: бах"),
])
def test_error_and_queue_continues(manager, source, tmp_path, fail, expected):
    before = _hash(source)
    bad = manager.submit([source], FakeConverter(fail=fail), TARGET, Preset.MEDIUM, tmp_path, formats=["txt"])
    assert _wait(lambda: bad.is_finished)
    assert bad.status == JobStatus.ERROR
    assert bad.error_message == expected
    assert bad.result_path is None
    assert _temp_files(tmp_path) == []

    good = manager.submit([source], FakeConverter(), TARGET, Preset.MEDIUM, tmp_path, formats=["txt"])
    assert _wait(lambda: good.is_finished)
    assert good.status == JobStatus.DONE
    assert _hash(source) == before


def test_heavy_jobs_one_at_a_time(source, tmp_path):
    mgr = JobManager(max_workers=4, max_heavy=1)
    try:
        conv = FakeConverter(category=Category.VIDEO, steps=5, delay=0.02, queue="media")
        jobs = [mgr.submit([source], conv, LONG_VIDEO, Preset.HIGH, tmp_path, formats=["txt"]) for _ in range(3)]
        light_conv = FakeConverter(steps=1, delay=0)
        light = mgr.submit([source], light_conv, TARGET, Preset.HIGH, tmp_path, formats=["txt"])
        assert _wait(lambda: light.is_finished)
        assert _wait(lambda: all(j.is_finished for j in jobs))
        assert all(j.status == JobStatus.DONE for j in jobs)
        assert conv.max_running == 1
    finally:
        mgr.shutdown(wait=True)


def test_many_to_one_stem_and_output_dir(manager, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    a = tmp_path / "первый.txt"
    b = tmp_path / "второй.txt"
    a.write_text("A", encoding="utf-8")
    b.write_text("B", encoding="utf-8")
    merge = Target("merge", "Объединить", "txt", many_to_one=True)
    job = manager.submit([a, b], FakeConverter(), merge, Preset.MEDIUM, out, formats=["txt", "txt"])
    assert _wait(lambda: job.is_finished)
    assert job.result_path == out / "первый.txt"
    assert job.result_path.read_text(encoding="utf-8") == "AB"


def test_callback_exception_does_not_break_queue(source, tmp_path):
    def bad_callback(job):
        raise RuntimeError("ошибка интерфейса")

    mgr = JobManager(max_workers=1, on_update=bad_callback)
    try:
        job = mgr.submit([source], FakeConverter(), TARGET, Preset.MEDIUM, tmp_path, formats=["txt"])
        assert _wait(lambda: job.is_finished)
        assert job.status == JobStatus.DONE
    finally:
        mgr.shutdown()


def test_unwritable_output_dir_gives_error(manager, source, tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise PermissionError(13, "Отказано в доступе")

    monkeypatch.setattr(naming, "make_temp_file", deny)
    job = manager.submit([source], FakeConverter(), TARGET, Preset.MEDIUM, tmp_path, formats=["txt"])
    assert _wait(lambda: job.is_finished)
    assert job.status == JobStatus.ERROR
    assert "Не удалось записать результат" in job.error_message


def test_shutdown_cancels_and_rejects_new(source, tmp_path):
    mgr = JobManager(max_workers=1)
    started = threading.Event()
    job = mgr.submit(
        [source], FakeConverter(steps=10_000, started=started), TARGET, Preset.MEDIUM, tmp_path, formats=["txt"]
    )
    assert started.wait(5)
    mgr.shutdown(wait=True)
    assert job.status == JobStatus.CANCELLED
    with pytest.raises(RuntimeError):
        mgr.submit([source], FakeConverter(), TARGET, Preset.MEDIUM, tmp_path, formats=["txt"])
    assert _temp_files(tmp_path) == []


def test_default_output_dir(source, monkeypatch):
    assert default_output_dir(source) == source.parent
    monkeypatch.setattr(naming, "can_write", lambda folder: False)
    assert default_output_dir(source) is None


# --- Ревью: форматы, snapshot, остановка с таймаутом -----------------------------


def test_formats_passed_to_converter(manager, source, tmp_path):
    conv = FakeConverter()
    job = manager.submit([source], conv, TARGET, Preset.MEDIUM, tmp_path, formats=["md"])
    assert _wait(lambda: job.is_finished)
    assert job.status == JobStatus.DONE
    assert conv.formats == ["md"]
    assert job.snapshot().formats == ["md"]


def test_submit_requires_matching_formats(manager, source, tmp_path):
    with pytest.raises(TypeError):
        manager.submit([source], FakeConverter(), TARGET, Preset.MEDIUM, tmp_path)
    with pytest.raises(ValueError):
        manager.submit([source], FakeConverter(), TARGET, Preset.MEDIUM, tmp_path, formats=[])


def test_snapshot_taken_under_lock(manager, source, tmp_path):
    started = threading.Event()
    job = manager.submit(
        [source], FakeConverter(steps=10_000, started=started), TARGET, Preset.MEDIUM, tmp_path,
        formats=["txt"],
    )
    assert started.wait(5)
    got: list = []
    with job._lock:  # пока блокировка занята, снимок ждёт
        thread = threading.Thread(target=lambda: got.append(job.snapshot()))
        thread.start()
        thread.join(0.3)
        assert thread.is_alive() and got == []
    thread.join(5)
    assert len(got) == 1 and got[0] is not job and got[0].sources == job.sources
    manager.cancel(job.id)


class HungConverter:
    """Конвертер, который не проверяет отмену (как зависший нативный код)."""

    category = Category.DOCUMENT

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def targets(self, fmt):
        return [TARGET]

    def convert(self, sources, dst, target, preset, progress, cancel, *, formats):
        Path(dst).write_bytes(b"partial")
        self.started.set()
        self.release.wait(30)
        Path(dst).write_bytes(b"late")  # поток «оживает» после остановки
        raise ConversionError("поздняя ошибка")


def test_shutdown_timeout_returns_unfinished_and_cleans(source, tmp_path):
    updates: list = []
    mgr = JobManager(max_workers=1, on_update=lambda job: updates.append(job.status))
    conv = HungConverter()
    job = mgr.submit([source], conv, TARGET, Preset.MEDIUM, tmp_path, formats=["txt"])
    assert conv.started.wait(5)
    assert len(_temp_files(tmp_path)) == 1

    started = time.monotonic()
    left = mgr.shutdown(wait=True, timeout=0.5)
    assert time.monotonic() - started < 3
    assert left == [job]
    assert _temp_files(tmp_path) == []  # временный файл убран, хотя поток жив
    assert all(t.daemon for t in threading.enumerate() if t.name.startswith("konverter-"))

    seen = len(updates)
    conv.release.set()  # поток доработал: ошибка не всплывает
    assert _wait(lambda: job.is_finished)
    assert len(updates) == seen
    assert _wait(lambda: _temp_files(tmp_path) == [])


def test_worker_threads_are_daemon():
    mgr = JobManager(max_workers=2, max_heavy=1)
    try:
        names = [t for t in threading.enumerate() if t.name.startswith("konverter-")]
        assert len(names) >= 3 and all(t.daemon for t in names)
    finally:
        assert mgr.shutdown(wait=True, timeout=5) == []

# --- Ревью 2, п.8: «в очереди» приходит раньше «выполняется» ----------------------


class _InstantQueue:
    """Очередь, которая выполняет задачу прямо в put() — как если бы рабочий
    поток забрал её мгновенно, раньше, чем submit успел что-то ещё сделать."""

    def __init__(self, manager: JobManager, real) -> None:
        self.manager = manager
        self.real = real

    def put(self, item) -> None:
        if hasattr(item, "cancel_token"):
            self.manager._run(item)
        else:
            self.real.put(item)


def test_submit_notifies_queued_before_running(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    events: list[JobStatus] = []
    manager = JobManager(max_workers=1, on_update=lambda job: events.append(job.status))
    manager._light_queue = _InstantQueue(manager, manager._light_queue)
    try:
        job = manager.submit([src], FakeConverter(steps=1, delay=0), TARGET, Preset.MEDIUM,
                             tmp_path, formats=["txt"])
        assert job.status == JobStatus.DONE
        assert events[0] == JobStatus.QUEUED
        assert events[-1] == JobStatus.DONE
        assert events.index(JobStatus.QUEUED) < events.index(JobStatus.RUNNING)
    finally:
        manager.shutdown(timeout=5)


def test_submit_racing_shutdown_does_not_hang(tmp_path):
    """shutdown между регистрацией задачи и постановкой в очередь: задача не висит."""
    src = tmp_path / "a.txt"
    src.write_text("x", encoding="utf-8")
    manager = JobManager(max_workers=1)

    def on_update(job):
        if job.status == JobStatus.QUEUED and not manager._closed:
            manager.shutdown(wait=True, timeout=5, cancel=False)

    manager._on_update = on_update
    job = manager.submit([src], FakeConverter(steps=1, delay=0), TARGET, Preset.MEDIUM,
                         tmp_path, formats=["txt"])
    assert job.is_finished
    assert job.status == JobStatus.CANCELLED


# --- Ревью этапа 3, п.4: отдельные очереди тяжёлых задач (media, pdf) ----------------


class BlockingConverter(FakeConverter):
    """Работает, пока не отпустят `release` (с проверкой отмены)."""

    def __init__(self, queue, category=Category.PDF):
        super().__init__(category=category, steps=1, delay=0, queue=queue)
        self.release = threading.Event()
        self.started_event = threading.Event()

    def convert(self, sources, dst, target, preset, progress, cancel, *, formats):
        with self._lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            self.started_event.set()
            while not self.release.wait(0.01):
                cancel.raise_if_cancelled()
            Path(dst).write_bytes(b"x")
        finally:
            with self._lock:
                self.running -= 1


LONG_PDF = Target("pdf_split", "Разделить", "pdf", is_long=True)


def test_review3_pdf_queue_does_not_block_light_jobs(source, tmp_path):
    mgr = JobManager(max_workers=2)
    pdf_conv = BlockingConverter("pdf")
    video_conv = BlockingConverter("media", category=Category.VIDEO)
    try:
        pdf_jobs = [mgr.submit([source], pdf_conv, LONG_PDF, Preset.MEDIUM, tmp_path,
                               formats=["pdf"]) for _ in range(10)]
        assert pdf_conv.started_event.wait(5)
        light = [mgr.submit([source], FakeConverter(category=Category.IMAGE, steps=1, delay=0),
                            TARGET, Preset.MEDIUM, tmp_path, formats=["png"]) for _ in range(3)]
        # Картинки завершаются, не дожидаясь PDF.
        assert _wait(lambda: all(j.is_finished for j in light))
        assert all(j.status == JobStatus.DONE for j in light)
        assert sum(j.status == JobStatus.RUNNING for j in pdf_jobs) == 1
        # Видео — в своей очереди, параллельно PDF.
        video = mgr.submit([source], video_conv, LONG_VIDEO, Preset.MEDIUM, tmp_path,
                           formats=["mp4"])
        assert video_conv.started_event.wait(5)
        assert video.status == JobStatus.RUNNING and pdf_conv.running == 1
        assert pdf_jobs[0].is_heavy and video.is_heavy and not light[0].is_heavy
        # Отмена в каждой очереди.
        assert mgr.cancel(pdf_jobs[0].id) and mgr.cancel(video.id)
        assert _wait(lambda: pdf_jobs[0].is_finished and video.is_finished)
        assert pdf_jobs[0].status == video.status == JobStatus.CANCELLED
        assert _wait(lambda: pdf_jobs[1].status == JobStatus.RUNNING)
        assert pdf_conv.max_running == 1
    finally:
        started = time.monotonic()
        left = mgr.shutdown(wait=True, timeout=5)
        assert time.monotonic() - started < 5
    assert left == []
    assert all(j.status == JobStatus.CANCELLED for j in pdf_jobs[1:])
    assert not [t for t in mgr._threads if t.is_alive()]


def test_review3_shutdown_timeout_covers_all_queues(source, tmp_path):
    """Зависшие задачи в обеих тяжёлых очередях: shutdown(timeout) не ждёт дольше."""
    mgr = JobManager(max_workers=1)

    class Stuck(BlockingConverter):
        def convert(self, sources, dst, target, preset, progress, cancel, *, formats):
            self.started_event.set()
            self.release.wait(10)  # отмену не проверяет

    pdf_conv, video_conv = Stuck("pdf"), Stuck("media", category=Category.VIDEO)
    try:
        jobs = [
            mgr.submit([source], pdf_conv, LONG_PDF, Preset.MEDIUM, tmp_path, formats=["pdf"]),
            mgr.submit([source], video_conv, LONG_VIDEO, Preset.MEDIUM, tmp_path,
                       formats=["mp4"]),
        ]
        assert pdf_conv.started_event.wait(5) and video_conv.started_event.wait(5)
        started = time.monotonic()
        left = mgr.shutdown(wait=True, timeout=0.5)
        assert time.monotonic() - started < 3
        assert sorted(j.id for j in left) == sorted(j.id for j in jobs)
    finally:
        pdf_conv.release.set()
        video_conv.release.set()
    assert _wait(lambda: not [t for t in mgr._threads if t.is_alive()])


def test_review3_queue_attribute_is_optional(source, tmp_path):
    """Без атрибута `queue` (и для не долгих вариантов) — общий лёгкий пул."""
    mgr = JobManager(max_workers=1)
    try:
        class Plain:
            category = Category.VIDEO

            def targets(self, fmt):
                return [LONG_VIDEO]

            def convert(self, sources, dst, target, preset, progress, cancel, *, formats):
                Path(dst).write_bytes(b"x")

        job = mgr.submit([source], Plain(), LONG_VIDEO, Preset.MEDIUM, tmp_path, formats=["mp4"])
        assert not job.is_heavy
        short = mgr.submit([source], FakeConverter(queue="pdf"), TARGET, Preset.MEDIUM, tmp_path,
                           formats=["pdf"])
        assert not short.is_heavy
        assert _wait(lambda: job.is_finished and short.is_finished)
        assert job.status == short.status == JobStatus.DONE
    finally:
        assert mgr.shutdown(wait=True, timeout=5) == []
