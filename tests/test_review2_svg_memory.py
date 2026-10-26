"""Ревью 2, п.1: растровая «бомба», встроенная в SVG, и лимит памяти дочернего процесса."""

from __future__ import annotations

import base64
import io
import multiprocessing
import struct
import time
import zlib
from pathlib import Path

import pytest
from PIL import Image

from konverter.converters import _isolated, images
from konverter.converters.base import CancelToken, ConversionError, Preset
from konverter.converters.images import ImageConverter

CONV = ImageConverter()


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png_header_only(width: int, height: int) -> bytes:
    """PNG с заголовком w×h и крошечным IDAT (для разбора заголовка этого хватает)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00" * 100))
        + _png_chunk(b"IEND", b"")
    )


def _real(fmt: str, size: tuple[int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (0, 128, 0)).save(buf, fmt)
    return buf.getvalue()


def _jpeg_with_big_app(width: int, height: int) -> bytes:
    """JPEG, у которого SOF стоит после нескольких больших APP-сегментов."""
    data = _real("JPEG", (8, 8))
    app = b"\xff\xe1" + struct.pack(">H", 60000) + b"\x00" * 59998
    data = data[:2] + app * 3 + data[2:]
    # Подменяем размеры в первом SOF.
    pos = 2
    while True:
        assert data[pos] == 0xFF
        marker = data[pos + 1]
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            return data[:pos + 5] + struct.pack(">HH", height, width) + data[pos + 9:]
        pos += 2 + length


def _gif(screen: tuple[int, int], frame: tuple[int, int]) -> bytes:
    sw, sh = screen
    fw, fh = frame
    return (
        b"GIF89a" + struct.pack("<HH", sw, sh) + b"\x00\x00\x00"
        + b"\x2c" + struct.pack("<HHHH", 0, 0, fw, fh) + b"\x00"
        + b"\x02\x02\x44\x01\x00" + b"\x3b"
    )


def _webp_vp8x(width: int, height: int) -> bytes:
    payload = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunk = b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def _svg_with(*blobs: tuple[str, bytes]) -> bytes:
    imgs = "".join(
        f'<image href="data:image/{mime};base64,{base64.b64encode(raw).decode()}" width="10" height="10"/>'
        for mime, raw in blobs
    )
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256">{imgs}</svg>'.encode()


# --- (а) проверка заголовков встроенных растров ---------------------------------


def test_embedded_png_bomb_rejected():
    """Сценарий ревьюера: PNG 12000×12000 внутри SVG должен отклоняться до рендера."""
    with pytest.raises(ConversionError, match="Встроенное в SVG изображение слишком большое"):
        images._sanitize_svg(_svg_with(("png", _png_header_only(12000, 12000))))


@pytest.mark.parametrize(
    "blob",
    [
        ("jpeg", "jpg_big"),
        ("gif", "gif_screen"),
        ("gif", "gif_frame"),
        ("webp", "webp_vp8x"),
    ],
)
def test_embedded_other_formats_bomb_rejected(blob):
    mime, kind = blob
    raw = {
        "jpg_big": lambda: _jpeg_with_big_app(12000, 12000),
        "gif_screen": lambda: _gif((12000, 12000), (1, 1)),
        "gif_frame": lambda: _gif((1, 1), (12000, 12000)),
        "webp_vp8x": lambda: _webp_vp8x(12000, 12000),
    }[kind]()
    with pytest.raises(ConversionError, match="Встроенное в SVG"):
        images._sanitize_svg(_svg_with((mime, raw)))


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP"])
def test_embedded_real_small_images_parsed(fmt):
    """Настоящие маленькие картинки всех четырёх форматов проходят и остаются в SVG."""
    raw = _real(fmt, (37, 21))
    assert images._embedded_image_size(raw) == (37, 21)
    text, _ = images._sanitize_svg(_svg_with((fmt.lower(), raw)))
    assert "data:image/" in text


def test_webp_lossless_and_lossy_parsed():
    buf = io.BytesIO()
    Image.new("RGBA", (33, 17), (1, 2, 3, 4)).save(buf, "WEBP", lossless=True)
    assert images._embedded_image_size(buf.getvalue()) == (33, 17)
    buf = io.BytesIO()
    Image.new("RGB", (33, 17)).save(buf, "WEBP", quality=50)
    assert images._embedded_image_size(buf.getvalue()) == (33, 17)


def test_embedded_total_limit(monkeypatch):
    monkeypatch.setattr(images, "SVG_EMBED_MAX_TOTAL_PIXELS", 1000)
    one = _real("PNG", (20, 20))  # 400 пикселей
    images._sanitize_svg(_svg_with(("png", one), ("png", one)))  # 800 — можно
    with pytest.raises(ConversionError, match="суммарно"):
        images._sanitize_svg(_svg_with(("png", one), ("png", one), ("png", one)))


def test_embedded_count_limit(monkeypatch):
    monkeypatch.setattr(images, "SVG_MAX_EMBEDDED", 2)
    one = _real("PNG", (2, 2))
    images._sanitize_svg(_svg_with(("png", one), ("png", one)))
    with pytest.raises(ConversionError, match="Слишком много встроенных"):
        images._sanitize_svg(_svg_with(("png", one), ("png", one), ("png", one)))


def test_unparseable_header_dropped():
    """Сигнатура PNG, но заголовок обрезан — ссылка удаляется как внешняя."""
    text, _ = images._sanitize_svg(_svg_with(("png", b"\x89PNG\r\n\x1a\n\x00\x00")))
    assert "data:" not in text


def test_embedded_href_canonicalised():
    """Пробелы в base64 убираются: resvg получает ровно те байты, что мы проверили."""
    raw = _real("PNG", (4, 4))
    b64 = base64.b64encode(raw).decode()
    spaced = " ".join(b64[i:i + 10] for i in range(0, len(b64), 10))
    data = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        f'<image href="data:image/png;base64,{spaced}" width="10" height="10"/></svg>'
    ).encode()
    text, _ = images._sanitize_svg(data)
    assert f"data:image/png;base64,{b64}" in text


def test_embedded_bomb_convert_fails_fast(tmp_path: Path):
    src = tmp_path / "bomb.svg"
    src.write_bytes(_svg_with(("png", _png_header_only(12000, 12000))))
    dst = tmp_path / "bomb.png"
    target = next(t for t in CONV.targets("svg") if t.id == "png")
    started = time.monotonic()
    with pytest.raises(ConversionError, match="Встроенное в SVG"):
        CONV.convert([src], dst, target, Preset.MEDIUM, lambda p: None, CancelToken(), formats=["svg"])
    assert time.monotonic() - started < 5
    assert not dst.exists()


# --- (б) лимит памяти дочернего процесса ---------------------------------------------


def _no_children() -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not multiprocessing.active_children():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(not hasattr(_isolated, "_windows_job") or __import__("os").name != "nt",
                    reason="объект задания есть только на Windows")
@pytest.mark.parametrize("how", ["python", "native"])
def test_child_memory_limit(monkeypatch, how):
    """Ребёнок пытается взять 600 МБ при лимите 200 МБ → «нехватка памяти», а не «повреждён»."""
    monkeypatch.setattr(_isolated, "PROCESS_MEMORY_LIMIT", 200 * 2**20)
    started = time.monotonic()
    with pytest.raises(_isolated.ChildFailed) as info:
        _isolated.run_isolated(
            "alloc", {"bytes": 600 * 2**20, "how": how},
            cancel=CancelToken(), timeout=30,
        )
    assert info.value.code == "memory"
    assert time.monotonic() - started < 30
    assert _no_children()


def test_child_memory_error_message(monkeypatch, tmp_path: Path):
    """Ошибка памяти ребёнка в конвертере → понятный текст про память."""
    def fake_run(*args, **kwargs):
        raise _isolated.ChildFailed("memory", "limit")

    monkeypatch.setattr(_isolated, "run_isolated", fake_run)
    src = tmp_path / "a.svg"
    src.write_bytes(_svg_with(("png", _real("PNG", (2, 2)))))
    target = next(t for t in CONV.targets("svg") if t.id == "png")
    with pytest.raises(ConversionError, match="Недостаточно памяти"):
        CONV.convert([src], tmp_path / "a.png", target, Preset.MEDIUM, lambda p: None,
                     CancelToken(), formats=["svg"])
