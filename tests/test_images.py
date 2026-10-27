"""Тесты конвертера изображений (converters/images.py).

Все входные файлы генерируются в tmp_path; сеть не используется.
"""

from __future__ import annotations

import base64
import io
import random
import struct
import time
import zlib
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw

from konverter.converters import images
from konverter.converters.base import (
    REGISTRY,
    CancelToken,
    Cancelled,
    Category,
    ConversionError,
    Preset,
)
from konverter.converters.images import ImageConverter
from konverter.core.detect import RAW_EXTENSIONS, detect

CONV = ImageConverter()
DATA_DIR = Path(__file__).parent / "data"

# Размер тестовых картинок для матрицы.
W, H = 120, 80
# SVG 200×100 → длинная сторона меньше 256 → растр 256×128.
SVG_W, SVG_H = 256, 128


# --- Вспомогательное ---------------------------------------------------------


def _photo(width: int, height: int, seed: int = 1) -> Image.Image:
    """«Фотография»: плавный градиент + шум, чтобы сжатие было реальным."""
    rnd = random.Random(seed)
    noise = bytes(rnd.randrange(256) for _ in range(width * height))
    base = Image.linear_gradient("L").resize((width, height))
    r = Image.blend(base, Image.frombytes("L", (width, height), noise), 0.35)
    g = Image.blend(base.transpose(Image.Transpose.FLIP_LEFT_RIGHT), r, 0.5)
    b = Image.blend(base.transpose(Image.Transpose.FLIP_TOP_BOTTOM), r, 0.3)
    img = Image.merge("RGB", (r, g, b))
    draw = ImageDraw.Draw(img)
    draw.ellipse((width // 4, height // 4, width // 2, height // 2), fill=(200, 40, 40))
    return img


def _make_input(fmt: str, folder: Path) -> Path:
    """Создаёт тестовый файл формата `fmt`; размер W×H (ICO — 64×64)."""
    img = _photo(W, H)
    path = folder / f"вход {fmt}.{fmt}"
    if fmt == "jpg":
        img.save(path, "JPEG", quality=90)
    elif fmt == "png":
        rgba = img.convert("RGBA")
        rgba.putpixel((0, 0), (0, 0, 0, 0))
        rgba.save(path, "PNG")
    elif fmt == "webp":
        img.save(path, "WEBP")
    elif fmt == "avif":
        img.save(path, "AVIF")
    elif fmt == "heic":
        images._ensure_heif()
        img.save(path, "HEIF")
    elif fmt == "gif":
        img.convert("P", palette=Image.Palette.ADAPTIVE).save(path, "GIF")
    elif fmt == "bmp":
        img.save(path, "BMP")
    elif fmt == "tiff":
        img.save(path, "TIFF")
    elif fmt == "ico":
        img.resize((64, 64)).save(path, "ICO", sizes=[(64, 64)])
    elif fmt == "svg":
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" '
            'viewBox="0 0 200 100"><rect width="200" height="100" fill="#3080ff"/>'
            '<circle cx="100" cy="50" r="40" fill="#ff0"/></svg>',
            encoding="utf-8",
        )
    else:
        raise AssertionError(fmt)
    return path


def _expected_size(fmt: str) -> tuple[int, int]:
    return {"ico": (64, 64), "svg": (SVG_W, SVG_H)}.get(fmt, (W, H))


def _target(fmt: str, target_id: str):
    for target in CONV.targets(fmt):
        if target.id == target_id:
            return target
    raise AssertionError(f"нет варианта {target_id} для {fmt}")


def _run(sources, dst: Path, fmt: str, target_id: str, preset=Preset.MEDIUM, cancel=None,
         formats=None):
    """Запуск конвертера; `formats` по умолчанию — `fmt` для каждого файла."""
    progress: list[float] = []
    if isinstance(sources, Path):
        sources = [sources]
    if formats is None:
        formats = [fmt] * len(sources)
    CONV.convert(
        sources, dst, _target(fmt, target_id), preset, progress.append, cancel or CancelToken(),
        formats=formats,
    )
    return progress


def _pdf_pages(path: Path) -> list[tuple[float, float]]:
    doc = pdfium.PdfDocument(str(path))
    try:
        return [doc[i].get_size() for i in range(len(doc))]
    finally:
        doc.close()


def _png_bytes(size, color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


# --- Регистрация и варианты --------------------------------------------------


def test_registered():
    assert isinstance(REGISTRY[Category.IMAGE], ImageConverter)


def test_targets_basic():
    ids = [t.id for t in CONV.targets("jpg")]
    assert "jpg" not in ids
    assert ids == ["png", "webp", "avif", "gif", "bmp", "tiff", "ico", "pdf_each", "pdf_single", "print_ppi"]
    assert len(CONV.targets("svg")) == 11
    assert "heic" not in [t.id for t in CONV.targets("heic")]
    assert CONV.targets("mp4") == []

    by_id = {t.id: t for t in CONV.targets("png")}
    for tid in ("jpg", "webp", "avif", "pdf_each", "pdf_single"):
        assert by_id[tid].uses_preset, tid
    for tid in ("gif", "bmp", "tiff", "ico"):
        assert not by_id[tid].uses_preset, tid
    assert by_id["pdf_single"].many_to_one and by_id["pdf_single"].is_long
    assert not by_id["pdf_each"].many_to_one and not by_id["pdf_each"].is_long
    assert not by_id["jpg"].is_long
    assert all(t.is_long for t in CONV.targets("raw"))


def test_target_labels_from_strings(monkeypatch):
    """Подписи вариантов берутся из strings.py через tr() (ревью: строки интерфейса)."""
    from konverter import strings

    for fmt in ("jpg", "png"):
        for target in CONV.targets(fmt):
            assert target.label == strings.tr(f"target_{target.id}"), target.id
            assert f"target_{target.id}" in strings.STRINGS["ru"]
    assert strings.tr("target_ico") == "ICO (значок)"
    assert strings.tr("target_pdf_single") == "PDF (все изображения в один файл)"
    # Подпись читается при каждом вызове — смена строки (языка) видна сразу.
    monkeypatch.setitem(strings.STRINGS["ru"], "target_png", "ПНГ-проверка")
    assert {t.id: t.label for t in CONV.targets("jpg")}["png"] == "ПНГ-проверка"


def test_target_order_constant():
    """Полный канонический порядок вариантов; targets() его соблюдает."""
    assert list(ImageConverter.target_order) == [
        "jpg", "png", "webp", "avif", "gif", "bmp", "tiff", "ico", "pdf_each", "pdf_single", "print_ppi",
    ]
    for fmt in images.INPUT_FORMATS:
        ids = [t.id for t in CONV.targets(fmt)]
        assert ids == [i for i in ImageConverter.target_order if i in ids], fmt


# --- Матрица: каждый вход × каждый выход -------------------------------------

_MATRIX_INPUTS = ["jpg", "png", "webp", "heic", "avif", "gif", "bmp", "tiff", "ico", "svg"]
_MATRIX = [(fmt, t.id) for fmt in _MATRIX_INPUTS for t in CONV.targets(fmt) if t.id != "print_ppi"]


@pytest.mark.parametrize(("fmt", "target_id"), _MATRIX)
def test_matrix(tmp_path: Path, fmt: str, target_id: str):
    src = _make_input(fmt, tmp_path)
    target = _target(fmt, target_id)
    dst = tmp_path / f"результат.{target.extension}"
    progress = _run(src, dst, fmt, target_id)

    assert progress and progress[-1] == 1.0
    assert all(0.0 <= p <= 1.0 for p in progress)
    # Создан только результат, исходник на месте.
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([src.name, dst.name])

    width, height = _expected_size(fmt)
    if target.extension == "pdf":
        pages = _pdf_pages(dst)
        assert len(pages) == 1
        assert pages[0] == pytest.approx((width * 0.75, height * 0.75), abs=0.5)
        return
    with Image.open(dst) as out:
        out.load()
        if target_id == "ico":
            assert out.size == (256, 256)
        else:
            assert out.size == (width, height)


# --- П10: пресеты ------------------------------------------------------------


@pytest.mark.parametrize("target_id", ["jpg", "webp", "avif", "pdf_each"])
def test_presets_size_order(tmp_path: Path, target_id: str):
    src = tmp_path / "photo.png"
    _photo(1500, 1000, seed=7).save(src)
    sizes = {}
    for preset in Preset:
        ext = "pdf" if target_id.startswith("pdf") else target_id
        dst = tmp_path / f"{preset.value}.{ext}"
        _run(src, dst, "png", target_id, preset=preset)
        sizes[preset] = dst.stat().st_size
    assert sizes[Preset.SMALL] < sizes[Preset.MEDIUM] < sizes[Preset.HIGH], sizes


def test_small_preset_limits_resolution(tmp_path: Path):
    src = tmp_path / "big.png"
    _photo(2400, 1200).save(src)
    dst = tmp_path / "small.jpg"
    _run(src, dst, "png", "jpg", preset=Preset.SMALL)
    with Image.open(dst) as out:
        assert out.size == (1920, 960)
    # HIGH разрешение не трогает.
    dst_high = tmp_path / "high.jpg"
    _run(src, dst_high, "png", "jpg", preset=Preset.HIGH)
    with Image.open(dst_high) as out:
        assert out.size == (2400, 1200)


# --- Обработка ---------------------------------------------------------------


def test_transparent_png_to_jpg_white(tmp_path: Path):
    src = tmp_path / "alpha.png"
    img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    img.paste((255, 0, 0, 255), (0, 0, 10, 10))
    img.save(src)
    for target_id, ext in (("jpg", "jpg"), ("bmp", "bmp")):
        dst = tmp_path / f"out.{ext}"
        _run(src, dst, "png", target_id)
        with Image.open(dst) as out:
            px = out.convert("RGB").getpixel((30, 30))
            assert all(c >= 250 for c in px), px


def test_palette_transparency_to_jpg_white(tmp_path: Path):
    src = tmp_path / "pal.gif"
    img = Image.new("P", (20, 20), 0)
    img.putpalette([0, 0, 0] * 1 + [255, 0, 0] * 255)
    img.info["transparency"] = 0
    img.save(src, transparency=0)
    dst = tmp_path / "pal.jpg"
    _run(src, dst, "gif", "jpg")
    with Image.open(dst) as out:
        assert all(c >= 250 for c in out.convert("RGB").getpixel((5, 5)))


def test_exif_orientation_applied_and_metadata_removed(tmp_path: Path):
    src = tmp_path / "exif.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # повернуть на 90°
    exif[0x010F] = "Камера"  # Make
    gps = {1: "N", 2: (55.0, 45.0, 0.0), 3: "E", 4: (37.0, 37.0, 0.0)}
    exif[0x8825] = gps
    _photo(60, 40).save(src, "JPEG", exif=exif.tobytes())
    with Image.open(src) as check:
        assert check.getexif().get(0x0112) == 6

    for target_id, ext in (("png", "png"), ("webp", "webp"), ("tiff", "tiff"), ("avif", "avif")):
        dst = tmp_path / f"out.{ext}"
        _run(src, dst, "jpg", target_id)
        with Image.open(dst) as out:
            assert out.size == (40, 60), target_id
            assert "exif" not in out.info, target_id
            exif_out = out.getexif()
            assert 0x8825 not in exif_out and 0x010F not in exif_out, target_id
    raw = (tmp_path / "out.png").read_bytes()
    assert b"eXIf" not in raw and b"Exif" not in raw


def test_cmyk_and_16bit(tmp_path: Path):
    cmyk = tmp_path / "cmyk.jpg"
    _photo(50, 30).convert("CMYK").save(cmyk, "JPEG")
    dst = tmp_path / "cmyk.png"
    _run(cmyk, dst, "jpg", "png")
    with Image.open(dst) as out:
        assert out.mode == "RGB" and out.size == (50, 30)

    # 16-битное серое: значения до 65535 не должны стать сплошным белым.
    gray16 = tmp_path / "g16.png"
    img = Image.new("I;16", (64, 16))
    img.putdata([(x * 1024) for _ in range(16) for x in range(64)])
    img.save(gray16)
    dst16 = tmp_path / "g16.jpg"
    _run(gray16, dst16, "png", "jpg")
    with Image.open(dst16) as out:
        lo, hi = out.convert("L").getextrema()
        assert lo < 20 and hi > 230


def test_animated_gif_to_webp(tmp_path: Path):
    src = tmp_path / "anim.gif"
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255)]
    frames = [Image.new("RGB", (32, 24), c) for c in colors]
    durations = [100, 200, 300, 400, 500]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=durations, loop=0)

    dst = tmp_path / "anim.webp"
    _run(src, dst, "gif", "webp")
    with Image.open(dst) as out:
        assert out.n_frames == len(frames)
        assert out.info.get("loop") == 0
        got = []
        for i in range(out.n_frames):
            out.seek(i)
            out.load()  # WEBP заполняет duration только при загрузке кадра
            got.append(out.info["duration"])
        assert got == durations

    # Обратно WEBP → GIF: анимация сохраняется.
    back = tmp_path / "back.gif"
    _run(dst, back, "webp", "gif")
    with Image.open(back) as out:
        assert out.n_frames == len(frames)
        assert out.info.get("loop") == 0

    # В прочие форматы — первый кадр.
    png = tmp_path / "first.png"
    _run(src, png, "gif", "png")
    with Image.open(png) as out:
        assert out.convert("RGB").getpixel((5, 5)) == (255, 0, 0)


