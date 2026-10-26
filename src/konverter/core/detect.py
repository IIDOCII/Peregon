"""Определение формата файла по содержимому (а не по расширению), Ф3–Ф4.

Входные файлы считаются враждебными, поэтому:
- из файла читается ограниченный объём: начало (`HEAD_SIZE`), хвост для
  поиска конца ZIP-архива, ограниченный центральный каталог ZIP, несколько
  служебных секторов OLE; magika сама читает только небольшие блоки
  в начале и в конце файла;
- архивы НЕ распаковываются (кроме первых сотен байт gzip — чтобы увидеть
  заголовок tar), XML не разбирается парсером — корневой тег ищется вручную
  в первых килобайтах;
- любая ошибка разбора внутри определения (битые структуры) не роняет
  программу: файл считается неопознанным (`None`), подробности — в лог.

Порядок определения:
1. собственные сигнатуры (быстро и предсказуемо; покрывают форматы, которых
   не знает magika: HEIC, AVIF, RAW, MOBI, FB2, AMR, MPEG-TS, WMA/WMV,
   а также ZIP- и OLE-контейнеры офисных документов);
2. magika (нейросеть Google) — в основном для текстовых форматов
   (HTML, Markdown, CSV, просто текст) и как запасной вариант;
3. puremagic (таблица сигнатур) — последний запасной вариант для бинарных
   форматов.

Ошибки чтения файла (нет файла, нет прав, это папка) — исключение
`UnreadableFileError` с понятным пользователю текстом; файл, формат которого
не поддерживается или не опознан, и пустой файл — результат `None`.

Функция `detect` потокобезопасна: модель magika загружается один раз
(под блокировкой), а сам вызов модели (onnxruntime `InferenceSession.run`)
по документации onnxruntime безопасен при вызове из нескольких потоков.

Известные ограничения (честно):
- WMA/WMV: различаются по наличию в заголовке ASF объекта видеопотока
  (GUID «Video Media»); у ASF-файлов с нестандартной структурой результат
  может быть неточным.
- MP4 только со звуком (без бренда `M4A `) отличается от M4A лишь по
  расширению (.m4a/.m4b → m4a); видеопоток внутри не ищется.
- RAW на основе TIFF (NEF, ARW, PEF, SRW и т.п.) опознаётся по тегу Make
  в первом IFD; обычный TIFF, снятый той же камерой и названный .tif/.tiff,
  считается TIFF. Нестандартные RAW (CRW, X3F новых версий и пр.) могут
  не опознаваться.
- MPEG-TS опознаётся по байту синхронизации 0x47 в начале нескольких пакетов
  подряд (188 или 192 байта); теоретически возможны ложные срабатывания
  на специально подобранных данных.
- md/txt/csv по содержимому часто неразличимы: если содержимое — текст,
  учитывается расширение.
- OLE (doc/xls/ppt) различаются по именам потоков; если каталог OLE
  прочитать не удалось — по расширению среди этих трёх.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import struct
import threading
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from konverter.converters.base import Category, DetectedFormat
from konverter.strings import tr

log = logging.getLogger(__name__)

# Сколько байт читать из начала файла для сигнатур.
HEAD_SIZE = 1024 * 1024
# Сколько байт с конца читать для поиска записи конца ZIP (EOCD + комментарий).
ZIP_TAIL_SIZE = 65535 + 22
# Предельный размер центрального каталога ZIP, который мы готовы прочитать.
MAX_ZIP_CENTRAL_DIR = 4 * 1024 * 1024
# Предельное число записей в ZIP для анализа содержимого.
MAX_ZIP_ENTRIES = 20000
# Предельный размер файла `mimetype` внутри ZIP.
MAX_ZIP_MIMETYPE = 256
# Предельное число секторов каталога OLE, которые мы читаем.
MAX_OLE_DIR_SECTORS = 128


class UnreadableFileError(Exception):
    """Файл нельзя прочитать: его нет, нет прав, это не файл и т.п.

    `reason` — текст для пользователя (на текущем языке интерфейса).
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class FormatInfo:
    """Сведения о поддерживаемом формате."""

    category: Category
    label: str
    extensions: tuple[str, ...]


# Расширения RAW-файлов камер.
RAW_EXTENSIONS = (
    "cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "dng", "orf",
    "rw2", "raf", "pef", "srw", "raw", "rwl", "3fr", "erf", "kdc", "mrw",
    "x3f", "iiq", "mef", "mos",
)

_I = Category.IMAGE
_V = Category.VIDEO
_A = Category.AUDIO

