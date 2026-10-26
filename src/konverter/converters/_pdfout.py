"""Модель документа → PDF (fpdf2) — только в дочернем процессе.

Простой рендер: A4, поля 18/15 мм, абзацы с переносом по словам (длинные
слова fpdf2 режет посимвольно), заголовки крупнее и жирным, списки с
отступом, таблицы — строками «ячейка | ячейка», код и преформатированный
текст — моноширинным шрифтом, ссылки — кликабельные (только http/https/mailto,
уже проверенные моделью). У книг каждая глава с новой страницы и попадает
в закладки PDF; обложка — отдельной первой страницей.

Шрифты — из системы (кириллица): `%WINDIR%\\Fonts` — Arial, затем Segoe UI,
Calibri, Times New Roman (жирный/курсив — из того же семейства, если есть,
иначе обычный); моноширинный — Consolas или Courier New. В проект шрифты не
копируются. Ни одного — `TaskError("no_font")`. Символов, которых нет в
шрифте (иероглифы, эмодзи), в PDF не будет (fpdf2 пишет предупреждение в лог).

Картинки: заново кодируются Pillow (уменьшение по пресету до `IMAGE_SIDE`,
JPEG для непрозрачных, PNG для прозрачных) — это и проверка, и экономия
размера. Пиксели одной картинки ≤ `Work.max_image_pixels` (проверено при
загрузке в модель); родитель держит на время задачи резерв в общем бюджете
`IMAGE_BUDGET`. Страниц ≤ `max_pages`: проверяется после каждого блока и
внутри блока — длинный текст пишется кусками по `PIECE_CHARS` символов,
между кусками — проверка числа страниц и отметка прогресса (сторож зависания).
"""

from __future__ import annotations

import io
import logging
import os
import time

from ._docmodel import (
    Doc,
    Heading,
    Img,
    ListBlock,
    Para,
    Picture,
    Pre,
    Quote,
    Rule,
    Span,
    Table,
    Work,
    chapter_label,
    spans_text,
)
from ._isolated import TaskError

MAX_PAGES = 5000
# Наибольшая сторона картинки в PDF, пикселей, и качество JPEG по пресету.
IMAGE_SIDE = {"high": 2400, "medium": 1600, "small": 1000}
JPEG_QUALITY = {"high": 90, "medium": 82, "small": 72}

# Семейства шрифтов: (обычный, жирный, курсив, жирный курсив).
FONT_FAMILIES = (
    ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
    ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
    ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
)
MONO_FONTS = ("consola.ttf", "cour.ttf")

_HEADING_SIZE = {1: 20, 2: 16, 3: 14, 4: 12.5, 5: 12, 6: 11.5}
_BODY_SIZE = 11
_LINE = 5.6
_INDENT = 7.0
_MAX_INDENT = 50.0


# Наибольший кусок текста за один вызов `pdf.write`, символов.
PIECE_CHARS = 2000


