"""Ревью 3: память параллельных детей, журнал временных файлов, data:-картинки SVG."""

from __future__ import annotations

import base64
import io
import logging
import multiprocessing
import struct
import threading
import time
import uuid
import zlib
from pathlib import Path

import pytest
from PIL import Image

from konverter.converters import _isolated, images
from konverter.converters.base import CancelToken, Cancelled, Preset
from konverter.converters.images import ImageConverter
from konverter.core import budget, naming, temp_journal
from konverter.core.budget import PixelBudget
from konverter.core.temp_journal import TempJournal

CONV = ImageConverter()


def _no_children() -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not multiprocessing.active_children():
            return True
        time.sleep(0.05)
    return False


# --- П.1(а): не больше MAX_CHILDREN дочерних процессов одновременно -------------------


def test_children_do_not_run_simultaneously(monkeypatch):
    """Две параллельные задачи: «работа» ребёнка (от заголовка до конца) не пересекается."""
    monkeypatch.setattr(_isolated, "MAX_CHILDREN", 1)
    intervals: list[tuple[float, float]] = []
    lock = threading.Lock()

    def work() -> None:
        started: list[float] = []
        try:
            _isolated.run_isolated(
                "sleep", {"seconds": 1.5}, cancel=CancelToken(), timeout=60,
                on_header=lambda *_: started.append(time.monotonic()),
            )
        except _isolated.ChildFailed:
            pass  # «sleep» выходит без пикселей — для теста это нормально
        ended = time.monotonic()
        with lock:
            intervals.append((started[0], ended))

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(intervals) == 2
    (a0, a1), (b0, b1) = sorted(intervals)
    assert b0 >= a1, f"процессы работали одновременно: {intervals}"
    assert _no_children()


def _hold_slot(token: CancelToken) -> tuple[threading.Thread, threading.Event, list]:
    """Запускает «зависшего» ребёнка, занимающего слот; ждёт, пока он стартует."""
    started = threading.Event()
    result: list = []

    def work() -> None:
        try:
            _isolated.run_isolated(
                "sleep", {"seconds": 600}, cancel=token, timeout=600,
                on_header=lambda *_: started.set(),
            )
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    assert started.wait(30)
    return thread, started, result


def test_waiting_for_slot_is_cancellable(monkeypatch):
    monkeypatch.setattr(_isolated, "MAX_CHILDREN", 1)
    holder = CancelToken()
    thread, _, held = _hold_slot(holder)
    try:
        waiter = CancelToken()
        threading.Timer(0.5, waiter.cancel).start()
        started = time.monotonic()
        with pytest.raises(Cancelled):
            _isolated.run_isolated("probe", {}, cancel=waiter, timeout=60)
        assert time.monotonic() - started < 2
    finally:
        holder.cancel()
        thread.join(15)
    assert held and isinstance(held[0], Cancelled)
    # Слот освобождён и после kill: следующая задача идёт сразу.
    assert _isolated.run_isolated("probe", {}, cancel=CancelToken(), timeout=60) == {"pyside": False, "qt_modules": [], "qt_app": False}
    assert _no_children()


def test_slot_wait_not_counted_in_timeout(monkeypatch):
    """Ожидание слота (≈4 с) не съедает таймаут обработки (3 с)."""
    monkeypatch.setattr(_isolated, "MAX_CHILDREN", 1)
    holder_started = threading.Event()

    def holder() -> None:
        try:
            _isolated.run_isolated(
                "sleep", {"seconds": 4}, cancel=CancelToken(), timeout=60,
                on_header=lambda *_: holder_started.set(),
            )
        except _isolated.ChildFailed:
            pass

    thread = threading.Thread(target=holder)
    thread.start()
    assert holder_started.wait(30)
    started = time.monotonic()
    info = _isolated.run_isolated("probe", {}, cancel=CancelToken(), timeout=3)
    assert info == {"pyside": False, "qt_modules": [], "qt_app": False}
    assert time.monotonic() - started > 3  # действительно ждали дольше таймаута
    thread.join(30)
    assert _no_children()


def test_max_children_auto_is_sane():
    assert _isolated._max_children() in (1, 2)


# --- П.1(б): резерв бюджета SVG учитывает встроенные картинки ------------------------


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png_header_only(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00" * 100)) + _png_chunk(b"IEND", b"")
    )