def test_gif_play_once_maps_to_webp_loop_1(tmp_path: Path):
    src = tmp_path / "once.gif"
    frames = [Image.new("RGB", (8, 8), c) for c in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=50)
    dst = tmp_path / "once.webp"
    _run(src, dst, "gif", "webp")
    with Image.open(dst) as out:
        assert out.info.get("loop") == 1


def test_multipage_tiff_to_pdf_each(tmp_path: Path):
    src = tmp_path / "pages.tiff"
    pages = [Image.new("RGB", (100 + 10 * i, 50), (40 * i, 0, 0)) for i in range(4)]
    pages[0].save(src, save_all=True, append_images=pages[1:])
    dst = tmp_path / "pages.pdf"
    _run(src, dst, "tiff", "pdf_each")
    sizes = _pdf_pages(dst)
    assert len(sizes) == 4
    assert [round(w / 0.75) for w, _ in sizes] == [100, 110, 120, 130]

    # В прочие форматы — первая страница.
    png = tmp_path / "pages.png"
    _run(src, png, "tiff", "png")
    with Image.open(png) as out:
        assert out.size == (100, 50)


def test_pdf_single_three_images_in_order(tmp_path: Path):
    sources = []
    for i, (fmt, w) in enumerate((("png", 100), ("jpg", 200), ("webp", 300))):
        path = tmp_path / f"{i} картинка.{fmt}"
        Image.new("RGB", (w, 50), (i * 80, 10, 10)).save(path)
        sources.append(path)
    dst = tmp_path / "все.pdf"
    progress = _run(sources, dst, "png", "pdf_single", formats=["png", "jpg", "webp"])
    sizes = _pdf_pages(dst)
    assert [round(w / 0.75) for w, _ in sizes] == [100, 200, 300]
    assert progress[-1] == 1.0 and progress == sorted(progress)