# Все поддерживаемые форматы: код → категория, подпись, допустимые расширения.
FORMATS: dict[str, FormatInfo] = {
    # Изображения
    "jpg": FormatInfo(_I, "JPEG", ("jpg", "jpeg", "jpe", "jfif")),
    "png": FormatInfo(_I, "PNG", ("png",)),
    "webp": FormatInfo(_I, "WebP", ("webp",)),
    "heic": FormatInfo(_I, "HEIC", ("heic", "heif", "hif")),
    "avif": FormatInfo(_I, "AVIF", ("avif",)),
    "gif": FormatInfo(_I, "GIF", ("gif",)),
    "bmp": FormatInfo(_I, "BMP", ("bmp", "dib")),
    "tiff": FormatInfo(_I, "TIFF", ("tif", "tiff")),
    "ico": FormatInfo(_I, "ICO", ("ico",)),
    "svg": FormatInfo(_I, "SVG", ("svg",)),
    "raw": FormatInfo(_I, "RAW", RAW_EXTENSIONS),
    # Видео
    "mp4": FormatInfo(_V, "Видео MP4", ("mp4", "m4v", "f4v")),
    "mov": FormatInfo(_V, "Видео MOV", ("mov", "qt")),
    "mkv": FormatInfo(_V, "Видео MKV", ("mkv",)),
    "avi": FormatInfo(_V, "Видео AVI", ("avi",)),
    "webm": FormatInfo(_V, "Видео WebM", ("webm",)),
    "wmv": FormatInfo(_V, "Видео WMV", ("wmv", "asf")),
    "flv": FormatInfo(_V, "Видео FLV", ("flv",)),
    "3gp": FormatInfo(_V, "Видео 3GP", ("3gp", "3gpp", "3g2")),
    "mts": FormatInfo(_V, "Видео MPEG-TS", ("mts", "m2ts", "m2t", "ts")),
    # Аудио
    "mp3": FormatInfo(_A, "Аудио MP3", ("mp3",)),
    "wav": FormatInfo(_A, "Аудио WAV", ("wav", "wave")),
    "flac": FormatInfo(_A, "Аудио FLAC", ("flac",)),
    "aac": FormatInfo(_A, "Аудио AAC", ("aac", "adts")),
    "m4a": FormatInfo(_A, "Аудио M4A", ("m4a", "m4b", "m4p")),
    "ogg": FormatInfo(_A, "Аудио OGG", ("ogg", "oga")),
    "opus": FormatInfo(_A, "Аудио Opus", ("opus", "ogg")),
    "wma": FormatInfo(_A, "Аудио WMA", ("wma", "asf")),
    "amr": FormatInfo(_A, "Аудио AMR", ("amr",)),
    # PDF
    "pdf": FormatInfo(Category.PDF, "PDF", ("pdf",)),
    # Документы
    "docx": FormatInfo(Category.DOCUMENT, "Документ Word (DOCX)", ("docx",)),
    "doc": FormatInfo(Category.DOCUMENT, "Документ Word 97–2003 (DOC)", ("doc",)),
    "odt": FormatInfo(Category.DOCUMENT, "Документ OpenDocument (ODT)", ("odt",)),
    "rtf": FormatInfo(Category.DOCUMENT, "Документ RTF", ("rtf",)),
    "txt": FormatInfo(Category.DOCUMENT, "Текст", ("txt", "text", "log")),
    "html": FormatInfo(Category.DOCUMENT, "Веб-страница HTML", ("html", "htm", "xhtml")),
    "md": FormatInfo(Category.DOCUMENT, "Markdown", ("md", "markdown")),
    # Таблицы
    "xlsx": FormatInfo(Category.SPREADSHEET, "Таблица Excel (XLSX)", ("xlsx",)),
    "xls": FormatInfo(Category.SPREADSHEET, "Таблица Excel 97–2003 (XLS)", ("xls",)),
    "ods": FormatInfo(Category.SPREADSHEET, "Таблица OpenDocument (ODS)", ("ods",)),
    "csv": FormatInfo(Category.SPREADSHEET, "Таблица CSV", ("csv", "tsv")),
    # Презентации
    "pptx": FormatInfo(Category.PRESENTATION, "Презентация PowerPoint (PPTX)", ("pptx",)),
    "ppt": FormatInfo(
        Category.PRESENTATION, "Презентация PowerPoint 97–2003 (PPT)", ("ppt", "pps")
    ),
    "odp": FormatInfo(Category.PRESENTATION, "Презентация OpenDocument (ODP)", ("odp",)),
    # Книги
    "epub": FormatInfo(Category.BOOK, "Книга EPUB", ("epub",)),
    "fb2": FormatInfo(Category.BOOK, "Книга FB2", ("fb2",)),
    "mobi": FormatInfo(Category.BOOK, "Книга MOBI", ("mobi", "azw", "azw3", "prc")),
    # Архивы
    "zip": FormatInfo(Category.ARCHIVE, "Архив ZIP", ("zip",)),
    "7z": FormatInfo(Category.ARCHIVE, "Архив 7z", ("7z",)),
    "rar": FormatInfo(Category.ARCHIVE, "Архив RAR", ("rar",)),
    "tar_gz": FormatInfo(Category.ARCHIVE, "Архив TAR.GZ", ("tgz", "tar.gz")),
}

# Результат внутренних проверок: (код формата, подпись) или None.
_Result = tuple[str, str] | None

# Особый результат сигнатур: формат опознан точно, но не поддерживается
# (MSI/MSG в контейнере OLE, видео Theora, просто .gz). Дальше не гадаем,
# чтобы magika не выдала «похожий» формат.
_UNSUPPORTED: tuple[str, str] = ("", "")


def _res(code: str, label: str | None = None) -> tuple[str, str]:
    """Результат с подписью по умолчанию из FORMATS."""
    return code, label if label is not None else FORMATS[code].label


# --- Публичные функции ------------------------------------------------------


