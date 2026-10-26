"""Фрагмент для GIF (Ф20, П26–П28): ядро (media.py), окно (ui/gif_fragment.py),
главное окно (группа из нескольких видео, «Применить ко всем»).

Тестовые видео генерируются здесь же через av. «Кодированное» видео: кадр
номер i — полосы чёрное/белое, двоичная запись i (младший бит слева), так по
любому кадру (GIF, картинке кадра) можно узнать, из какого места видео он
взят. Видео 3 мин — 96×16 при 5 к/с (900 кадров, H.264, ключевые кадры
редко: seek к 1:10 попадает на ключевой кадр раньше начала).
"""

from __future__ import annotations

import multiprocessing
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest
from PySide6.QtCore import Qt

from konverter import paths
from konverter.converters import _isolated, media
from konverter.converters.base import REGISTRY, CancelToken, Category, ConversionError, Preset, Target
from konverter.core.jobs import JobManager, JobStatus
from konverter.strings import tr
from konverter.ui import gif_fragment
from konverter.ui.gif_fragment import GifChoice, GifFragmentDialog, format_time, parse_time
from konverter.ui.main_window import MainWindow

VIDEO = media.VideoConverter()
BITS = 12
STRIPE = 8
CHILD_TIMEOUT = 30000
DETECT_TIMEOUT = 30000
JOB_TIMEOUT = 60000


# --- Генерация и разбор тестовых видео --------------------------------------


def make_coded_video(path: Path, seconds: float, fps: int, *, container: str = "mp4") -> Path:
    """Видео, где кадр i — полосы с двоичной записью i (см. докстринг модуля)."""
    width, height = BITS * STRIPE, 16
    with av.open(str(path), "w", format=container) as out:
        stream = out.add_stream("libx264", rate=fps, options={"preset": "ultrafast"})
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        for index in range(int(round(seconds * fps))):
            img = np.zeros((height, width, 3), np.uint8)
            for bit in range(BITS):
                if index >> bit & 1:
                    img[:, bit * STRIPE:(bit + 1) * STRIPE] = 255
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = index
            frame.time_base = Fraction(1, fps)
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return path


def make_hd_video(path: Path, seconds: float = 2.0, fps: int = 30, size=(1920, 1080)) -> Path:
    """1080p: градиент со сдвигом (кодируется быстро, но кадры «настоящего» размера)."""
    width, height = size
    base = np.tile(np.linspace(0, 255, width, dtype=np.float32), (height, 1))
    with av.open(str(path), "w", format="mp4") as out:
        stream = out.add_stream("libx264", rate=fps, options={"preset": "ultrafast"})
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        for index in range(int(round(seconds * fps))):
            shifted = np.roll(base, index * 16, axis=1).astype(np.uint8)
            img = np.stack([shifted, np.flipud(shifted), np.full_like(shifted, 90)], axis=2)
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = index
            frame.time_base = Fraction(1, fps)
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return path