def test_ico_output_sizes(tmp_path: Path):
    src = tmp_path / "wide.png"
    Image.new("RGB", (300, 100), (0, 128, 0)).save(src)
    dst = tmp_path / "icon.ico"
    _run(src, dst, "png", "ico")
    with Image.open(dst) as out:
        assert out.size == (256, 256)
        assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(out.info["sizes"])
        out.load()
        rgba = out.convert("RGBA")
        assert rgba.getpixel((128, 5))[3] == 0  # прозрачные поля сверху
        assert rgba.getpixel((128, 128))[3] == 255


def test_unicode_and_spaces_in_names(tmp_path: Path):
    folder = tmp_path / "папка с пробелами"
    folder.mkdir()
    src = folder / "фото — копия (1) ✓.jpeg"
    _photo(30, 20).save(src, "JPEG")
    dst = folder / "результат № 1.png"
    _run(src, dst, "jpg", "png")
    with Image.open(dst) as out:
        assert out.size == (30, 20)


# --- SVG: безопасность -------------------------------------------------------


def _svg_with_refs(secret: Path) -> str:
    uri = secret.resolve().as_uri()
    abs_path = str(secret.resolve())
    return f"""<?xml version="1.0"?>
<?xml-stylesheet href="evil.css"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     width="100" height="100" xml:base="file:///C:/">
  <style>@import url("http://example.com/x.css"); rect {{ fill: url(http://example.com/p) }}</style>
  <rect width="100" height="100" fill="#ffffff"/>
  <image href="{uri}" x="0" y="0" width="50" height="50"/>
  <image xlink:href="{abs_path}" x="50" y="0" width="50" height="50"/>
  <image href="{secret.name}" x="0" y="50" width="50" height="50"/>
  <image href="../{secret.parent.name}/{secret.name}" x="50" y="50" width="50" height="50"/>
  <image href="http://example.com/x.png" width="10" height="10"/>
  <use href="other.svg#a"/>
  <rect style="fill: url('file:///C:/x.svg#g')" width="5" height="5"/>
</svg>"""