def file_extension(name: str) -> str:
    """Расширение имени файла в нижнем регистре без точки ("tar.gz" — особый случай)."""
    lower = name.lower()
    if lower.endswith(".tar.gz"):
        return "tar.gz"
    _, dot, ext = lower.rpartition(".")
    return ext if dot else ""


def extension_matches(name: str, code: str) -> bool:
    """Подходит ли расширение файла `name` формату `code`."""
    info = FORMATS.get(code)
    if info is None:
        return False
    return file_extension(name) in info.extensions


def warm_up() -> bool:
    """Заранее загружает модель magika (например, при старте в фоне).

    Возвращает True, если модель доступна.
    """
    return _get_magika() is not None


def detect(path: Path) -> DetectedFormat | None:
    """Определяет формат файла по содержимому.

    Возвращает `DetectedFormat` или None, если формат не поддерживается,
    не опознан или файл пустой. Если файл нельзя прочитать (нет файла,
    нет прав, это папка), бросает `UnreadableFileError`.
    """
    path = Path(path)
    try:
        st = os.stat(path)  # ссылки разыменовываются: пользователь бросил именно её
    except OSError as exc:
        raise UnreadableFileError(path, _os_error_reason(exc)) from exc
    if not stat.S_ISREG(st.st_mode):
        raise UnreadableFileError(path, tr("err_not_a_file"))
    if st.st_size == 0:
        return None

    try:
        with open(path, "rb") as f:
            result = _detect_stream(f, st.st_size, path.name)
    except OSError as exc:
        raise UnreadableFileError(path, _os_error_reason(exc)) from exc
    except Exception:  # noqa: BLE001 — враждебный файл не должен ронять программу
        log.exception("Сбой при определении формата %s", path)
        return None

    if result is None:
        return None
    code, label = result
    return DetectedFormat(
        fmt=code,
        category=FORMATS[code].category,
        label=label,
        extension_mismatch=not extension_matches(path.name, code),
    )


def _os_error_reason(exc: OSError) -> str:
    """Понятный пользователю текст ошибки файловой системы."""
    if isinstance(exc, FileNotFoundError):
        return tr("err_file_not_found")
    if isinstance(exc, PermissionError):
        return tr("err_file_no_access")
    if isinstance(exc, IsADirectoryError):
        return tr("err_not_a_file")
    return tr("err_file_read").format(error=exc.strerror or exc)


# --- Основной конвейер ------------------------------------------------------


@dataclass
class _Ctx:
    """Что известно о файле во время определения."""

    f: BinaryIO
    size: int
    head: bytes
    ext: str


def _detect_stream(f: BinaryIO, size: int, name: str) -> _Result:
    ctx = _Ctx(f=f, size=size, head=f.read(HEAD_SIZE), ext=file_extension(name))
    if not ctx.head:
        return None

    result = _by_signature(ctx)
    if result is _UNSUPPORTED:
        return None
    if result is not None:
        return result

    result = _by_magika(ctx)
    if result is not None:
        return result

    if _looks_like_text(ctx.head):
        return _text_format(None, ctx)

    return _by_puremagic(ctx)


