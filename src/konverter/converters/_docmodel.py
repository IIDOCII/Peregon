"""Промежуточная модель документа/книги и её чтение из HTML, запись в HTML/MD/TXT.

Только для дочернего процесса (задача "docs", см. `_docjob`).

Модель (`Doc`): метаданные (название, авторы, язык, обложка), картинки
(`Picture`: уже проверенные Pillow байты JPEG/PNG) и главы (`Chapter`: подпись
для оглавления + блоки). Блоки: заголовок, абзац, список, таблица, картинка,
преформатированный текст, цитата, разделитель. Текст внутри — «отрезки»
(`Span`) с признаками жирный/курсив/код и ссылкой; перевод строки внутри
абзаца — символ "\\n" в тексте отрезка.

Санитайзер HTML устроен как ПЕРЕСБОРКА, а не как вырезание: вход (HTML,
XHTML глав EPUB, HTML из Markdown, главы MOBI) разбирается в модель по белому
списку смысловых тегов, а выходной HTML/XHTML/FB2/MD строится заново только из
модели. Поэтому в результат физически не может попасть то, чего модель не
умеет хранить: `<script>`, `<style>`, атрибуты `on*`, `style`, `class`,
`<iframe>/<object>/<embed>/<form>/<meta>/<base>/<link>`, CSS с `url(`/
`@import`. Ссылки остаются только со схемами http/https/mailto (`safe_href`),
картинки — только встроенные в сам файл (запись EPUB/DOCX, `data:` в HTML) и
только после проверки Pillow; внешние картинки (`http://…` — «пиксель-
маячок», пути к локальным файлам) не загружаются и пропускаются.
"""

from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass, field
from typing import Callable

from ._isolated import TaskError

# --- Лимиты по умолчанию (родитель передаёт свои) -----------------------------

MAX_CHARS = 200_000_000
MAX_BLOCKS = 2_000_000
MAX_CHAPTERS = 10_000
MAX_IMAGES = 2_000
MAX_IMAGE_BYTES = 256 * 2**20
MAX_IMAGE_PIXELS = 40_000_000
MAX_TABLE_CELLS = 1_000_000
# Глубина вложенности элементов, которую обходит разбор HTML (глубже — текстом).
MAX_DEPTH = 120
MAX_HREF = 2048

# --- Модель -------------------------------------------------------------------


@dataclass
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    href: str | None = None

    def same_style(self, other: "Span") -> bool:
        return (self.bold, self.italic, self.code, self.href) == (
            other.bold, other.italic, other.code, other.href)


@dataclass
class Heading:
    level: int
    spans: list[Span]


@dataclass
class Para:
    spans: list[Span]


@dataclass
class ListBlock:
    ordered: bool
    items: list[list]  # каждый пункт — список блоков


@dataclass
class Table:
    rows: list[list[list[Span]]]
    header: int = 0  # сколько первых строк — заголовок


@dataclass
class Img:
    id: str
    alt: str = ""


@dataclass
class Pre:
    text: str


@dataclass
class Quote:
    blocks: list


@dataclass
class Rule:
    pass


@dataclass
class PageBreak:
    """Только при разборе MOBI (`<mbp:pagebreak/>`): граница главы."""


@dataclass
class Picture:
    data: bytes
    media_type: str  # "image/jpeg" | "image/png"
    width: int
    height: int

    @property
    def ext(self) -> str:
        return "jpg" if self.media_type == "image/jpeg" else "png"


@dataclass
class Chapter:
    title: str
    blocks: list


@dataclass
class Doc:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    language: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    images: dict[str, Picture] = field(default_factory=dict)
    cover: str | None = None


# --- Учёт работы и лимитов ------------------------------------------------------


class Work:
    """Состояние задачи в ребёнке: лимиты, прогресс, счётчики."""

    def __init__(self, args: dict, report) -> None:
        self.args = args
        self.limits = dict(args.get("limits") or {})
        self.report = report
        self.mark = 0
        self.chars = 0
        self.blocks = 0
        self.image_bytes = 0
        self.max_chars = self.lim("max_chars", MAX_CHARS)
        self.max_blocks = self.lim("max_blocks", MAX_BLOCKS)
        self.max_images = self.lim("max_images", MAX_IMAGES)
        self.max_image_bytes = self.lim("max_image_bytes", MAX_IMAGE_BYTES)
        self.max_image_pixels = self.lim("max_image_pixels", MAX_IMAGE_PIXELS)
        self.max_chapters = self.lim("max_chapters", MAX_CHAPTERS)
        self.max_table_cells = self.lim("max_table_cells", MAX_TABLE_CELLS)
        self.table_cells = 0

    def lim(self, key: str, default: int) -> int:
        try:
            return int(self.limits.get(key, default))
        except (TypeError, ValueError):
            return default

    def tick(self, fraction: float | None = None) -> None:
        """Отметка продвижения (для сторожа зависания и полосы прогресса)."""
        self.mark += 1
        self.report(fraction, self.mark)

    def add_text(self, count: int) -> None:
        self.chars += count
        if self.chars > self.max_chars:
            raise TaskError("too_much_text", self.max_chars / 1e6)

    def add_block(self) -> None:
        self.blocks += 1
        if self.blocks > self.max_blocks:
            raise TaskError("too_complex")
        if self.blocks % 1000 == 0:
            self.tick()

    def add_cells(self, count: int) -> None:
        self.table_cells += count
        if self.table_cells > self.max_table_cells:
            raise TaskError("too_many_cells", self.max_table_cells)