def test_svg_external_refs_not_loaded(tmp_path: Path, monkeypatch):
    secret = tmp_path / "secret.png"
    Image.new("RGB", (50, 50), (255, 0, 0)).save(secret)
    svg = tmp_path / "evil.svg"
    svg.write_text(_svg_with_refs(secret), encoding="utf-8")
    # Рабочая папка = папка с «секретом»: относительные ссылки тоже не должны сработать.
    monkeypatch.chdir(tmp_path)

    dst = tmp_path / "evil.png"
    _run(svg, dst, "svg", "png")
    with Image.open(dst) as out:
        rgb = out.convert("RGB")
        colors = rgb.getcolors(maxcolors=1_000_000)
    reddish = [c for _, c in colors if c[0] > 200 and c[1] < 80 and c[2] < 80]
    assert not reddish, "в растр попала картинка из локального файла"


def test_svg_sanitize_strips_links():
    secret = Path("C:/secret/secret.png")
    text, _root = images._sanitize_svg(_svg_with_refs(secret).encode("utf-8"))
    assert "example.com" not in text
    assert "secret" not in text
    assert "file:" not in text
    assert "evil.css" not in text
    assert "other.svg" not in text
    assert "<style" not in text


def test_svg_entity_cannot_smuggle_image(tmp_path: Path):
    data = (
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY e "<image href=\'secret.png\' '
        b'width=\'10\' height=\'10\'/>">]>'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">&e;</svg>'
    )
    text, _ = images._sanitize_svg(data)
    assert "secret" not in text and "DOCTYPE" not in text