def _by_signature(ctx: _Ctx) -> _Result:
    """Собственные сигнатуры. Порядок важен: от более точных к менее точным."""
    h = ctx.head

    if b"%PDF-" in h[:1024]:
        return _res("pdf")
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return _res("png")
    if h.startswith(b"\xff\xd8\xff"):
        return _res("jpg")
    if h.startswith((b"GIF87a", b"GIF89a")):
        return _res("gif")
    if h[:4] in (b"RIFF", b"RF64") and len(h) >= 12:
        kind = h[8:12]
        if kind == b"WEBP":
            return _res("webp")
        if kind == b"WAVE":
            return _res("wav")
        if kind == b"AVI ":
            return _res("avi")
    if h.startswith(b"BM") and len(h) >= 26:
        dib_size = struct.unpack_from("<I", h, 14)[0]
        if dib_size in (12, 16, 40, 52, 56, 64, 108, 124):
            return _res("bmp")
    if h[:4] in (b"IIRO", b"IIRS", b"MMOR"):
        return _raw("Olympus ORF")
    if h[:4] == b"IIU\x00":
        return _raw("Panasonic RW2")
    if h[:4] in (b"II*\x00", b"MM\x00*"):
        return _tiff_or_raw(ctx)
    if h[:4] in (b"II+\x00", b"MM\x00+"):
        return _res("tiff", "TIFF (BigTIFF)")
    if h.startswith(b"FUJIFILMCCD-RAW"):
        return _raw("Fujifilm RAF")
    if h.startswith(b"\x00MRM"):
        return _raw("Minolta MRW")
    if h.startswith(b"FOVb"):
        return _raw("Sigma X3F")
    if _is_ico(h):
        return _res("ico")
    if len(h) >= 12 and h[4:8] == b"ftyp":
        return _isobmff(ctx)
    if h.startswith(b"\x1a\x45\xdf\xa3"):
        return _ebml(h)
    if h.startswith(b"FLV\x01"):
        return _res("flv")
    if h.startswith(_ASF_HEADER_GUID):
        return _asf(h)
    if h.startswith(b"OggS"):
        return _ogg(h)
    if h.startswith(b"fLaC"):
        return _res("flac")
    if h.startswith((b"#!AMR\n", b"#!AMR-WB\n", b"#!AMR_MC1.0\n", b"#!AMR-WB_MC1.0\n")):
        return _res("amr")
    if _is_mpegts(h):
        return _res("mts")
    if h.startswith(b"ID3"):
        return _after_id3(h)
    if _is_adts_stream(h, 0):
        return _res("aac")
    if _is_mp3_stream(h, 0):
        return _res("mp3")
    if h[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return _zip(ctx)
    if h.startswith(b"7z\xbc\xaf\x27\x1c"):
        return _res("7z")
    if h.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return _res("rar")
    if h.startswith(b"\x1f\x8b"):
        return _gzip(h)
    if h.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _ole(ctx)
    if h[60:68] == b"BOOKMOBI":
        return _res("mobi")
    if h.lstrip(b"\xef\xbb\xbf").startswith(b"{\\rtf"):
        return _res("rtf")

    root = _xml_root_name(h)
    if root == "svg":
        return _res("svg")
    if root == "FictionBook":
        return _res("fb2")
    return None


# --- Изображения и RAW ------------------------------------------------------


def _raw(maker_label: str) -> tuple[str, str]:
    return "raw", f"RAW ({maker_label})"


# Производители (по тегу Make), чьи RAW-файлы основаны на TIFF.
_RAW_MAKERS = {
    "NIKON": "Nikon NEF",
    "SONY": "Sony ARW",
    "PENTAX": "Pentax PEF",
    "RICOH": "Pentax PEF",
    "SAMSUNG": "Samsung SRW",
    "OLYMPUS": "Olympus ORF",
    "OM DIGITAL": "OM System ORF",
    "CANON": "Canon",
    "LEICA": "Leica",
    "HASSELBLAD": "Hasselblad",
    "KODAK": "Kodak",
    "EASTMAN KODAK": "Kodak",
    "PHASE ONE": "Phase One",
    "MAMIYA": "Mamiya",
    "MINOLTA": "Minolta",
    "KONICA MINOLTA": "Minolta",
    "EPSON": "Epson",
}
# Подписи без конкретного формата: к ним добавляется расширение файла.
_GENERIC_MAKER_LABELS = {
    "Canon", "Leica", "Hasselblad", "Kodak", "Phase One", "Mamiya", "Minolta", "Epson",
}


def _tiff_or_raw(ctx: _Ctx) -> _Result:
    """TIFF или RAW на основе TIFF (CR2, NEF, ARW, DNG, PEF, SRW…)."""
    h = ctx.head
    if h[8:11] == b"CR\x02":
        return _raw("Canon CR2")

    make, is_dng = _tiff_ifd0_info(h)
    if is_dng:
        return _raw("DNG")
    maker_label = None
    upper = make.upper()
    for key, label in _RAW_MAKERS.items():
        if upper.startswith(key):
            maker_label = label
            break
    if maker_label is not None:
        if ctx.ext in ("tif", "tiff"):
            return _res("tiff")  # обычный TIFF, снятый камерой этой марки
        if ctx.ext in RAW_EXTENSIONS and maker_label in _GENERIC_MAKER_LABELS:
            return _raw(f"{maker_label} {ctx.ext.upper()}")
        return _raw(maker_label)
    return _res("tiff")


def _tiff_ifd0_info(h: bytes) -> tuple[str, bool]:
    """Читает из первого IFD тег Make (271) и наличие DNGVersion (50706)."""
    endian = "<" if h[:2] == b"II" else ">"
    make = ""
    is_dng = False
    try:
        offset = struct.unpack_from(endian + "I", h, 4)[0]
        count = struct.unpack_from(endian + "H", h, offset)[0]
        count = min(count, 1024)
        for i in range(count):
            pos = offset + 2 + i * 12
            if pos + 12 > len(h):
                break
            tag, typ, n = struct.unpack_from(endian + "HHI", h, pos)
            if tag == 50706:
                is_dng = True
            elif tag == 271 and typ == 2 and 0 < n <= 256:
                if n <= 4:
                    raw = h[pos + 8: pos + 8 + n]
                else:
                    value_off = struct.unpack_from(endian + "I", h, pos + 8)[0]
                    raw = h[value_off: value_off + n]
                make = raw.split(b"\x00", 1)[0].decode("ascii", "ignore").strip()
    except struct.error:
        pass
    return make, is_dng


def _is_ico(h: bytes) -> bool:
    """Значок Windows: заголовок 00 00 01 00, разумное число записей."""
    if len(h) < 22 or h[:4] != b"\x00\x00\x01\x00":
        return False
    count = struct.unpack_from("<H", h, 4)[0]
    if not 1 <= count <= 256 or len(h) < 6 + 16 * count:
        return False
    for i in range(min(count, 16)):
        entry = 6 + 16 * i
        reserved = h[entry + 3]
        size, offset = struct.unpack_from("<II", h, entry + 8)
        if reserved != 0 or size == 0 or offset < 6 + 16 * count:
            return False
    return True


# --- ISO-BMFF (MP4, MOV, HEIC, AVIF, CR3, M4A, 3GP) --------------------------

_HEIC_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}
_M4A_BRANDS = {b"M4A ", b"M4B ", b"M4P "}
_MP4_BRANDS = {
    b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"iso7", b"iso8", b"iso9",
    b"mp41", b"mp42", b"mp71", b"avc1", b"dash", b"M4V ", b"M4VH", b"M4VP",
    b"mmp4", b"MSNV", b"NDAS", b"NDSC", b"f4v ", b"kddi", b"XAVC", b"mp4v",
}


