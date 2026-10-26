"""Аудио и видео (этап 2): конвертеры media.py и общий режим задач _isolated.run_task.

Входные файлы генерируются здесь же через av (синтетические: тон + шум в
звуке, две цветные половины + шум в видео, 1–2 с, малое разрешение).

Матрица «каждый вход × каждый выход» (П12) выполняется функцией
`media.child_task` ПРЯМО в процессе тестов — это тот же код, что работает в
дочернем процессе, но без 0,6 с на запуск ребёнка для каждой из ~100 пар.
Путь через настоящий дочерний процесс (`convert` → `_isolated.run_task`)
проверяется отдельными тестами: выборочные пары, прогресс, отмена, пресеты,
поворот, метаданные, «злые» файлы, длинные пути, сквозной JobManager.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import shutil
import struct
import threading
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest

from konverter.converters import _isolated, media
from konverter.converters.base import (
    REGISTRY,
    CancelToken,
    Cancelled,
    Category,
    ConversionError,
    Preset,
)
from konverter.core import naming
from konverter.core.jobs import JobManager, JobStatus
from konverter.strings import tr

VIDEO = media.VideoConverter()
AUDIO = media.AudioConverter()

# --- Генерация тестовых файлов --------------------------------------------------

# Код формата → (муксер, видеокодек, звуковой кодек).
VIDEO_SPECS = {
    "mp4": ("mp4", "libx264", "aac"),
    "mov": ("mov", "libx264", "aac"),
    "mkv": ("matroska", "libx264", "aac"),
    "avi": ("avi", "mpeg4", "libmp3lame"),
    "webm": ("webm", "libvpx-vp9", "libopus"),
    "wmv": ("asf", "wmv2", "wmav2"),
    "flv": ("flv", "flv", "libmp3lame"),
    "3gp": ("3gp", "h263", "aac"),
    "mts": ("mpegts", "libx264", "aac"),
}
# Код формата → (муксер, кодек).
AUDIO_SPECS = {
    "mp3": ("mp3", "libmp3lame"),
    "wav": ("wav", "pcm_s16le"),
    "flac": ("flac", "flac"),
    "aac": ("adts", "aac"),
    "m4a": ("ipod", "aac"),
    "ogg": ("ogg", "vorbis"),
    "opus": ("opus", "libopus"),
    "wma": ("asf", "wmav2"),
}
_SAMPLE_FMT = {
    "aac": "fltp", "libmp3lame": "fltp", "libopus": "s16", "wmav2": "fltp",
    "pcm_s16le": "s16", "flac": "s16", "vorbis": "fltp",
}


def _encoder_exists(name: str) -> bool:
    try:
        av.Codec(name, "w")
        return True
    except Exception:  # noqa: BLE001
        return False


AMR_ENCODER = next(
    (n for n in ("libopencore_amrnb", "amr_nb", "libvo_amrwbenc") if _encoder_exists(n)), None
)


def _audio_stream(out, codec: str, rate: int, channels: int):
    options = {"strict": "experimental"} if codec == "vorbis" else {}
    stream = out.add_stream(codec, rate=rate, options=options)
    cc = stream.codec_context
    cc.layout = "mono" if channels == 1 and codec != "vorbis" else "stereo"
    cc.format = _SAMPLE_FMT[codec]
    if codec == "wmav2":
        cc.bit_rate = 128_000
    return stream


def _write_audio(out, stream, seconds: float, rate: int) -> None:
    cc = stream.codec_context
    frame_size = cc.frame_size or 1024
    total = int(seconds * rate)
    rng = np.random.default_rng(1)
    channels = cc.layout.nb_channels
    pos = 0
    while pos < total:
        n = min(frame_size, total - pos)
        if n < frame_size and cc.name in ("vorbis", "wmav2"):
            n = frame_size  # эти энкодеры не принимают короткий последний кадр
        t = np.arange(pos, pos + n) / rate
        tone = (0.3 * np.sin(2 * np.pi * 440 * t) + 0.05 * rng.standard_normal(n)).astype(np.float32)
        data = np.tile(tone, (channels, 1))
        if cc.format.name in ("s16", "s16p"):
            data = (data * 32767).astype(np.int16)
        if not cc.format.is_planar:
            data = data.T.reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(data), format=cc.format.name, layout=cc.layout.name
        )
        frame.sample_rate = rate
        frame.pts = pos
        frame.time_base = Fraction(1, rate)
        for packet in stream.encode(frame):
            out.mux(packet)
        pos += n
    for packet in stream.encode(None):
        out.mux(packet)


def make_audio(path: Path, fmt: str, seconds: float = 1.5, rate: int = 44100,
               channels: int = 2, metadata: dict | None = None) -> Path:
    container, codec = AUDIO_SPECS[fmt]
    if codec == "libopus":
        rate = 48000
    with av.open(str(path), "w", format=container) as out:
        if metadata:
            out.metadata.update(metadata)
        stream = _audio_stream(out, codec, rate, channels)
        _write_audio(out, stream, seconds, rate)
    return path


def make_video(path: Path, fmt: str, seconds: float = 1.5, size=(64, 48), fps: int = 10,
               audio: bool = True, rotation: float | None = None,
               metadata: dict | None = None, noise: bool = True) -> Path:
    """Видео: левая половина красная, правая синяя (+ шум), звук — тон 440 Гц."""
    container, vcodec, acodec = VIDEO_SPECS[fmt]
    if vcodec == "h263":
        size = (176, 144)  # H.263 допускает только стандартные размеры
    width, height = size
    rate = 48000 if acodec == "libopus" else 44100
    options = {"preset": "ultrafast"} if vcodec == "libx264" else {}
    if vcodec == "libvpx-vp9":
        options = {"cpu-used": "8", "deadline": "realtime"}
    with av.open(str(path), "w", format=container) as out:
        if metadata:
            out.metadata.update(metadata)
        vstream = out.add_stream(vcodec, rate=fps, options=options)
        vstream.width, vstream.height, vstream.pix_fmt = width, height, "yuv420p"
        if rotation is not None:
            vstream.set_display_rotation(rotation)
        astream = _audio_stream(out, acodec, rate, 2) if audio else None
        rng = np.random.default_rng(0)
        base = np.zeros((height, width, 3), np.int16)
        base[:, : width // 2] = (220, 30, 30)
        base[:, width // 2:] = (30, 30, 220)
        for index in range(int(round(seconds * fps))):
            img = base + rng.integers(-40, 40, base.shape) if noise else base
            frame = av.VideoFrame.from_ndarray(np.clip(img, 0, 255).astype(np.uint8), format="rgb24")
            frame.pts = index
            frame.time_base = Fraction(1, fps)
            for packet in vstream.encode(frame):
                out.mux(packet)
        for packet in vstream.encode(None):
            out.mux(packet)
        if astream is not None:
            _write_audio(out, astream, seconds, rate)
    return path


def info(path: Path) -> dict:
    """Длительность, потоки (тип → имя декодера) и метаданные файла."""
    with av.open(str(path)) as c:
        duration = c.duration / 1_000_000 if c.duration else None
        streams = [(s.type, s.codec_context.name) for s in c.streams]
        return {"duration": duration, "streams": streams, "metadata": dict(c.metadata)}


class FakeReport:
    """Колбэк прогресса для прямого вызова child_task."""

    def __init__(self) -> None:
        self.values: list[float] = []

    def __call__(self, fraction, *marks) -> None:
        if fraction is not None:
            self.values.append(fraction)

    def final(self) -> None:
        pass


def run_inline(src: Path, dst: Path, fmt: str, target: str, preset: Preset = Preset.MEDIUM) -> dict:
    return media.child_task(
        {"src": str(src), "dst": str(dst), "fmt": fmt, "target": target, "preset": preset.value},
        FakeReport(),
    )


def target_of(conv, fmt: str, target_id: str):
    return next(t for t in conv.targets(fmt) if t.id == target_id)


def convert(conv, src: Path, dst: Path, fmt: str, target_id: str, preset: Preset = Preset.MEDIUM,
            progress=None, cancel: CancelToken | None = None) -> None:
    conv.convert(
        [src], dst, target_of(conv, fmt, target_id), preset,
        progress or (lambda p: None), cancel or CancelToken(), formats=[fmt],
    )


def no_children() -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not multiprocessing.active_children():
            return True
        time.sleep(0.05)
    return False


# Ожидаемые имена ДЕКОДЕРОВ для кодеков результата.
VIDEO_CODEC = {"mp4": "h264", "mov": "h264", "mkv": "h264", "webm": "vp9", "avi": "mpeg4", "gif": "gif"}
AUDIO_CODEC = {
    "mp4": {"aac"}, "mov": {"aac"}, "mkv": {"aac"}, "webm": {"opus", "libopus"},
    "avi": {"mp3", "mp3float"}, "audio_mp3": {"mp3", "mp3float"}, "audio_wav": {"pcm_s16le"},
    "mp3": {"mp3", "mp3float"}, "wav": {"pcm_s16le"}, "flac": {"flac"}, "m4a": {"aac"},
    "ogg": {"vorbis"}, "opus": {"opus", "libopus"},
}


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="module")
def video_inputs(media_dir) -> dict[str, Path]:
    return {fmt: make_video(media_dir / f"видео {fmt}.{fmt}", fmt) for fmt in VIDEO_SPECS}


@pytest.fixture(scope="module")
def audio_inputs(media_dir) -> dict[str, Path]:
    return {fmt: make_audio(media_dir / f"звук {fmt}.{fmt}", fmt) for fmt in AUDIO_SPECS}


@pytest.fixture(scope="module")
def long_video(media_dir) -> Path:
    """30 с, 320×240, 25 к/с с шумом: перекодирование в VP9 идёт несколько секунд."""
    return make_video(media_dir / "долгое.mkv", "mkv", seconds=30, size=(320, 240), fps=25)


# --- Варианты и регистрация ----------------------------------------------------


def test_registered():
    assert isinstance(REGISTRY[Category.VIDEO], media.VideoConverter)
    assert isinstance(REGISTRY[Category.AUDIO], media.AudioConverter)


def test_video_targets():
    ids = [t.id for t in VIDEO.targets("mp4")]
    assert ids == ["mkv", "webm", "mov", "avi", "gif", "audio_mp3", "audio_wav"]
    assert [t.id for t in VIDEO.targets("wmv")] == list(media.VIDEO_TARGET_ORDER)
    for target in VIDEO.targets("mts"):
        assert target.is_long
        assert target.label == tr(f"target_{target.id}") and target.label != f"target_{target.id}"
        assert target.uses_preset == (target.id != "audio_wav")
    assert VIDEO.targets("mp3") == [] and VIDEO.targets("") == []
    assert VIDEO.target_order == media.VIDEO_TARGET_ORDER


def test_audio_targets():
    assert [t.id for t in AUDIO.targets("wav")] == ["mp3", "flac", "m4a", "ogg", "opus"]
    assert [t.id for t in AUDIO.targets("amr")] == list(media.AUDIO_TARGET_ORDER)
    presets = {t.id: t.uses_preset for t in AUDIO.targets("wma")}
    assert presets == {"mp3": True, "wav": False, "flac": False, "m4a": True, "ogg": True, "opus": True}
    assert all(t.is_long for t in AUDIO.targets("aac"))
    assert AUDIO.targets("mp4") == []


# --- П12: матрица вход × выход (код ребёнка в процессе тестов) -----------------------


def _check_result(src: Path, dst: Path, target: str, video: bool) -> None:
    got, expected = info(dst), info(src)
    assert got["duration"] is not None and expected["duration"] is not None
    assert abs(got["duration"] - expected["duration"]) <= 0.5, (got, expected)
    types = {t: name for t, name in got["streams"]}
    if video and target in VIDEO_CODEC:
        assert types.get("video") == VIDEO_CODEC[target]
    else:
        assert "video" not in types
    if target == "gif":
        assert "audio" not in types
    else:
        assert types.get("audio") in AUDIO_CODEC[target], got
    assert len(got["streams"]) <= 2


@pytest.mark.parametrize("fmt", list(VIDEO_SPECS))
def test_video_matrix(fmt, video_inputs, tmp_path):
    src = video_inputs[fmt]
    for target in VIDEO.targets(fmt):
        dst = tmp_path / f"out_{target.id}.{target.extension}"
        run_inline(src, dst, fmt, target.id)
        _check_result(src, dst, target.id, video=True)


@pytest.mark.parametrize("fmt", list(AUDIO_SPECS) + ["amr"])
def test_audio_matrix(fmt, audio_inputs, tmp_path):
    if fmt == "amr":
        if AMR_ENCODER is None:
            pytest.skip("в сборке FFmpeg из av нет энкодера AMR — тестовый AMR не сгенерировать")
        pytest.skip("генерация AMR не реализована")  # pragma: no cover
    src = audio_inputs[fmt]
    for target in AUDIO.targets(fmt):
        dst = tmp_path / f"out_{target.id}.{target.extension}"
        run_inline(src, dst, fmt, target.id)
        _check_result(src, dst, target.id, video=False)


def test_ffmpeg_log_callback_is_c_noop(audio_inputs, tmp_path):
    """Регрессия: числовой уровень журнала av ставит Python-обратный вызов,
    и декодирование зависало (взаимная блокировка на GIL из потоков FFmpeg)
    после настройки logging в процессе. child_task должен оставлять заглушку."""
    av.logging.set_level(av.logging.ERROR)
    try:
        run_inline(audio_inputs["wav"], tmp_path / "x.mp3", "wav", "mp3")
        assert av.logging.get_level() is None
    finally:
        av.logging.set_level(None)


def make_multichannel(path: Path, layout: str, container: str = "wav", codec: str = "pcm_s16le",
                      seconds: float = 1.0, rate: int = 48000, video: bool = False) -> Path:
    """Тишина + тон в первом канале с раскладкой `layout` (PCM/FLAC), при `video` — с видео."""
    channels = av.AudioLayout(layout).nb_channels
    with av.open(str(path), "w", format=container) as out:
        stream = out.add_stream(codec, rate=rate)
        stream.codec_context.layout = layout
        stream.codec_context.format = "s16"
        if video:
            vstream = out.add_stream("libx264", rate=10, options={"preset": "ultrafast"})
            vstream.width, vstream.height, vstream.pix_fmt = 64, 48, "yuv420p"
            for index in range(int(seconds * 10)):
                frame = av.VideoFrame.from_ndarray(np.zeros((48, 64, 3), np.uint8), format="rgb24")
                frame.pts = index
                frame.time_base = Fraction(1, 10)
                for packet in vstream.encode(frame):
                    out.mux(packet)
            for packet in vstream.encode(None):
                out.mux(packet)
        n = 1024
        for pos in range(0, int(seconds * rate), n):
            data = np.zeros((n, channels), np.int16)
            data[:, 0] = (8000 * np.sin(2 * np.pi * 440 * np.arange(pos, pos + n) / rate)).astype(np.int16)
            frame = av.AudioFrame.from_ndarray(data.reshape(1, -1), format="s16", layout=layout)
            frame.sample_rate = rate
            frame.pts = pos
            frame.time_base = Fraction(1, rate)
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return path


def _audio_channels(path: Path) -> tuple[int, str]:
    with av.open(str(path)) as c:
        cc = c.streams.audio[0].codec_context
        return cc.channels, cc.layout.name


def test_mono_and_6ch_audio(tmp_path):
    mono = make_audio(tmp_path / "mono.wav", "wav", channels=1)
    run_inline(mono, tmp_path / "mono.ogg", "wav", "ogg")  # Vorbis — только стерео
    run_inline(mono, tmp_path / "mono.mp3", "wav", "mp3")
    with av.open(str(tmp_path / "mono.mp3")) as c:
        assert c.streams.audio[0].codec_context.channels == 1
    # Ревью 1, п. 1: 3–8 каналов во FLAC/WAV («повреждён» из-за AudioLayout(int)).
    for layout, channels in (("5.1", 6), ("7.1", 8), ("3.0", 3), ("quad", 4)):
        wav = make_multichannel(tmp_path / f"{layout}.wav", layout)
        run_inline(wav, tmp_path / f"{layout}.flac", "wav", "flac")
        assert _audio_channels(tmp_path / f"{layout}.flac")[0] == channels
        run_inline(tmp_path / f"{layout}.flac", tmp_path / f"{layout}_back.wav", "flac", "wav")
        assert _audio_channels(tmp_path / f"{layout}_back.wav")[0] == channels
        _check_result(wav, tmp_path / f"{layout}_back.wav", "wav", video=False)
        # В сжатые форматы — сведение в стерео.
        run_inline(wav, tmp_path / f"{layout}.mp3", "wav", "mp3")
        assert _audio_channels(tmp_path / f"{layout}.mp3")[0] == 2


def test_extract_6ch_audio_from_video(tmp_path):
    """«Только звук WAV» из видео с 5.1 — 6 каналов."""
    src = make_multichannel(tmp_path / "кино.mkv", "5.1", container="matroska", video=True)
    dst = tmp_path / "кино.wav"
    run_inline(src, dst, "mkv", "audio_wav")
    assert _audio_channels(dst)[0] == 6


# --- Через настоящий дочерний процесс -----------------------------------------------


def test_child_video_and_audio(video_inputs, audio_inputs, tmp_path):
    dst = tmp_path / "out.mkv"
    values: list[float] = []
    convert(VIDEO, video_inputs["mp4"], dst, "mp4", "mkv", progress=values.append)
    _check_result(video_inputs["mp4"], dst, "mkv", video=True)
    assert values[-1] == 1.0
    dst = tmp_path / "out.opus"
    convert(AUDIO, audio_inputs["flac"], dst, "flac", "opus")
    _check_result(audio_inputs["flac"], dst, "opus", video=False)
    assert no_children()


def test_progress_monotonic(long_video, tmp_path):
    """П13: прогресс растёт монотонно от 0 до 1."""
    values: list[float] = []
    convert(VIDEO, long_video, tmp_path / "out.mp4", "mkv", "mp4", Preset.SMALL, values.append)
    assert len(values) >= 3
    assert all(0.0 <= v <= 1.0 for v in values)
    assert all(b >= a for a, b in zip(values, values[1:]))
    assert values[0] < 0.6 and values[-1] == 1.0


def test_cancel_long_video(long_video, tmp_path):
    """П13: отмена < 2 с, процесса-ребёнка нет, dst не переименован (через JobManager)."""
    folder = tmp_path / "work"
    folder.mkdir()
    src = folder / "клип.mkv"
    src.write_bytes(long_video.read_bytes())
    started = threading.Event()

    def on_update(job):
        if job.progress > 0.02:
            started.set()

    manager = JobManager(max_workers=1, on_update=on_update)
    try:
        job = manager.submit([src], VIDEO, target_of(VIDEO, "mkv", "webm"), Preset.HIGH,
                             folder, formats=["mkv"])
        assert started.wait(60), "конвертация не началась"
        assert job.status == JobStatus.RUNNING
        t0 = time.monotonic()
        manager.cancel(job.id)
        while not job.is_finished and time.monotonic() - t0 < 10:
            time.sleep(0.01)
        elapsed = time.monotonic() - t0
        assert job.status == JobStatus.CANCELLED
        assert elapsed < 2.0, elapsed
    finally:
        manager.shutdown(timeout=10)
    assert no_children()
    assert sorted(p.name for p in folder.iterdir()) == ["клип.mkv"]


def test_cancel_direct_leaves_dst_unrenamed(long_video, tmp_path):
    dst = tmp_path / ".~tmp.part.webm"
    token = CancelToken()
    errors: list[BaseException] = []
    progressed = threading.Event()

    def work():
        try:
            convert(VIDEO, long_video, dst, "mkv", "webm", Preset.HIGH,
                    progress=lambda p: progressed.set(), cancel=token)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    assert progressed.wait(60)
    t0 = time.monotonic()
    token.cancel()
    thread.join(10)
    assert time.monotonic() - t0 < 2.0
    assert errors and isinstance(errors[0], Cancelled)
    assert no_children()
    # Конвертер ничего не переименовывает: рядом только dst (его убирает ядро).
    assert [p.name for p in tmp_path.iterdir()] == [dst.name]


# --- Изоляция: общий режим задач -----------------------------------------------------


def test_run_task_progress_and_result():
    values: list[float] = []
    result = _isolated.run_task(
        "test_progress", {"steps": 5, "delay": 0.15}, cancel=CancelToken(),
        progress=values.append, stall_timeout=10, max_time=60,
    )
    assert result == {"steps": 5}
    assert values == sorted(values) and values[-1] == 1.0
    assert no_children()


def test_run_task_crash_is_child_failed():
    started = time.monotonic()
    with pytest.raises(_isolated.ChildFailed) as info_:
        _isolated.run_task("test_abort", {}, cancel=CancelToken(), stall_timeout=10, max_time=60)
    assert info_.value.code == "crashed"
    assert time.monotonic() - started < 15
    assert no_children()


def test_run_task_hang_is_stalled():
    started = time.monotonic()
    with pytest.raises(ConversionError, match="Обработка зависла"):
        _isolated.run_task("test_hang", {}, cancel=CancelToken(), stall_timeout=1.5, max_time=60)
    assert time.monotonic() - started < 10
    assert no_children()


def test_run_task_spin_without_advance_is_stalled():
    """Ребёнок зовёт report, но отметки не растут — это не продвижение."""
    with pytest.raises(ConversionError, match="Обработка зависла"):
        _isolated.run_task("test_spin", {}, cancel=CancelToken(), stall_timeout=1.5, max_time=60)
    assert no_children()


def test_run_task_max_time():
    with pytest.raises(ConversionError, match="превышено время"):
        _isolated.run_task(
            "test_progress", {"steps": 1000, "delay": 0.05}, cancel=CancelToken(),
            stall_timeout=30, max_time=1.5,
        )
    assert no_children()


def test_run_task_user_error():
    with pytest.raises(_isolated.TaskFailed) as info_:
        _isolated.run_task("test_error", {"key": "no_audio"}, cancel=CancelToken(),
                           stall_timeout=10, max_time=60)
    assert info_.value.key == "no_audio" and info_.value.params == (1, 2.5)


def test_run_task_cancel_fast():
    token = CancelToken()
    errors: list[BaseException] = []

    def work():
        try:
            _isolated.run_task("test_hang", {}, cancel=token, stall_timeout=60, max_time=60)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    time.sleep(1.5)
    t0 = time.monotonic()
    token.cancel()
    thread.join(10)
    assert time.monotonic() - t0 < 2.0
    assert errors and isinstance(errors[0], Cancelled)
    assert no_children()


def test_long_task_does_not_block_svg_slot(monkeypatch):
    """Долгая задача A/V занимает свой слот: SVG/RAW (run_isolated) не ждут её."""
    monkeypatch.setattr(_isolated, "MAX_CHILDREN", 1)
    token = CancelToken()
    thread = threading.Thread(
        target=lambda: pytest.raises(
            Cancelled, _isolated.run_task, "test_hang", {}, cancel=token,
            stall_timeout=60, max_time=60,
        )
    )
    thread.start()
    try:
        time.sleep(0.5)
        started = time.monotonic()
        result = _isolated.run_isolated("probe", {}, cancel=CancelToken(), timeout=30)
        assert result == {"pyside": False, "qt_modules": [], "qt_app": False}
        assert time.monotonic() - started < 10
    finally:
        token.cancel()
        thread.join(10)
    assert no_children()


def test_converter_maps_crash_and_hang(monkeypatch, audio_inputs, tmp_path):
    """Падение ребёнка → «Файл повреждён…», зависание → «Обработка зависла»."""
    monkeypatch.setattr(media, "_TASK", "test_abort")
    with pytest.raises(ConversionError, match="повреждён"):
        convert(AUDIO, audio_inputs["wav"], tmp_path / "a.mp3", "wav", "mp3")
    monkeypatch.setattr(media, "_TASK", "test_hang")
    monkeypatch.setattr(media, "STALL_TIMEOUT", 1.5)
    with pytest.raises(ConversionError, match="Обработка зависла"):
        convert(AUDIO, audio_inputs["wav"], tmp_path / "b.mp3", "wav", "mp3")
    assert no_children()


def test_user_message_formatting():
    assert media._user_message("gif_too_long", (61, 60.0)) == (
        "Слишком длинное видео для GIF: 61 с, максимум 60 с"
    )
    assert media._user_message("неизвестно", ()) == media.MSG_DAMAGED
    assert media._user_message("too_large", (1,)) == media.MSG_DAMAGED


# --- П14: звук из видео, GIF ----------------------------------------------------------


def test_extract_audio(video_inputs, tmp_path):
    for target, codec in (("audio_mp3", {"mp3", "mp3float"}), ("audio_wav", {"pcm_s16le"})):
        dst = tmp_path / f"звук.{target[-3:]}"
        convert(VIDEO, video_inputs["mov"], dst, "mov", target)
        got = info(dst)
        assert [t for t, _ in got["streams"]] == ["audio"]
        assert got["streams"][0][1] in codec
        assert abs(got["duration"] - info(video_inputs["mov"])["duration"]) <= 0.5


def test_extract_audio_without_audio(tmp_path):
    src = make_video(tmp_path / "немое.mp4", "mp4", audio=False)
    with pytest.raises(ConversionError, match="^В видео нет звуковой дорожки$"):
        convert(VIDEO, src, tmp_path / "x.mp3", "mp4", "audio_mp3")
    # В видео-варианты немое видео конвертируется (только видеодорожка).
    dst = tmp_path / "немое.mkv"
    convert(VIDEO, src, dst, "mp4", "mkv")
    assert [t for t, _ in info(dst)["streams"]] == ["video"]


def test_audio_file_without_video_in_video_target(tmp_path):
    src = make_audio(tmp_path / "звук.m4a", "m4a")  # MP4 только со звуком (detect: mp4)
    with pytest.raises(ConversionError, match="нет видеодорожки"):
        convert(VIDEO, src, tmp_path / "x.mkv", "mp4", "mkv")
    dst = tmp_path / "звук.mp3"
    convert(VIDEO, src, dst, "mp4", "audio_mp3")
    assert info(dst)["streams"][0][0] == "audio"


def test_video_to_gif(tmp_path):
    src = make_video(tmp_path / "клип.mp4", "mp4", seconds=2, size=(640, 360), fps=25)
    for preset, side, fps in ((Preset.MEDIUM, 480, 12), (Preset.SMALL, 320, 10)):
        dst = tmp_path / f"клип_{preset.value}.gif"
        convert(VIDEO, src, dst, "mp4", "gif", preset)
        with av.open(str(dst)) as c:
            frames = list(c.decode(video=0))
        assert frames[0].width == side and frames[0].height == side * 360 // 640
        assert abs(len(frames) - 2 * fps) <= 2
        assert abs(info(dst)["duration"] - 2.0) <= 0.5


def test_gif_too_long(tmp_path):
    src = make_video(tmp_path / "длинное.mp4", "mp4", seconds=65, size=(32, 32), fps=2,
                     audio=False, noise=False)
    with pytest.raises(ConversionError, match=r"Слишком длинное видео для GIF: 65 с, максимум 60 с"):
        convert(VIDEO, src, tmp_path / "x.gif", "mp4", "gif")
    assert no_children()


# --- Пресеты ---------------------------------------------------------------------


def test_presets_mp4_sizes(tmp_path):
    src = make_video(tmp_path / "src.webm", "webm", seconds=2, size=(320, 240), fps=25)
    sizes = {}
    for preset in Preset:
        dst = tmp_path / f"{preset.value}.mp4"
        convert(VIDEO, src, dst, "webm", "mp4", preset)
        sizes[preset] = dst.stat().st_size
    assert sizes[Preset.SMALL] < sizes[Preset.MEDIUM] < sizes[Preset.HIGH], sizes


def test_presets_mp3_sizes(audio_inputs, tmp_path):
    sizes = {}
    for preset in Preset:
        dst = tmp_path / f"{preset.value}.mp3"
        convert(AUDIO, audio_inputs["wav"], dst, "wav", "mp3", preset)
        sizes[preset] = dst.stat().st_size
    assert sizes[Preset.SMALL] < sizes[Preset.MEDIUM] < sizes[Preset.HIGH], sizes


def test_small_preset_720p(tmp_path):
    src = make_video(tmp_path / "big.mp4", "mp4", seconds=0.5, size=(1280, 960), fps=10, audio=False)
    for preset, height in ((Preset.SMALL, 720), (Preset.MEDIUM, 960)):
        dst = tmp_path / f"{preset.value}.mkv"
        run_inline(src, dst, "mp4", "mkv", preset)
        with av.open(str(dst)) as c:
            cc = c.streams.video[0].codec_context
            assert (cc.width, cc.height) == (height * 4 // 3, height)
    # Без увеличения: маленькое видео остаётся маленьким.
    small = make_video(tmp_path / "small.mp4", "mp4", seconds=0.5, size=(320, 240), audio=False)
    run_inline(small, tmp_path / "s.mkv", "mp4", "mkv", Preset.SMALL)
    with av.open(str(tmp_path / "s.mkv")) as c:
        assert c.streams.video[0].codec_context.height == 240


def test_output_side_cap(video_inputs, tmp_path, monkeypatch):
    """Перекодированное видео не больше MAX_OUTPUT_SIDE по длинной стороне."""
    monkeypatch.setattr(media, "MAX_OUTPUT_SIDE", 32)
    run_inline(video_inputs["mkv"], tmp_path / "cap.mp4", "mkv", "mp4", Preset.MEDIUM)
    with av.open(str(tmp_path / "cap.mp4")) as c:
        cc = c.streams.video[0].codec_context
        assert (cc.width, cc.height) == (32, 24)


# --- Remux ------------------------------------------------------------------------


def _packets(path: Path, kind: str, count: int = 5) -> list[bytes]:
    with av.open(str(path)) as c:
        stream = c.streams.video[0] if kind == "video" else c.streams.audio[0]
        result = []
        for packet in c.demux(stream):
            if packet.size:
                result.append(bytes(packet))
            if len(result) >= count:
                break
        return result


def _same_video_packets(dst: Path, src: Path) -> None:
    """Видео скопировано: пакеты те же, только из первого (ключевого) вырезан SEI x264."""
    got, expected = _packets(dst, "video"), _packets(src, "video")
    assert got[1:] == expected[1:]
    assert b"x264 - core" in expected[0] and b"x264 - core" not in got[0]
    assert len(got[0]) < len(expected[0])


def test_remux_high_copies_streams(video_inputs, tmp_path):
    src = video_inputs["mkv"]
    dst = tmp_path / "copy.mp4"
    result = run_inline(src, dst, "mkv", "mp4", Preset.HIGH)
    assert result == {"copied_video": True, "copied_audio": True}
    _same_video_packets(dst, src)
    assert _packets(dst, "audio") == _packets(src, "audio")
    _check_result(src, dst, "mp4", video=True)
    # «Среднее» перекодирует.
    result = run_inline(src, tmp_path / "enc.mp4", "mkv", "mp4", Preset.MEDIUM)
    assert result == {"copied_video": False, "copied_audio": False}


def test_remux_mpegts_to_mp4(video_inputs, tmp_path):
    """H.264 из MPEG-TS (Annex B) копируется в MP4 и декодируется."""
    dst = tmp_path / "ts.mp4"
    result = run_inline(video_inputs["mts"], dst, "mts", "mp4", Preset.HIGH)
    assert result["copied_video"]
    with av.open(str(dst)) as c:
        frames = sum(1 for _ in c.decode(video=0))
    assert frames == 15
    _check_result(video_inputs["mts"], dst, "mp4", video=True)


def test_remux_through_child(video_inputs, tmp_path):
    dst = tmp_path / "copy.mov"
    convert(VIDEO, video_inputs["mp4"], dst, "mp4", "mov", Preset.HIGH)
    _same_video_packets(dst, video_inputs["mp4"])


# --- Поворот и метаданные -------------------------------------------------------------


@pytest.mark.parametrize("preset", [Preset.MEDIUM, Preset.HIGH])
def test_rotation_applied(tmp_path, preset):
    """Матрица «повернуть на 90° по часовой»: результат 32×64, красная половина сверху."""
    src = make_video(tmp_path / "телефон.mp4", "mp4", seconds=1, size=(64, 32),
                     rotation=-90, noise=False)
    for target in ("mkv", "mov", "webm", "gif"):
        dst = tmp_path / f"r_{preset.value}.{target}"
        convert(VIDEO, src, dst, "mp4", target, preset)
        with av.open(str(dst)) as c:
            frame = next(c.decode(video=0))
            assert (frame.width, frame.height) == (32, 64)
            assert not frame.rotation
            rgb = frame.to_ndarray(format="rgb24").astype(int)
        top, bottom = rgb[4, 16], rgb[-5, 16]
        assert top[0] > 150 and top[2] < 100, top  # красный
        assert bottom[2] > 150 and bottom[0] < 100, bottom  # синий


def test_metadata_not_copied(tmp_path):
    meta = {"location": "+55.7558+037.6173/", "title": "Секретное название", "artist": "Автор"}
    # MOV, как у iPhone: координаты текстом в атоме ©xyz.
    src = make_video(tmp_path / "gps.mov", "mov", metadata=meta)
    assert b"+55.7558" in src.read_bytes()
    assert "location" in info(src)["metadata"]
    for target, preset in (("mp4", Preset.MEDIUM), ("mp4", Preset.HIGH), ("mkv", Preset.HIGH),
                           ("audio_mp3", Preset.MEDIUM), ("gif", Preset.MEDIUM)):
        dst = tmp_path / f"out_{target}_{preset.value}"
        convert(VIDEO, src, dst, "mov", target, preset)
        data = dst.read_bytes()
        assert b"55.7558" not in data
        assert "Секретное".encode() not in data and "Автор".encode() not in data
        assert not {"location", "title", "artist"} & {k.lower() for k in info(dst)["metadata"]}
    tagged = make_audio(tmp_path / "tag.flac", "flac", metadata={"title": "Секретное название"})
    dst = tmp_path / "tag.mp3"
    convert(AUDIO, tagged, dst, "flac", "mp3")
    assert "Секретное".encode() not in dst.read_bytes()


# --- «Злые» файлы ------------------------------------------------------------------------


def test_empty_and_garbage_files(tmp_path):
    empty = tmp_path / "пусто.mp4"
    empty.write_bytes(b"")
    garbage = tmp_path / "мусор.mp3"
    garbage.write_bytes(os.urandom(64 * 1024))
    with pytest.raises(ConversionError):
        convert(VIDEO, empty, tmp_path / "a.mkv", "mp4", "mkv")
    with pytest.raises(ConversionError):
        convert(AUDIO, garbage, tmp_path / "b.wav", "mp3", "wav")
    with pytest.raises(ConversionError):
        convert(VIDEO, garbage, tmp_path / "c.gif", "mkv", "gif")
    assert no_children()


def test_truncated_mp4(video_inputs, tmp_path):
    data = video_inputs["mp4"].read_bytes()
    half = tmp_path / "половина.mp4"
    half.write_bytes(data[: len(data) // 2])
    with pytest.raises(ConversionError, match="повреждён"):
        convert(VIDEO, half, tmp_path / "x.mkv", "mp4", "mkv")
    assert no_children()


def test_huge_resolution_rejected(tmp_path):
    src = make_video(tmp_path / "широкое.mp4", "mp4", seconds=0.2, size=(8200, 64), audio=False,
                     noise=False)
    with pytest.raises(ConversionError, match="Слишком большое разрешение видео: 8200×64"):
        convert(VIDEO, src, tmp_path / "x.mkv", "mp4", "mkv")
    assert no_children()


def test_limits_passed_to_child(video_inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MAX_FRAME_PIXELS", 1000)
    with pytest.raises(ConversionError, match="Слишком большое разрешение видео: 64×48"):
        convert(VIDEO, video_inputs["mkv"], tmp_path / "x.mp4", "mkv", "mp4")
    monkeypatch.setattr(media, "MAX_FRAME_PIXELS", 40_000_000)
    monkeypatch.setattr(media, "MAX_DURATION", 1.0)
    with pytest.raises(ConversionError, match="Слишком длинный файл"):
        convert(VIDEO, video_inputs["mkv"], tmp_path / "y.mp4", "mkv", "mp4")
    monkeypatch.setattr(media, "MAX_DURATION", 24 * 3600.0)
    monkeypatch.setattr(media, "MAX_STREAMS", 1)
    with pytest.raises(ConversionError):
        convert(VIDEO, video_inputs["mkv"], tmp_path / "z.mp4", "mkv", "mp4")
    assert no_children()


def test_wrong_format_code_is_error(audio_inputs, tmp_path):
    """Демультиплексор выбирается по коду из detect: FLAC под кодом mp3 — ошибка."""
    with pytest.raises(ConversionError):
        convert(AUDIO, audio_inputs["flac"], tmp_path / "x.wav", "mp3", "wav")
    with pytest.raises(ConversionError):
        AUDIO.convert([audio_inputs["flac"]], tmp_path / "y.wav", target_of(AUDIO, "flac", "wav"),
                      Preset.MEDIUM, lambda p: None, CancelToken(), formats=["mp4"])


# --- Имена и пути ------------------------------------------------------------------------


def test_unicode_and_long_path(audio_inputs, video_inputs, tmp_path):
    base = naming.os_path(tmp_path / ("д" * 120) / ("е" * 120))
    os.makedirs(base)
    src = os.path.join(base, "звук с пробелами — тест.wav")
    with open(src, "wb") as fh:
        fh.write(audio_inputs["wav"].read_bytes())
    dst = os.path.join(base, ".~результат.part.mp3")
    assert len(dst) > 260 and dst.startswith("\\\\?\\")
    convert(AUDIO, Path(src), Path(dst), "wav", "mp3")
    assert info(Path(dst))["streams"][0][1] in {"mp3", "mp3float"}
    vsrc = os.path.join(base, "видео 1.mp4")
    with open(vsrc, "wb") as fh:
        fh.write(video_inputs["mp4"].read_bytes())
    vdst = os.path.join(base, "кадры.gif")
    convert(VIDEO, Path(vsrc), Path(vdst), "mp4", "gif")
    assert os.path.getsize(vdst) > 0


# --- Сквозной тест через JobManager ---------------------------------------------------------


def test_job_manager_group_to_mp3(audio_inputs, tmp_path):
    folder = tmp_path / "музыка"
    folder.mkdir()
    first = folder / "песня 1.wav"
    second = folder / "песня 2.flac"
    first.write_bytes(audio_inputs["wav"].read_bytes())
    second.write_bytes(audio_inputs["flac"].read_bytes())
    existing = folder / "песня 1.mp3"
    existing.write_bytes(b"old")
    done = threading.Event()
    jobs = []

    def on_update(job):
        if all(j.is_finished for j in jobs) and len(jobs) == 2:
            done.set()

    manager = JobManager(max_workers=2, on_update=on_update)
    try:
        for src, fmt in ((first, "wav"), (second, "flac")):
            jobs.append(manager.submit([src], AUDIO, target_of(AUDIO, fmt, "mp3"), Preset.MEDIUM,
                                       folder, formats=[fmt]))
        deadline = time.monotonic() + 60
        while not all(j.is_finished for j in jobs) and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        manager.shutdown(timeout=10)
    assert [j.status for j in jobs] == [JobStatus.DONE, JobStatus.DONE], [j.error_message for j in jobs]
    assert jobs[0].result_path.name == "песня 1 (1).mp3"
    assert jobs[1].result_path.name == "песня 2.mp3"
    assert existing.read_bytes() == b"old"
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        ["песня 1.wav", "песня 2.flac", "песня 1.mp3", "песня 1 (1).mp3", "песня 2.mp3"]
    )
    for job in jobs:
        assert info(job.result_path)["streams"][0][1] in {"mp3", "mp3float"}
    assert no_children()


# --- Ревью этапа 2, раунд 1 ---------------------------------------------------------------


class _Conn:
    """Канал ребёнка → родителя для настоящего `_isolated._Reporter` (с временем)."""

    def __init__(self) -> None:
        self.messages: list[tuple[float, tuple]] = []
        self.t0 = time.monotonic()

    def send(self, message) -> None:
        self.messages.append((time.monotonic() - self.t0, message))


def _child_args(src: Path, dst: Path, fmt: str, target: str, preset: Preset = Preset.MEDIUM) -> dict:
    return {"src": str(src), "dst": str(dst), "fmt": fmt, "target": target, "preset": preset.value}


def test_gif_second_pass_reports_progress(tmp_path, monkeypatch):
    """П.2: во втором проходе GIF приходят сообщения, доля доходит почти до 1,
    пауз длиннее «сторожа» нет даже на медленном втором проходе."""
    src = make_video(tmp_path / "g.mp4", "mp4", seconds=3, size=(160, 120), fps=25, audio=False)
    monkeypatch.setattr(_isolated, "TASK_REPORT_INTERVAL", 0.02)
    stall = 0.6  # «короткий STALL»: второй проход идёт ≈ 2 с
    original = media._GifPipe.push

    def slow_push(self, frame):
        if self.palette is not None:
            time.sleep(0.03)
        return original(self, frame)

    monkeypatch.setattr(media._GifPipe, "push", slow_push)
    conn = _Conn()
    reporter = _isolated._Reporter(conn)
    media.child_task(_child_args(src, tmp_path / "g.gif", "mp4", "gif"), reporter)
    reporter.final()
    elapsed = time.monotonic() - conn.t0
    times = [t for t, m in conn.messages if m[0] == "progress"]
    values = [m[1] for _t, m in conn.messages if m[0] == "progress" and m[1] is not None]
    assert values == sorted(values)
    assert max(values) >= 0.95, values[-5:]
    assert sum(1 for v in values if v > 0.55) >= 5
    gaps = [b - a for a, b in zip([0.0] + times, times + [elapsed])]
    assert max(gaps) < stall, max(gaps)


def make_gap_video(path: Path, seconds: int = 10, hole=(3.0, 5.0)) -> Path:
    """MKV: видео `seconds` с, звук — тон с дырой `hole` (пакетов там нет)."""
    rate, fps = 48000, 10
    with av.open(str(path), "w", format="matroska") as out:
        v = out.add_stream("libx264", rate=fps, options={"preset": "ultrafast"})
        v.width, v.height, v.pix_fmt = 64, 48, "yuv420p"
        a = out.add_stream("aac", rate=rate)
        a.codec_context.layout = "stereo"
        a.codec_context.format = "fltp"
        for i in range(seconds * fps):
            frame = av.VideoFrame.from_ndarray(np.zeros((48, 64, 3), np.uint8), format="rgb24")
            frame.pts = i
            frame.time_base = Fraction(1, fps)
            for packet in v.encode(frame):
                out.mux(packet)
        for packet in v.encode(None):
            out.mux(packet)
        n, pos = 1024, 0
        while pos < seconds * rate:
            if not (hole[0] * rate <= pos < hole[1] * rate):
                tone = (0.5 * np.sin(2 * np.pi * 440 * np.arange(pos, pos + n) / rate)).astype(np.float32)
                frame = av.AudioFrame.from_ndarray(np.stack([tone, tone]), format="fltp", layout="stereo")
                frame.sample_rate = rate
                frame.pts = pos
                frame.time_base = Fraction(1, rate)
                for packet in a.encode(frame):
                    out.mux(packet)
            pos += n
        for packet in a.encode(None):
            out.mux(packet)
    return path


def stream_ends(path: Path) -> dict[str, float]:
    """Конец каждой дорожки (pts + duration последнего пакета), с."""
    with av.open(str(path)) as c:
        result: dict[str, float] = {}
        for packet in c.demux():
            if packet.pts is not None:
                end = float((packet.pts + (packet.duration or 0)) * packet.time_base)
                result[packet.stream.type] = max(result.get(packet.stream.type, 0.0), end)
        return result


def _loud_times(path: Path) -> list[tuple[float, bool]]:
    """(время кадра звука, громкий ли он) по декодированному звуку."""
    with av.open(str(path)) as c:
        result = []
        for frame in c.decode(audio=0):
            data = frame.to_ndarray().astype(np.float64)
            result.append((float(frame.pts * frame.time_base), float(np.sqrt((data ** 2).mean())) > 0.05))
        return result


def test_audio_gap_keeps_sync(tmp_path):
    """П.3: дыра 3–5 с в звуке заполняется тишиной — звук не уезжает от видео."""
    src = make_gap_video(tmp_path / "дыра.mkv")
    for target in ("mp4", "webm"):
        dst = tmp_path / f"дыра.{target}"
        run_inline(src, dst, "mkv", target)
        ends = stream_ends(dst)
        assert abs(ends["audio"] - 10.0) <= 0.1, ends
        frames = _loud_times(dst)
        onset = next(t for t, loud in frames if t > 4.0 and loud)
        assert abs(onset - 5.0) <= 0.1, onset
        assert not any(loud for t, loud in frames if 3.1 < t < 4.9)


def test_mts_concat_full_duration(tmp_path):
    """П.4: склейка двух MTS (метки начинаются заново) — полная длительность."""
    a = make_video(tmp_path / "a.mts", "mts", seconds=5, size=(64, 48), fps=25)
    b = make_video(tmp_path / "b.mts", "mts", seconds=5, size=(64, 48), fps=25)
    cat = tmp_path / "склейка.mts"
    cat.write_bytes(a.read_bytes() + b.read_bytes())
    for preset in (Preset.HIGH, Preset.MEDIUM):
        dst = tmp_path / f"cat_{preset.value}.mp4"
        run_inline(cat, dst, "mts", "mp4", preset)
        assert abs(info(dst)["duration"] - 10.0) <= 0.3, (preset, info(dst))
        ends = stream_ends(dst)
        assert abs(ends["video"] - 10.0) <= 0.2 and abs(ends["audio"] - 10.0) <= 0.2, ends
        with av.open(str(dst)) as c:
            assert abs(sum(1 for _ in c.decode(video=0)) - 250) <= 2


class _FakePacket:
    def __init__(self, dts: int) -> None:
        self.pts = self.dts = dts
        self.size = 10
        self.duration = 1
        self.time_base = Fraction(1, 25)
        self.stream = None


class _FakeOut:
    def __init__(self) -> None:
        self.muxed = 0

    def add_stream_from_template(self, stream):
        return object()

    def mux(self, packet) -> None:
        self.muxed += 1


class _FakeStream:
    time_base = Fraction(1, 25)

    class codec_context:  # noqa: N801 — как у av.Stream
        name = "aac"


def test_copy_backward_timestamps_are_error():
    """П.4: в формате без разрывов отброшено > 5 % пакетов → «Повреждены метки времени»."""
    for backward, fails in ((2, False), (10, True)):
        out = _FakeOut()
        copy = media._CopyStream(out, _FakeStream(), 0.0)
        for dts in list(range(100)) + list(range(50, 50 + backward)):
            copy.packet(_FakePacket(dts))
        if fails:
            with pytest.raises(media.TaskError) as exc:
                copy.flush()
            assert exc.value.key == "bad_timestamps"
            assert media._user_message("bad_timestamps", ()) == "Повреждены метки времени"
        else:
            copy.flush()
        assert out.muxed == 100


class _FakeFrame:
    def __init__(self, pts: int) -> None:
        self.pts = pts
        self.time_base = Fraction(1, 25)


class _FakeEncStream:
    def encode(self, frame):
        return [] if frame is None else [object()]


def test_frame_writer_backward_timestamps_are_error():
    for backward, fails in ((2, False), (10, True)):
        writer = media._FrameWriter(_FakeOut(), _FakeEncStream(), Fraction(1, 90000), 0.0)
        for pts in list(range(100)) + list(range(50, 50 + backward)):
            writer.write(_FakeFrame(pts))
        if fails:
            with pytest.raises(media.TaskError, match="bad_timestamps"):
                writer.flush()
        else:
            writer.flush()
        assert writer.produced == 100


# П.5: место на диске и размер результата.

_GB = 2**30


def _fake_usage(free_values: list[int]):
    """disk_usage, который отдаёт свободное место из списка (последнее — дальше всегда)."""
    calls: list[str] = []

    def usage(path):
        calls.append(str(path))
        free = free_values[min(len(calls) - 1, len(free_values) - 1)]
        return shutil._ntuple_diskusage(100 * _GB, 100 * _GB - free, free)

    usage.calls = calls
    return usage


def test_no_space_before_start(audio_inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(media.shutil, "disk_usage", _fake_usage([_GB // 2]))
    with pytest.raises(media.TaskError) as exc:
        run_inline(audio_inputs["wav"], tmp_path / "x.flac", "wav", "flac")
    assert exc.value.key == "no_space"
    need, free = exc.value.params
    assert need >= 1.0 and abs(free - 0.5) < 1e-6
    assert media._user_message("no_space", (1.5, 0.5)) == (
        "Недостаточно места на диске: нужно ~1.5 ГБ, свободно 0.5 ГБ"
    )
    assert not (tmp_path / "x.flac").exists()


def test_space_estimate_pcm(audio_inputs, tmp_path, monkeypatch):
    """Оценка PCM: 1,5 с × 44100 × 2 канала × 2 байта = 264 600 байт."""
    monkeypatch.setattr(media, "DISK_RESERVE", 0)
    monkeypatch.setattr(media.shutil, "disk_usage", _fake_usage([200_000]))
    with pytest.raises(media.TaskError, match="no_space"):
        run_inline(audio_inputs["flac"], tmp_path / "x.wav", "flac", "wav")
    monkeypatch.setattr(media.shutil, "disk_usage", _fake_usage([400_000, 10 * _GB]))
    run_inline(audio_inputs["flac"], tmp_path / "y.wav", "flac", "wav")


def test_output_size_limit(audio_inputs, video_inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MAX_OUTPUT_BYTES", 20_000)
    # Оценка PCM (264 600 байт) больше предела — ошибка до создания файла.
    with pytest.raises(media.TaskError) as exc:
        run_inline(audio_inputs["flac"], tmp_path / "x.wav", "flac", "wav")
    assert exc.value.key == "output_too_large"
    assert not (tmp_path / "x.wav").exists()
    # GIF заранее не оценить — срабатывает счётчик записанных байт.
    monkeypatch.setattr(media, "MAX_OUTPUT_BYTES", 2_000)
    counted: list[int] = []
    original = media._MediaJob._account

    def spy(self, size):
        counted.append(size)
        return original(self, size)

    monkeypatch.setattr(media._MediaJob, "_account", spy)
    with pytest.raises(media.TaskError) as exc:
        run_inline(video_inputs["mkv"], tmp_path / "x.gif", "mkv", "gif")
    assert exc.value.key == "output_too_large"
    assert sum(counted) > 2_000 and sum(counted[:-1]) <= 2_000


def test_disk_full_while_writing(audio_inputs, video_inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "DISK_CHECK_BYTES", 1)
    for src, fmt, target in ((audio_inputs["flac"], "flac", "wav"),
                             (video_inputs["mkv"], "mkv", "mp4"),
                             (video_inputs["mkv"], "mkv", "gif")):
        usage = _fake_usage([10 * _GB, _GB // 2])
        monkeypatch.setattr(media.shutil, "disk_usage", usage)
        with pytest.raises(media.TaskError) as exc:
            run_inline(src, tmp_path / f"x.{target}", fmt, target)
        assert exc.value.key == "disk_full", target
        assert len(usage.calls) >= 2
    assert media._user_message("disk_full", (1.0,)) == (
        "Недостаточно места на диске: свободно меньше 1.0 ГБ"
    )


def test_disk_errors_through_child(audio_inputs, tmp_path, monkeypatch):
    """Параметры места уходят в ребёнка через limits; текст ошибки — у родителя."""
    monkeypatch.setattr(media, "MAX_OUTPUT_BYTES", 20_000)
    with pytest.raises(ConversionError, match="^Слишком большой результат: больше 0 ГБ$"):
        convert(AUDIO, audio_inputs["flac"], tmp_path / "x.wav", "flac", "wav")
    monkeypatch.setattr(media, "MAX_OUTPUT_BYTES", 50 * _GB)
    monkeypatch.setattr(media, "DISK_RESERVE", 10**18)
    with pytest.raises(ConversionError, match="^Недостаточно места на диске: нужно ~"):
        convert(AUDIO, audio_inputs["flac"], tmp_path / "y.wav", "flac", "wav")
    assert no_children()


# П.6: приватность при копировании.


def test_remux_strips_sei_user_data(video_inputs, tmp_path):
    """x264 пишет SEI user data со строкой настроек — при копировании её нет."""
    for fmt, target in (("mkv", "mp4"), ("mts", "mp4"), ("mp4", "mkv"), ("mp4", "mov")):
        src = video_inputs[fmt]
        assert b"x264 - core" in src.read_bytes()
        dst = tmp_path / f"{fmt}_{target}.{target}"
        result = run_inline(src, dst, fmt, target, Preset.HIGH)
        assert result["copied_video"], (fmt, target)
        assert b"x264 - core" not in dst.read_bytes(), (fmt, target)
        with av.open(str(dst)) as c:
            assert sum(1 for _ in c.decode(video=0)) == 15
            extradata = bytes(c.streams.video[0].codec_context.extradata)
        assert media._strip_sei_extradata("h264", extradata) == extradata, (fmt, target)
        assert _frame_hashes(dst) == _frame_hashes(src), (fmt, target)
        _check_result(src, dst, target, video=True)


def _frame_hashes(path: Path) -> list[str]:
    """MD5 декодированных кадров первой видеодорожки."""
    with av.open(str(path)) as c:
        return [hashlib.md5(frame.to_ndarray().tobytes()).hexdigest() for frame in c.decode(video=0)]


def _make_hevc(path: Path, container: str, frames: int = 15) -> Path:
    """HEVC (x265) без звука: x265 кладёт SEI со строкой настроек в extradata."""
    with av.open(str(path), "w", format=container) as out:
        stream = out.add_stream("libx265", rate=10,
                                options={"preset": "ultrafast", "x265-params": "log-level=error"})
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        rng = np.random.default_rng(0)
        for index in range(frames):
            img = rng.integers(0, 255, (48, 64, 3)).astype(np.uint8)
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = index
            frame.time_base = Fraction(1, 10)
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return path


def test_remux_hevc_strips_sei_from_extradata(tmp_path):
    """SEI x265 лежат в extradata (hvcC): при копировании их нет ни там, ни в файле;
    кадры декодируются без изменений."""
    for container, fmt in (("matroska", "mkv"), ("mp4", "mp4")):
        src = _make_hevc(tmp_path / f"hevc.{fmt}", container)
        with av.open(str(src)) as c:
            assert b"x265 (build" in bytes(c.streams.video[0].codec_context.extradata)
        dst = tmp_path / f"hevc_{fmt}.mkv"
        result = run_inline(src, dst, fmt, "mkv", Preset.HIGH)
        assert result["copied_video"], fmt
        assert b"x265 (build" not in dst.read_bytes(), fmt
        with av.open(str(dst)) as c:
            extradata = bytes(c.streams.video[0].codec_context.extradata)
        assert extradata and b"x265 (build" not in extradata, fmt
        assert media._strip_sei_extradata("hevc", extradata) == extradata, fmt
        hashes = _frame_hashes(dst)
        assert len(hashes) == 15 and hashes == _frame_hashes(src), fmt


def test_strip_sei_extradata_formats():
    """Разбор extradata: hvcC, avcC и Annex B; неразбираемая → None."""
    vps, sps, pps = b"\x40\x01\x0c", b"\x42\x01\x01", b"\x44\x01\xc1"
    sei, suffix = b"\x4e\x01\x05secret", b"\x50\x01\x05secret"

    def hvcc(nals: list[bytes]) -> bytes:
        data = bytearray(b"\x01" + b"\x00" * 21 + bytes([len(nals)]))
        for nal in nals:
            data += bytes([0x80 | ((nal[0] >> 1) & 0x3F)]) + struct.pack(">HH", 1, len(nal)) + nal
        return bytes(data)

    got = media._strip_sei_extradata("hevc", hvcc([vps, sps, pps, sei, suffix]))
    assert got == hvcc([vps, sps, pps])
    annexb = b"\x00\x00\x00\x01" + vps + b"\x00\x00\x01" + sei + b"\x00\x00\x00\x01" + sps
    assert media._strip_sei_extradata("hevc", annexb) == b"\x00\x00\x00\x01" + vps + b"\x00\x00\x00\x01" + sps
    h264_annexb = b"\x00\x00\x00\x01\x67\x42" + b"\x00\x00\x01\x06\x05secret" + b"\x00\x00\x01\x68\xce"
    assert media._strip_sei_extradata("h264", h264_annexb) == b"\x00\x00\x00\x01\x67\x42\x00\x00\x00\x01\x68\xce"
    avcc = b"\x01\x42\x00\x1e\xff\xe1\x00\x02\x67\x42\x01\x00\x02\x68\xce"
    assert media._strip_sei_extradata("h264", avcc) == avcc
    assert media._strip_sei_extradata("hevc", hvcc([vps])[:-2]) is None  # обрезана
    assert media._strip_sei_extradata("hevc", b"\x02garbage") is None
    assert media._strip_sei_extradata("h264", avcc[:-1]) is None


def test_remux_unparsable_extradata_reencodes(tmp_path, monkeypatch):
    """extradata не разобрать → видео не копируется, а перекодируется."""
    src = _make_hevc(tmp_path / "hevc.mkv", "matroska")
    monkeypatch.setattr(media, "_strip_sei_extradata", lambda name, data: None)
    dst = tmp_path / "out.mkv"
    result = run_inline(src, dst, "mkv", "mkv", Preset.HIGH)
    assert not result["copied_video"]
    assert b"x265 (build" not in dst.read_bytes()


def _with_garbage_packets(src: Path, dst: Path, every: set[int]) -> Path:
    """Копия MKV, где пакеты видео с номерами из `every` заменены мусором."""
    garbage = b"\x7f\xff\xff\xff" + bytes(range(40))  # длина NAL больше пакета
    with av.open(str(src)) as inp, av.open(str(dst), "w", format="matroska") as out:
        istream = inp.streams.video[0]
        ostream = out.add_stream_from_template(istream)
        for index, packet in enumerate(inp.demux(istream)):
            if packet.size == 0:
                continue
            if index in every:
                bad = av.Packet(garbage)
                bad.pts, bad.dts, bad.time_base = packet.pts, packet.dts, packet.time_base
                packet = bad
            packet.stream = ostream
            out.mux(packet)
    return dst


def test_remux_garbage_packets(tmp_path):
    """Неразбираемый пакет отбрасывается (не копируется как есть);
    много таких → «Повреждены данные видео»."""
    base = make_video(tmp_path / "base.mkv", "mkv", seconds=10, audio=False)
    one = _with_garbage_packets(base, tmp_path / "one.mkv", {50})
    dst = tmp_path / "one_out.mkv"
    result = run_inline(one, dst, "mkv", "mkv", Preset.HIGH)
    assert result["copied_video"]
    assert bytes(range(40)) not in dst.read_bytes()
    many = _with_garbage_packets(base, tmp_path / "many.mkv", set(range(1, 100, 3)))
    with pytest.raises(media.TaskError) as exc:
        run_inline(many, tmp_path / "many_out.mkv", "mkv", "mkv", Preset.HIGH)
    assert exc.value.key == "bad_video"
    assert media._user_message("bad_video", ()) == "Повреждены данные видео"


def _xiph_split(data: bytes) -> list[bytes]:
    """extradata Xiph (как у FFmpeg/Matroska): число пакетов − 1, размеры, пакеты."""
    count, pos, sizes = data[0] + 1, 1, []
    for _ in range(count - 1):
        size = 0
        while True:
            byte = data[pos]
            pos += 1
            size += byte
            if byte < 255:
                break
        sizes.append(size)
    packets = []
    for size in sizes:
        packets.append(data[pos:pos + size])
        pos += size
    packets.append(data[pos:])
    return packets


def _xiph_join(packets: list[bytes]) -> bytes:
    head = bytearray([len(packets) - 1])
    for packet in packets[:-1]:
        head += b"\xff" * (len(packet) // 255) + bytes([len(packet) % 255])
    return bytes(head) + b"".join(packets)


def _vorbis_comment(tag: bytes) -> bytes:
    vendor = b"test vendor"
    return (b"\x03vorbis" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 1)
            + struct.pack("<I", len(tag)) + tag + b"\x01")


def test_vorbis_not_copied(tmp_path):
    """Теги Vorbis лежат в extradata (заголовок комментариев; так пишет, например,
    mkvmerge) — Vorbis перекодируется, исходных тегов в результате нет."""
    ogg = make_audio(tmp_path / "tag.ogg", "ogg")
    secret = "Секретное".encode()
    src = tmp_path / "vorbis.mkv"
    with av.open(str(ogg)) as inp, av.open(str(src), "w", format="matroska") as out:
        astream = out.add_stream_from_template(inp.streams.audio[0])
        headers = _xiph_split(bytes(inp.streams.audio[0].codec_context.extradata))
        headers[1] = _vorbis_comment("TITLE=Секретное название".encode())
        astream.codec_context.extradata = _xiph_join(headers)
        vstream = out.add_stream("libx264", rate=10, options={"preset": "ultrafast"})
        vstream.width, vstream.height, vstream.pix_fmt = 64, 48, "yuv420p"
        for index in range(15):
            frame = av.VideoFrame.from_ndarray(np.zeros((48, 64, 3), np.uint8), format="rgb24")
            frame.pts = index
            frame.time_base = Fraction(1, 10)
            for packet in vstream.encode(frame):
                out.mux(packet)
        for packet in vstream.encode(None):
            out.mux(packet)
        for packet in inp.demux(inp.streams.audio[0]):
            if packet.size:
                packet.stream = astream
                out.mux(packet)
    assert secret in src.read_bytes()  # заголовок комментариев — в CodecPrivate
    dst = tmp_path / "out.webm"
    result = run_inline(src, dst, "mkv", "webm", Preset.HIGH)
    assert not result["copied_audio"]
    assert secret not in dst.read_bytes()
    _check_result(src, dst, "webm", video=True)


# П.7: предел числа кадров.


def test_too_many_frames(video_inputs, tmp_path, monkeypatch):
    """15 кадров за 1,5 с при пределе 1,5 × 1 + 5 — «Слишком сложный файл»."""
    monkeypatch.setattr(media, "MAX_FPS_FACTOR", 1)
    monkeypatch.setattr(media, "MAX_FRAMES_BASE", 5)
    for target in ("mp4", "gif"):
        with pytest.raises(media.TaskError) as exc:
            run_inline(video_inputs["mkv"], tmp_path / f"x.{target}", "mkv", target)
        assert exc.value.key == "too_complex"
    monkeypatch.setattr(media, "MAX_FPS_FACTOR", 240)
    monkeypatch.setattr(media, "MAX_FRAMES_BASE", 1000)
    monkeypatch.setattr(media, "MAX_PIXEL_FRAMES", 64 * 48 * 5)
    with pytest.raises(media.TaskError, match="too_complex"):
        run_inline(video_inputs["mkv"], tmp_path / "y.mp4", "mkv", "mp4")
    assert media._user_message("too_complex", ()) == "Слишком сложный файл: слишком много кадров"