def test_svg_external_entity_rejected(tmp_path: Path):
    secret = tmp_path / "secret.txt"
    secret.write_text("СЕКРЕТ", encoding="utf-8")
    svg = tmp_path / "xxe.svg"
    svg.write_text(
        f'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "{secret.as_uri()}">]>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><text>&x;</text></svg>',
        encoding="utf-8",
    )
    with pytest.raises(ConversionError):
        _run(svg, tmp_path / "xxe.png", "svg", "png")


def test_svg_billion_laughs_rejected(tmp_path: Path):
    ents = '<!ENTITY a "aaaaaaaaaa">' + "".join(
        f'<!ENTITY {chr(98 + i)} "{("&" + chr(97 + i) + ";") * 10}">' for i in range(8)
    )
    svg = tmp_path / "lol.svg"
    svg.write_text(
        f'<?xml version="1.0"?><!DOCTYPE svg [{ents}]>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><text>&i;</text></svg>',
        encoding="utf-8",
    )
    start = time.monotonic()
    with pytest.raises(ConversionError):
        _run(svg, tmp_path / "lol.png", "svg", "png")
    assert time.monotonic() - start < 5


def test_svg_embedded_data_png_allowed(tmp_path: Path):
    b64 = base64.b64encode(_png_bytes((10, 10), (0, 200, 0))).decode()
    svg = tmp_path / "data.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256">'
        f'<image href="data:image/png;base64,{b64}" width="256" height="256"/></svg>',
        encoding="utf-8",
    )
    dst = tmp_path / "data.png"
    _run(svg, dst, "svg", "png")
    with Image.open(dst) as out:
        r, g, b = out.convert("RGB").getpixel((128, 128))[:3]
        assert g > 150 and r < 50


def test_svg_data_uri_with_svg_payload_stripped():
    inner = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode()
    data = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        f'<image href="data:image/png;base64,{inner}" width="10" height="10"/></svg>'
    ).encode()
    text, _ = images._sanitize_svg(data)
    assert "data:" not in text


def test_svg_size_limits(tmp_path: Path):
    svg = tmp_path / "huge.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100000" height="50000">'
        '<rect width="100%" height="100%" fill="red"/></svg>',
        encoding="utf-8",
    )
    dst = tmp_path / "huge.png"
    _run(svg, dst, "svg", "png")
    with Image.open(dst) as out:
        assert out.size == (8192, 4096)

    tiny = tmp_path / "tiny.svg"
    tiny.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 8"><rect width="16" height="8"/></svg>',
        encoding="utf-8",
    )
    _run(tiny, tmp_path / "tiny.png", "svg", "png")
    with Image.open(tmp_path / "tiny.png") as out:
        assert out.size == (256, 128)