def _isobmff(ctx: _Ctx) -> _Result:
    h = ctx.head
    box_size = struct.unpack_from(">I", h, 0)[0]
    major = h[8:12]
    end = min(max(box_size, 16), 4096, len(h))
    compatible = {h[i:i + 4] for i in range(16, end - 3, 4)}
    brands = {major} | compatible

    if b"crx " in brands:
        return _raw("Canon CR3")
    if major in _AVIF_BRANDS:
        return _res("avif")
    if major in _HEIC_BRANDS:
        if major in (b"mif1", b"msf1") and brands & _AVIF_BRANDS:
            return _res("avif")
        return _res("heic")
    if major in _M4A_BRANDS:
        return _res("m4a")
    if major == b"qt  ":
        return _res("mov")
    if major.startswith((b"3gp", b"3g2", b"3ge", b"3gg", b"3gs")):
        return _res("3gp")
    if major in _MP4_BRANDS or compatible & {b"isom", b"iso2", b"mp41", b"mp42"}:
        # MP4 только со звуком отличаем от M4A лишь по расширению (см. ограничения).
        if ctx.ext in FORMATS["m4a"].extensions:
            return _res("m4a")
        return _res("mp4")
    if brands & _AVIF_BRANDS:
        return _res("avif")
    if brands & _HEIC_BRANDS:
        return _res("heic")
    return None


# --- Прочие видео- и аудиоконтейнеры ---------------------------------------


def _ebml(h: bytes) -> _Result:
    """Matroska/WebM: тип документа записан в заголовке EBML."""
    header = h[:4096]
    if b"\x42\x82\x84webm" in header or b"webm" in header[:64]:
        return _res("webm")
    if b"matroska" in header:
        return _res("mkv")
    return None


_ASF_HEADER_GUID = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
# GUID типов потоков ASF (в порядке байтов файла).
_ASF_VIDEO_MEDIA = bytes.fromhex("c0ef19bc4d5bcf11a8fd00805f5c442b")
_ASF_AUDIO_MEDIA = bytes.fromhex("409e69f84d5bcf11a8fd00805f5c442b")


def _asf(h: bytes) -> _Result:
    """ASF: WMV, если в заголовке есть видеопоток, иначе WMA (если есть звук)."""
    header_size = struct.unpack_from("<Q", h, 16)[0] if len(h) >= 24 else len(h)
    header = h[: min(header_size, len(h))]
    if _ASF_VIDEO_MEDIA in header:
        return _res("wmv")
    if _ASF_AUDIO_MEDIA in header:
        return _res("wma")
    return None


def _ogg(h: bytes) -> _Result:
    """OGG: кодек виден в первой странице."""
    first_page = h[:512]
    if b"OpusHead" in first_page:
        return _res("opus")
    if b"\x80theora" in first_page:
        return _UNSUPPORTED  # видео Theora — не поддерживаем
    return _res("ogg")


def _is_mpegts(h: bytes) -> bool:
    """MPEG-TS (188 байт на пакет) или M2TS/MTS (192 байта, 4 байта метки времени)."""
    for packet, start in ((188, 0), (192, 4)):
        available = (len(h) - start) // packet
        n = min(available, 8)
        if n >= 3 and all(h[start + i * packet] == 0x47 for i in range(n)):
            return True
    return False


_MP3_BITRATES_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_MP3_BITRATES_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
_MP3_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame_length(h: bytes, i: int) -> int | None:
    """Длина кадра MPEG Audio Layer III, начинающегося с `i`, или None."""
    if i + 4 > len(h) or h[i] != 0xFF or (h[i + 1] & 0xE0) != 0xE0:
        return None
    version = (h[i + 1] >> 3) & 0x03  # 3 — MPEG1, 2 — MPEG2, 0 — MPEG2.5
    layer = (h[i + 1] >> 1) & 0x03  # 1 — Layer III
    if version == 1 or layer != 1:
        return None
    bitrate_idx = h[i + 2] >> 4
    rate_idx = (h[i + 2] >> 2) & 0x03
    if bitrate_idx in (0, 15) or rate_idx == 3:
        return None
    padding = (h[i + 2] >> 1) & 0x01
    rate = _MP3_SAMPLE_RATES[version][rate_idx]
    if version == 3:
        return 144 * _MP3_BITRATES_V1[bitrate_idx] * 1000 // rate + padding
    return 72 * _MP3_BITRATES_V2[bitrate_idx] * 1000 // rate + padding


def _is_mp3_stream(h: bytes, start: int) -> bool:
    """Три подряд корректных кадра MP3 (или кадры до конца короткого файла)."""
    pos = start
    for n in range(3):
        length = _mp3_frame_length(h, pos)
        if length is None:
            return n >= 1 and pos >= len(h)  # закончились данные после целого кадра
        pos += length
    return True