def decode_index(rgb: np.ndarray) -> int:
    """Номер кадра по полосам (картинка любого размера с тем же соотношением)."""
    height, width = rgb.shape[:2]
    step = width / BITS
    value = 0
    for bit in range(BITS):
        if rgb[height // 2, int((bit + 0.5) * step)].mean() > 128:
            value |= 1 << bit
    return value


def image_index(image: media.FrameImage) -> int:
    rgb = np.frombuffer(image.data, np.uint8).reshape(image.height, image.width, 3)
    return decode_index(rgb)


def gif_frames(path: Path) -> list:
    with av.open(str(path)) as c:
        return list(c.decode(video=0))


def gif_duration(path: Path) -> float:
    with av.open(str(path)) as c:
        return c.duration / 1_000_000


def gif_target() -> Target:
    return next(t for t in VIDEO.targets("mp4") if t.id == "gif")


def convert_gif(src: Path, dst: Path, fragment=None, progress=None, preset=Preset.MEDIUM) -> None:
    VIDEO.convert([src], dst, gif_target(), preset, progress or (lambda p: None), CancelToken(),
                  formats=["mp4"], fragment=fragment)


def no_children(timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not multiprocessing.active_children():
            return True
        time.sleep(0.05)
    return False


class Report:
    def __call__(self, fraction, *marks) -> None:
        pass

    def final(self) -> None:
        pass


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("gif_fragment")


@pytest.fixture(scope="module")
def long_video(media_dir) -> Path:
    """3 минуты, 5 к/с, 96×16."""
    return make_coded_video(media_dir / "три минуты.mp4", 180, 5)


@pytest.fixture(scope="module")
def short_video(media_dir) -> Path:
    """3 секунды, 10 к/с."""
    return make_coded_video(media_dir / "короткое.mp4", 3, 10)


@pytest.fixture(scope="module")
def hd_video(media_dir) -> Path:
    return make_hd_video(media_dir / "hd.mp4")


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    """data/ (в том числе data/temp для предпросмотра) — во временной папке."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.setattr(paths, "app_root", lambda: app_dir)
    return app_dir


def temp_files() -> list[Path]:
    folder = paths.temp_dir()
    return list(folder.iterdir()) if folder is not None and folder.exists() else []


# --- Ядро: проверка фрагмента -----------------------------------------------


def test_parse_fragment():
    assert media.parse_fragment(None) is None
    assert media.parse_fragment((70, 100)) == (70.0, 100.0)
    assert media.parse_fragment([0.0, 60.0]) == (0.0, 60.0)
    for bad in ((5, 5), (10, 5), (-1, 3), (float("nan"), 3), (0, float("inf")), ("0", 3),
                (True, 3), (1,), "ab", 5):
        with pytest.raises(media.FragmentError) as info:
            media.parse_fragment(bad)
        assert info.value.key == "fragment_bad"
    with pytest.raises(media.FragmentError) as info:
        media.parse_fragment((0, 60.5))
    assert info.value.key == "fragment_too_long"


def test_core_rejects_bad_fragment(short_video, tmp_path):
    """П27 (ядро): длиннее 60 с, конец ≤ начала, фрагмент не для GIF — ошибка до запуска ребёнка."""
    with pytest.raises(ConversionError, match="слишком длинный: 61.0 с, максимум 60 с"):
        convert_gif(short_video, tmp_path / "a.gif", (0, 61))
    with pytest.raises(ConversionError, match="конец должен быть позже начала"):
        convert_gif(short_video, tmp_path / "b.gif", (2, 2))
    with pytest.raises(ConversionError, match="конец должен быть позже начала"):
        convert_gif(short_video, tmp_path / "c.gif", (2, 1))
    mp4 = next(t for t in VIDEO.targets("mkv") if t.id == "mp4")
    with pytest.raises(ConversionError, match="только для GIF"):
        VIDEO.convert([short_video], tmp_path / "d.mp4", mp4, Preset.MEDIUM, lambda p: None,
                      CancelToken(), formats=["mp4"], fragment=(0, 1))
    # Ребёнок проверяет ещё раз (аргументам из родителя не доверяет).
    with pytest.raises(_isolated.TaskError) as info:
        media.child_task({"src": str(short_video), "dst": str(tmp_path / "e.gif"), "fmt": "mp4",
                          "target": "gif", "preset": "medium", "fragment": [0, 90]}, Report())
    assert info.value.key == "fragment_too_long"
    assert no_children()


# --- П26: видео 3 мин → фрагмент 1:10–1:40 ----------------------------------


def test_p26_fragment_of_long_video(long_video, tmp_path):
    dst = tmp_path / "фрагмент.gif"
    values: list[float] = []
    convert_gif(long_video, dst, (70.0, 100.0), progress=values.append)
    assert abs(gif_duration(dst) - 30.0) <= 0.5
    frames = gif_frames(dst)
    assert frames[0].pts == 0  # метки результата начинаются с 0
    first_index = decode_index(frames[0].to_ndarray(format="rgb24"))
    assert first_index == 70 * 5  # кадр видео на 1:10
    last_index = decode_index(frames[-1].to_ndarray(format="rgb24"))
    assert last_index == 100 * 5 - 1  # последний кадр раньше 1:40
    # Прогресс — по длине фрагмента: монотонный, доходит до конца.
    assert values == sorted(values) and values[-1] == 1.0
    # Картинка начала из окна (тот же ребёнок) — тот же кадр, что первый кадр GIF.
    info = media.fragment_frames(long_video, "mp4", 70.0, 100.0, CancelToken())
    assert info.duration == pytest.approx(180.0, abs=0.5)
    assert image_index(info.first) == first_index
    assert image_index(info.last) == last_index
    assert no_children()


def test_fragment_end_beyond_duration_is_clipped(short_video, tmp_path):
    """Конец позже фактической длины — GIF до конца видео, без ошибки."""
    dst = tmp_path / "хвост.gif"
    convert_gif(short_video, dst, (1.0, 50.0))
    assert abs(gif_duration(dst) - 2.0) <= 0.5
    assert decode_index(gif_frames(dst)[0].to_ndarray(format="rgb24")) == 10
    info = media.fragment_frames(short_video, "mp4", 1.0, 50.0, CancelToken())
    assert image_index(info.first) == 10
    assert image_index(info.last) == 29  # последний кадр видео


def test_fragment_start_after_end_and_empty(short_video, media_dir, tmp_path):
    with pytest.raises(ConversionError, match=r"Начало фрагмента \(10.0 с\) — за концом видео \(3.0 с\)"):
        convert_gif(short_video, tmp_path / "a.gif", (10.0, 20.0))
    # 1 к/с: кадры на 0, 1, 2 с — между 0,2 и 0,5 с кадров нет.
    sparse = make_coded_video(media_dir / "редкое.mp4", 3, 1)
    with pytest.raises(ConversionError, match="Во фрагменте нет ни одного кадра"):
        convert_gif(sparse, tmp_path / "b.gif", (0.2, 0.5))
    assert no_children()


def test_gif_without_fragment_unchanged(short_video, long_video, tmp_path):
    """Регрессия: без фрагмента — всё видео (≤ 60 с), длиннее — gif_too_long, как раньше."""
    dst = tmp_path / "всё.gif"
    convert_gif(short_video, dst)
    frames = gif_frames(dst)
    assert abs(gif_duration(dst) - 3.0) <= 0.5
    assert decode_index(frames[0].to_ndarray(format="rgb24")) == 0
    assert abs(len(frames) - 3 * 12) <= 2  # «Среднее»: 12 к/с
    with pytest.raises(ConversionError, match="Слишком длинное видео для GIF: 180 с, максимум 60 с"):
        convert_gif(long_video, tmp_path / "x.gif")
    assert no_children()


def test_job_manager_passes_fragment(short_video, tmp_path):
    """Фрагмент из `submit(options=...)` доходит до конвертера; без options — как раньше."""
    manager = JobManager()
    try:
        out = tmp_path / "out"
        out.mkdir()
        with_fragment = manager.submit([short_video], VIDEO, gif_target(), Preset.MEDIUM, out, "a",
                                       formats=["mp4"], options={"fragment": (1.0, 2.0)})
        plain = manager.submit([short_video], VIDEO, gif_target(), Preset.MEDIUM, out, "b",
                               formats=["mp4"])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (with_fragment.is_finished and plain.is_finished):
            time.sleep(0.05)
        assert with_fragment.status == JobStatus.DONE, with_fragment.error_message
        assert plain.status == JobStatus.DONE, plain.error_message
        assert abs(gif_duration(with_fragment.result_path) - 1.0) <= 0.5
        assert abs(gif_duration(plain.result_path) - 3.0) <= 0.5
    finally:
        manager.shutdown(wait=True, timeout=10)


# --- П28: предпросмотр (ядро) ------------------------------------------------


def test_preview_1080p_fast_and_small(hd_video, tmp_path):
    started = time.monotonic()
    data = media.gif_preview(hd_video, "mp4", 0.0, 2.0, tmp_path / "p.gif", CancelToken())
    elapsed = time.monotonic() - started
    print(f"\nпредпросмотр 1080p (2 с, ядро): {elapsed:.2f} с")
    assert elapsed <= 5.0
    path = tmp_path / "check.gif"
    path.write_bytes(data)
    frames = gif_frames(path)
    side, fps, _colors = media.PREVIEW_GIF_SETTINGS
    assert max(frames[0].width, frames[0].height) <= side
    assert abs(len(frames) - 2 * fps) <= 2
    assert no_children()


def test_frames_of_broken_video(tmp_path):
    broken = tmp_path / "битое.mp4"
    broken.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x13\x37" * 4000)
    with pytest.raises(ConversionError, match="повреждён"):
        media.fragment_frames(broken, "mp4", 0.0, None, CancelToken())
    assert no_children()


# --- Окно: время, значения по умолчанию, ограничения (П27) ------------------


def test_parse_and_format_time():
    assert parse_time("70") == 70
    assert parse_time("1:10") == 70
    assert parse_time("01:10.5") == pytest.approx(70.5)
    assert parse_time("0:05,3") == pytest.approx(5.3)
    assert parse_time("1:02:03") == 3723
    assert parse_time(" 2:00.0 ") == 120
    for bad in ("", "abc", "1:60", "1:61:00", "-5", "1::2", "1:2:3:4"):
        assert parse_time(bad) is None, bad
    assert format_time(70) == "01:10.0"
    assert format_time(70.46) == "01:10.5"
    assert format_time(3723.0) == "1:02:03.0"
    assert parse_time(format_time(99.9)) == pytest.approx(99.9)


def open_dialog(qtbot, src: Path, fmt: str = "mp4", **kwargs) -> GifFragmentDialog:
    dialog = GifFragmentDialog(src, fmt, **kwargs)
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog


def wait_ready(qtbot, dialog: GifFragmentDialog) -> None:
    qtbot.waitUntil(lambda: dialog.ready or dialog.read_error is not None, timeout=CHILD_TIMEOUT)


def test_dialog_defaults_short_and_long(qtbot, short_video, long_video):
    """П27: видео ≤ 60 с — целиком; длиннее — 0–60 с; кадры по краям видны."""
    short = open_dialog(qtbot, short_video)
    wait_ready(qtbot, short)
    assert short.fragment == (0.0, 3.0)
    assert short.make_button.isEnabled()
    assert short.first_label.pixmap() is not None and not short.first_label.pixmap().isNull()
    assert image_index(short.frames_info.first) == 0
    assert image_index(short.frames_info.last) == 29
    short.reject()
    long = open_dialog(qtbot, long_video)
    wait_ready(qtbot, long)
    assert long.fragment == (0.0, 60.0)
    assert long.start_slider.maximum() == 1800  # 180 с в десятых
    assert long.make_button.isEnabled()
    long.reject()
    assert short.choice.kind == "cancel" and long.choice.kind == "cancel"
    assert no_children()


def test_dialog_blocks_bad_fragment(qtbot, long_video):
    """П27: длиннее 60 с, конец ≤ начала, неверное время — «Сделать GIF» выключена с пояснением."""
    dialog = open_dialog(qtbot, long_video)
    wait_ready(qtbot, dialog)
    dialog.set_fragment(10.0, 71.0)
    assert not dialog.make_button.isEnabled() and not dialog.preview_button.isEnabled()
    assert dialog.hint_label.text() == tr("gif_err_too_long").format(max="60")
    dialog.set_fragment(30.0, 30.0)
    assert not dialog.make_button.isEnabled()
    assert dialog.hint_label.text() == tr("gif_err_end_before_start")
    dialog.set_fragment(40.0, 20.0)
    assert not dialog.make_button.isEnabled()
    # Ползунок тоже ограничен длиной видео; ввод больше длины обрезается.
    dialog.set_fragment(170.0, 500.0)
    assert dialog.fragment == (170.0, 180.0)
    assert dialog.make_button.isEnabled()
    # Неверно введённое время.
    dialog.end_edit.setText("абв")
    dialog.end_edit.editingFinished.emit()
    assert not dialog.make_button.isEnabled()
    assert "абв" in dialog.hint_label.text()
    # Ввод времени вручную в формате мм:сс.
    dialog.start_edit.setText("1:10")
    dialog.start_edit.editingFinished.emit()
    dialog.end_edit.setText("01:40.0")
    dialog.end_edit.editingFinished.emit()
    assert dialog.fragment == (70.0, 100.0)
    assert dialog.start_slider.value() == 700 and dialog.end_slider.value() == 1000
    assert dialog.make_button.isEnabled() and dialog.hint_label.text() == ""
    # Кадры обновились (с задержкой) — это кадры 1:10 и последний перед 1:40.
    qtbot.waitUntil(lambda: dialog.frames_info is not None and dialog.frames_info.start == 70.0,
                    timeout=CHILD_TIMEOUT)
    assert image_index(dialog.frames_info.first) == 350
    assert image_index(dialog.frames_info.last) == 499
    dialog.make_button.click()
    assert dialog.choice == GifChoice("one", 70.0, 100.0)
    assert no_children()


def test_dialog_debounce_single_request(qtbot, long_video, monkeypatch):
    """Частые изменения — один запрос кадров после паузы (дребезг)."""
    calls: list[tuple] = []
    real = gif_fragment._frames_job

    def counting(source, fmt, start, end, token):
        calls.append((start, end))
        return real(source, fmt, start, end, token)

    monkeypatch.setattr(gif_fragment, "_frames_job", counting)
    dialog = open_dialog(qtbot, long_video)
    wait_ready(qtbot, dialog)
    calls.clear()
    for start in range(10, 30):
        dialog.set_fragment(float(start), float(start + 30))
    qtbot.waitUntil(lambda: dialog.frames_info.start == 29.0, timeout=CHILD_TIMEOUT)
    assert calls == [(29.0, 59.0)]
    dialog.reject()


# --- П28: предпросмотр в окне ------------------------------------------------


def test_dialog_preview_1080p(qtbot, hd_video):
    dialog = open_dialog(qtbot, hd_video)
    wait_ready(qtbot, dialog)
    assert dialog.fragment == (0.0, 2.0)
    started = time.monotonic()
    with qtbot.waitSignal(dialog.preview_shown, timeout=10000):
        dialog.preview_button.click()
    elapsed = time.monotonic() - started
    print(f"\nпредпросмотр 1080p (2 с, окно): {elapsed:.2f} с")
    assert elapsed <= 5.0
    assert dialog.movie is not None and dialog.movie.isValid()
    assert dialog.preview_label.movie() is dialog.movie
    assert not temp_files()  # файл предпросмотра уже удалён (GIF — из памяти)
    dialog.reject()
    assert no_children()


def test_dialog_close_cancels_preview(qtbot, short_video, monkeypatch):
    """Закрытие окна во время предпросмотра: ребёнок убит, временного файла нет."""
    dialog = open_dialog(qtbot, short_video)
    wait_ready(qtbot, dialog)
    assert no_children()
    # Предпросмотр «зависает» в ребёнке (тестовая задача) — его прервёт только закрытие.
    monkeypatch.setattr(media, "_TASK", "test_hang")
    dialog.preview_button.click()
    qtbot.waitUntil(lambda: bool(multiprocessing.active_children()), timeout=CHILD_TIMEOUT)
    assert temp_files()  # временный файл предпросмотра создан
    dialog.close()
    assert no_children(5.0)
    qtbot.waitUntil(lambda: not temp_files(), timeout=5000)
    assert dialog.choice.kind == "cancel"


def test_dialog_broken_video(qtbot, tmp_path):
    """Битое видео: понятное сообщение в окне, «Сделать GIF» выключена, без исключений."""
    broken = tmp_path / "битое.mp4"
    broken.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x13\x37" * 4000)
    dialog = open_dialog(qtbot, broken)
    wait_ready(qtbot, dialog)
    assert not dialog.ready
    assert dialog.status_label.text() == tr("gif_read_failed").format(error=media.MSG_DAMAGED)
    assert not dialog.make_button.isEnabled() and not dialog.preview_button.isEnabled()
    assert dialog.cancel_button.isEnabled()
    dialog.cancel_button.click()
    assert dialog.choice.kind == "cancel"
    assert no_children()


def test_dialog_preview_error_message(qtbot, short_video, monkeypatch):
    """Ошибка в ребёнке при предпросмотре — сообщение в окне, а не падение."""
    dialog = open_dialog(qtbot, short_video)
    wait_ready(qtbot, dialog)
    monkeypatch.setattr(media, "_TASK", "test_abort")
    with qtbot.waitSignal(dialog.preview_failed, timeout=CHILD_TIMEOUT) as blocker:
        dialog.preview_button.click()
    assert blocker.args[0] == tr("gif_preview_failed").format(error=media.MSG_DAMAGED)
    assert dialog.preview_label.text() == blocker.args[0]
    qtbot.waitUntil(lambda: not temp_files(), timeout=5000)
    dialog.reject()
    assert no_children()


def test_apply_all_button(qtbot, short_video):
    dialog = open_dialog(qtbot, short_video, index=1, total=3)
    assert dialog.windowTitle() == tr("gif_dialog_title_n").format(index=1, total=3)
    wait_ready(qtbot, dialog)
    dialog.apply_all_spin.setValue(5)
    assert dialog.apply_all_button.text() == tr("gif_apply_all").format(n=5)
    assert dialog.apply_all_spin.maximum() == 60
    dialog.apply_all_button.click()
    assert dialog.choice == GifChoice("all", 0.0, 5.0)
    single = open_dialog(qtbot, short_video)
    assert single.apply_all_button is None
    single.reject()
    assert no_children()


# --- Главное окно: группа из нескольких видео -------------------------------


@pytest.fixture
def window(qtbot):
    win = MainWindow(warm_up_detector=False)
    qtbot.addWidget(win)
    yield win
    win.shutdown()


def _video_group(qtbot, win: MainWindow, folder: Path, seconds=(3, 2, 4)) -> list[Path]:
    folder.mkdir()
    sources = [make_coded_video(folder / f"видео{i}.mp4", s, 10) for i, s in enumerate(seconds)]
    win.add_paths(sources)
    qtbot.waitUntil(lambda: not win.is_busy_detecting and len(win.group_panels) >= 1,
                    timeout=DETECT_TIMEOUT)
    panel = win.group_panels[0]
    assert panel.group.category == Category.VIDEO and len(panel.group.files) == len(seconds)
    assert panel.set_target("gif")
    return sources


def _wait_jobs(qtbot, win: MainWindow, count: int) -> list:
    qtbot.waitUntil(lambda: len(win.job_manager.jobs()) == count
                    and all(j.is_finished for j in win.job_manager.jobs()), timeout=JOB_TIMEOUT)
    return win.job_manager.jobs()


def test_group_dialog_per_video_and_cancel(qtbot, window, tmp_path, monkeypatch):
    """Окно — по очереди для каждого видео; «Отмена» — это видео не конвертируется."""
    sources = _video_group(qtbot, window, tmp_path / "видео")
    asked: list[tuple] = []
    answers = [GifChoice("one", 1.0, 2.0), gif_fragment.CANCEL, GifChoice("one", 0.5, 3.5)]

    def fake_ask(source, fmt, index, total):
        asked.append((Path(source).name, fmt, index, total))
        return answers[index - 1]

    monkeypatch.setattr(window, "_ask_gif_fragment", fake_ask)
    window.group_panels[0].convert_button.click()
    assert [a[2:] for a in asked] == [(1, 3), (2, 3), (3, 3)]
    jobs = _wait_jobs(qtbot, window, 2)
    assert all(j.status == JobStatus.DONE for j in jobs), [j.error_message for j in jobs]
    by_name = {j.sources[0].name: j for j in jobs}
    asked_names = [a[0] for a in asked]
    cancelled = asked_names[1]
    assert cancelled not in by_name
    assert by_name[asked_names[0]].options == {"fragment": (1.0, 2.0)}
    assert abs(gif_duration(by_name[asked_names[0]].result_path) - 1.0) <= 0.5
    assert abs(gif_duration(by_name[asked_names[2]].result_path) - 3.0) <= 0.5
    assert any(tr("gif_skipped").split(":")[0] in n.text() for n in _notice_texts(window))
    assert {s.name for s in sources} == set(asked_names)


def _notice_texts(win: MainWindow):
    from PySide6.QtWidgets import QLabel

    labels = []
    for notice in win.notices:
        labels.extend(notice.findChildren(QLabel))
    return labels


def test_group_apply_all(qtbot, window, tmp_path, monkeypatch):
    """«Применить ко всем: первые N с» — остальные видео без новых окон, фрагмент 0..min(N, длина)."""
    _video_group(qtbot, window, tmp_path / "видео", seconds=(3, 2, 4))
    asked: list[int] = []

    def fake_ask(source, fmt, index, total):
        asked.append(index)
        return GifChoice("all", 0.0, 2.5)

    monkeypatch.setattr(window, "_ask_gif_fragment", fake_ask)
    window.group_panels[0].convert_button.click()
    assert asked == [1]
    jobs = _wait_jobs(qtbot, window, 3)
    assert all(j.status == JobStatus.DONE for j in jobs), [j.error_message for j in jobs]
    assert all(j.options == {"fragment": (0.0, 2.5)} for j in jobs)
    durations = sorted(round(gif_duration(j.result_path), 1) for j in jobs)
    # Видео 2 с короче N — целиком; 3 и 4 с — первые 2,5 с.
    assert abs(durations[0] - 2.0) <= 0.5 and abs(durations[1] - 2.5) <= 0.5
    assert abs(durations[2] - 2.5) <= 0.5
    assert window.group_panels == []


def test_group_real_dialog_does_not_block_queue(qtbot, window, tmp_path, monkeypatch):
    """Настоящее окно: пока оно открыто, уже поставленная задача выполняется."""
    _video_group(qtbot, window, tmp_path / "видео", seconds=(2, 2))
    seen_running: list[bool] = []

    class AutoDialog(GifFragmentDialog):
        def exec(self) -> int:  # noqa: A003 — имя из Qt
            self.show()
            qtbot.waitUntil(lambda: self.ready, timeout=CHILD_TIMEOUT)
            if self.windowTitle().endswith("2 из 2"):
                # Задача первого видео идёт (или уже готова), пока открыто второе окно.
                qtbot.waitUntil(lambda: any(j.is_finished for j in window.job_manager.jobs()),
                                timeout=JOB_TIMEOUT)
                seen_running.append(True)
            self.set_fragment(0.5, 1.5)
            self.make_button.click()
            return int(self.result())

    from konverter.ui import main_window as main_window_module

    monkeypatch.setattr(main_window_module, "GifFragmentDialog", AutoDialog)
    window.group_panels[0].convert_button.click()
    assert seen_running == [True]
    jobs = _wait_jobs(qtbot, window, 2)
    assert all(j.status == JobStatus.DONE for j in jobs), [j.error_message for j in jobs]
    assert all(j.options == {"fragment": (0.5, 1.5)} for j in jobs)
    assert no_children()


def test_gif_dialog_only_for_gif(qtbot, window, tmp_path, monkeypatch):
    """Другие варианты видео окна не открывают (прежнее поведение)."""
    _video_group(qtbot, window, tmp_path / "видео", seconds=(1,))
    monkeypatch.setattr(window, "_ask_gif_fragment",
                        lambda *a: pytest.fail("окно GIF не должно открываться"))
    assert window.group_panels[0].set_target("mkv")
    window.group_panels[0].convert_button.click()
    jobs = _wait_jobs(qtbot, window, 1)
    assert jobs[0].options == {}
    assert REGISTRY[Category.VIDEO] is not None


# --- Длительность неизвестна --------------------------------------------------


class _WriteOnly:
    """Файл без seek: муксер Matroska не может дописать длительность."""

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, chunk) -> int:
        self.data += bytes(chunk)
        return len(chunk)


def make_video_without_duration(path: Path, seconds: float = 2.0, fps: int = 10) -> Path:
    sink = _WriteOnly()
    with av.open(sink, "w", format="matroska") as out:
        stream = out.add_stream("libx264", rate=fps, options={"preset": "ultrafast"})
        stream.width, stream.height, stream.pix_fmt = BITS * STRIPE, 16, "yuv420p"
        for index in range(int(seconds * fps)):
            img = np.zeros((16, BITS * STRIPE, 3), np.uint8)
            for bit in range(BITS):
                if index >> bit & 1:
                    img[:, bit * STRIPE:(bit + 1) * STRIPE] = 255
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = index
            frame.time_base = Fraction(1, fps)
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    path.write_bytes(bytes(sink.data))
    with av.open(str(path), format="matroska") as c:
        assert c.duration is None and c.streams.video[0].duration is None
    return path


def test_unknown_duration(qtbot, tmp_path):
    """Длительность неизвестна: ввод вручную (≤ 60 с), конец позже длины обрезается ядром."""
    src = make_video_without_duration(tmp_path / "без длины.mkv")
    dialog = open_dialog(qtbot, src, "mkv")
    wait_ready(qtbot, dialog)
    assert dialog.ready and dialog.duration is None
    assert dialog.duration_label.text() == tr("gif_duration_unknown").format(max="60")
    assert not dialog.start_slider.isEnabled() and dialog.start_edit.isEnabled()
    assert dialog.fragment == (0.0, 60.0) and dialog.make_button.isEnabled()
    assert image_index(dialog.frames_info.first) == 0
    assert image_index(dialog.frames_info.last) == 19  # последний кадр видео
    dialog.set_fragment(5.0, 70.0)
    assert not dialog.make_button.isEnabled()  # 65 с > 60 с
    dialog.set_fragment(0.5, 60.0)
    assert dialog.make_button.isEnabled()
    dialog.make_button.click()
    mkv_gif = next(t for t in VIDEO.targets("mkv") if t.id == "gif")
    dst = tmp_path / "без длины.gif"
    VIDEO.convert([src], dst, mkv_gif, Preset.MEDIUM, lambda p: None, CancelToken(),
                  formats=["mkv"], fragment=(dialog.choice.start, dialog.choice.end))
    assert abs(gif_duration(dst) - 1.5) <= 0.5
    assert decode_index(gif_frames(dst)[0].to_ndarray(format="rgb24")) == 5
    # Начало за концом (длина неизвестна, проверить заранее нельзя) — «нет кадров».
    with pytest.raises(ConversionError, match="Во фрагменте нет ни одного кадра"):
        VIDEO.convert([src], tmp_path / "пусто.gif", mkv_gif, Preset.MEDIUM, lambda p: None,
                      CancelToken(), formats=["mkv"], fragment=(10.0, 20.0))
    info = media.fragment_frames(src, "mkv", 10.0, 20.0, CancelToken())
    assert info.first is None and info.last is None
    assert no_children()


# --- Ревью 1: короткие видео, точная длительность, AVI с B-кадрами ------------


def test_parse_fragment_overflow():
    for bad in ((10**400, 5), (0, 10**400)):
        with pytest.raises(media.FragmentError) as info:
            media.parse_fragment(bad)
        assert info.value.key == "fragment_bad"


def test_one_frame_video(qtbot, media_dir, tmp_path):
    """Видео из 1 кадра (0,04 с): фрагмент по умолчанию — целиком, GIF делается."""
    src = make_coded_video(media_dir / "один кадр.mp4", 0.04, 25)
    dialog = open_dialog(qtbot, src)
    wait_ready(qtbot, dialog)
    assert dialog.duration == pytest.approx(0.04)
    assert dialog.fragment == (0.0, dialog.duration)
    assert dialog.make_button.isEnabled(), dialog.hint_label.text()
    dialog.make_button.click()
    assert dialog.choice.kind == "one"
    dst = tmp_path / "один.gif"
    convert_gif(src, dst, (dialog.choice.start, dialog.choice.end))
    frames = gif_frames(dst)
    assert len(frames) >= 1 and decode_index(frames[0].to_ndarray(format="rgb24")) == 0


def test_default_fragment_is_whole_video(qtbot, media_dir, tmp_path):
    """Видео 10,04 с: по умолчанию — точная длительность, в GIF все кадры (как без фрагмента)."""
    src = make_coded_video(media_dir / "10,04.mp4", 10.04, 25)
    dialog = open_dialog(qtbot, src)
    wait_ready(qtbot, dialog)
    assert dialog.fragment == (0.0, pytest.approx(10.04))
    # Потеря фокуса без правки текста («00:10.0») конец не округляет.
    dialog.end_edit.editingFinished.emit()
    assert dialog.fragment[1] == pytest.approx(10.04)
    # Ползунок в крайнем правом положении — тоже точная длительность.
    dialog.end_slider.setValue(dialog.end_slider.minimum())
    dialog.end_slider.setValue(dialog.end_slider.maximum())
    assert dialog.fragment[1] == pytest.approx(10.04)
    dialog.make_button.click()
    whole = tmp_path / "целиком.gif"
    fragment = tmp_path / "фрагмент.gif"
    convert_gif(src, whole)
    convert_gif(src, fragment, (dialog.choice.start, dialog.choice.end))
    a, b = gif_frames(whole), gif_frames(fragment)
    assert len(a) == len(b)
    last = lambda frames: decode_index(frames[-1].to_ndarray(format="rgb24"))  # noqa: E731
    assert last(a) == last(b) >= 245
    assert gif_duration(whole) == pytest.approx(gif_duration(fragment), abs=0.01)


def test_avi_bframes_whole_fragment(media_dir, tmp_path):
    """H.264 в AVI с B-кадрами: фрагмент на всё видео = GIF без фрагмента (метки AVI сдвинуты)."""
    src = media_dir / "bframes.avi"
    with av.open(str(src), "w", format="avi") as out:
        stream = out.add_stream("libx264", rate=25, options={"preset": "ultrafast", "bf": "2"})
        stream.width, stream.height, stream.pix_fmt = BITS * STRIPE, 16, "yuv420p"
        for index in range(75):
            img = np.zeros((16, BITS * STRIPE, 3), np.uint8)
            img[:, : (index * 3) % (BITS * STRIPE)] = 255
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = index
            frame.time_base = Fraction(1, 25)
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    target = next(t for t in VIDEO.targets("avi") if t.id == "gif")
    results = {}
    for name, frag in (("all", None), ("frag", (0.0, 3.0))):
        dst = tmp_path / f"{name}.gif"
        VIDEO.convert([src], dst, target, Preset.MEDIUM, lambda p: None, CancelToken(),
                      formats=["avi"], fragment=frag)
        results[name] = (len(gif_frames(dst)), gif_duration(dst))
    assert results["frag"] == results["all"]


def test_invalid_fragment_clears_thumbs(qtbot, short_video):
    dialog = open_dialog(qtbot, short_video)
    wait_ready(qtbot, dialog)
    dialog.set_fragment(2.0, 1.0)
    for label in (dialog.first_label, dialog.last_label):
        assert label.text() == tr("gif_frame_unavailable")
        assert label.pixmap() is None or label.pixmap().isNull()
    dialog.reject()


def test_preview_one_pass_long_fragment(long_video, tmp_path, monkeypatch):
    """Предпросмотр 60 с: один проход по файлу, к/с предпросмотра, число кадров ограничено."""
    opens: list[str] = []
    real_open = media._open_input

    def counting_open(src, fmt, max_streams):
        opens.append(src)
        return real_open(src, fmt, max_streams)

    monkeypatch.setattr(media, "_open_input", counting_open)
    report = Report()
    dst = tmp_path / "p.gif"
    media.child_task({"action": "preview", "src": str(long_video), "dst": str(dst), "fmt": "mp4",
                      "target": "gif", "preset": "medium", "fragment": [60.0, 120.0],
                      "gif_settings": list(media.PREVIEW_GIF_SETTINGS)}, report)
    assert len(opens) == 2  # заголовки + один проход (без второго чтения файла)
    with av.open(str(dst)) as c:
        packets = [p for p in c.demux(video=0) if p.size]
    # Видео 5 к/с < к/с предпросмотра: каждый кадр видео — кадр GIF, ровно через 0,2 с.
    assert abs(len(packets) - 60 * 5) <= 1
    assert len(packets) <= media.PREVIEW_MAX_FRAMES
    assert {p.duration for p in packets[:-1]} == {20}
    frames = gif_frames(dst)
    assert decode_index(frames[0].to_ndarray(format="rgb24")) == 60 * 5
    assert abs(gif_duration(dst) - 60.0) <= 0.5


# --- Ревью 2: заниженная длительность в заголовке, потоки декодера ----------


def test_understated_duration_keeps_gif_limit(media_dir, tmp_path, monkeypatch):
    """Заголовок «врёт» (55 с вместо 120): фрагмент 0–60 с не даёт GIF длиннее предела."""
    src = make_coded_video(media_dir / "120 с.mp4", 120, 5)
    real = media._MediaJob._inspect

    def lying(self, inp):
        video, audio, _duration = real(self, inp)
        return video, audio, 55.0

    monkeypatch.setattr(media._MediaJob, "_inspect", lying)
    for single in (False, True):
        dst = tmp_path / f"врёт{single}.gif"
        args = {"src": str(src), "dst": str(dst), "fmt": "mp4", "target": "gif",
                "preset": "medium", "fragment": [0.0, 60.0]}
        if single:
            args.update(action="preview", gif_settings=list(media.PREVIEW_GIF_SETTINGS))
        media.child_task(args, Report())
        assert gif_duration(dst) <= media.GIF_MAX_SECONDS + 0.5, single


def test_preview_decoder_threads_by_frame_size():
    class CC:
        def __init__(self, w, h):
            self.width, self.height = w, h

    class S:
        type = "video"

        def __init__(self, w, h):
            self.codec_context = CC(w, h)

    cpus = __import__("os").cpu_count() or 1
    for (w, h), expected in (((1920, 1080), min(16, cpus)), ((5120, 2880), min(8, cpus)),
                             ((8000, 5000), 2)):
        stream = S(w, h)
        media._MediaJob._setup_decoder(stream, media.PREVIEW_DECODER_THREADS)
        assert stream.codec_context.thread_count == expected, (w, h)


# --- Вид окна: заголовок, место предпросмотра, анимации ----------------------


def test_dialog_title_and_compact_preview_area(qtbot, short_video):
    dialog = open_dialog(qtbot, short_video, index=1, total=2)
    name_font = dialog.name_label.font()
    assert dialog.name_label.text() == short_video.name
    assert name_font.bold() and name_font.pointSizeF() > dialog.duration_label.font().pointSizeF()
    # Пока предпросмотра нет — строка подсказки, а не пустое поле 240×140.
    assert dialog.preview_label.minimumHeight() == 0
    assert dialog.preview_label.sizeHint().height() < 3 * dialog.fontMetrics().height()
    # «Применить ко всем» — вторичная: не основная кнопка, плоская.
    assert dialog.make_button.isDefault()
    assert dialog.apply_all_button.isFlat() and not dialog.apply_all_button.isDefault()
    dialog.reject()
    assert no_children()


@pytest.mark.parametrize("animations", [True, False])
def test_preview_respects_animation_setting_and_click_toggles(qtbot, short_video, monkeypatch,
                                                              animations):
    from PySide6.QtGui import QMovie

    from konverter.ui import common

    monkeypatch.setattr(common, "system_animations_enabled", lambda: animations)
    dialog = open_dialog(qtbot, short_video)
    wait_ready(qtbot, dialog)
    with qtbot.waitSignal(dialog.preview_shown, timeout=CHILD_TIMEOUT):
        dialog.preview_button.click()
    running, paused = QMovie.MovieState.Running, QMovie.MovieState.Paused
    # Анимации выключены в Windows — предпросмотр сам не крутится (первый кадр).
    assert dialog.movie.state() == (running if animations else paused)
    qtbot.mouseClick(dialog.preview_label, Qt.MouseButton.LeftButton)
    assert dialog.movie.state() == (paused if animations else running)
    qtbot.mouseClick(dialog.preview_label, Qt.MouseButton.LeftButton)
    assert dialog.movie.state() == (running if animations else paused)
    dialog.reject()
    assert no_children()


def test_thumbnails_inside_frame(qtbot, short_video):
    """Картинка кадра — внутри рамки метки (рамка видна целиком, в том числе справа)."""
    dialog = open_dialog(qtbot, short_video)
    wait_ready(qtbot, dialog)
    qtbot.waitUntil(lambda: not dialog.last_label.pixmap().isNull(), timeout=CHILD_TIMEOUT)
    for label in (dialog.first_label, dialog.last_label):
        pixmap = label.pixmap()
        logical = pixmap.size() / pixmap.devicePixelRatio()
        inner = label.contentsRect()
        assert label.frameWidth() > 0
        assert logical.width() <= inner.width() and logical.height() <= inner.height()
    dialog.reject()
    assert no_children()