def _pieces(text: str, size: int = PIECE_CHARS):
    """Текст кусками ≤ `size` символов; разрез — после пробела/перевода строки, если есть."""
    start = 0
    length = len(text)
    while length - start > size:
        end = start + size
        cut = max(text.rfind(" ", start + size // 2, end), text.rfind("\n", start + size // 2, end))
        if cut >= 0:
            end = cut + 1
        yield text[start:end]
        start = end
    if start < length:
        yield text[start:]


def fonts_dir() -> str:
    return os.path.join(os.environ.get("WINDIR") or r"C:\Windows", "Fonts")


def find_fonts(folder: str | None = None) -> tuple[dict[str, str], str | None]:
    """Пути шрифтов: ({"": обычный, "B": …, "I": …, "BI": …}, моноширинный | None)."""
    folder = folder or fonts_dir()

    def path(name: str) -> str | None:
        full = os.path.join(folder, name)
        return full if os.path.isfile(full) else None

    styles: dict[str, str] = {}
    for family in FONT_FAMILIES:
        regular = path(family[0])
        if regular is None:
            continue
        styles[""] = regular
        for key, name in zip(("B", "I", "BI"), family[1:]):
            styles[key] = path(name) or regular
        break
    if not styles:
        raise TaskError("no_font")
    mono = next((p for p in (path(n) for n in MONO_FONTS) if p), None)
    return styles, mono


class _PdfWriter:
    def __init__(self, doc: Doc, work: Work, *, book: bool, preset: str) -> None:
        from fpdf import FPDF

        logging.getLogger("fpdf").setLevel(logging.ERROR)
        logging.getLogger("fontTools").setLevel(logging.ERROR)
        self.doc = doc
        self.work = work
        self.book = book
        self.side = IMAGE_SIDE.get(preset, IMAGE_SIDE["medium"])
        self.quality = JPEG_QUALITY.get(preset, JPEG_QUALITY["medium"])
        self.max_pages = work.lim("max_pages", MAX_PAGES)
        styles, mono = find_fonts(work.limits.get("fonts_dir"))
        pdf = FPDF(format="A4", unit="mm")
        pdf.set_margins(18, 15, 18)
        pdf.set_auto_page_break(True, margin=15)
        for key, font_path in styles.items():
            pdf.add_font("Main", key, font_path)
        self.mono = False
        if mono:
            pdf.add_font("Mono", "", mono)
            self.mono = True
        pdf.set_font("Main", "", _BODY_SIZE)
        pdf.set_title(doc.title or "")
        if doc.authors:
            pdf.set_author(", ".join(doc.authors))
        pdf.set_creator("Peregon")
        if doc.language:
            pdf.set_lang(doc.language)
        self.pdf = pdf
        self.left = pdf.l_margin
        self._last_tick = time.monotonic()

    # -- служебное --

    def _check_pages(self) -> None:
        if self.pdf.page_no() > self.max_pages:
            raise TaskError("too_many_pages", self.max_pages)

    def _progress(self) -> None:
        """Между кусками текста: лимит страниц и (не чаще раза в 0,25 с) отметка."""
        self._check_pages()
        now = time.monotonic()
        if now - self._last_tick >= 0.25:
            self._last_tick = now
            self.work.tick()

    def _font(self, span: Span | None, size: float, bold: bool = False, italic: bool = False):
        if span is not None and span.code and self.mono:
            self.pdf.set_font("Mono", "", size * 0.92)
            return
        b = bold or (span is not None and span.bold)
        i = italic or (span is not None and span.italic)
        self.pdf.set_font("Main", ("B" if b else "") + ("I" if i else ""), size)

    def _spans(self, spans: list[Span], size: float = _BODY_SIZE, *, bold: bool = False,
               italic: bool = False, line: float = _LINE) -> None:
        pdf = self.pdf
        wrote = False
        for span in spans:
            text = span.text.replace("\t", "    ")
            if not text:
                continue
            self._font(span, size, bold, italic)
            if span.href:
                pdf.set_text_color(20, 60, 170)
            # Длинный текст — кусками: внутри одного `pdf.write` нет ни
            # прогресса, ни проверки числа страниц (≈5 с на мегабайт текста).
            for piece in _pieces(text):
                pdf.write(line, piece, link=span.href or "")
                self._progress()
            if span.href:
                pdf.set_text_color(0, 0, 0)
            wrote = True
        if wrote:
            pdf.ln(line)
        self._font(None, _BODY_SIZE)

    def _margin(self, indent: float) -> None:
        margin = self.left + min(indent, _MAX_INDENT)
        self.pdf.set_left_margin(margin)
        self.pdf.set_x(margin)

    # -- блоки --

    def blocks(self, blocks: list, indent: float = 0.0) -> None:
        for block in blocks:
            self.work.add_block()
            self.block(block, indent)
            self._check_pages()

    def block(self, block, indent: float) -> None:
        pdf = self.pdf
        self._margin(indent)
        if isinstance(block, Heading):
            size = _HEADING_SIZE.get(block.level, _BODY_SIZE)
            pdf.ln(3 if block.level > 1 else 4)
            self._spans(block.spans, size, bold=True, line=size * 0.5)
            pdf.ln(1.5)
        elif isinstance(block, Para):
            self._spans(block.spans)
            pdf.ln(1.8)
        elif isinstance(block, ListBlock):
            for number, item in enumerate(block.items, 1):
                marker = f"{number}. " if block.ordered else "• "
                self._margin(indent)
                self._font(None, _BODY_SIZE)
                first, rest = (item[0], item[1:]) if item and isinstance(item[0], Para) else (None, item)
                spans = [Span(marker)] + (list(first.spans) if first else [])
                self._spans(spans)
                self.blocks(rest, indent + _INDENT)
            pdf.ln(1.8)
        elif isinstance(block, Table):
            for index, row in enumerate(block.rows):
                spans: list[Span] = []
                for cell_index, cell in enumerate(row):
                    if cell_index:
                        spans.append(Span("  |  "))
                    spans.extend(Span(s.text.replace("\n", " "), s.bold, s.italic, s.code, s.href)
                                 for s in cell)
                self._spans(spans or [Span(" ")], bold=index < block.header)
            pdf.ln(1.8)
        elif isinstance(block, Pre):
            self._spans([Span(block.text, code=True)], 9.5, line=4.6)
            pdf.ln(1.8)
        elif isinstance(block, Quote):
            self.blocks(block.blocks, indent + _INDENT)
            self._margin(indent)
        elif isinstance(block, Rule):
            self._font(None, _BODY_SIZE)
            pdf.cell(0, _LINE, "* * *", align="C", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.8)
        elif isinstance(block, Img):
            picture = self.doc.images.get(block.id)
            if picture is not None:
                self.image(picture, full_page=False)
        self._margin(indent)

    def _encode(self, picture: Picture) -> tuple[bytes, int, int] | None:
        from PIL import Image

        try:
            with Image.open(io.BytesIO(picture.data)) as image:
                image.load()
                has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
                image = image.convert("RGBA" if has_alpha else "RGB")
                if max(image.size) > self.side:
                    image.thumbnail((self.side, self.side))
                out = io.BytesIO()
                if has_alpha:
                    image.save(out, format="PNG", compress_level=6)
                else:
                    image.save(out, format="JPEG", quality=self.quality)
                return out.getvalue(), image.size[0], image.size[1]
        except Exception:  # noqa: BLE001 — битая картинка пропускается
            return None

    def image(self, picture: Picture, *, full_page: bool) -> None:
        encoded = self._encode(picture)
        if encoded is None:
            return
        data, width, height = encoded
        pdf = self.pdf
        area_w = pdf.w - pdf.l_margin - pdf.r_margin
        area_h = pdf.h - pdf.t_margin - pdf.b_margin
        # Естественный размер — 150 точек на дюйм, но не больше области.
        w = width / 150 * 25.4
        h = height / 150 * 25.4
        limit_h = area_h if full_page else area_h * 0.85
        scale = min(1.0, area_w / w, limit_h / h) if not full_page else min(area_w / w, limit_h / h)
        w, h = w * scale, h * scale
        if pdf.get_y() + h > pdf.page_break_trigger:
            pdf.add_page()
        x = pdf.l_margin + (area_w - w) / 2
        try:
            pdf.image(io.BytesIO(data), x=x, y=pdf.get_y(), w=w, h=h)
        except Exception:  # noqa: BLE001
            return
        pdf.set_y(pdf.get_y() + h + 2)
        self.work.tick()

    # -- документ --

    def render(self) -> bytes:
        pdf = self.pdf
        doc = self.doc
        chapters = doc.chapters
        if self.book and doc.cover and doc.cover in doc.images:
            pdf.add_page()
            self.image(doc.images[doc.cover], full_page=True)
        total = max(1, len(chapters))
        for index, chapter in enumerate(chapters):
            if self.book or index == 0:
                pdf.add_page()
            if self.book:
                pdf.set_font("Main", "", _BODY_SIZE)
                label = " ".join(chapter_label(chapter, index).split())
                try:
                    pdf.start_section(label[:200] or f"Глава {index + 1}", level=0)
                except Exception:  # noqa: BLE001 — закладка не обязательна
                    pass
            self.blocks(chapter.blocks)
            self.work.tick(0.05 + 0.85 * (index + 1) / total)
        if pdf.page_no() == 0:
            pdf.add_page()
        self.work.tick(0.92)
        return bytes(pdf.output())


def doc_to_pdf(doc: Doc, work: Work, *, book: bool, preset: str) -> bytes:
    """Модель → байты PDF."""
    has_text = any(spans_text(getattr(b, "spans", [])) or not isinstance(b, (Para, Heading))
                   for ch in doc.chapters for b in ch.blocks)
    if not has_text and not doc.cover:
        raise TaskError("empty")
    return _PdfWriter(doc, work, book=book, preset=preset).render()