def _adts_frame_length(h: bytes, i: int) -> int | None:
    """Длина кадра AAC ADTS, начинающегося с `i`, или None."""
    if i + 7 > len(h) or h[i] != 0xFF or (h[i + 1] & 0xF6) != 0xF0:
        return None
    if ((h[i + 2] >> 2) & 0x0F) >= 13:
        return None
    length = ((h[i + 3] & 0x03) << 11) | (h[i + 4] << 3) | (h[i + 5] >> 5)
    return length if length >= 7 else None


def _is_adts_stream(h: bytes, start: int) -> bool:
    pos = start
    for n in range(3):
        length = _adts_frame_length(h, pos)
        if length is None:
            return n >= 1 and pos >= len(h)
        pos += length
    return True


def _after_id3(h: bytes) -> _Result:
    """Тег ID3 в начале: смотрим, что за ним (обычно MP3, иногда AAC или FLAC)."""
    if len(h) < 10:
        return None
    size = 0
    for b in h[6:10]:
        size = (size << 7) | (b & 0x7F)
    pos = 10 + size + (10 if h[5] & 0x10 else 0)  # +футер ID3v2.4
    # Пропускаем возможное заполнение нулями.
    limit = min(len(h), pos + 4096)
    while pos < limit and h[pos] == 0:
        pos += 1
    if h[pos:pos + 4] == b"fLaC":
        return _res("flac")
    if _adts_frame_length(h, pos) is not None:
        return _res("aac")
    return _res("mp3")


# --- ZIP-контейнеры ---------------------------------------------------------

_ODF_MIMETYPES = {
    b"application/vnd.oasis.opendocument.text": "odt",
    b"application/vnd.oasis.opendocument.spreadsheet": "ods",
    b"application/vnd.oasis.opendocument.presentation": "odp",
    b"application/epub+zip": "epub",
}


def _zip(ctx: _Ctx) -> _Result:
    """ZIP: отличаем DOCX/XLSX/PPTX, ODT/ODS/ODP, EPUB от обычного архива."""
    # 1. ODF и EPUB по стандарту кладут несжатый файл `mimetype` первым.
    h = ctx.head
    if h[:4] == b"PK\x03\x04" and len(h) >= 30:
        method, = struct.unpack_from("<H", h, 8)
        csize, = struct.unpack_from("<I", h, 18)
        name_len, extra_len = struct.unpack_from("<HH", h, 26)
        name = h[30:30 + name_len]
        if name == b"mimetype" and method == 0 and csize <= MAX_ZIP_MIMETYPE:
            start = 30 + name_len + extra_len
            code = _ODF_MIMETYPES.get(h[start:start + csize].strip())
            if code:
                return _res(code)

    # 2. Имена из центрального каталога (без распаковки, с ограничениями).
    names = _zip_names(ctx)
    if names is None:
        return _res("zip")
    if "mimetype" in names:
        code = _zip_mimetype(ctx)
        if code:
            return _res(code)
    if "word/document.xml" in names:
        return _res("docx")
    if "xl/workbook.xml" in names:
        return _res("xlsx")
    if "ppt/presentation.xml" in names:
        return _res("pptx")
    return _res("zip")


def _zip_limits_ok(ctx: _Ctx) -> bool:
    """Проверяет по записи конца архива (EOCD), что каталог не слишком велик."""
    f = ctx.f
    tail_size = min(ctx.size, ZIP_TAIL_SIZE)
    f.seek(ctx.size - tail_size)
    tail = f.read(tail_size)
    idx = tail.rfind(b"PK\x05\x06")
    if idx < 0 or idx + 22 > len(tail):
        return False
    entries, cd_size = struct.unpack_from("<HI", tail, idx + 10)
    if entries == 0xFFFF or cd_size == 0xFFFFFFFF:
        # ZIP64: настоящие значения — в отдельной записи.
        loc = idx - 20
        if loc < 0 or tail[loc:loc + 4] != b"PK\x06\x07":
            return False
        z64_offset, = struct.unpack_from("<Q", tail, loc + 8)
        if z64_offset + 56 > ctx.size:
            return False
        f.seek(z64_offset)
        rec = f.read(56)
        if len(rec) < 56 or rec[:4] != b"PK\x06\x06":
            return False
        entries, cd_size = struct.unpack_from("<QQ", rec, 32)
    return entries <= MAX_ZIP_ENTRIES and cd_size <= MAX_ZIP_CENTRAL_DIR


def _zip_names(ctx: _Ctx) -> set[str] | None:
    """Имена записей ZIP или None, если каталог битый или слишком большой."""
    try:
        if not _zip_limits_ok(ctx):
            return None
        ctx.f.seek(0)
        with zipfile.ZipFile(ctx.f) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                return None
            return {info.filename for info in infos}
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, struct.error, ValueError,
            EOFError, NotImplementedError):
        return None


def _zip_mimetype(ctx: _Ctx) -> str | None:
    """Читает маленький файл `mimetype` из ZIP (если он не первый в архиве)."""
    try:
        ctx.f.seek(0)
        with zipfile.ZipFile(ctx.f) as zf:
            info = zf.getinfo("mimetype")
            if info.file_size > MAX_ZIP_MIMETYPE or info.compress_size > MAX_ZIP_MIMETYPE:
                return None
            if info.flag_bits & 0x1:  # зашифрован
                return None
            with zf.open(info) as member:
                data = member.read(MAX_ZIP_MIMETYPE + 1)
            return _ODF_MIMETYPES.get(data.strip())
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError, NotImplementedError,
            zlib.error, struct.error, ValueError, EOFError):
        return None


