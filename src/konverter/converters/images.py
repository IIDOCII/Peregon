"""Конвертер изображений (категория IMAGE).

Вход: jpg png webp heic avif gif bmp tiff ico svg raw.
Выход: JPG, PNG, WEBP, GIF, BMP, TIFF, ICO, AVIF, PDF (каждое изображение
отдельно или все в один файл).

Принятые решения (документация поведения):

* «В тот же формат» (jpg→jpg, webp→webp, …) не предлагается вовсе — это
  самый простой и однозначный вариант; отдельного режима «Сжать» нет.
* Пресеты (Ф7) влияют на JPG, WEBP, AVIF и PDF (см. `_QUALITY`,
  `_SMALL_MAX_SIDE`, `_PDF_JPEG_QUALITY`). Для SMALL изображение дополнительно
  уменьшается так, чтобы длинная сторона была не больше 1920 px (для PDF —
  1600 px). PNG/TIFF/GIF/BMP/ICO сохраняются без потерь, пресет на них не
  влияет (`uses_preset=False`).
* EXIF-поворот применяется (`ImageOps.exif_transpose`). Метаданные (EXIF,
  GPS, XMP, комментарии) в результат НЕ переносятся — ради приватности:
  у каждого кадра очищается `info`, а `exif=` при сохранении не передаётся.
  ICC-профиль не переносится, вместо этого цвета переводятся в sRGB
  (если профиль читается), так что вид картинки сохраняется.
* Прозрачность при сохранении в JPG, BMP и PDF накладывается на белый фон.
* Анимация сохраняется только для GIF→WEBP и WEBP→GIF (все кадры,
  длительности, повтор). В остальные форматы идёт первый кадр.
  Повтор: GIF без NETSCAPE-блока (проиграть один раз) ↔ WEBP loop=1;
  GIF loop=0 ↔ WEBP loop=0 (бесконечно); GIF loop=n ↔ WEBP loop=n+1
  (в GIF n — число ПОВТОРОВ, в WEBP — число проигрываний).
* Многостраничный TIFF в PDF попадает целиком (страница на страницу),
  в остальные форматы — первая страница.
* ICO: квадрат 256×256 с прозрачными полями, исходник вписывается
  с сохранением пропорций (маленькие картинки увеличиваются), в файл
  пишутся размеры 16, 24, 32, 48, 64, 128, 256.
* PDF собирается через fpdf2: каждое изображение пережимается в JPEG
  с качеством по пресету и встраивается без повторного перекодирования
  (DCTDecode). Размер страницы равен размеру изображения при 96 dpi
  (1 px = 0,75 pt), длинная сторона страницы не больше 14400 pt (предел
  спецификации PDF). Pillow `save("PDF")` не выбран, т.к. требует держать
  все страницы в памяти разом и хуже контролирует размер страницы.
* Тип входного файла НЕ угадывается: декодер выбирается строго по коду
  формата из `core.detect` (параметр `formats` метода `convert`):
  `svg` → resvg, `raw` → rawpy, остальные → Pillow с разрешённым для этого
  кода набором форматов (`_PIL_FORMATS_BY_CODE`). PNG под именем `.nef`
  остаётся PNG, а PNG с текстом «<svg» внутри не принимается за SVG.
* SVG рендерится resvg-py после очистки (см. `_sanitize_svg`): удаляются все
  внешние ссылки, остаются только ссылки внутрь документа (`#id`) и
  встроенные растровые картинки `data:image/png|jpeg|gif|webp`. У встроенных
  картинок разбирается заголовок (PNG IHDR, JPEG SOF, GIF экран и кадры,
  WEBP VP8/VP8L/VP8X): больше `SVG_EMBED_MAX_PIXELS` на одну, больше
  `SVG_EMBED_MAX_TOTAL_PIXELS` на все или больше `SVG_MAX_EMBEDDED` штук —
  ошибка; заголовок не разобран — ссылка удаляется (с записью в лог).
  Пиксели встроенных картинок резервируются в бюджете памяти вместе с
  холстом. Размер растра:
  по width/height/viewBox, длинная сторона приводится к диапазону
  [256, 8192] px.
* RAW: rawpy (LibRaw), `postprocess(use_camera_wb=True, output_bps=8)`;
  для SMALL — `half_size=True` (быстрее, всё равно потом уменьшаем).
  Размеры проверяются до распаковки.
* Рендер SVG и декодирование RAW идут в ОТДЕЛЬНОМ процессе
  (`_isolated.py`) с таймаутом (`_isolated.SVG_TIMEOUT`, `RAW_TIMEOUT`) и
  отменой: процесс убивается, «бомба» SVG не вешает программу.

Безопасность: входные файлы считаются враждебными.
* `Image.MAX_IMAGE_PIXELS` = 200 Мп (глобально для процесса),
  `DecompressionBombWarning` превращён в ошибку (глобальный фильтр warnings);
  размеры проверяются явно сразу после ленивого `Image.open`, до `load()`.
* Память: перед декодированием пикселей (после чтения размеров) задача
  резервирует пиксели в общем бюджете процесса (`core.budget.IMAGE_BUDGET`,
  200 Мп на все одновременно декодируемые изображения) и освобождает после
  сохранения. Многостраничный TIFF резервирует постранично, анимация —
  все кадры сразу (они одновременно в памяти).
* Суммарные лимиты: страницы TIFF — не больше `MAX_PAGES` и
  `MAX_TOTAL_PIXELS` пикселей во всех страницах; анимация — не больше
  `MAX_FRAMES` кадров и `MAX_ANIMATION_PIXELS` во всех кадрах.
* Pillow открывает только разрешённые форматы (`formats=`), чтобы не
  задействовать экзотические декодеры (EPS → Ghostscript и т.п.).
* `LOAD_TRUNCATED_IMAGES` не включается: обрезанный файл — ошибка.
* Любая ошибка декодирования превращается в `ConversionError`.
* Временные файлы не создаются: всё в памяти (пиксели SVG/RAW из дочернего
  процесса — через общую память), результат — только в `dst`.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import math
import re
import struct
import warnings
import zlib
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from PIL import Image, ImageCms, ImageOps

from konverter.core import budget
from konverter.strings import tr

from . import _isolated
from .base import (
    CancelToken,
    Cancelled,
    Category,
    ConversionError,
    Preset,
    ProgressCallback,
    Target,
)

log = logging.getLogger(__name__)

# --- Лимиты ------------------------------------------------------------------

# Максимум пикселей в одном изображении (кадре, странице).
MAX_PIXELS = 200_000_000
# Суммарный лимит пикселей во всех кадрах анимации (≈1,2 ГБ в RGBA).
MAX_ANIMATION_PIXELS = 300_000_000
# Максимум кадров анимации.
MAX_FRAMES = 5000
# Максимум страниц многостраничного TIFF.
MAX_PAGES = 2000
# Суммарный лимит пикселей во всех страницах многостраничного TIFF.
MAX_TOTAL_PIXELS = 400_000_000
# Максимальный размер SVG-файла и SVG после распаковки .svgz, байт.
SVG_MAX_BYTES = 32 * 1024 * 1024
SVG_MAX_UNPACKED = 64 * 1024 * 1024
# Пределы длинной стороны растра при рендере SVG, px.
SVG_MAX_SIDE = 8192
SVG_MIN_SIDE = 256
# Встроенные в SVG растры (data:image/…): resvg распаковывает каждый целиком
# в RGBA (4 байта на пиксель), поэтому лимиты строже MAX_PIXELS: на одну
# картинку — 64 Мп (≈ 256 МБ, это холст 8192×8192 — больше SVG всё равно
# не рендерится), на все вместе — 128 Мп, и не больше SVG_MAX_EMBEDDED штук.
SVG_EMBED_MAX_PIXELS = 64_000_000
SVG_EMBED_MAX_TOTAL_PIXELS = 128_000_000
SVG_MAX_EMBEDDED = 100
# Максимальный размер RAW-файла (читается целиком в память), байт.
RAW_MAX_BYTES = 512 * 1024 * 1024

Image.MAX_IMAGE_PIXELS = MAX_PIXELS
# Предупреждение о «бомбе» должно останавливать обработку, а не печататься.
warnings.simplefilter("error", Image.DecompressionBombWarning)

# --- Тексты ошибок -----------------------------------------------------------

MSG_DAMAGED = "Файл повреждён или не является изображением"
MSG_NO_MEMORY = "Недостаточно памяти для обработки изображения"


def _too_large_message(pixels: int, what: str = "Изображение") -> str:
    return (
        f"{what} слишком большое: {pixels / 1_000_000:.0f} Мп, "
        f"максимум {MAX_PIXELS // 1_000_000} Мп"
    )


# --- Регистрация HEIC (один раз) -----------------------------------------------

_heif_registered = False


def _ensure_heif() -> None:
    """Подключает чтение HEIC/HEIF к Pillow (однократно)."""
    global _heif_registered
    if not _heif_registered:
        import pillow_heif

        pillow_heif.register_heif_opener()
        _heif_registered = True


# Форматы Pillow, которые разрешено открывать для кода формата из detect.
# JPEG включает и MPO. HEIC и AVIF — родственные контейнеры (ISO-BMFF),
# detect различает их по брендам, поэтому разрешены оба декодера.
_PIL_FORMATS_BY_CODE: dict[str, list[str]] = {
    "jpg": ["JPEG"],
    "png": ["PNG"],
    "webp": ["WEBP"],
    "heic": ["HEIF", "AVIF"],
    "avif": ["AVIF", "HEIF"],
    "gif": ["GIF"],
    "bmp": ["BMP"],
    "tiff": ["TIFF"],
    "ico": ["ICO"],
}

# --- Пресеты -----------------------------------------------------------------

# Качество кодирования для форматов с потерями.
_QUALITY: dict[str, dict[Preset, int]] = {
    "jpg": {Preset.HIGH: 92, Preset.MEDIUM: 80, Preset.SMALL: 65},
    "webp": {Preset.HIGH: 90, Preset.MEDIUM: 78, Preset.SMALL: 62},
    "avif": {Preset.HIGH: 80, Preset.MEDIUM: 62, Preset.SMALL: 45},
}
# Длинная сторона при SMALL для JPG/WEBP/AVIF.
_SMALL_MAX_SIDE = 1920
# PDF: качество JPEG внутри и ограничение длинной стороны (None — без него).
_PDF_JPEG_QUALITY = {Preset.HIGH: 90, Preset.MEDIUM: 75, Preset.SMALL: 55}
_PDF_MAX_SIDE: dict[Preset, int | None] = {
    Preset.HIGH: None,
    Preset.MEDIUM: None,
    Preset.SMALL: 1600,
}
# PDF: 96 dpi → 0,75 pt на пиксель; предел стороны страницы по спецификации.
_PDF_PT_PER_PX = 0.75
_PDF_MAX_PAGE_PT = 14400.0

# Размеры значков в ICO.
_ICO_SIZES = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
_ICO_CANVAS = 256

_WHITE = (255, 255, 255)

# --- Варианты (targets) ------------------------------------------------------

INPUT_FORMATS = ("jpg", "png", "webp", "heic", "avif", "gif", "bmp", "tiff", "ico", "svg", "raw")

# (id, расширение, uses_preset) — порядок показа пользователю.
# Подпись варианта — строка интерфейса `target_<id>` из strings.py (через tr()).
_OUTPUTS = [
    ("jpg", "jpg", True),
    ("png", "png", False),
    ("webp", "webp", True),
    ("avif", "avif", True),
    ("gif", "gif", False),
    ("bmp", "bmp", False),
    ("tiff", "tiff", False),
    ("ico", "ico", False),
]
TARGET_PDF_EACH = "pdf_each"
TARGET_PDF_SINGLE = "pdf_single"

# Полный канонический порядок вариантов (для смешанных групп его соблюдает
# core.grouping.common_targets: «JPG» не уезжает в конец списка).
TARGET_ORDER: tuple[str, ...] = (
    *(tid for tid, _ext, _uses in _OUTPUTS),
    TARGET_PDF_EACH,
    TARGET_PDF_SINGLE,
)


def _target_label(target_id: str) -> str:
    """Подпись варианта на текущем языке интерфейса."""
    return tr(f"target_{target_id}")


# --- Ошибки декодирования ----------------------------------------------------


@contextmanager
def _decoding() -> Iterator[None]:
    """Превращает любые ошибки разбора входного файла в ConversionError."""
    try:
        yield
    except (Cancelled, ConversionError):
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        # Pillow пишет «Image size (N pixels) exceeds limit …» — берём N.
        match = re.search(r"\((\d+) pixels\)", str(exc))
        if match:
            raise ConversionError(_too_large_message(int(match.group(1)))) from exc
        raise ConversionError(
            f"Изображение слишком большое, максимум {MAX_PIXELS // 1_000_000} Мп"
        ) from exc
    except MemoryError as exc:
        raise ConversionError(MSG_NO_MEMORY) from exc
    except _isolated.ChildFailed as exc:
        log.info("Дочерний процесс декодирования: %s", exc)
        raise ConversionError(MSG_NO_MEMORY if exc.code == "memory" else MSG_DAMAGED) from exc
    except Exception as exc:  # битые файлы дают самые разные исключения
        raise ConversionError(MSG_DAMAGED) from exc


@contextmanager
def _encoding() -> Iterator[None]:
    """Ошибки записи результата → ConversionError с понятным текстом."""
    try:
        yield
    except MemoryError as exc:
        raise ConversionError(MSG_NO_MEMORY) from exc
    except OSError as exc:
        raise ConversionError(f"Не удалось сохранить результат: {exc}") from exc


def _check_size(width: int, height: int, what: str = "Изображение") -> None:
    """Проверяет размеры ДО декодирования пикселей."""
    if width <= 0 or height <= 0:
        raise ConversionError(MSG_DAMAGED)
    pixels = width * height
    if pixels > MAX_PIXELS:
        raise ConversionError(_too_large_message(pixels, what))


# --- Приведение режимов ------------------------------------------------------

_SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))


def _icc_to_srgb(img: Image.Image, icc: bytes, out_mode: str) -> Image.Image | None:
    """Переводит цвета из встроенного ICC-профиля в sRGB; None — не вышло."""
    try:
        src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        return ImageCms.profileToProfile(img, src_profile, _SRGB_PROFILE, outputMode=out_mode)
    except Exception:
        return None


def _high_bit_to_l(img: Image.Image) -> Image.Image:
    """16-битное/32-битное/float серое → 8-битное L с масштабированием."""
    import numpy as np

    arr = np.asarray(img)
    if arr.dtype.kind == "f":
        arr = np.nan_to_num(arr.astype(np.float64), nan=0.0, posinf=0.0, neginf=0.0)
        lo, hi = float(arr.min()), float(arr.max())
        scaled = (arr - lo) * (255.0 / (hi - lo)) if hi > lo else np.zeros_like(arr)
        out = scaled.clip(0, 255).astype(np.uint8)
    else:
        arr = arr.astype(np.int64)
        hi = int(arr.max()) if arr.size else 0
        if img.mode.startswith("I;16") or 255 < hi <= 65535:
            out = (arr.clip(0, 65535) >> 8).astype(np.uint8)
        elif hi <= 255:
            out = arr.clip(0, 255).astype(np.uint8)
        else:
            out = (arr.clip(0, hi) * 255 // hi).astype(np.uint8)
    return Image.fromarray(out, "L")


def _normalize(img: Image.Image) -> Image.Image:
    """Приводит кадр к режиму L, RGB или RGBA в sRGB и очищает метаданные.

    Возвращает НОВОЕ изображение (не связанное с файлом) с пустым `info`.
    """
    icc = img.info.get("icc_profile")
    mode = img.mode
    if mode in ("I;16", "I;16L", "I;16B", "I;16N", "I", "F"):
        out = _high_bit_to_l(img)
        icc = None
    elif mode == "CMYK":
        out = (_icc_to_srgb(img, icc, "RGB") if icc else None) or img.convert("RGB")
        icc = None
    elif img.has_transparency_data:
        out = img.convert("RGBA")
    elif mode in ("1", "L"):
        out = img.convert("L")
        icc = None  # серые профили не трогаем
    else:
        out = img.convert("RGB")
    if icc and out.mode in ("RGB", "RGBA"):
        out = _icc_to_srgb(out, icc, out.mode) or out
    if out is img:
        out = img.copy()
    out.info = {}
    return out


def _prepare_frame(img: Image.Image) -> Image.Image:
    """Текущий кадр/страница: EXIF-поворот + нормализация режима."""
    return _normalize(ImageOps.exif_transpose(img))


def _flatten(img: Image.Image) -> Image.Image:
    """Накладывает прозрачное изображение на белый фон (для JPG/BMP/PDF)."""
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, _WHITE)
        bg.paste(img, mask=img.getchannel("A"))
        return bg
    if img.mode not in ("RGB", "L"):
        return img.convert("RGB")
    return img


def _shrink(img: Image.Image, max_side: int | None) -> Image.Image:
    """Уменьшает так, чтобы длинная сторона была ≤ max_side (не увеличивает)."""
    if max_side and max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return img


# --- Бюджет памяти -------------------------------------------------------------


def _reserve(stack: ExitStack, pixels: int, cancel: CancelToken) -> None:
    """Резервирует пиксели в общем бюджете до закрытия `stack`."""
    stack.enter_context(budget.IMAGE_BUDGET.reserve(pixels, cancel))


# --- SVG ---------------------------------------------------------------------

# data:image/<растр>[;параметр…];base64,<данные> (RFC 2397: «;base64» — последним).
# Параметры MIME (charset=… и т.п.) допускаются и отбрасываются.
_DATA_IMAGE_RE = re.compile(
    r"^data:image/(?:png|jpe?g|gif|webp)((?:;[^;,]*)*?);base64,(.*)$", re.I | re.S
)
_CSS_URL_RE = re.compile(r"url\s*\(\s*([^)]*)\)?", re.IGNORECASE)
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_SVG_NS = "http://www.w3.org/2000/svg"
# Элементы, которые удаляются целиком.
_SVG_DROP_ELEMENTS = {"script", "foreignObject", "font-face-uri", "font-face-src"}
# Единицы длины → px (CSS 96 dpi).
_UNITS = {
    "": 1.0, "px": 1.0, "pt": 96 / 72, "pc": 16.0, "mm": 96 / 25.4,
    "cm": 96 / 2.54, "in": 96.0, "em": 16.0, "ex": 8.0,
}
_LENGTH_RE = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-z]*)\s*$", re.I)


def _png_size(data: bytes) -> tuple[int, int] | None:
    """PNG: первый блок обязан быть IHDR длиной 13 байт."""
    if len(data) < 24 or data[12:16] != b"IHDR" or data[8:12] != b"\x00\x00\x00\x0d":
        return None
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return width, height


# Маркеры SOF (кадр JPEG): все, кроме DHT (C4), JPG (C8) и DAC (CC).
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """JPEG: идём по сегментам от SOI до первого SOF и берём его размеры."""
    pos, end = 2, len(data)
    while pos + 4 <= end:
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:  # байт-заполнитель
            pos += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # маркеры без длины
            pos += 2
            continue
        if marker in (0xD8, 0xD9, 0xDA):  # SOI/EOI/SOS раньше SOF — не разбираем
            return None
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if length < 2:
            return None
        if marker in _JPEG_SOF:
            if length < 7 or pos + 9 > end:
                return None
            height = int.from_bytes(data[pos + 5:pos + 7], "big")
            width = int.from_bytes(data[pos + 7:pos + 9], "big")
            return width, height  # высота 0 (DNL) отсеется проверкой > 0
        pos += 2 + length
    return None


def _gif_skip_subblocks(data: bytes, pos: int) -> int | None:
    end = len(data)
    while pos < end:
        size = data[pos]
        pos += 1
        if size == 0:
            return pos
        pos += size
    return None


def _gif_size(data: bytes) -> tuple[int, int] | None:
    """GIF: логический экран и все кадры; итог — охватывающий их прямоугольник.

    Кадр может выходить за пределы экрана, а декодер может завести холст
    по размеру кадра, поэтому учитываем и кадры.
    """
    if len(data) < 13:
        return None
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    flags = data[10]
    pos = 13
    if flags & 0x80:
        pos += 3 * (2 << (flags & 0x07))
    end = len(data)
    while pos < end:
        block = data[pos]
        if block == 0x3B:  # конец файла
            break
        if block == 0x21:  # расширение: метка + подблоки
            skipped = _gif_skip_subblocks(data, pos + 2)
            if skipped is None:
                return None
            pos = skipped
        elif block == 0x2C:  # кадр
            if pos + 10 > end:
                return None
            left, top, fw, fh = struct.unpack("<HHHH", data[pos + 1:pos + 9])
            width, height = max(width, left + fw), max(height, top + fh)
            fflags = data[pos + 9]
            pos += 10
            if fflags & 0x80:
                pos += 3 * (2 << (fflags & 0x07))
            skipped = _gif_skip_subblocks(data, pos + 1)  # +1: минимальный размер кода LZW
            if skipped is None:
                return None
            pos = skipped
        else:
            return None
    return width, height


def _webp_size(data: bytes) -> tuple[int, int] | None:
    """WEBP: первый блок VP8 / VP8L / VP8X."""
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        return (
            int.from_bytes(data[24:27], "little") + 1,
            int.from_bytes(data[27:30], "little") + 1,
        )
    return None


def _embedded_image_size(data: bytes) -> tuple[int, int] | None:
    """Размеры растра PNG/JPEG/GIF/WEBP по заголовку; None — тип не наш или не разобран.

    Тип определяем по сигнатуре (а не по MIME из data:-URI): resvg тоже
    смотрит на содержимое, и под видом «image/png» можно подсунуть SVG.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        size = _png_size(data)
    elif data.startswith(b"\xff\xd8\xff"):
        size = _jpeg_size(data)
    elif data.startswith((b"GIF87a", b"GIF89a")):
        size = _gif_size(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        size = _webp_size(data)
    else:
        return None
    if size is None or size[0] <= 0 or size[1] <= 0:
        return None
    return size


def _check_data_image(ref: str) -> tuple[str, int] | None:
    """Проверяет data:-URI со встроенным растром.

    Возвращает (канонический data:-URI, число пикселей) или None, если ссылку
    надо удалить (не base64, не растр PNG/JPEG/GIF/WEBP, заголовок не разобран);
    об удалении пишется в лог (только причина, без содержимого).
    Допускается: параметры MIME перед «;base64» (отбрасываются), пробелы
    внутри base64, недостающий паддинг «=». После нормализации base64
    декодируется строго (только алфавит base64, паддинг лишь в конце).
    Канонический URI — заново закодированные проверенные байты с MIME по
    сигнатуре: resvg получает ровно те байты, заголовок которых проверен здесь.
    Слишком большая картинка → ConversionError.
    """
    if ref[:5].lower() != "data:":
        log.info("SVG: внешняя ссылка <image> удалена")
        return None
    match = _DATA_IMAGE_RE.match(ref)
    if not match:
        log.warning("SVG: встроенная картинка удалена (неподдерживаемый data:-URI)")
        return None
    b64 = re.sub(r"[ \t\r\n\f]+", "", match.group(2))
    if len(b64) % 4 == 1:
        log.warning("SVG: встроенная картинка удалена (неверная длина base64)")
        return None
    b64 += "=" * (-len(b64) % 4)  # недостающий паддинг
    try:
        raw = binascii.a2b_base64(b64.encode("ascii"), strict_mode=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        log.warning("SVG: встроенная картинка удалена (ошибка base64)")
        return None
    size = _embedded_image_size(raw)
    if size is None:
        log.warning("SVG: встроенная картинка удалена (не PNG/JPEG/GIF/WEBP или заголовок не разобран)")
        return None
    pixels = size[0] * size[1]
    if pixels > SVG_EMBED_MAX_PIXELS:
        raise ConversionError(
            f"Встроенное в SVG изображение слишком большое: {size[0]}×{size[1]} "
            f"(максимум {SVG_EMBED_MAX_PIXELS // 1_000_000} Мп)"
        )
    if raw.startswith(b"\x89PNG"):
        mime = "png"
    elif raw.startswith(b"\xff\xd8"):
        mime = "jpeg"
    elif raw.startswith(b"GIF"):
        mime = "gif"
    else:
        mime = "webp"
    return f"data:image/{mime};base64,{base64.b64encode(raw).decode('ascii')}", pixels


def _css_is_safe(text: str) -> bool:
    """В CSS/атрибуте нет ссылок наружу: только url(#id), без @import и экранирования."""
    low = text.lower()
    if "@import" in low or "\\" in text:
        return False
    for match in _CSS_URL_RE.finditer(text):
        ref = match.group(1).strip().strip("'\"").strip()
        if not ref.startswith("#"):
            return False
    return True


def _sanitize_svg(data: bytes) -> tuple[str, object]:
    """(текст SVG, корневой элемент) — см. `_sanitize_svg_ex`."""
    text, root, _ = _sanitize_svg_ex(data)
    return text, root


def _sanitize_svg_ex(data: bytes) -> tuple[str, object, int]:
    """Безопасно разбирает SVG и вырезает всё, что может обратиться наружу.

    Разбор: lxml без сети, без загрузки внешних DTD, с раскрытием только
    ВНУТРЕННИХ сущностей (внешние → ошибка разбора), с защитой libxml2 от
    «миллиарда смешков». Затем из дерева удаляются: инструкции обработки
    (xml-stylesheet), комментарии, неразрешённые сущности, <script>,
    <foreignObject>, атрибуты href/xlink:href, ведущие не на `#id` (кроме
    data:-растра в <image> с проверенным заголовком — см. `_check_data_image`),
    xml:base, атрибуты и <style> с url() наружу,
    @import или CSS-экранированием. На выход идёт только корневой элемент
    (без DOCTYPE), поэтому resvg повторно сущности не раскроет.
    Возвращает (текст SVG, корневой элемент, сумма пикселей оставленных
    встроенных растров — resvg распакует каждый целиком, это память сверх холста).
    """
    from lxml import etree

    parser = etree.XMLParser(
        resolve_entities="internal",
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_pis=True,
        remove_comments=True,
    )
    root = etree.fromstring(data, parser)
    if not isinstance(root.tag, str) or etree.QName(root).localname != "svg":
        raise ConversionError(MSG_DAMAGED)

    to_remove = []
    embedded_count = 0
    embedded_pixels = 0
    for el in root.iter():
        if el is root:
            pass
        elif not isinstance(el.tag, str):  # комментарии, PI, сущности
            to_remove.append(el)
            continue
        local = etree.QName(el).localname
        if local in _SVG_DROP_ELEMENTS:
            to_remove.append(el)
            continue
        if local == "style" and not _css_is_safe("".join(el.itertext())):
            to_remove.append(el)
            continue
        for attr in list(el.attrib):
            qname = etree.QName(attr)
            value = el.attrib[attr]
            if qname.localname == "href":
                ref = value.strip()
                if ref.startswith("#"):
                    continue
                if local == "image":
                    checked = _check_data_image(ref)
                    if checked is not None:
                        embedded_count += 1
                        embedded_pixels += checked[1]
                        if embedded_count > SVG_MAX_EMBEDDED:
                            raise ConversionError(
                                f"Слишком много встроенных в SVG изображений "
                                f"(максимум {SVG_MAX_EMBEDDED})"
                            )
                        if embedded_pixels > SVG_EMBED_MAX_TOTAL_PIXELS:
                            raise ConversionError(
                                "Встроенные в SVG изображения суммарно слишком большие "
                                f"(максимум {SVG_EMBED_MAX_TOTAL_PIXELS // 1_000_000} Мп)"
                            )
                        el.attrib[attr] = checked[0]
                        continue
                del el.attrib[attr]
            elif qname.namespace == _XML_NS and qname.localname == "base":
                del el.attrib[attr]
            elif not _css_is_safe(value):
                del el.attrib[attr]
    for el in to_remove:
        parent = el.getparent()
        if parent is not None:
            # Сохраняем хвостовой текст, чтобы не склеивать соседний текст.
            if el.tail:
                prev = el.getprevious()
                if prev is not None:
                    prev.tail = (prev.tail or "") + el.tail
                else:
                    parent.text = (parent.text or "") + el.tail
            parent.remove(el)
    return etree.tostring(root, encoding="unicode"), root, embedded_pixels


def _parse_length(value: str | None) -> float | None:
    if not value:
        return None
    match = _LENGTH_RE.match(value)
    if not match:
        return None  # проценты и мусор — как будто размер не задан
    unit = match.group(2).lower()
    if unit not in _UNITS:
        return None
    number = float(match.group(1)) * _UNITS[unit]
    return number if math.isfinite(number) and number > 0 else None


def _svg_natural_size(root) -> tuple[float, float]:
    """Собственный размер SVG в px по width/height/viewBox (как в resvg)."""
    width = _parse_length(root.get("width"))
    height = _parse_length(root.get("height"))
    vb_w = vb_h = None
    view_box = root.get("viewBox")
    if view_box:
        parts = re.split(r"[\s,]+", view_box.strip())
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            nums = []
        if len(nums) == 4 and all(math.isfinite(n) for n in nums) and nums[2] > 0 and nums[3] > 0:
            vb_w, vb_h = nums[2], nums[3]
    if width and height:
        return width, height
    if vb_w and vb_h:
        if width:
            return width, width * vb_h / vb_w
        if height:
            return height * vb_w / vb_h, height
        return vb_w, vb_h
    if width or height:
        side = width or height
        return side, side
    return 100.0, 100.0


def _svg_render_size(root) -> tuple[int, int]:
    """Размер растра: длинная сторона в диапазоне [SVG_MIN_SIDE, SVG_MAX_SIDE]."""
    w, h = _svg_natural_size(root)
    longest = max(w, h)
    scale = 1.0
    if longest > SVG_MAX_SIDE:
        scale = SVG_MAX_SIDE / longest
    elif longest < SVG_MIN_SIDE:
        scale = SVG_MIN_SIDE / longest
    return (
        max(1, min(SVG_MAX_SIDE, round(w * scale))),
        max(1, min(SVG_MAX_SIDE, round(h * scale))),
    )


def _read_svg_bytes(path: Path) -> bytes:
    size = path.stat().st_size
    if size > SVG_MAX_BYTES:
        raise ConversionError(f"SVG-файл слишком большой (максимум {SVG_MAX_BYTES // 2**20} МБ)")
    data = path.read_bytes()
    if data[:2] == b"\x1f\x8b":  # .svgz — распаковка с ограничением размера
        try:
            dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
            unpacked = dec.decompress(data, SVG_MAX_UNPACKED)
        except zlib.error as exc:
            raise ConversionError(MSG_DAMAGED) from exc
        if dec.unconsumed_tail:
            raise ConversionError("SVG после распаковки слишком большой")
        data = unpacked
    return data


def _decode_svg(path: Path, cancel: CancelToken, stack: ExitStack) -> Image.Image:
    """Очистка SVG здесь, рендер — в дочернем процессе с таймаутом."""
    data = _read_svg_bytes(path)
    svg_text, root, embedded_pixels = _sanitize_svg_ex(data)
    del data
    width, height = _svg_render_size(root)
    _check_size(width, height)

    def on_header(w: int, h: int) -> None:
        # Холст плюс встроенные растры (каждый resvg распакует целиком).
        # Больше бюджета — урезается до бюджета (`PixelBudget.acquire`):
        # такая задача ждёт и дальше работает одна.
        _reserve(stack, w * h + embedded_pixels, cancel)

    rendered = _isolated.run_isolated(
        "svg",
        {"svg": svg_text, "width": width, "height": height},
        cancel=cancel,
        timeout=_isolated.SVG_TIMEOUT,
        on_header=on_header,
        check_pixels=lambda w, h: _check_size(w, h),
    )
    return _normalize(rendered)


# --- RAW ---------------------------------------------------------------------


def _decode_raw(path: Path, half_size: bool, cancel: CancelToken, stack: ExitStack) -> Image.Image:
    """Размеры и пиксели RAW — из дочернего процесса (LibRaw) с таймаутом."""
    if path.stat().st_size > RAW_MAX_BYTES:
        raise ConversionError(f"RAW-файл слишком большой (максимум {RAW_MAX_BYTES // 2**20} МБ)")

    def on_header(width: int, height: int, raw_width: int, raw_height: int) -> None:
        _check_size(width, height)
        _check_size(raw_width, raw_height, "Снимок")
        pixels = width * height
        if half_size:
            pixels = max(1, pixels // 4)
        _reserve(stack, pixels, cancel)

    return _isolated.run_isolated(
        "raw",
        {"path": str(path), "half_size": half_size},
        cancel=cancel,
        timeout=_isolated.RAW_TIMEOUT,
        on_header=on_header,
        check_pixels=lambda w, h: _check_size(w, h),
    )


# --- Источник ----------------------------------------------------------------


@dataclass
class _Animation:
    frames: list[Image.Image] = field(default_factory=list)
    durations: list[int] = field(default_factory=list)
    loop: int | None = None  # в терминах WEBP: 0 — бесконечно, n — проигрываний


class _Source:
    """Открытый входной файл: Pillow-изображение (лениво) или готовый растр.

    Декодер выбирается строго по коду формата `fmt` из detect. Резервации
    бюджета памяти живут до закрытия `stack` (кроме постраничных — см. `pages`).
    """

    def __init__(
        self,
        path: Path,
        fmt: str,
        stack: ExitStack,
        half_size_raw: bool,
        cancel: CancelToken,
    ) -> None:
        self.path = path
        self._stack = stack
        self._cancel = cancel
        self._pil: Image.Image | None = None  # ленивый файл Pillow
        self._ready: Image.Image | None = None  # растр SVG/RAW
        fmt = (fmt or "").lower()
        if fmt not in INPUT_FORMATS:
            raise ConversionError(f"Формат «{fmt}» не поддерживается конвертером изображений")
        try:
            if path.stat().st_size == 0:
                raise ConversionError(MSG_DAMAGED)
        except OSError as exc:
            raise ConversionError(f"Не удалось прочитать файл: {exc.strerror or exc}") from exc
        with _decoding():
            if fmt == "svg":
                self._ready = _decode_svg(path, cancel, stack)
            elif fmt == "raw":
                self._ready = _decode_raw(path, half_size_raw, cancel, stack)
            else:
                _ensure_heif()
                self._pil = stack.enter_context(
                    Image.open(path, formats=_PIL_FORMATS_BY_CODE[fmt])
                )
                _check_size(*self._pil.size)

    @property
    def pil_format(self) -> str | None:
        return self._pil.format if self._pil is not None else None

    def _frame_count(self) -> int:
        if self._pil is None:
            return 1
        with _decoding():
            return int(getattr(self._pil, "n_frames", 1) or 1)

    @property
    def is_animated(self) -> bool:
        """Анимация GIF/WEBP (только её сохраняем покадрово)."""
        return (
            self.pil_format in ("GIF", "WEBP")
            and bool(getattr(self._pil, "is_animated", False))
            and self._frame_count() > 1
        )

    @property
    def is_multipage_tiff(self) -> bool:
        return self.pil_format == "TIFF" and self._frame_count() > 1

    def first(self) -> Image.Image:
        """Первый кадр/страница, уже повёрнутый и нормализованный."""
        if self._ready is not None:
            return self._ready
        with _decoding():
            self._pil.seek(0)
            _check_size(*self._pil.size)
            # Резервация — до load(), живёт до закрытия источника (и сохранения).
            _reserve(self._stack, self._pil.size[0] * self._pil.size[1], self._cancel)
            self._pil.load()
            return _prepare_frame(self._pil)

    def pages(self, cancel: CancelToken) -> Iterator[Image.Image]:
        """Все страницы многостраничного TIFF (или единственное изображение).

        Бюджет памяти резервируется постранично: страница держит резервацию,
        пока потребитель обрабатывает её, и отдаёт перед следующей.
        """
        if not self.is_multipage_tiff:
            yield self.first()
            return
        count = self._frame_count()
        if count > MAX_PAGES:
            raise ConversionError(f"Слишком много страниц: {count}, максимум {MAX_PAGES}")
        total = 0
        for index in range(count):
            cancel.raise_if_cancelled()
            with _decoding():
                self._pil.seek(index)
                width, height = self._pil.size
                _check_size(width, height, what="Страница")
            total += width * height
            if total > MAX_TOTAL_PIXELS:
                raise ConversionError(
                    f"Слишком много данных: больше {MAX_TOTAL_PIXELS // 1_000_000} Мп "
                    f"во всех страницах (превышено на странице {index + 1} из {count})"
                )
            with budget.IMAGE_BUDGET.reserve(width * height, cancel):
                with _decoding():
                    self._pil.load()
                    page = _prepare_frame(self._pil)
                yield page
                del page

    def animation(self, cancel: CancelToken, progress: ProgressCallback, share: float) -> _Animation:
        """Все кадры анимации с длительностями и числом повторов.

        Все кадры одновременно в памяти, поэтому бюджет резервируется сразу
        на все (до закрытия источника, т.е. и на время сохранения).
        """
        pil = self._pil
        assert pil is not None
        count = self._frame_count()
        if count > MAX_FRAMES:
            raise ConversionError(f"Слишком много кадров: {count}, максимум {MAX_FRAMES}")
        total = pil.size[0] * pil.size[1] * count
        if total > MAX_ANIMATION_PIXELS:
            raise ConversionError(
                f"Анимация слишком большая: {total / 1_000_000:.0f} Мп во всех кадрах, "
                f"максимум {MAX_ANIMATION_PIXELS // 1_000_000} Мп"
            )
        _reserve(self._stack, total, cancel)
        anim = _Animation()
        decoded = 0
        with _decoding():
            pil.seek(0)
            raw_loop = pil.info.get("loop")
            if pil.format == "GIF":
                # GIF: нет блока — один раз; 0 — бесконечно; n — n повторов.
                anim.loop = 1 if raw_loop is None else (0 if raw_loop == 0 else raw_loop + 1)
            else:
                anim.loop = int(raw_loop) if raw_loop is not None else 0
            for index in range(count):
                cancel.raise_if_cancelled()
                pil.seek(index)
                _check_size(*pil.size, what="Кадр")
                # Размер кадра проверяем и фактический (а не только по первому).
                decoded += pil.size[0] * pil.size[1]
                if decoded > MAX_ANIMATION_PIXELS:
                    raise ConversionError(
                        f"Анимация слишком большая: больше "
                        f"{MAX_ANIMATION_PIXELS // 1_000_000} Мп во всех кадрах"
                    )
                pil.load()
                duration = pil.info.get("duration")
                anim.durations.append(int(duration) if duration is not None else 100)
                frame = pil.convert("RGBA")
                frame.info = {}
                anim.frames.append(frame)
                progress(share * (index + 1) / count)
        return anim


# --- Сохранение --------------------------------------------------------------


def _save_raster(img: Image.Image, dst: Path, target_id: str, preset: Preset) -> None:
    """Сохраняет один кадр в растровый формат `target_id`."""
    if target_id in _QUALITY and preset == Preset.SMALL:
        img = _shrink(img, _SMALL_MAX_SIDE)
    with _encoding():
        if target_id == "jpg":
            _flatten(img).save(
                dst, "JPEG", quality=_QUALITY["jpg"][preset], optimize=True, progressive=True
            )
        elif target_id == "png":
            img.save(dst, "PNG", compress_level=6)
        elif target_id == "webp":
            out = img if img.mode in ("RGB", "RGBA") else img.convert("RGB")
            out.save(dst, "WEBP", quality=_QUALITY["webp"][preset], method=4)
        elif target_id == "avif":
            out = img if img.mode in ("RGB", "RGBA") else img.convert("RGB")
            out.save(dst, "AVIF", quality=_QUALITY["avif"][preset])
        elif target_id == "gif":
            if img.mode == "RGBA":
                img.save(dst, "GIF")  # Pillow сам строит палитру с прозрачностью
            else:
                img.convert("RGB").quantize(256).save(dst, "GIF")
        elif target_id == "bmp":
            _flatten(img).save(dst, "BMP")
        elif target_id == "tiff":
            img.save(dst, "TIFF", compression="tiff_lzw")
        elif target_id == "ico":
            _ico_canvas(img).save(dst, "ICO", sizes=_ICO_SIZES)
        else:
            raise ConversionError(f"Неизвестный вариант конвертации: {target_id}")


def _ico_canvas(img: Image.Image) -> Image.Image:
    """Квадрат 256×256 с прозрачными полями, картинка вписана по центру."""
    rgba = img.convert("RGBA")
    scale = _ICO_CANVAS / max(rgba.size)
    size = (max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale)))
    resized = rgba.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (_ICO_CANVAS, _ICO_CANVAS), (0, 0, 0, 0))
    canvas.paste(resized, ((_ICO_CANVAS - size[0]) // 2, (_ICO_CANVAS - size[1]) // 2))
    return canvas


def _save_animation(anim: _Animation, dst: Path, target_id: str, preset: Preset) -> None:
    frames = anim.frames
    if target_id == "webp" and preset == Preset.SMALL:
        frames = [_shrink(f, _SMALL_MAX_SIDE) for f in frames]
    with _encoding():
        if target_id == "webp":
            frames[0].save(
                dst,
                "WEBP",
                save_all=True,
                append_images=frames[1:],
                duration=anim.durations,
                loop=anim.loop if anim.loop is not None else 0,
                quality=_QUALITY["webp"][preset],
                method=4,
            )
        elif target_id == "gif":
            has_alpha = any(f.getextrema()[3][0] < 255 for f in frames)
            if not has_alpha:
                frames = [f.convert("RGB") for f in frames]
            kwargs: dict = {}
            # WEBP loop: 0 — бесконечно, 1 — один раз (в GIF без блока), n — n-1 повторов.
            if anim.loop is not None and anim.loop != 1:
                kwargs["loop"] = 0 if anim.loop == 0 else anim.loop - 1
            frames[0].save(
                dst,
                "GIF",
                save_all=True,
                append_images=frames[1:],
                duration=anim.durations,
                disposal=2 if has_alpha else 0,
                **kwargs,
            )
        else:
            raise ConversionError(f"Анимация не поддерживается для {target_id}")


class _PdfBuilder:
    """Собирает PDF: страница на изображение, картинка — JPEG без перекодирования."""

    def __init__(self, preset: Preset) -> None:
        from fpdf import FPDF

        self._preset = preset
        self._pdf = FPDF(unit="pt")
        self._pdf.set_auto_page_break(False)
        self._pdf.set_margins(0, 0, 0)
        self.pages = 0

    def add(self, img: Image.Image) -> None:
        img = _shrink(_flatten(img), _PDF_MAX_SIDE[self._preset])
        buf = io.BytesIO()
        with _encoding():
            img.save(buf, "JPEG", quality=_PDF_JPEG_QUALITY[self._preset], optimize=True)
        w_pt = img.width * _PDF_PT_PER_PX
        h_pt = img.height * _PDF_PT_PER_PX
        longest = max(w_pt, h_pt)
        if longest > _PDF_MAX_PAGE_PT:
            factor = _PDF_MAX_PAGE_PT / longest
            w_pt, h_pt = w_pt * factor, h_pt * factor
        self._pdf.add_page(format=(w_pt, h_pt))
        buf.seek(0)
        self._pdf.image(buf, x=0, y=0, w=w_pt, h=h_pt)
        self.pages += 1

    def save(self, dst: Path) -> None:
        if not self.pages:
            raise ConversionError("Нет изображений для PDF")
        with _encoding():
            self._pdf.output(str(dst))


# --- Конвертер ---------------------------------------------------------------


class ImageConverter:
    """Конвертер изображений (см. докстринг модуля)."""

    category = Category.IMAGE
    # Канонический порядок вариантов (см. TARGET_ORDER).
    target_order = TARGET_ORDER

    def targets(self, fmt: str) -> list[Target]:
        fmt = (fmt or "").lower()
        if fmt not in INPUT_FORMATS:
            return []
        is_raw = fmt == "raw"
        result = [
            Target(
                id=tid, label=_target_label(tid), extension=ext, uses_preset=uses,
                is_long=is_raw,
            )
            for tid, ext, uses in _OUTPUTS
            if tid != fmt
        ]
        result.append(
            Target(
                id=TARGET_PDF_EACH,
                label=_target_label(TARGET_PDF_EACH),
                extension="pdf",
                uses_preset=True,
                is_long=is_raw,
            )
        )
        result.append(
            Target(
                id=TARGET_PDF_SINGLE,
                label=_target_label(TARGET_PDF_SINGLE),
                extension="pdf",
                uses_preset=True,
                many_to_one=True,
                is_long=True,
            )
        )
        return result

    def convert(
        self,
        sources: list[Path],
        dst: Path,
        target: Target,
        preset: Preset,
        progress: ProgressCallback,
        cancel: CancelToken,
        *,
        formats: list[str],
    ) -> None:
        cancel.raise_if_cancelled()
        if not sources:
            raise ConversionError("Нет файлов для конвертации")
        sources = [Path(s) for s in sources]
        formats = [str(f or "").lower() for f in (formats or [])]
        if len(formats) != len(sources):
            raise ConversionError("Внутренняя ошибка: не переданы форматы исходных файлов")
        dst = Path(dst)
        if target.id == TARGET_PDF_SINGLE:
            self._to_single_pdf(sources, formats, dst, preset, progress, cancel)
        elif len(sources) != 1:
            raise ConversionError("Этот вариант обрабатывает один файл за раз")
        elif target.id == TARGET_PDF_EACH:
            self._to_pdf(sources[0], formats[0], dst, preset, progress, cancel)
        else:
            self._to_raster(sources[0], formats[0], dst, target.id, preset, progress, cancel)
        progress(1.0)

    # -- варианты --

    def _to_raster(
        self,
        src: Path,
        fmt: str,
        dst: Path,
        target_id: str,
        preset: Preset,
        progress: ProgressCallback,
        cancel: CancelToken,
    ) -> None:
        half = preset == Preset.SMALL and target_id in _QUALITY
        # Резервация бюджета живёт до конца `with`, т.е. и на время сохранения.
        with ExitStack() as stack:
            source = _Source(src, fmt, stack, half_size_raw=half, cancel=cancel)
            cancel.raise_if_cancelled()
            if target_id in ("gif", "webp") and source.is_animated:
                anim = source.animation(cancel, progress, share=0.8)
                cancel.raise_if_cancelled()
                _save_animation(anim, dst, target_id, preset)
                return
            image = source.first()
            progress(0.5)
            cancel.raise_if_cancelled()
            _save_raster(image, dst, target_id, preset)

    def _to_pdf(
        self,
        src: Path,
        fmt: str,
        dst: Path,
        preset: Preset,
        progress: ProgressCallback,
        cancel: CancelToken,
    ) -> None:
        builder = _PdfBuilder(preset)
        with ExitStack() as stack:
            source = _Source(src, fmt, stack, half_size_raw=preset == Preset.SMALL, cancel=cancel)
            for page in source.pages(cancel):
                builder.add(page)
        cancel.raise_if_cancelled()
        progress(0.9)
        builder.save(dst)

    def _to_single_pdf(
        self,
        sources: list[Path],
        formats: list[str],
        dst: Path,
        preset: Preset,
        progress: ProgressCallback,
        cancel: CancelToken,
    ) -> None:
        builder = _PdfBuilder(preset)
        total = len(sources)
        for index, (src, fmt) in enumerate(zip(sources, formats)):
            cancel.raise_if_cancelled()
            with ExitStack() as stack:
                try:
                    source = _Source(
                        src, fmt, stack, half_size_raw=preset == Preset.SMALL, cancel=cancel
                    )
                    for page in source.pages(cancel):
                        builder.add(page)
                except ConversionError as exc:
                    raise ConversionError(f"{src.name}: {exc}") from exc
            progress(0.95 * (index + 1) / total)
            cancel.raise_if_cancelled()
        builder.save(dst)
