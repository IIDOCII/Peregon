"""Изоляция SVG/RAW в дочернем процессе, бюджет памяти, суммарные лимиты (ревью, п.2, 3, 6)."""

from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from konverter.converters import _isolated, images
from konverter.converters.base import CancelToken, Cancelled, ConversionError, Preset
from konverter.converters.images import ImageConverter
from konverter.core import budget
from konverter.core.budget import PixelBudget

CONV = ImageConverter()


def _target(fmt: str, target_id: str):
    return next(t for t in CONV.targets(fmt) if t.id == target_id)


def _convert(src: Path, dst: Path, fmt: str, target_id: str, cancel: CancelToken | None = None):
    CONV.convert(
        [src], dst, _target(fmt, target_id), Preset.MEDIUM, lambda p: None,
        cancel or CancelToken(), formats=[fmt],
    )


def _bomb_svg(path: Path) -> Path:
    """~700 байт: feTurbulence numOctaves=8 + несколько <use> на холсте 8192×8192."""
    uses = "".join(f'<use href="#r" x="{i}"/>' for i in range(8))
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="8192" height="8192">'
        '<defs><filter id="f" x="0" y="0" width="1" height="1">'
        '<feTurbulence baseFrequency="0.01" numOctaves="8"/></filter>'
        '<rect id="r" width="8192" height="8192" filter="url(#f)"/></defs>'
        + uses + "</svg>",
        encoding="utf-8",
    )
    return path


def _no_children() -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not multiprocessing.active_children():
            return True
        time.sleep(0.05)
    return False


# --- П.2: дочерний процесс, таймаут, отмена ---------------------------------------


def test_svg_bomb_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(_isolated, "SVG_TIMEOUT", 3.0)
    src = _bomb_svg(tmp_path / "bomb.svg")
    dst = tmp_path / "bomb.png"
    started = time.monotonic()
    with pytest.raises(ConversionError, match="превышено время обработки"):
        _convert(src, dst, "svg", "png")
    assert time.monotonic() - started < 15
    assert _no_children()
    assert budget.IMAGE_BUDGET.used == 0


def test_svg_bomb_cancel(tmp_path):
    src = _bomb_svg(tmp_path / "bomb.svg")
    token = CancelToken()
    result: list[BaseException] = []

    def work():
        try:
            _convert(src, tmp_path / "bomb.png", "svg", "png", token)
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    time.sleep(2.0)  # процесс запущен и рендерит
    cancelled_at = time.monotonic()
    token.cancel()
    thread.join(10)
    assert not thread.is_alive()
    assert time.monotonic() - cancelled_at < 2
    assert len(result) == 1 and isinstance(result[0], Cancelled)
    assert _no_children()
    assert budget.IMAGE_BUDGET.used == 0