# --- gzip (tar.gz) ----------------------------------------------------------


def _gzip(h: bytes) -> _Result:
    """gzip, внутри которого tar (проверяем заголовок ustar первого блока)."""
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = d.decompress(h[:65536], 1024)  # не больше 1 КБ распакованных данных
    except zlib.error:
        return None
    if len(out) >= 262 and out[257:262] == b"ustar":
        return _res("tar_gz")
    return _UNSUPPORTED  # просто .gz или старый tar без ustar — не поддерживаем


# --- OLE (DOC, XLS, PPT) ----------------------------------------------------

_OLE_END = 0xFFFFFFFA  # значения >= этого — служебные (конец цепочки и т.п.)


def _ole(ctx: _Ctx) -> _Result:
    names = _ole_stream_names(ctx)
    if "WordDocument" in names:
        return _res("doc")
    if "Workbook" in names or "Book" in names:
        return _res("xls")
    if "PowerPoint Document" in names:
        return _res("ppt")
    if not names and ctx.ext in ("doc", "xls", "ppt"):
        return _res(ctx.ext)  # каталог не прочитался — доверяем расширению
    if not names and ctx.ext == "pps":
        return _res("ppt")
    return _UNSUPPORTED  # другой OLE-файл (MSI, MSG и т.п.)


def _ole_stream_names(ctx: _Ctx) -> set[str]:
    """Имена потоков из каталога OLE (Compound File Binary). Пусто — не удалось."""
    f, size = ctx.f, ctx.size
    hdr = ctx.head[:512]
    if len(hdr) < 512:
        return set()
    sector_shift, = struct.unpack_from("<H", hdr, 0x1E)
    if sector_shift not in (9, 12):
        return set()
    sector_size = 1 << sector_shift
    num_fat, first_dir = struct.unpack_from("<II", hdr, 0x2C)
    difat = [
        v for v in struct.unpack_from("<109I", hdr, 0x4C)[: min(num_fat, 109)]
        if v < _OLE_END
    ]
    per_sector = sector_size // 4
    fat_cache: dict[int, bytes] = {}

    def read_sector(n: int) -> bytes | None:
        offset = (n + 1) * sector_size
        if offset + sector_size > size:
            return None
        f.seek(offset)
        data = f.read(sector_size)
        return data if len(data) == sector_size else None

    def next_sector(n: int) -> int:
        idx = n // per_sector
        if idx >= len(difat):
            return _OLE_END
        if idx not in fat_cache:
            data = read_sector(difat[idx])
            if data is None:
                return _OLE_END
            fat_cache[idx] = data
        return struct.unpack_from("<I", fat_cache[idx], (n % per_sector) * 4)[0]

    names: set[str] = set()
    seen: set[int] = set()
    current = first_dir
    for _ in range(MAX_OLE_DIR_SECTORS):
        if current >= _OLE_END or current in seen:
            break
        seen.add(current)
        data = read_sector(current)
        if data is None:
            break
        for pos in range(0, sector_size, 128):
            entry = data[pos:pos + 128]
            name_len, = struct.unpack_from("<H", entry, 0x40)
            obj_type = entry[0x42]
            if obj_type in (1, 2, 5) and 2 <= name_len <= 64:
                names.add(entry[: name_len - 2].decode("utf-16-le", "ignore"))
        current = next_sector(current)
    return names


# --- XML и текст ------------------------------------------------------------

_XML_NAME_RE = re.compile(r"<([A-Za-z_][\w.\-]*(?::[A-Za-z_][\w.\-]*)?)")


def _decode_sample(h: bytes, limit: int = 65536) -> str | None:
    """Декодирует начало файла для поиска тегов; None — не похоже на текст."""
    sample = h[:limit]
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return sample.decode("utf-16", "ignore")
    if sample.startswith(b"\xef\xbb\xbf"):
        return sample[3:].decode("utf-8", "ignore")
    if b"\x00" in sample[:4096]:
        return None
    # Имена тегов — ASCII, поэтому latin-1 подходит для любой 8-битной кодировки.
    return sample.decode("latin-1")


def _xml_root_name(h: bytes) -> str | None:
    """Локальное имя корневого элемента XML (без префикса) или None."""
    text = _decode_sample(h)
    if text is None:
        return None
    pos, n = 0, len(text)
    for _ in range(10000):
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            return None
        if text.startswith("<?", pos):
            end = text.find("?>", pos)
            if end < 0:
                return None
            pos = end + 2
        elif text.startswith("<!--", pos):
            end = text.find("-->", pos)
            if end < 0:
                return None
            pos = end + 3
        elif text.startswith("<!", pos):
            close = text.find(">", pos)
            bracket = text.find("[", pos)
            if 0 <= bracket < close:  # внутреннее подмножество DOCTYPE
                end_br = text.find("]", bracket)
                close = text.find(">", end_br) if end_br >= 0 else -1
            if close < 0:
                return None
            pos = close + 1
        elif text.startswith("<", pos):
            match = _XML_NAME_RE.match(text, pos)
            if match is None:
                return None
            return match.group(1).rpartition(":")[2]
        else:
            return None
    return None