def _svg_embedding(raw: bytes) -> bytes:
    b64 = base64.b64encode(raw).decode()
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        f'<image href="data:image/png;base64,{b64}" width="100" height="100"/></svg>'
    ).encode()


def _convert_svg_with_fake_child(monkeypatch, tmp_path: Path, svg: bytes) -> list[int]:
    seen: list[int] = []

    def fake_run(kind, payload, *, cancel, timeout, on_header=None, check_pixels=None):
        on_header(payload["width"], payload["height"])
        seen.append(budget.IMAGE_BUDGET.used)
        return Image.new("RGBA", (payload["width"], payload["height"]))

    monkeypatch.setattr(_isolated, "run_isolated", fake_run)
    src = tmp_path / "a.svg"
    src.write_bytes(svg)
    target = next(t for t in CONV.targets("svg") if t.id == "png")
    CONV.convert([src], tmp_path / "a.png", target, Preset.MEDIUM, lambda p: None,
                 CancelToken(), formats=["svg"])
    return seen


def test_svg_budget_includes_embedded_pixels(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(budget, "IMAGE_BUDGET", PixelBudget(budget.DEFAULT_LIMIT))
    seen = _convert_svg_with_fake_child(monkeypatch, tmp_path, _svg_embedding(_png_header_only(7000, 7000)))
    assert seen and seen[0] >= 7000 * 7000 + 256 * 256
    assert budget.IMAGE_BUDGET.used == 0


def test_svg_budget_reserve_truncated_to_limit(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(budget, "IMAGE_BUDGET", PixelBudget(1_000_000))
    seen = _convert_svg_with_fake_child(monkeypatch, tmp_path, _svg_embedding(_png_header_only(7000, 7000)))
    assert seen == [1_000_000]
    assert budget.IMAGE_BUDGET.used == 0


# --- П.2: журнал с неудалёнными записями сохраняется -----------------------------------


def _our(ext: str = "") -> str:
    name = f"{naming.TEMP_PREFIX}{uuid.uuid4().hex}{naming.TEMP_MARK}"
    return f"{name}.{ext}" if ext else name


def _cleanup(journals: Path) -> int:
    journal = TempJournal(lambda: journals)
    try:
        return journal.cleanup_orphans()
    finally:
        journal.close()


def test_journal_kept_for_entries_not_removed(tmp_path, monkeypatch):
    journals = tmp_path / "journals"
    journals.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    stuck = work / _our("png")  # «занят антивирусом» — удалить не выходит
    stuck.write_bytes(b"x")
    gone = work / _our("png")
    gone.write_bytes(b"y")
    dead = journals / "active-999999-deadbeef.txt"
    dead.write_text(f"+{stuck}\n+{gone}\n", encoding="utf-8")

    real = naming.remove_orphan
    monkeypatch.setattr(naming, "remove_orphan", lambda p: False if Path(p) == stuck else real(p))
    assert _cleanup(journals) == 1
    assert not gone.exists() and stuck.exists()
    # Журнал остался, и в нём только неудалённая запись.
    left = list(journals.glob("active-*.txt"))
    assert left, "журнал с неудалённой записью удалён"
    text = "".join(p.read_text(encoding="utf-8") for p in left)
    assert str(stuck) in text and str(gone) not in text

    # Следующий запуск: удалить удалось — журнала больше нет.
    monkeypatch.setattr(naming, "remove_orphan", real)
    assert _cleanup(journals) == 1
    assert not stuck.exists()
    assert not list(journals.glob("active-*.txt"))


def test_journal_entry_dropped_after_retry_period(tmp_path, monkeypatch, caplog):
    journals = tmp_path / "journals"
    journals.mkdir()
    stuck = tmp_path / _our("png")
    stuck.write_bytes(b"x")
    (journals / "active-999999-deadbeef.txt").write_text(f"+{stuck}\n", encoding="utf-8")
    monkeypatch.setattr(naming, "remove_orphan", lambda p: False)

    assert _cleanup(journals) == 0
    assert list(journals.glob("active-*.txt"))  # первая неудача — запись хранится

    later = time.time() + (temp_journal.ORPHAN_RETRY_DAYS + 1) * 86400
    monkeypatch.setattr(temp_journal, "_now", lambda: later)
    with caplog.at_level(logging.WARNING, logger="konverter.core.temp_journal"):
        assert _cleanup(journals) == 0
    assert not list(journals.glob("active-*.txt"))  # старая запись отброшена
    assert any("отброшена" in r.getMessage() for r in caplog.records)
    assert stuck.exists()  # сам файл не трогали


def test_journal_entry_kept_within_retry_period(tmp_path, monkeypatch):
    journals = tmp_path / "journals"
    journals.mkdir()
    stuck = tmp_path / _our("png")
    (journals / "active-999999-deadbeef.txt").write_text(f"+{stuck}\n", encoding="utf-8")
    monkeypatch.setattr(naming, "remove_orphan", lambda p: False)
    assert _cleanup(journals) == 0
    soon = time.time() + (temp_journal.ORPHAN_RETRY_DAYS - 1) * 86400
    monkeypatch.setattr(temp_journal, "_now", lambda: soon)
    assert _cleanup(journals) == 0
    left = list(journals.glob("active-*.txt"))
    assert left and str(stuck) in left[0].read_text(encoding="utf-8")


def test_invalid_journal_entry_not_kept(tmp_path):
    """Запись не нашего формата (подделка) не хранится для повторов."""
    journals = tmp_path / "journals"
    journals.mkdir()
    (journals / "active-999999-deadbeef.txt").write_text(
        f"+{tmp_path / 'чужой.txt'}\n+relative{naming.TEMP_MARK}\n", encoding="utf-8"
    )
    assert _cleanup(journals) == 0
    assert not list(journals.glob("active-*.txt"))


# --- П.3: data:-картинки без паддинга и с параметрами MIME ------------------------------


def _small_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (5, 3), (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


def _svg_href(href: str) -> bytes:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        f'<image href="{href}" width="10" height="10"/></svg>'
    ).encode()


def _unpadded(raw: bytes) -> str:
    b64 = base64.b64encode(raw).decode().rstrip("=")
    assert len(b64) % 4  # действительно без паддинга
    return b64


@pytest.mark.parametrize(
    "prefix",
    [
        "data:image/png;base64,",
        "data:image/png;charset=utf-8;base64,",
        "data:image/png; charset=utf-8 ;name=x.png;base64,",
        "DATA:IMAGE/PNG;CHARSET=UTF-8;BASE64,",
    ],
)
def test_data_image_unpadded_and_params_kept(prefix):
    raw = _small_png()
    # Подбираем картинку, у которой base64 без паддинга действительно короче.
    while len(base64.b64encode(raw)) == len(base64.b64encode(raw).rstrip(b"=")):
        raw += b"\x00"
    text, _ = images._sanitize_svg(_svg_href(prefix + _unpadded(raw)))
    canonical = "data:image/png;base64," + base64.b64encode(raw).decode()
    assert canonical in text  # канонический href: ровно проверенные байты


def test_data_image_params_with_padding():
    raw = _small_png()
    b64 = base64.b64encode(raw).decode()
    text, _ = images._sanitize_svg(_svg_href("data:image/jpeg;charset=x;base64," + b64))
    # MIME — по сигнатуре (PNG), параметр отброшен.
    assert "data:image/png;base64," + b64 in text
    assert "charset" not in text


@pytest.mark.parametrize(
    "href",
    [
        "data:image/png;base64,QUJDRA=====",  # лишний паддинг
        "data:image/png;base64,QUJD$RA",  # чужой символ
        "data:image/png;base64,QUJDR",  # длина 5: паддингом не исправить
        "data:image/png;base64;charset=x,QUJD",  # base64 не последним параметром
        "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4",  # не растр
    ],
)
def test_bad_data_image_still_dropped_and_logged(href, caplog):
    with caplog.at_level(logging.WARNING, logger="konverter.converters.images"):
        text, _ = images._sanitize_svg(_svg_href(href))
    assert "data:" not in text
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "встроенн" in messages.lower()
    assert "QUJD" not in messages and "PHN2" not in messages  # без содержимого


def test_unpadded_non_image_bytes_dropped():
    """После дописывания паддинга байты всё равно проверяются по сигнатуре."""
    b64 = base64.b64encode(b"<svg>evil</svg>!").decode().rstrip("=")
    text, _ = images._sanitize_svg(_svg_href("data:image/png;base64," + b64))
    assert "data:" not in text