def test_svgz(tmp_path: Path):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><rect width="300" height="300" fill="blue"/></svg>'
    src = tmp_path / "pic.svgz"
    co = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
    src.write_bytes(co.compress(svg) + co.flush())
    dst = tmp_path / "pic.png"
    _run(src, dst, "svg", "png")
    with Image.open(dst) as out:
        assert out.size == (300, 300)


# --- Битые и враждебные файлы ------------------------------------------------


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def test_decompression_bomb_header(tmp_path: Path):
    """PNG-заголовок 50000×50000 (2500 Мп) при крошечном файле."""
    ihdr = struct.pack(">IIBBBBB", 50000, 50000, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" * 1000)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )
    src = tmp_path / "bomb.png"
    src.write_bytes(data)
    start = time.monotonic()
    with pytest.raises(ConversionError, match="слишком большое: 2500 Мп, максимум 200 Мп"):
        _run(src, tmp_path / "bomb.jpg", "png", "jpg")
    assert time.monotonic() - start < 2
    assert not (tmp_path / "bomb.jpg").exists()


def test_bomb_just_over_limit(tmp_path: Path):
    """Между 1× и 2× MAX_IMAGE_PIXELS Pillow только предупреждает — у нас ошибка."""
    ihdr = struct.pack(">IIBBBBB", 15000, 15000, 8, 0, 0, 0, 0)  # 225 Мп
    data = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IEND", b"")
    src = tmp_path / "bomb2.png"
    src.write_bytes(data)
    with pytest.raises(ConversionError, match="слишком большое"):
        _run(src, tmp_path / "bomb2.webp", "png", "webp")


@pytest.mark.parametrize(
    ("name", "fmt", "content"),
    [
        ("empty.png", "png", b""),
        ("garbage.jpg", "jpg", bytes(random.Random(3).randrange(256) for _ in range(5000))),
        ("text.png", "png", "это не картинка".encode("utf-8")),
        ("broken.svg", "svg", b'<svg xmlns="http://www.w3.org/2000/svg"><rect'),
        ("not_svg_root.svg", "svg", b'<html><svg xmlns="http://www.w3.org/2000/svg"/></html>'),
    ],
)
def test_broken_files(tmp_path: Path, name: str, fmt: str, content: bytes):
    src = tmp_path / name
    src.write_bytes(content)
    with pytest.raises(ConversionError):
        _run(src, tmp_path / "out.ico", fmt, "ico")


def test_truncated_jpeg(tmp_path: Path):
    full = io.BytesIO()
    _photo(400, 300).save(full, "JPEG", quality=90)
    src = tmp_path / "cut.jpg"
    src.write_bytes(full.getvalue()[: len(full.getvalue()) // 2])
    with pytest.raises(ConversionError, match="повреждён"):
        _run(src, tmp_path / "cut.png", "jpg", "png")


def test_truncated_png(tmp_path: Path):
    full = io.BytesIO()
    _photo(200, 200).save(full, "PNG")
    src = tmp_path / "cut.png"
    src.write_bytes(full.getvalue()[:-2000])
    with pytest.raises(ConversionError):
        _run(src, tmp_path / "cut.jpg", "png", "jpg")


def test_missing_file(tmp_path: Path):
    with pytest.raises(ConversionError):
        _run(tmp_path / "нет такого.png", tmp_path / "out.jpg", "png", "jpg")


# --- RAW ---------------------------------------------------------------------

_RAW_SAMPLES = sorted(
    p for p in DATA_DIR.glob("*") if p.suffix.lower().lstrip(".") in RAW_EXTENSIONS
) if DATA_DIR.is_dir() else []


def test_raw_sample(tmp_path: Path):
    if not _RAW_SAMPLES:
        pytest.skip("нет образца RAW в tests/data/ (например, sample.dng)")
    for sample in _RAW_SAMPLES:
        dst = tmp_path / f"{sample.stem}.jpg"
        _run(sample, dst, "raw", "jpg", preset=Preset.SMALL)
        with Image.open(dst) as out:
            assert max(out.size) <= 1920


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("bad.dng", b"II*\x00\x08\x00\x00\x00" + b"\x00" * 200),
        ("bad.cr2", b"II*\x00\x10\x00\x00\x00CR\x02\x00" + b"\xff" * 300),
        ("bad.nef", bytes(random.Random(5).randrange(256) for _ in range(4000))),
        ("bad.cr3", b"\x00\x00\x00\x18ftypcrx " + b"\x00" * 100),
    ],
)
def test_broken_raw(tmp_path: Path, name: str, content: bytes):
    src = tmp_path / name
    src.write_bytes(content)
    with pytest.raises(ConversionError):
        _run(src, tmp_path / "out.jpg", "raw", "jpg")


# --- П.4 ревью: декодер выбирается по коду формата из detect ------------------


def _convert_detected(src: Path, dst: Path, target_id: str) -> None:
    """Как в программе: формат — из detect, конвертер его не угадывает."""
    found = detect(src)
    assert found is not None
    CONV.convert(
        [src], dst, _target(found.fmt, target_id), Preset.MEDIUM, lambda p: None,
        CancelToken(), formats=[found.fmt],
    )


def test_png_renamed_to_nef_converts(tmp_path: Path):
    src = tmp_path / "снимок.nef"
    _photo(40, 30).save(src, "PNG")
    assert detect(src).fmt == "png"
    dst = tmp_path / "out.jpg"
    _convert_detected(src, dst, "jpg")
    with Image.open(dst) as out:
        assert out.format == "JPEG" and out.size == (40, 30)


def test_png_with_svg_text_chunk_is_png(tmp_path: Path):
    from PIL import PngImagePlugin

    src = tmp_path / "текст.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>')
    _photo(40, 30).save(src, "PNG", pnginfo=info)
    assert detect(src).fmt == "png"
    dst = tmp_path / "out.webp"
    _convert_detected(src, dst, "webp")
    with Image.open(dst) as out:
        assert out.format == "WEBP" and out.size == (40, 30)


def test_formats_required_and_checked(tmp_path: Path):
    src = _make_input("png", tmp_path)
    target = _target("png", "jpg")
    with pytest.raises(TypeError):
        CONV.convert([src], tmp_path / "o.jpg", target, Preset.MEDIUM, lambda p: None,
                     CancelToken())  # formats обязателен
    with pytest.raises(ConversionError):
        CONV.convert([src], tmp_path / "o.jpg", target, Preset.MEDIUM, lambda p: None,
                     CancelToken(), formats=[])
    with pytest.raises(ConversionError):
        CONV.convert([src], tmp_path / "o.jpg", target, Preset.MEDIUM, lambda p: None,
                     CancelToken(), formats=["mp4"])


def test_pillow_restricted_to_detected_format(tmp_path: Path):
    """Код "jpg" не даёт открыть PNG: Pillow ограничен форматом из detect."""
    src = _make_input("png", tmp_path)
    with pytest.raises(ConversionError, match="повреждён"):
        _run(src, tmp_path / "o.bmp", "jpg", "bmp")


# --- Отмена ------------------------------------------------------------------


def test_cancel_before_start(tmp_path: Path):
    src = _make_input("png", tmp_path)
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        _run(src, tmp_path / "out.jpg", "png", "jpg", cancel=token)
    assert not (tmp_path / "out.jpg").exists()


def test_cancel_pdf_single_after_first(tmp_path: Path):
    sources = []
    for i in range(3):
        path = tmp_path / f"{i}.png"
        Image.new("RGB", (20, 20), (i * 50, 0, 0)).save(path)
        sources.append(path)
    token = CancelToken()
    seen: list[float] = []

    def on_progress(value: float) -> None:
        seen.append(value)
        token.cancel()  # отмена сразу после первого изображения

    with pytest.raises(Cancelled):
        CONV.convert(
            sources, tmp_path / "out.pdf", _target("png", "pdf_single"), Preset.MEDIUM,
            on_progress, token, formats=["png"] * 3,
        )
    assert len(seen) == 1
    assert not (tmp_path / "out.pdf").exists()


def test_cancel_during_animation(tmp_path: Path):
    src = tmp_path / "anim.gif"
    frames = [Image.new("RGB", (16, 16), (i * 20, 0, 0)) for i in range(10)]
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=50, loop=0)
    token = CancelToken()

    def on_progress(value: float) -> None:
        if value > 0.2:
            token.cancel()

    with pytest.raises(Cancelled):
        CONV.convert(
            [src], tmp_path / "out.webp", _target("gif", "webp"), Preset.MEDIUM, on_progress, token,
            formats=["gif"],
        )