# --- Текст --------------------------------------------------------------------

# Управляющие символы (кроме \t и \n), суррогаты и «не-символы»: недопустимы
# в XML (DOCX, FB2, EPUB) и бесполезны в тексте.
_BAD_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff￾￿]")
_WS = re.compile(r"[ \t\n\r\f\v]+")


def clean_text(text: str) -> str:
    """Текст без недопустимых символов; переводы строк — "\\n"."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _BAD_CHARS.sub("", text)


def spans_text(spans: list[Span]) -> str:
    return "".join(s.text for s in spans)


def merge_spans(spans: list[Span]) -> list[Span]:
    result: list[Span] = []
    for span in spans:
        if not span.text:
            continue
        if result and result[-1].same_style(span):
            result[-1] = Span(result[-1].text + span.text, span.bold, span.italic,
                              span.code, span.href)
        else:
            result.append(span)
    return result


def normalize_spans(spans: list[Span]) -> list[Span]:
    """Схлопывание пробелов как в HTML; "\\n" (из `<br>`) сохраняется."""
    out: list[Span] = []
    at_start = True
    for span in spans:
        if span.text == "\n":
            if out:
                out[-1].text = out[-1].text.rstrip(" ")
            out.append(Span("\n", span.bold, span.italic, span.code, span.href))
            at_start = True
            continue
        text = _WS.sub(" ", span.text)
        if at_start:
            text = text.lstrip(" ")
        if not text:
            continue
        at_start = text.endswith(" ")
        out.append(Span(text, span.bold, span.italic, span.code, span.href))
    while out and (out[-1].text == "\n" or not out[-1].text.strip(" ")):
        out.pop()
    if out:
        out[-1].text = out[-1].text.rstrip(" ")
    while out and out[0].text == "\n":
        out.pop(0)
    return merge_spans(out)


_HREF_STRIP = "".join(chr(code) for code in range(0x21)) + "\x7f"
_HREF_DROP = str.maketrans("", "", "\t\n\r")


def safe_href(href: str | None) -> str | None:
    """Ссылка, которую можно оставить в результате (http/https/mailto), или None."""
    if not href:
        return None
    text = href.translate(_HREF_DROP).strip(_HREF_STRIP)
    if not text or len(text) > MAX_HREF or _BAD_CHARS.search(text):
        return None
    low = text.lower()
    if low.startswith(("http://", "https://", "mailto:")):
        return text
    return None


# --- Картинки -----------------------------------------------------------------


class Pictures:
    """Набор картинок документа: проверка Pillow, лимиты, нормализация в JPEG/PNG."""

    def __init__(self, work: Work, images: dict[str, Picture]) -> None:
        self.work = work
        self.images = images
        self._by_key: dict[object, str | None] = {}

    def known(self, key: object) -> bool:
        """Картинка с этим ключом уже обработана (до чтения данных — без распаковки)."""
        return key is not None and key in self._by_key

    def add(self, data: bytes, key: object = None) -> str | None:
        """Id картинки в `images` или None (не картинка, слишком большая, лимит)."""
        if key is not None and key in self._by_key:
            return self._by_key[key]
        result = None
        if len(self.images) < self.work.max_images and data:
            picture = load_picture(data, self.work.max_image_pixels)
            if picture is not None:
                if self.work.image_bytes + len(picture.data) <= self.work.max_image_bytes:
                    self.work.image_bytes += len(picture.data)
                    result = f"img{len(self.images) + 1:04d}"
                    self.images[result] = picture
                self.work.tick()
        if key is not None:
            self._by_key[key] = result
        return result


# --- Метаданные картинок -------------------------------------------------------
#
# Единая политика с этапом 1: метаданные (EXIF с GPS, XMP, IPTC, комментарии,
# тексты PNG) в результат не попадают. JPEG и PNG чистятся БЕЗ перекодирования
# (сегменты/чанки вырезаются, сжатые данные копируются байт в байт — пиксели те
# же); при сбое разбора — перекодирование Pillow без метаданных. Остальные
# форматы (GIF, WEBP, BMP …) и так перекодируются Pillow в PNG, а Pillow не
# переносит EXIF/XMP/тексты, если их не передать явно.

_JPEG_MAX_SEGMENTS = 10_000
# Белый список маркеров JPEG, которые остаются (всё прочее, в т.ч.
# зарезервированные 0x02–0xBF, JPG 0xC8, JPGn 0xF0–0xFD, DHP 0xDE, вырезается):
# SOF0–SOF15 с DHT (0xC4) и DAC (0xCC), SOS (0xDA, со сжатыми данными и RSTn
# внутри), DQT, DNL, DRI, EXP.
_JPEG_KEEP_MARKERS = frozenset(
    {m for m in range(0xC0, 0xD0) if m != 0xC8} | {0xDA, 0xDB, 0xDC, 0xDD, 0xDF})
# Сегменты APPn, которые остаются (по маркеру и началу содержимого):
# APP0 — только JFIF (обрезается до 14 байт, без миниатюры; JFXX несёт
# миниатюру со своим EXIF), APP2 — только профиль ICC (MPF во APP2 несёт
# вторые картинки со своим EXIF), APP14 — Adobe (нужен для CMYK/YCCK).
_JPEG_KEEP_APP = {0xE0: (b"JFIF\x00",), 0xE2: (b"ICC_PROFILE\x00",),
                  0xEE: (b"Adobe",)}
# Данные JFIF без миниатюры: «JFIF\0», версия (2), единицы (1), плотность X/Y (4).
_JFIF_HEAD = 12
# APP0 JFIF 1.01, плотность 1:1 без единиц.
_JFIF_DEFAULT = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Белый список чанков PNG (всё прочее — eXIf, тексты, tIME, caBX/C2PA, zxIf,
# частные и неизвестные чанки — вырезается); кадры APNG сохраняются.
_PNG_KEEP = frozenset({
    b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP",
    b"sBIT", b"pHYs", b"bKGD", b"acTL", b"fcTL", b"fdAT",
})
_PNG_MAX_CHUNKS = 100_000


def strip_jpeg_metadata(data: bytes) -> bytes | None:
    """JPEG только с маркерами из белого списка (без EXIF, XMP, IPTC, COM, MPF,
    миниатюр и неизвестных сегментов); None — не разобрать.

    Сжатые данные (после SOS) копируются как есть, прогрессивные JPEG
    (несколько SOS с таблицами между ними) разбираются по маркерам; всё после
    EOI (хвосты с дополнительными картинками) отбрасывается.
    """
    size = len(data)
    if size < 4 or data[:2] != b"\xff\xd8":
        return None
    out: list[bytes] = []
    jfif: bytes | None = None
    adobe = False
    pos = 2
    segments = 0
    while True:
        segments += 1
        if segments > _JPEG_MAX_SEGMENTS or pos >= size or data[pos] != 0xFF:
            return None
        while pos < size and data[pos] == 0xFF:  # заполняющие 0xFF
            pos += 1
        if pos >= size:
            return None
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            # JFIF — первым после SOI (python-docx узнаёт JPEG только по «JFIF»
            # или «Exif» со смещения 6). Без JFIF и без Adobe (у камер EXIF был
            # вместо JFIF) — добавляется стандартный JFIF; при Adobe не
            # добавляется: для 3 компонент JFIF перекрыл бы его преобразование цвета.
            if jfif is None and not adobe:
                jfif = _JFIF_DEFAULT
            head = [b"\xff\xd8"] + ([jfif] if jfif is not None else [])
            return b"".join(head + out + [b"\xff\xd9"])
        if marker in (0x00, 0xD8) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            return None  # не на своём месте
        if pos + 2 > size:
            return None
        length = (data[pos] << 8) | data[pos + 1]
        end = pos + length
        if length < 2 or end > size:
            return None
        payload = data[pos + 2:end]
        if 0xE0 <= marker <= 0xEF:
            prefixes = _JPEG_KEEP_APP.get(marker)
            keep = prefixes is not None and payload.startswith(prefixes)
            if keep and marker == 0xE0:
                # JFIF — без миниатюры (Xthumbnail = Ythumbnail = 0), ставится
                # первым (см. выше); короче стандарта — заменяется стандартным.
                if jfif is None and len(payload) >= _JFIF_HEAD:
                    jfif = b"\xff\xe0\x00\x10" + payload[:_JFIF_HEAD] + b"\x00\x00"
                keep = False
            elif keep and marker == 0xEE:
                adobe = True
        else:
            keep = marker in _JPEG_KEEP_MARKERS  # COM, JPGn, зарезервированные — нет
        if keep:
            out.append(data[pos - 2:end])
        pos = end
        if marker == 0xDA:  # SOS: сжатые данные до следующего маркера (не RSTn, не 0xFF00)
            scan = pos
            while True:
                found = data.find(b"\xff", scan)
                if found < 0 or found + 1 >= size:
                    return None
                nxt = data[found + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                    scan = found + 1 if nxt == 0xFF else found + 2
                    continue
                out.append(data[pos:found])
                pos = found
                break


def strip_png_metadata(data: bytes) -> bytes | None:
    """PNG только с чанками из белого списка (без данных после IEND); None — не разобрать."""
    size = len(data)
    if not data.startswith(_PNG_SIGNATURE):
        return None
    out = [_PNG_SIGNATURE]
    pos = len(_PNG_SIGNATURE)
    for _ in range(_PNG_MAX_CHUNKS):
        if pos + 12 > size:
            return None
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if length > 0x7FFFFFFF or end > size:
            return None
        if kind in _PNG_KEEP:
            out.append(data[pos:end])
        pos = end
        if kind == b"IEND":
            return b"".join(out)
    return None


def _reencode(image, fmt: str) -> bytes:
    """Перекодирование Pillow без метаданных (запасной путь)."""
    out = io.BytesIO()
    image.load()
    icc = image.info.get("icc_profile")
    extra = {"icc_profile": icc} if icc else {}
    if fmt == "JPEG":
        clean = image.copy()
    else:
        has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
        clean = image.convert("RGBA" if has_alpha else "RGB")
    # Pillow берёт часть метаданных (комментарий, XMP) из `info` исходника —
    # очищаем, оставляя только профиль ICC (передан явно).
    clean.info = {}
    if fmt == "JPEG":
        clean.save(out, format="JPEG", quality=92, **extra)
    else:
        clean.save(out, format="PNG", compress_level=6, **extra)
    return out.getvalue()


def load_picture(data: bytes, max_pixels: int) -> Picture | None:
    """Проверенная картинка без метаданных: JPEG/PNG — те же сжатые данные, прочее — в PNG.

    SVG и прочий вектор не принимаются (могут содержать скрипты и ссылки).
    """
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                return None
            fmt = image.format
            if fmt == "MPO":
                # JPEG с MPF (стереопары, Ultra HDR): первая картинка — обычный
                # JPEG; strip_jpeg_metadata вырежет MPF и вторые картинки после EOI.
                fmt = "JPEG"
            if fmt in ("JPEG", "PNG") and (fmt == "PNG" or image.mode in ("RGB", "L", "CMYK")):
                image.verify()  # только сразу после открытия
                try:
                    with Image.open(io.BytesIO(data)) as probe:
                        orientation = int(probe.getexif().get(0x0112) or 1)
                except Exception:  # noqa: BLE001 — битый EXIF: поворота нет
                    orientation = 1
                if orientation in range(2, 9):
                    # EXIF-поворот (как на этапе 1): без тега Orientation снимок
                    # лёг бы набок — поворачиваем пиксели и перекодируем.
                    from PIL import ImageOps

                    with Image.open(io.BytesIO(data)) as again:
                        turned = ImageOps.exif_transpose(again)
                        clean = _reencode(turned, fmt)
                        width, height = turned.size
                else:
                    strip = strip_jpeg_metadata if fmt == "JPEG" else strip_png_metadata
                    clean = strip(bytes(data))
                    if clean is None:
                        with Image.open(io.BytesIO(data)) as again:
                            clean = _reencode(again, fmt)
                # verify() у JPEG ничего не проверяет, у PNG — не распаковывает
                # IDAT: результат декодируется целиком (размер уже ограничен
                # max_pixels); битая картинка отбрасывается.
                with Image.open(io.BytesIO(clean)) as check:
                    if check.format != fmt or check.size != (width, height):
                        return None
                    check.load()
                return Picture(clean, "image/jpeg" if fmt == "JPEG" else "image/png",
                               width, height)
            image.seek(0)
            image.load()
            has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
            converted = image.convert("RGBA" if has_alpha else "RGB")
            out = io.BytesIO()
            converted.save(out, format="PNG", compress_level=6)
            return Picture(out.getvalue(), "image/png", width, height)
    except Exception:  # noqa: BLE001 — враждебные данные: не картинка
        return None


def data_uri_bytes(src: str, limit: int) -> bytes | None:
    """Байты картинки из `data:image/…;base64,…` (не больше `limit`) или None."""
    head, sep, payload = src.partition(",")
    if not sep:
        return None
    head = head.strip().lower()
    if not head.startswith("data:image/") or not head.endswith(";base64"):
        return None
    if head.startswith("data:image/svg"):
        return None
    if len(payload) > limit * 4 // 3 + 16:
        return None
    try:
        return base64.b64decode("".join(payload.split()), validate=True)
    except (ValueError, TypeError):
        return None


# --- HTML → модель -------------------------------------------------------------

# Удаляются вместе с содержимым.
_DROP = frozenset({
    "script", "style", "noscript", "template", "iframe", "frame", "frameset", "object",
    "applet", "head", "title", "canvas", "audio", "video", "map", "button", "select",
    "textarea", "option", "optgroup", "datalist", "math", "dialog", "portal", "slot",
})
# «Пустые» элементы HTML: сам элемент удаляется, но его «дети» обрабатываются
# как обычный текст — парсер libxml2 не знает, что `<embed>`, `<input>` и
# т.п. не имеют содержимого, и вкладывает в них всё, что идёт следом.
_DROP_VOID = frozenset({
    "embed", "param", "meta", "link", "base", "source", "track", "area", "input",
    "keygen", "col", "wbr", "basefont", "bgsound",
})
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# Блоки-контейнеры: их текст становится абзацами.
_BLOCKS = frozenset({
    "p", "div", "section", "article", "main", "header", "footer", "aside", "nav", "body",
    "html", "figure", "figcaption", "center", "details", "summary", "address", "hgroup",
    "dl", "dt", "dd", "form", "fieldset", "legend", "li", "caption", "tr", "td", "th",
    "tbody", "thead", "tfoot",
})
_BOLD = frozenset({"b", "strong"})
_ITALIC = frozenset({"i", "em", "cite", "dfn", "var"})
_CODE = frozenset({"code", "kbd", "samp", "tt"})


def _local(element) -> str:
    from ._safexml import localname

    return localname(element)


class _Ctx:
    """Текущий список блоков и накопитель отрезков абзаца."""

    def __init__(self, work: Work) -> None:
        self.work = work
        self.out: list = []
        self.buf: list[Span] = []

    def flush(self) -> None:
        if self.buf:
            spans = normalize_spans(self.buf)
            self.buf = []
            if spans:
                self.work.add_text(sum(len(s.text) for s in spans))
                self.work.add_block()
                self.out.append(Para(spans))

    def add(self, block) -> None:
        self.flush()
        self.work.add_block()
        self.out.append(block)


@dataclass(frozen=True)
class _Style:
    bold: bool = False
    italic: bool = False
    code: bool = False
    href: str | None = None

    def span(self, text: str) -> Span:
        return Span(text, self.bold, self.italic, self.code, self.href)


class HtmlReader:
    """Разбор дерева lxml (HTML или XHTML) в блоки модели по белому списку.

    `image(src)` — функция «атрибут src картинки → id в `Doc.images` или None»
    (внешние ссылки она не загружает).
    """

    def __init__(self, work: Work, image: Callable[[str], str | None] | None = None) -> None:
        self.work = work
        self.image = image or (lambda _src: None)

    def blocks(self, root) -> list:
        ctx = _Ctx(self.work)
        body = None
        for element in root.iter():
            if _local(element) == "body":
                body = element
                break
        self._children(body if body is not None else root, ctx, _Style(), 0)
        ctx.flush()
        return ctx.out

    # -- обход --

    def _text(self, ctx: _Ctx, text: str | None, style: _Style) -> None:
        if text:
            ctx.buf.append(style.span(clean_text(text)))

    def _children(self, element, ctx: _Ctx, style: _Style, depth: int) -> None:
        self._text(ctx, element.text, style)
        for child in element:
            if isinstance(child.tag, str):
                self._element(child, ctx, style, depth + 1)
            else:
                self._text(ctx, _entity_text(child), style)
            self._text(ctx, child.tail, style)

    def _sub(self, element, style: _Style, depth: int) -> list:
        sub = _Ctx(self.work)
        self._children(element, sub, style, depth)
        sub.flush()
        return sub.out

    def _element(self, el, ctx: _Ctx, style: _Style, depth: int) -> None:
        name = _local(el)
        if name in _DROP or not name:
            return
        if name in _DROP_VOID:
            self._children(el, ctx, style, depth)
            return
        if depth > MAX_DEPTH:
            # Слишком глубокая вложенность: дальше — только текст.
            self._text(ctx, " ".join(t for t in el.itertext() if isinstance(t, str)), style)
            return
        if name in _HEADINGS:
            blocks = self._sub(el, style, depth)
            spans: list[Span] = []
            rest = []
            for block in blocks:
                if isinstance(block, Para):
                    if spans:
                        spans.append(Span(" "))
                    spans.extend(block.spans)
                else:
                    rest.append(block)
            spans = normalize_spans([Span(s.text.replace("\n", " "), s.bold, s.italic, s.code,
                                          s.href) for s in spans])
            if spans:
                ctx.add(Heading(_HEADINGS[name], spans))
            for block in rest:
                ctx.add(block)
            return
        if name in ("ul", "ol", "menu", "dir"):
            items = []
            for child in el:
                if not isinstance(child.tag, str) or _local(child) in _DROP:
                    continue
                item = self._sub(child, style, depth + 1)
                if item:
                    items.append(item)
            if el.text and el.text.strip():
                items.insert(0, [Para(normalize_spans([style.span(clean_text(el.text))]))])
            if items:
                ctx.add(ListBlock(name == "ol", items))
            return
        if name == "table":
            table = self._table(el, style, depth)
            if table is not None:
                ctx.add(table)
            return
        if name in ("pre", "listing", "xmp", "plaintext"):
            text = clean_text("".join(t for t in el.itertext() if isinstance(t, str)))
            text = text.strip("\n")
            if text:
                self.work.add_text(len(text))
                ctx.add(Pre(text))
            return
        if name == "blockquote":
            blocks = self._sub(el, style, depth)
            if blocks:
                ctx.add(Quote(blocks))
            return
        if name == "hr":
            ctx.add(Rule())
            return
        if name == "br":
            ctx.buf.append(style.span("\n"))
            return
        if name == "mbp:pagebreak":
            ctx.add(PageBreak())
            return
        if name in ("img", "image"):
            self._img(el, ctx)
            return
        if name == "svg":
            for child in el.iter():
                if isinstance(child.tag, str) and _local(child) == "image":
                    self._img(child, ctx)
            return
        if name in _BLOCKS:
            ctx.flush()
            if name == "dt":
                style = _Style(True, style.italic, style.code, style.href)
            self._children(el, ctx, style, depth)
            ctx.flush()
            return
        if name == "a":
            href = None
            for key, value in el.attrib.items():
                if _attr_local(key) == "href":
                    href = safe_href(value)
            style = _Style(style.bold, style.italic, style.code, href)
        elif name in _BOLD:
            style = _Style(True, style.italic, style.code, style.href)
        elif name in _ITALIC:
            style = _Style(style.bold, True, style.code, style.href)
        elif name in _CODE:
            style = _Style(style.bold, style.italic, True, style.href)
        # Прочие (span, font, small, sub, sup, u, s, q, abbr, mark, label, неизвестные):
        # прозрачны — остаётся только текст.
        self._children(el, ctx, style, depth)

    def _img(self, el, ctx: _Ctx) -> None:
        src = None
        alt = ""
        for key, value in el.attrib.items():
            local = _attr_local(key)
            if local == "recindex":  # MOBI: номер записи с картинкой
                src = "recindex:" + value
            elif local in ("src", "href") and src is None:
                src = value
            elif local == "alt":
                alt = clean_text(value)[:500]
        if not src:
            return
        image_id = self.image(src.strip())
        if image_id is not None:
            ctx.add(Img(image_id, alt))

    def _table(self, el, style: _Style, depth: int) -> Table | None:
        rows: list[list[list[Span]]] = []
        header = 0
        header_open = True
        for tr in el.iter():
            if not isinstance(tr.tag, str) or _local(tr) != "tr":
                continue
            owner = next((a for a in tr.iterancestors() if _local(a) == "table"), None)
            if owner is not el:
                continue
            cells = []
            all_th = True
            in_head = any(_local(a) == "thead" for a in tr.iterancestors() if a is not el)
            for cell in tr:
                if not isinstance(cell.tag, str) or _local(cell) not in ("td", "th"):
                    continue
                if _local(cell) != "th":
                    all_th = False
                blocks = self._sub(cell, style, depth + 1)
                cells.append(_flatten(blocks))
            if not cells:
                continue
            self.work.add_cells(len(cells))
            if header_open and (in_head or all_th):
                header += 1
            else:
                header_open = False
            rows.append(cells)
        if not rows:
            return None
        return Table(rows, min(header, len(rows)))


def _entity_text(node) -> str:
    """Неразвёрнутая ссылка на сущность в XHTML (`&nbsp;` при внешнем DTD).

    Сущности никогда не раскрываются по DTD; известные имена HTML5
    (`html.entities.html5`) заменяются своими символами, прочие — пустотой.
    """
    from lxml import etree

    if not isinstance(node, etree._Entity):
        return ""
    import html.entities

    return html.entities.html5.get(f"{node.name};", "")


def _attr_local(key: str) -> str:
    return key.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _flatten(blocks: list) -> list[Span]:
    """Блоки ячейки таблицы → отрезки (абзацы через перевод строки)."""
    spans: list[Span] = []
    for block in blocks:
        part = block_spans(block)
        if not part:
            continue
        if spans:
            spans.append(Span("\n"))
        spans.extend(part)
    return merge_spans(spans)


def block_spans(block) -> list[Span]:
    """Отрезки «плоского» текста любого блока (для ячеек, подписей)."""
    if isinstance(block, (Para, Heading)):
        return list(block.spans)
    if isinstance(block, Pre):
        return [Span(block.text, code=True)]
    if isinstance(block, (Quote,)):
        return _flatten(block.blocks)
    if isinstance(block, ListBlock):
        out: list[Span] = []
        for item in block.items:
            part = _flatten(item)
            if part:
                if out:
                    out.append(Span("\n"))
                out.extend(part)
        return out
    if isinstance(block, Table):
        out = []
        for row in block.rows:
            if out:
                out.append(Span("\n"))
            for index, cell in enumerate(row):
                if index:
                    out.append(Span(" | "))
                out.extend(cell)
        return out
    return []


def plain_text(blocks: list) -> str:
    return "".join(s.text for b in blocks for s in block_spans(b))


def html_title(root) -> str:
    for element in root.iter():
        if isinstance(element.tag, str) and _local(element) == "title":
            text = clean_text("".join(t for t in element.itertext() if isinstance(t, str)))
            return " ".join(text.split())[:500]
    return ""


def chapter_label(chapter: Chapter, index: int) -> str:
    """Подпись главы для оглавления: своя, первый заголовок или «Глава N»."""
    title = " ".join((chapter.title or "").split())
    if title:
        return title[:300]
    for block in chapter.blocks:
        if isinstance(block, Heading):
            text = " ".join(spans_text(block.spans).split())
            if text:
                return text[:300]
    return f"Глава {index + 1}"


def split_by_breaks(blocks: list) -> list[list]:
    """Блоки → части по `PageBreak` (пустые части отбрасываются)."""
    parts: list[list] = [[]]
    for block in blocks:
        if isinstance(block, PageBreak):
            parts.append([])
        else:
            parts[-1].append(block)
    return [part for part in parts if part]


def split_by_headings(blocks: list) -> list[list]:
    """Части по заголовкам верхнего уровня (самый крупный уровень, ≥ 2 раз)."""
    levels = [b.level for b in blocks if isinstance(b, Heading)]
    if not levels:
        return [blocks] if blocks else []
    top = min(levels)
    if levels.count(top) < 2:
        return [blocks]
    parts: list[list] = [[]]
    for block in blocks:
        if isinstance(block, Heading) and block.level == top and parts[-1]:
            parts.append([])
        parts[-1].append(block)
    return [part for part in parts if part]


# --- Модель → HTML/XHTML ---------------------------------------------------------

XHTML_NS = "http://www.w3.org/1999/xhtml"

# Стили результата — только свои, без url() и @import.
HTML_CSS = (
    "body{font-family:Georgia,'Times New Roman',serif;line-height:1.5;max-width:46em;"
    "margin:1em auto;padding:0 1em}"
    "table{border-collapse:collapse;margin:.5em 0}td,th{border:1px solid #999;"
    "padding:.2em .5em;vertical-align:top}pre{white-space:pre-wrap;"
    "font-family:Consolas,monospace}img{max-width:100%;height:auto}"
    "blockquote{margin:.5em 2em;font-style:italic}"
)
# Политика безопасности страницы-результата: никаких скриптов, сети и встраиваний;
# картинки — только встроенные (data:), стили — только свои встроенные.
HTML_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"


class HtmlWriter:
    """Сборка элементов lxml из блоков модели (HTML или XHTML с пространством имён)."""

    def __init__(self, image_src: Callable[[str], str | None], ns: str | None = None,
                 work: Work | None = None) -> None:
        self.image_src = image_src
        self.ns = ns
        self.work = work

    def tag(self, name: str) -> str:
        return f"{{{self.ns}}}{name}" if self.ns else name

    def sub(self, parent, name: str, **attrs):
        from lxml import etree

        element = etree.SubElement(parent, self.tag(name))
        for key, value in attrs.items():
            element.set(key, value)
        return element

    @staticmethod
    def _append_text(parent, text: str) -> None:
        if not text:
            return
        if len(parent):
            last = parent[-1]
            last.tail = (last.tail or "") + text
        else:
            parent.text = (parent.text or "") + text

    def inline(self, parent, spans: list[Span]) -> None:
        for span in spans:
            target = parent
            if span.href:
                target = self.sub(target, "a", href=span.href)
            if span.bold:
                target = self.sub(target, "strong")
            if span.italic:
                target = self.sub(target, "em")
            if span.code:
                target = self.sub(target, "code")
            lines = span.text.split("\n")
            for index, line in enumerate(lines):
                if index:
                    self.sub(target, "br")
                self._append_text(target, line)

    def blocks(self, parent, blocks: list) -> None:
        for block in blocks:
            if self.work is not None:
                self.work.add_block()
            self.block(parent, block)

    def block(self, parent, block) -> None:
        if isinstance(block, Heading):
            self.inline(self.sub(parent, f"h{max(1, min(6, block.level))}"), block.spans)
        elif isinstance(block, Para):
            self.inline(self.sub(parent, "p"), block.spans)
        elif isinstance(block, ListBlock):
            lst = self.sub(parent, "ol" if block.ordered else "ul")
            for item in block.items:
                li = self.sub(lst, "li")
                if len(item) == 1 and isinstance(item[0], Para):
                    self.inline(li, item[0].spans)
                else:
                    self.blocks(li, item)
        elif isinstance(block, Table):
            table = self.sub(parent, "table")
            for index, row in enumerate(block.rows):
                tr = self.sub(table, "tr")
                for cell in row:
                    self.inline(self.sub(tr, "th" if index < block.header else "td"), cell)
        elif isinstance(block, Img):
            src = self.image_src(block.id)
            if src:
                div = self.sub(parent, "div")
                div.set("class", "image")
                self.sub(div, "img", src=src, alt=block.alt or "")
        elif isinstance(block, Pre):
            pre = self.sub(parent, "pre")
            pre.text = block.text
        elif isinstance(block, Quote):
            self.blocks(self.sub(parent, "blockquote"), block.blocks)
        elif isinstance(block, Rule):
            self.sub(parent, "hr")


def doc_to_html(doc: Doc, work: Work, *, embed_limit: int) -> str:
    """Модель → самостоятельная HTML-страница (картинки — `data:` URI)."""
    from lxml import etree

    used = [0]

    def image_src(image_id: str) -> str | None:
        picture = doc.images.get(image_id)
        if picture is None or used[0] + len(picture.data) > embed_limit:
            return None
        used[0] += len(picture.data)
        encoded = base64.b64encode(picture.data).decode("ascii")
        return f"data:{picture.media_type};base64,{encoded}"

    html = etree.Element("html")
    html.set("lang", doc.language or "ru")
    head = etree.SubElement(html, "head")
    etree.SubElement(head, "meta", charset="utf-8")
    csp = etree.SubElement(head, "meta")
    csp.set("http-equiv", "Content-Security-Policy")
    csp.set("content", HTML_CSP)
    etree.SubElement(head, "meta", name="viewport", content="width=device-width, initial-scale=1")
    title = etree.SubElement(head, "title")
    title.text = doc.title or ""
    style = etree.SubElement(head, "style")
    style.text = HTML_CSS
    body = etree.SubElement(html, "body")
    writer = HtmlWriter(image_src, None, work)
    total = max(1, len(doc.chapters))
    for index, chapter in enumerate(doc.chapters):
        writer.blocks(body, chapter.blocks)
        work.tick((index + 1) / total * 0.9)
    text = etree.tostring(html, method="html", encoding="unicode")
    return "<!DOCTYPE html>\n" + text + "\n"


# --- Модель → Markdown ---------------------------------------------------------

_MD_SPECIAL = re.compile(r"([\\`*_\[\]{}#|>~])")
_MD_ENTITY = re.compile(r"&(?=#?[A-Za-z0-9]+;)")
_MD_LINE_START = re.compile(r"^(\s*)(\d+)([.)])(\s)|^(\s*)([-+=])(\s|$)", re.M)


def md_escape(text: str) -> str:
    """Текст для Markdown: спецсимволы экранированы, `<` — `&lt;` (сырой HTML невозможен)."""
    text = _MD_SPECIAL.sub(r"\\\1", text)
    text = _MD_ENTITY.sub("&amp;", text)
    text = text.replace("<", "&lt;")
    return _MD_LINE_START.sub(
        lambda m: (f"{m.group(1)}{m.group(2)}\\{m.group(3)}{m.group(4)}" if m.group(2)
                   else f"{m.group(5)}\\{m.group(6)}{m.group(7)}"), text)


def _md_url(url: str) -> str:
    return (url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
            .replace("<", "%3C").replace(">", "%3E"))


def md_inline(spans: list[Span], *, breaks: bool = True) -> str:
    parts: list[str] = []
    for span in spans:
        lines = span.text.split("\n")
        rendered = []
        for line in lines:
            if not line:
                rendered.append("")
                continue
            lead = line[: len(line) - len(line.lstrip(" "))]
            trail = line[len(line.rstrip(" ")):]
            core = line.strip(" ")
            if not core:
                rendered.append(line)
                continue
            if span.code:
                ticks = "`"
                while ticks in core:
                    ticks += "`"
                pad = " " if core.startswith("`") or core.endswith("`") else ""
                core = f"{ticks}{pad}{core}{pad}{ticks}"
            else:
                core = md_escape(core)
            if span.italic:
                core = f"*{core}*"
            if span.bold:
                core = f"**{core}**"
            if span.href:
                core = f"[{core}]({_md_url(span.href)})"
            rendered.append(lead + core + trail)
        parts.append(("  \n" if breaks else " ").join(rendered))
    return "".join(parts)


def _md_blocks(blocks: list, work: Work) -> list[str]:
    out: list[str] = []
    for block in blocks:
        work.add_block()
        text = _md_block(block, work)
        if text:
            out.append(text)
    return out


def _md_block(block, work: Work) -> str:
    if isinstance(block, Heading):
        return "#" * max(1, min(6, block.level)) + " " + md_inline(block.spans, breaks=False)
    if isinstance(block, Para):
        return md_inline(block.spans)
    if isinstance(block, ListBlock):
        lines = []
        for number, item in enumerate(block.items, 1):
            marker = f"{number}. " if block.ordered else "- "
            body = "\n\n".join(_md_blocks(item, work)) or ""
            indent = " " * len(marker)
            body_lines = body.split("\n")
            lines.append(marker + body_lines[0])
            lines.extend((indent + line) if line else "" for line in body_lines[1:])
        return "\n".join(lines)
    if isinstance(block, Table):
        width = max(len(row) for row in block.rows)

        def cell(spans):
            return md_inline(spans, breaks=False).replace("\n", " ") or " "

        rows = [[cell(c) for c in row] + [" "] * (width - len(row)) for row in block.rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        return "\n".join(lines)
    if isinstance(block, Img):
        alt = md_escape(" ".join(block.alt.split())) if block.alt else ""
        return f"*\\[изображение{': ' + alt if alt else ''}\\]*"
    if isinstance(block, Pre):
        fence = "```"
        while fence in block.text:
            fence += "`"
        return f"{fence}\n{block.text}\n{fence}"
    if isinstance(block, Quote):
        inner = "\n\n".join(_md_blocks(block.blocks, work))
        return "\n".join(("> " + line) if line else ">" for line in inner.split("\n"))
    if isinstance(block, Rule):
        return "* * *"
    return ""


def doc_to_markdown(doc: Doc, work: Work) -> str:
    parts: list[str] = []
    total = max(1, len(doc.chapters))
    for index, chapter in enumerate(doc.chapters):
        parts.extend(_md_blocks(chapter.blocks, work))
        work.tick((index + 1) / total * 0.9)
    return "\n\n".join(parts) + "\n"


# --- Модель → TXT -------------------------------------------------------------


def _txt_blocks(blocks: list, indent: str, work: Work) -> list[str]:
    out: list[str] = []
    for block in blocks:
        work.add_block()
        text = _txt_block(block, indent, work)
        if text is not None and text.strip():
            out.append(text)
    return out


def _indent(text: str, indent: str) -> str:
    return "\n".join(indent + line if line else line for line in text.split("\n"))


def _txt_block(block, indent: str, work: Work) -> str | None:
    if isinstance(block, (Heading, Para)):
        return _indent(spans_text(block.spans), indent)
    if isinstance(block, ListBlock):
        lines = []
        for number, item in enumerate(block.items, 1):
            marker = f"{number}. " if block.ordered else "• "
            body = "\n".join(_txt_blocks(item, "", work))
            body_lines = body.split("\n")
            lines.append(indent + marker + body_lines[0])
            lines.extend(indent + " " * len(marker) + line for line in body_lines[1:])
        return "\n".join(lines)
    if isinstance(block, Table):
        return "\n".join(
            indent + "\t".join(spans_text(c).replace("\n", " ") for c in row)
            for row in block.rows
        )
    if isinstance(block, Pre):
        return _indent(block.text, indent)
    if isinstance(block, Quote):
        return "\n\n".join(_txt_blocks(block.blocks, indent + "    ", work))
    if isinstance(block, Rule):
        return indent + "* * *"
    return None


def doc_to_text(doc: Doc, work: Work, *, header: bool) -> str:
    """Модель → текст ("\\n"); `header` — в начале название и авторы (для книг)."""
    parts: list[str] = []
    if header:
        head = [line for line in (doc.title, ", ".join(doc.authors)) if line]
        if head:
            parts.append("\n".join(head))
    total = max(1, len(doc.chapters))
    for index, chapter in enumerate(doc.chapters):
        chunk = "\n\n".join(_txt_blocks(chapter.blocks, "", work))
        if chunk:
            parts.append(chunk)
        work.tick((index + 1) / total * 0.9)
    return "\n\n\n".join(parts) + "\n"