def _looks_like_text(h: bytes) -> bool:
    """Грубая проверка, что начало файла — текст (UTF-8/16 или 8-битная кодировка)."""
    sample = h[:8192]
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        # Обрезанный в конце выборки многобайтовый символ — не повод отказать.
        if exc.start >= len(sample) - 3 and len(sample) == 8192:
            return True
    controls = sum(1 for b in sample if b < 0x20 and b not in (9, 10, 12, 13))
    return controls <= len(sample) // 100


# Расширения, которые решают спор между неотличимыми по содержимому txt/md/csv.
_AMBIGUOUS_TEXT_EXT = {
    "txt": "txt", "text": "txt", "log": "txt",
    "md": "md", "markdown": "md",
    "csv": "csv", "tsv": "csv",
}


def _text_format(magika_label: str | None, ctx: _Ctx) -> _Result:
    """Выбирает текстовый формат по метке magika и расширению."""
    if not _looks_like_text(ctx.head):
        return None
    if magika_label == "svg" or (magika_label == "xml" and _xml_root_name(ctx.head) == "svg"):
        return _res("svg")
    if magika_label == "rtf":
        return _res("rtf")
    by_ext = _AMBIGUOUS_TEXT_EXT.get(ctx.ext)
    if by_ext is not None:
        return _res(by_ext)
    if magika_label == "html":
        return _res("html")
    if ctx.ext in FORMATS["html"].extensions and b"<" in ctx.head[:4096]:
        return _res("html")
    if magika_label == "markdown":
        return _res("md")
    if magika_label in ("csv", "tsv"):
        return _res("csv")
    return _res("txt")


# --- magika -----------------------------------------------------------------

_magika_lock = threading.Lock()
_magika_instance = None
_magika_failed = False

# Метки magika для бинарных форматов → наши коды.
_MAGIKA_BINARY = {
    "jpeg": "jpg", "png": "png", "webp": "webp", "heif": "heic", "avif": "avif",
    "gif": "gif", "bmp": "bmp", "tiff": "tiff", "ico": "ico",
    "mp4": "mp4", "qt": "mov", "mkv": "mkv", "avi": "avi", "webm": "webm",
    "wmv": "wmv", "flv": "flv", "3gp": "3gp", "mpegts": "mts",
    "mp3": "mp3", "wav": "wav", "flac": "flac", "ogg": "ogg", "wma": "wma",
    "pdf": "pdf", "doc": "doc", "xls": "xls", "ppt": "ppt",
    "docx": "docx", "xlsx": "xlsx", "pptx": "pptx",
    "odt": "odt", "ods": "ods", "odp": "odp", "epub": "epub",
    "zip": "zip", "sevenzip": "7z", "rar": "rar",
}


def _get_magika():
    """Ленивая потокобезопасная загрузка модели magika (один раз)."""
    global _magika_instance, _magika_failed
    if _magika_instance is not None or _magika_failed:
        return _magika_instance
    with _magika_lock:
        if _magika_instance is None and not _magika_failed:
            try:
                from magika import Magika

                _magika_instance = Magika()
            except Exception:  # noqa: BLE001 — без magika продолжаем на сигнатурах
                log.exception("Не удалось загрузить модель magika")
                _magika_failed = True
    return _magika_instance


def _by_magika(ctx: _Ctx) -> _Result:
    magika = _get_magika()
    if magika is None:
        return None
    ctx.f.seek(0)
    result = magika.identify_stream(ctx.f)
    if not result.ok:
        return None
    label = str(result.output.label)
    is_text = bool(result.output.is_text)
    code = _MAGIKA_BINARY.get(label)
    if code is not None:
        # Двоичные форматы, которые мы различаем точнее сигнатурами, но
        # сигнатуры не сработали: доверяем magika только для «простых» случаев.
        if code in ("docx", "xlsx", "pptx", "odt", "ods", "odp", "epub", "zip"):
            return None  # ZIP без сигнатуры PK — что-то странное
        return _res(code)
    if is_text:
        return _text_format(label, ctx)
    return None


# --- puremagic --------------------------------------------------------------

# Коды, которые можно принимать от puremagic (сигнатуры бинарных форматов).
# Кортеж, а не множество: порядок важен (".ogg" → ogg раньше, чем opus).
_PUREMAGIC_ALLOWED = (
    "jpg", "png", "webp", "heic", "avif", "gif", "bmp", "tiff", "ico", "raw",
    "mp4", "mov", "mkv", "avi", "webm", "wmv", "flv", "3gp", "mts",
    "mp3", "wav", "flac", "aac", "m4a", "ogg", "opus", "wma", "amr",
    "pdf", "7z", "rar", "mobi",
)


def _by_puremagic(ctx: _Ctx) -> _Result:
    try:
        from puremagic.main import identify_all
    except ImportError:
        return None
    tail_size = min(ctx.size, 4096)
    ctx.f.seek(ctx.size - tail_size)
    tail = ctx.f.read(tail_size)
    for match in identify_all(ctx.head[:8192], tail):
        if match.confidence < 0.4:
            continue
        ext = str(match.extension or "").lower().lstrip(".")
        for code in _PUREMAGIC_ALLOWED:
            if ext in FORMATS[code].extensions:
                if code == "raw":
                    return _raw(ext.upper())
                return _res(code)
    return None