def test_hung_child_killed_on_cancel():
    token = CancelToken()
    threading.Timer(1.5, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        _isolated.run_isolated("sleep", {"seconds": 600}, cancel=token, timeout=60)
    assert time.monotonic() - started < 5
    assert _no_children()


def test_child_does_not_import_pyside():
    import PySide6.QtCore  # noqa: F401 — в родителе GUI загружен

    info = _isolated.run_isolated("probe", {}, cancel=CancelToken(), timeout=60)
    assert info == {"pyside": False, "qt_modules": [], "qt_app": False}
    assert _no_children()


# --- П.3: бюджет мегапикселей -------------------------------------------------------


def test_budget_two_150mp_not_simultaneous():
    pool = PixelBudget(200_000_000)
    active = 0
    peak = 0
    lock = threading.Lock()

    def task():
        nonlocal active, peak
        with pool.reserve(150_000_000):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.3)
            with lock:
                active -= 1

    threads = [threading.Thread(target=task) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak == 1
    assert pool.used == 0


def test_budget_small_ones_share_and_huge_capped():
    pool = PixelBudget(100)
    with pool.reserve(40), pool.reserve(40):
        assert pool.used == 80
    assert pool.acquire(10_000) == 100  # больше бюджета — урезается, работает одна
    pool.release(100)
    assert pool.used == 0


def test_budget_wait_is_cancellable():
    pool = PixelBudget(100)
    held = pool.acquire(100)
    token = CancelToken()
    threading.Timer(0.3, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        pool.acquire(50, token)
    assert time.monotonic() - started < 2
    pool.release(held)
    assert pool.used == 0
    assert pool.acquire(100) == 100  # очередь не «застряла» после отмены


def test_budget_fifo_big_request_not_starved():
    pool = PixelBudget(100)
    first = pool.acquire(60)
    order: list[str] = []
    big = threading.Thread(target=lambda: (pool.acquire(100), order.append("big")))
    big.start()
    time.sleep(0.2)
    # Мелкий запрос поместился бы (60 + 30 ≤ 100), но стоит после большого.
    small = threading.Thread(target=lambda: (pool.acquire(30), order.append("small")))
    small.start()
    time.sleep(0.2)
    assert order == []
    pool.release(first)
    big.join(5)
    assert order == ["big"]
    pool.release(100)
    small.join(5)
    assert order == ["big", "small"]
    pool.release(30)


def test_image_conversions_respect_budget(tmp_path, monkeypatch):
    """Две картинки по 10 000 px при бюджете 15 000 px не декодируются одновременно."""
    sources = []
    for i in range(2):
        path = tmp_path / f"{i}.png"
        Image.new("RGB", (100, 100), (i * 100, 0, 0)).save(path)
        sources.append(path)

    active = 0
    peak = 0
    lock = threading.Lock()
    original = images._save_raster

    def slow_save(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.3)
        try:
            return original(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(images, "_save_raster", slow_save)

    def run_pair(limit: int) -> int:
        nonlocal peak
        peak = 0
        monkeypatch.setattr(budget, "IMAGE_BUDGET", PixelBudget(limit))
        errors: list[BaseException] = []

        def work(src: Path):
            try:
                _convert(src, tmp_path / f"{src.stem}-{limit}.jpg", "png", "jpg")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(s,)) for s in sources]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        assert errors == []
        assert budget.IMAGE_BUDGET.used == 0
        return peak

    assert run_pair(15_000) == 1
    assert run_pair(30_000) == 2  # контроль: при большом бюджете идут параллельно


# --- П.6: суммарные лимиты страниц и кадров ----------------------------------------


def _multipage_tiff(path: Path, pages: int) -> Path:
    frames = [Image.new("RGB", (100, 50), (40 * i, 0, 0)) for i in range(pages)]
    frames[0].save(path, save_all=True, append_images=frames[1:])
    return path


def test_tiff_total_pixels_limit(tmp_path, monkeypatch):
    src = _multipage_tiff(tmp_path / "pages.tiff", 4)
    monkeypatch.setattr(images, "MAX_TOTAL_PIXELS", 12_000)  # 2 страницы по 5000 px
    with pytest.raises(ConversionError, match="Слишком много данных"):
        _convert(src, tmp_path / "pages.pdf", "tiff", "pdf_each")
    assert budget.IMAGE_BUDGET.used == 0


def test_tiff_max_pages(tmp_path, monkeypatch):
    src = _multipage_tiff(tmp_path / "pages.tiff", 4)
    monkeypatch.setattr(images, "MAX_PAGES", 3)
    with pytest.raises(ConversionError, match="Слишком много страниц: 4, максимум 3"):
        _convert(src, tmp_path / "pages.pdf", "tiff", "pdf_each")


def test_tiff_pages_reserve_budget_per_page(tmp_path, monkeypatch):
    """Бюджет меньше суммы страниц, но больше одной страницы — PDF собирается."""
    src = _multipage_tiff(tmp_path / "pages.tiff", 4)
    monkeypatch.setattr(budget, "IMAGE_BUDGET", PixelBudget(6_000))
    _convert(src, tmp_path / "pages.pdf", "tiff", "pdf_each")
    assert (tmp_path / "pages.pdf").stat().st_size > 0
    assert budget.IMAGE_BUDGET.used == 0


def test_animation_total_pixels_limit(tmp_path, monkeypatch):
    src = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (20, 20), (i * 20, 0, 0)) for i in range(5)]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=50, loop=0)
    monkeypatch.setattr(images, "MAX_ANIMATION_PIXELS", 1_000)
    with pytest.raises(ConversionError, match="Анимация слишком большая"):
        _convert(src, tmp_path / "anim.webp", "gif", "webp")
