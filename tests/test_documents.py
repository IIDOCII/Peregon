"""Этап 4: документы без LibreOffice — TXT, HTML, MD, DOCX (П19) и безопасность.

Проверяется: взаимные конвертации без порчи кириллицы (кодировки TXT/MD),
сохранение форматирования, PDF из txt/md/html (текст извлекается
pypdfium2), санитайзер HTML/MD (результат без активного содержимого и
внешних ресурсов), XXE и ZIP-бомбы в DOCX, лимиты, сквозной JobManager.
"""

from __future__ import annotations

import base64
import io
import os
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import docx
import pytest
from docx.shared import Inches

from docs_helpers import (
    EVIL_HTML,
    LAUGHS,
    assert_clean,
    bomb_zip,
    file_uri,
    make_inproc,
    many_entries_zip,
    no_children,
    jpeg_bytes,
    png_bytes,
    run,
    secret,  # noqa: F401 — фикстура
    target,
    wait_jobs,
)
from konverter.converters import _docjob, documents
from konverter.converters.base import REGISTRY, CancelToken, Category, ConversionError, Preset
from konverter.core.detect import detect
from konverter.core.jobs import JobManager, JobStatus
from test_books import pdf_text

CONV = documents.DocumentConverter()


def quote_url(url: str) -> str:
    """Так сериализатор HTML (lxml) пишет не-ASCII в href: процентами от UTF-8."""
    return quote(url, safe=":/?#[]@!$&'()*+,;=%")


LINK_HTML = f'<a href="{quote_url("https://example.org/страница")}">ссылкой</a>'
LINK_QUOTED = quote_url("https://example.org/страница")


@pytest.fixture
def inproc(monkeypatch):
    return make_inproc(monkeypatch)


def make_docx(path: Path, *, title="Отчёт", author="Иван Петров") -> Path:
    document = docx.Document()
    document.core_properties.title = title
    document.core_properties.author = author
    document.add_heading("Главный заголовок", level=1)
    paragraph = document.add_paragraph("Обычный текст, ")
    paragraph.add_run("жирный").bold = True
    paragraph.add_run(" и ")
    paragraph.add_run("курсив").italic = True
    paragraph.add_run(".")
    document.add_heading("Подраздел", level=2)
    document.add_paragraph("Пункт один", style="List Bullet")
    document.add_paragraph("Пункт два", style="List Bullet")
    document.add_paragraph("Шаг первый", style="List Number")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Имя"
    table.cell(0, 1).text = "Город"
    table.cell(1, 0).text = "Ольга"
    table.cell(1, 1).text = "Казань"
    document.add_picture(io.BytesIO(png_bytes()), width=Inches(1))
    document.add_paragraph("Последний абзац.")
    document.save(path)
    return path


def docx_text(path: Path) -> str:
    document = docx.Document(path)
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.extend(c.text for c in row.cells)
    return "\n".join(parts)


def _order(text: str, *parts: str) -> None:
    positions = [text.find(p) for p in parts]
    assert all(p >= 0 for p in positions), (parts, positions)
    assert positions == sorted(positions), (parts, positions)


MD = """# Заголовок документа

Абзац с **жирным**, *курсивом*, `кодом` и [ссылкой](https://example.org/страница).

## Список

- первый пункт
- второй пункт

1. раз
2. два

| Колонка | Значение |
|---------|----------|
| ёжик    | 42       |

```
код  с   пробелами
```

> цитата
"""


# --- Варианты -----------------------------------------------------------------------------


def test_targets_matrix():
    assert isinstance(REGISTRY[Category.DOCUMENT], documents.DocumentConverter)
    assert [t.id for t in CONV.targets("txt")] == ["docx", "pdf", "html", "md"]
    assert [t.id for t in CONV.targets("md")] == ["docx", "pdf", "html", "txt"]
    assert [t.id for t in CONV.targets("html")] == ["docx", "pdf", "md", "txt"]
    # Этап 5: DOCX → PDF, DOC/ODT/RTF → DOCX/PDF — через LibreOffice.
    assert [t.id for t in CONV.targets("docx")] == ["pdf", "html", "md", "txt"]
    for fmt in ("doc", "odt", "rtf"):
        assert [t.id for t in CONV.targets(fmt)] == ["docx", "pdf"]
    for fmt in ("pdf", "xlsx"):
        assert CONV.targets(fmt) == []
    for t in CONV.targets("txt"):
        assert t.is_long and not t.uses_preset and t.label and not t.label.startswith("target_")


# --- П19: кодировки TXT/MD ---------------------------------------------------------------


TEXT = "Привет, мир!\nВторая строка — ёлка.\n\nНовый абзац: «кавычки», №5."


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1251", "utf-16"])
def test_p19_txt_encodings_to_html_md_docx(tmp_path, inproc, encoding):
    src = tmp_path / "текст.txt"
    src.write_bytes(TEXT.encode(encoding))
    html = run(CONV, src, tmp_path / "o.html", "txt", "html").read_text("utf-8")
    assert "Привет, мир!<br>Вторая строка — ёлка." in html
    assert "<p>Новый абзац: «кавычки», №5.</p>" in html
    md = run(CONV, src, tmp_path / "o.md", "txt", "md").read_bytes().decode("utf-8")
    assert "Привет, мир\\!" in md or "Привет, мир!" in md
    assert "ёлка" in md and "«кавычки», №5" in md
    text = docx_text(run(CONV, src, tmp_path / "o.docx", "txt", "docx"))
    assert "Привет, мир!\nВторая строка — ёлка." in text
    assert "Новый абзац: «кавычки», №5." in text


def test_p19_md_cp1251(tmp_path, inproc):
    src = tmp_path / "заметки.md"
    src.write_bytes(MD.encode("cp1251"))
    html = run(CONV, src, tmp_path / "o.html", "md", "html").read_text("utf-8")
    assert "<h1>Заголовок документа</h1>" in html
    assert "ёжик" in html


# --- П19: MD ↔ HTML ↔ TXT ↔ DOCX -----------------------------------------------------------


def test_p19_md_to_html_structure(tmp_path, inproc):
    src = tmp_path / "doc.md"
    src.write_text(MD, encoding="utf-8")
    html = run(CONV, src, tmp_path / "o.html", "md", "html").read_text("utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "<title>Заголовок документа</title>" in html
    for fragment in ("<h1>Заголовок документа</h1>", "<h2>Список</h2>", "<strong>жирным</strong>",
                     "<em>курсивом</em>", "<code>кодом</code>",
                     LINK_HTML,
                     "<ul><li>первый пункт</li><li>второй пункт</li></ul>",
                     "<ol><li>раз</li><li>два</li></ol>", "<th>Колонка</th>", "<td>ёжик</td>",
                     "<pre>код  с   пробелами</pre>", "<blockquote><p>цитата</p></blockquote>"):
        assert fragment in html, fragment
    assert "Content-Security-Policy" in html and "default-src 'none'" in html


def test_p19_html_to_md_and_back(tmp_path, inproc):
    src = tmp_path / "doc.md"
    src.write_text(MD, encoding="utf-8")
    html_path = run(CONV, src, tmp_path / "a.html", "md", "html")
    md = run(CONV, html_path, tmp_path / "b.md", "html", "md").read_bytes().decode("utf-8")
    for fragment in ("# Заголовок документа", "## Список", "**жирным**", "*курсивом*", "`кодом`",
                     f"[ссылкой]({LINK_QUOTED})",
                     "- первый пункт", "1. раз", "| Колонка | Значение |", "| ёжик | 42 |",
                     "```\r\nкод  с   пробелами\r\n```", "> цитата"):
        assert fragment in md, fragment
    txt = run(CONV, html_path, tmp_path / "c.txt", "html", "txt").read_bytes().decode("utf-8")
    _order(txt, "Заголовок документа", "жирным", "• первый пункт", "1. раз", "ёжик\t42",
           "код  с   пробелами", "цитата")


def test_p19_docx_to_html_md_txt(tmp_path, inproc):
    src = make_docx(tmp_path / "отчёт.docx")
    assert detect(src).fmt == "docx"
    html = run(CONV, src, tmp_path / "o.html", "docx", "html").read_text("utf-8")
    for fragment in ("<title>Отчёт</title>", "<h1>Главный заголовок</h1>", "<h2>Подраздел</h2>",
                     "<strong>жирный</strong>", "<em>курсив</em>",
                     "<ul><li>Пункт один</li><li>Пункт два</li></ul>", "<ol><li>Шаг первый</li></ol>",
                     "<td>Ольга</td>", 'src="data:image/png;base64,'):
        assert fragment in html, fragment
    _order(html, "Главный заголовок", "Подраздел", "Пункт один", "Ольга", "data:image/png",
           "Последний абзац")
    md = run(CONV, src, tmp_path / "o.md", "docx", "md").read_bytes().decode("utf-8")
    assert "# Главный заголовок" in md and "**жирный**" in md and "- Пункт один" in md
    assert "*\\[изображение\\]*" in md and "data:" not in md
    txt = run(CONV, src, tmp_path / "o.txt", "docx", "txt").read_bytes().decode("utf-8")
    _order(txt, "Главный заголовок", "Обычный текст, жирный и курсив.", "• Пункт один",
           "Ольга\tКазань", "Последний абзац.")


def test_p19_text_formats_to_docx(tmp_path, inproc):
    src = tmp_path / "doc.md"
    src.write_text(MD, encoding="utf-8")
    out = run(CONV, src, tmp_path / "o.docx", "md", "docx")
    assert detect(out).fmt == "docx"
    document = docx.Document(out)
    styles = [p.style.name for p in document.paragraphs]
    assert "Heading 1" in styles and "Heading 2" in styles and "List Bullet" in styles
    assert "List Number" in styles
    bold = [r.text for p in document.paragraphs for r in p.runs if r.bold]
    assert "жирным" in bold
    assert document.tables[0].cell(1, 0).text == "ёжик"
    links = [rel.target_ref for rel in document.part.rels.values() if rel.is_external]
    assert links == ["https://example.org/страница"]
    assert document.core_properties.author == ""
    assert document.core_properties.comments == ""
    # DOCX → MD обратно: структура сохранилась.
    md = run(CONV, out, tmp_path / "back.md", "docx", "md").read_bytes().decode("utf-8")
    assert "# Заголовок документа" in md and "- первый пункт" in md and "1. раз" in md
    assert "[ссылкой](https://example.org/страница)" in md
    assert "`код  с   пробелами`" in md and "цитата" in md


def test_html_input_with_images(tmp_path, inproc):
    data = base64.b64encode(png_bytes()).decode()
    page = (f'<html><head><meta charset="windows-1251"><title>Картинки</title></head><body>'
            f'<p>Текст</p><img src="data:image/png;base64,{data}" alt="встроенная">'
            f'<img src="http://example.com/x.png"><img src="file:///C:/Windows/win.ini">'
            f'<img src="data:image/png;base64,!!!не base64!!!"></body></html>')
    src = tmp_path / "p.html"
    src.write_bytes(page.encode("cp1251"))
    out_docx = run(CONV, src, tmp_path / "o.docx", "html", "docx")
    document = docx.Document(out_docx)
    assert len(document.inline_shapes) == 1
    out = run(CONV, src, tmp_path / "o.md", "html", "md").read_bytes().decode("utf-8")
    assert "*\\[изображение: встроенная\\]*" in out
    assert "example.com" not in out and "win.ini" not in out


# --- PDF из txt/md/html ---------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["txt", "md", "html"])
def test_p19_pdf_cyrillic(tmp_path, inproc, fmt):
    content = {
        "txt": TEXT,
        "md": MD,
        "html": "<html><body><h1>Заголовок документа</h1><p>Абзац — ёжик и "
                "<b>жирный</b>.</p></body></html>",
    }[fmt]
    src = tmp_path / f"файл.{fmt}"
    src.write_text(content, encoding="utf-8")
    out = run(CONV, src, tmp_path / "o.pdf", fmt, "pdf")
    assert out.read_bytes().startswith(b"%PDF")
    text = pdf_text(out)
    if fmt == "txt":
        _order(text, "Привет, мир!", "ёлка", "«кавычки», №5")
    else:
        assert "Заголовок документа" in text and "ёжик" in text


def test_pdf_many_pages_limit(tmp_path, inproc, monkeypatch):
    monkeypatch.setitem(_docjob.LIMITS, "max_pages", 3)
    src = tmp_path / "big.txt"
    src.write_text("\n\n".join(f"Абзац {i} " + "текст " * 50 for i in range(300)), encoding="utf-8")
    with pytest.raises(ConversionError, match="Слишком много страниц"):
        run(CONV, src, tmp_path / "o.pdf", "txt", "pdf")


def test_txt_lines_without_blank_lines_are_paragraphs(tmp_path, inproc):
    """Русская TXT-книга: абзац = строка, пустых строк нет — 10 000 абзацев, не один."""
    count = 10_000
    src = tmp_path / "книга.txt"
    src.write_text("\n".join(f"Абзац номер {i}, текст." for i in range(count)), encoding="utf-8")
    out = run(CONV, src, tmp_path / "o.docx", "txt", "docx")
    paragraphs = [p.text for p in docx.Document(out).paragraphs if p.text.strip()]
    assert len(paragraphs) == count
    assert paragraphs[0] == "Абзац номер 0, текст." and paragraphs[-1].startswith("Абзац номер 9999")
    html = run(CONV, src, tmp_path / "o.html", "txt", "html").read_text("utf-8")
    assert html.count("<p>") == count and "<br>" not in html
    md = run(CONV, src, tmp_path / "o.md", "txt", "md").read_bytes().decode("utf-8")
    md_paragraphs = [p for p in re.split(r"(?:\r\n){2,}", md) if "Абзац номер" in p]
    assert len(md_paragraphs) == count


def test_txt_blank_line_paragraphs_keep_line_breaks(tmp_path, inproc):
    """Есть пустые строки — абзацы по ним, одиночные переводы строк внутри сохраняются."""
    text = "\n\n".join(f"Строка {i}а\nстрока {i}б" for i in range(50))
    src = tmp_path / "т.txt"
    src.write_text(text, encoding="utf-8")
    html = run(CONV, src, tmp_path / "o.html", "txt", "html").read_text("utf-8")
    assert html.count("<p>") == 50
    assert "Строка 7а<br>строка 7б" in html


def _pdf_work(limits: dict | None = None):
    """Work для прямого вызова `_pdfout` с журналом времени отметок прогресса."""
    from konverter.converters._docmodel import Work

    stamps: list[float] = []
    marks: list[int] = []

    def report(_fraction, mark):
        if not marks or mark > marks[-1]:
            stamps.append(time.monotonic())
            marks.append(mark)

    return Work({"limits": limits or {}}, report), stamps


def _big_para_doc(chars: int):
    from konverter.converters._docmodel import Chapter, Doc, Para, Span

    words = ("слово " * (chars // 6 + 1))[:chars]
    return Doc(title="Т", chapters=[Chapter("", [Para([Span(words)])])])


def test_pdf_big_block_progress_regular(tmp_path):
    """Один огромный абзац (≈400 тыс. символов): отметки прогресса идут регулярно."""
    from konverter.converters._pdfout import doc_to_pdf

    work, stamps = _pdf_work()
    start = time.monotonic()
    data = doc_to_pdf(_big_para_doc(400_000), work, book=False, preset="medium")
    end = time.monotonic()
    assert data.startswith(b"%PDF")
    points = [start, *stamps, end]
    gaps = [b - a for a, b in zip(points, points[1:])]
    assert len(stamps) > 5
    assert max(gaps) < 2.0, max(gaps)


def test_pdf_txt_3mb_progress_regular(tmp_path, monkeypatch):
    """TXT ≈3 МБ без пустых строк → PDF: паузы между отметками малы."""
    from konverter.converters import _pdfout
    from konverter.converters.documents import read_txt

    src_text = "\n".join(f"Строка {i}: " + "текст " * 22 for i in range(22_000))
    data = src_text.encode("utf-8")
    assert len(data) > 3 * 10**6
    work, stamps = _pdf_work()
    start = time.monotonic()
    doc = read_txt(work, data)
    pdf = _pdfout.doc_to_pdf(doc, work, book=False, preset="medium")
    end = time.monotonic()
    assert pdf.startswith(b"%PDF")
    points = [start, *stamps, end]
    gaps = [b - a for a, b in zip(points, points[1:])]
    assert max(gaps) < 5.0, max(gaps)


def test_pdf_max_pages_inside_big_block(tmp_path):
    """Лимит страниц срабатывает внутри одного большого блока — быстро."""
    from konverter.converters._isolated import TaskError
    from konverter.converters._pdfout import doc_to_pdf

    work, _stamps = _pdf_work({"max_pages": 3})
    start = time.monotonic()
    with pytest.raises(TaskError) as info:
        doc_to_pdf(_big_para_doc(3_000_000), work, book=False, preset="medium")
    assert info.value.key == "too_many_pages"
    assert time.monotonic() - start < 3.0


# --- Санитайзер HTML/MD ----------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["html", "md"])
def test_sanitizer_html_and_md_input(tmp_path, inproc, fmt):
    src = tmp_path / f"evil.{fmt}"
    content = EVIL_HTML if fmt == "html" else "# Markdown\n\n" + EVIL_HTML + "\n\nТекст после."
    src.write_text(content, encoding="utf-8")
    # HTML-результат: из MD — напрямую, из HTML — через MD (html → html не предлагается).
    if fmt == "md":
        html_out = run(CONV, src, tmp_path / "o.html", "md", "html")
        md_out = run(CONV, html_out, tmp_path / "o.md", "html", "md")
    else:
        md_out = run(CONV, src, tmp_path / "o.md", "html", "md")
        html_out = run(CONV, md_out, tmp_path / "o.html", "md", "html")
    html = html_out.read_text("utf-8")
    assert_clean(html.replace("Content-Security-Policy", ""))
    for fragment in ("<strong>жирный</strong>", "<em>курсив</em>",
                     f'<a href="{quote_url("https://example.com/путь")}">хорошая ссылка</a>',
                     "<li>Пункт один</li>", "<ol>", "<th>Имя</th>", "<td>Москва</td>",
                     "Текст в div"):
        assert fragment in html, fragment
    assert "плохая" in html  # текст ссылки остался, ссылка — нет
    md = md_out.read_bytes().decode("utf-8")
    assert_clean(md)
    assert "<" not in md.replace("&lt;", "")
    assert ("[хорошая ссылка](https://example.com/путь)" in md
            or f"[хорошая ссылка]({quote_url('https://example.com/путь')})" in md)
    docx_out = run(CONV, src, tmp_path / "o.docx", fmt, "docx")
    with zipfile.ZipFile(docx_out) as archive:
        rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
    assert "javascript" not in rels.lower() and "evil.example" not in rels
    assert "https://example.com/путь" in rels


def test_md_output_escapes_markup(tmp_path, inproc):
    src = tmp_path / "t.txt"
    src.write_text("Текст <script>alert(1)</script> и *звёздочки* _под_ [ссылка](javascript:x)\n"
                   "# не заголовок\n1. не список", encoding="utf-8")
    md = run(CONV, src, tmp_path / "o.md", "txt", "md").read_bytes().decode("utf-8")
    assert "<script" not in md and "&lt;script\\>" in md
    assert "\\*звёздочки\\*" in md and "\\_под\\_" in md and "\\[ссылка\\]" in md
    assert "\\# не заголовок" in md and "1\\. не список" in md
    # MD → HTML этого результата: теги остаются текстом.
    html = run(CONV, tmp_path / "o.md", tmp_path / "o.html", "md", "html").read_text("utf-8")
    assert "<script" not in html and "&lt;script&gt;" in html
    assert "<h1>" not in html and "<ol>" not in html


def test_html_billion_laughs_and_deep_nesting(tmp_path, inproc):
    src = tmp_path / "lol.html"
    src.write_text(LAUGHS.replace("lolz", "html") + "<html><body><p>&lol9; текст</p>"
                   + "<div>" * 5000 + "глубоко" + "</div>" * 5000 + "</body></html>",
                   encoding="utf-8")
    t0 = time.monotonic()
    out = run(CONV, src, tmp_path / "o.txt", "html", "txt")
    assert time.monotonic() - t0 < 10
    text = out.read_bytes().decode("utf-8")
    assert "текст" in text and "lollol" not in text
    assert out.stat().st_size < 100_000


# --- XXE и ZIP-бомбы в DOCX ----------------------------------------------------------------


def _rewrite_docx(src: Path, dst: Path, name: str, transform) -> Path:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == name:
                data = transform(data)
            zout.writestr(info.filename, data)
    return dst


def test_xxe_in_docx(tmp_path, inproc, secret):
    base = make_docx(tmp_path / "base.docx")

    def inject(data: bytes) -> bytes:
        text = data.decode("utf-8")
        decl, rest = text.split("?>", 1)
        doctype = f'<!DOCTYPE w:document [<!ENTITY xxe SYSTEM "{file_uri(secret)}">]>'
        rest = rest.replace("Последний абзац.", "&xxe;")
        return (decl + "?>" + doctype + rest).encode("utf-8")

    src = _rewrite_docx(base, tmp_path / "xxe.docx", "word/document.xml", inject)
    for tid in ("html", "txt"):
        out = tmp_path / f"o.{tid}"
        with pytest.raises(ConversionError, match="сущност"):
            run(CONV, src, out, "docx", tid)
        assert "СЕКРЕТ" not in out.read_text("utf-8", errors="ignore")

    def laughs(data: bytes) -> bytes:
        decl, rest = data.decode("utf-8").split("?>", 1)
        return (decl + "?>" + LAUGHS.replace("lolz", "w:document")
                + rest.replace("Последний абзац.", "&lol9;")).encode("utf-8")

    src2 = _rewrite_docx(base, tmp_path / "lol.docx", "word/document.xml", laughs)
    t0 = time.monotonic()
    with pytest.raises(ConversionError, match="сущност"):
        run(CONV, src2, tmp_path / "o2.txt", "docx", "txt")
    assert time.monotonic() - t0 < 5


@pytest.mark.parametrize("fmt", ["txt", "md", "html"])
def test_docx_output_metadata(tmp_path, inproc, fmt):
    """Свойства DOCX-результата: не из шаблона python-docx (2013 год, Mac Word, Normal.dotm)."""
    import datetime

    content = {"txt": TEXT, "md": MD,
               "html": '<html><head><meta name="author" content="Кто-то"><title>Т</title></head>'
                       "<body><p>Текст</p></body></html>"}[fmt]
    src = tmp_path / f"ф.{fmt}"
    src.write_text(content, encoding="utf-8")
    before = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    out = run(CONV, src, tmp_path / "o.docx", fmt, "docx")
    after = datetime.datetime.now(datetime.timezone.utc)
    props = docx.Document(out).core_properties
    for stamp in (props.created, props.modified):
        assert stamp is not None
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=datetime.timezone.utc)
        assert before - datetime.timedelta(seconds=2) <= stamp <= after
    assert props.author == "" and props.last_modified_by == "" and props.comments == ""
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        app = archive.read("docProps/app.xml").decode("utf-8")
        core = archive.read("docProps/core.xml").decode("utf-8")
        rels = archive.read("_rels/.rels").decode("utf-8")
    assert "<Application>Peregon</Application>" in app
    for bad in ("Macintosh", "Normal.dotm", "AppVersion", "Template"):
        assert bad not in app, bad
    assert "python-docx" not in core and "2013" not in core
    assert "docProps/thumbnail.jpeg" not in names and "thumbnail" not in rels


_DOCTYPE_XML ='<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "y">]><r>&x;</r>'


def _zip_with(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


@pytest.mark.parametrize("name,payload", [
    ("a.xml", b"\xef\xbb\xbf" + _DOCTYPE_XML.encode("utf-8")),
    ("a.xml", b"\xef\xbb\xbf  \r\n" + _DOCTYPE_XML.encode("utf-8")),
    ("a.xml", _DOCTYPE_XML.encode("utf-32")),  # с BOM (LE)
    ("a.xml", b"\x00\x00\xfe\xff" + _DOCTYPE_XML.encode("utf-32-be")),
    ("a.xml", _DOCTYPE_XML.encode("utf-32-le")),  # без BOM: <\0\0\0
    ("a.xml", _DOCTYPE_XML.encode("utf-32-be")),  # без BOM: \0\0\0<
    ("a.xml", ("  " + _DOCTYPE_XML).encode("utf-32-le")),
    ("a.xml", ("  " + _DOCTYPE_XML).encode("utf-16-le")),
    # Расширение не XML — проверяется по содержимому.
    ("word/media/x.bin", b"\xef\xbb\xbf" + _DOCTYPE_XML.encode("utf-8")),
    ("customXml/item", _DOCTYPE_XML.encode("utf-32-be")),
    ("docProps/thing.dat", _DOCTYPE_XML.encode("utf-16")),
    ("noext", ("\n " + _DOCTYPE_XML).encode("utf-8")),
])
def test_precheck_ooxml_doctype_encodings(tmp_path, name, payload):
    from konverter.converters._isolated import TaskError
    from konverter.converters._safezip import precheck_ooxml

    src = _zip_with(tmp_path / "x.docx", {"[Content_Types].xml": b"<Types/>", name: payload})
    with pytest.raises(TaskError) as info:
        precheck_ooxml(str(src))
    assert info.value.key == "xml_entities"


def test_precheck_ooxml_binary_parts_pass(tmp_path):
    """Картинки, шрифты и прочие двоичные части не принимаются за XML."""
    import random

    from konverter.converters._safezip import precheck_ooxml

    rng = random.Random(7)
    members = {
        "[Content_Types].xml": b'\xef\xbb\xbf<?xml version="1.0"?><Types/>',
        "word/media/a.png": png_bytes(),
        "word/media/b.jpg": jpeg_bytes(),
        "word/media/c.gif": b"GIF89a" + b"<!x" * 100,
        "word/fonts/f.odttf": bytes(rng.randrange(256) for _ in range(200_000)),
        "word/vbaProject.bin": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"<!" * 100,
        "word/x.utf32.xml": '<?xml version="1.0"?><r/>'.encode("utf-32"),
        "word/empty.xml": b"",
    }
    precheck_ooxml(str(_zip_with(tmp_path / "ok.docx", members)))


def test_zip_bombs_in_docx(tmp_path, inproc):
    base = make_docx(tmp_path / "base.docx")
    with zipfile.ZipFile(base) as archive:
        members = {n: archive.read(n) for n in archive.namelist()}
    members.pop("word/media/image1.png", None)
    bomb = bomb_zip(tmp_path / "bomb.docx", members=dict(members), bomb_name="word/media/image1.png")
    t0 = time.monotonic()
    with pytest.raises(ConversionError, match="ZIP-бомб"):
        run(CONV, bomb, tmp_path / "o.txt", "docx", "txt")
    assert time.monotonic() - t0 < 10
    many = many_entries_zip(tmp_path / "many.docx", first=members)
    with pytest.raises(ConversionError, match="Слишком много частей"):
        run(CONV, many, tmp_path / "o2.txt", "docx", "txt")


# --- Битые, пустые, мусорные; JobManager ----------------------------------------------------


@pytest.mark.parametrize("payload", [b"PK\x03\x04" + b"\x00" * 100, os.urandom(2048),
                                     b"PK\x05\x06" + b"\x00" * 18])
def test_garbage_docx(tmp_path, inproc, payload):
    src = tmp_path / "x.docx"
    src.write_bytes(payload)
    with pytest.raises(ConversionError):
        run(CONV, src, tmp_path / "o.txt", "docx", "txt")


def test_docx_without_document_part(tmp_path, inproc):
    src = tmp_path / "x.docx"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    with pytest.raises(ConversionError):
        run(CONV, src, tmp_path / "o.txt", "docx", "txt")


def test_empty_and_whitespace_inputs(tmp_path, inproc):
    empty = tmp_path / "e.txt"
    empty.write_bytes(b"")
    with pytest.raises(ConversionError, match="нет содержимого"):
        run(CONV, empty, tmp_path / "o.html", "txt", "html")
    blank = tmp_path / "b.html"
    blank.write_text("<html><body>   <script>x()</script></body></html>", encoding="utf-8")
    with pytest.raises(ConversionError, match="нет содержимого"):
        run(CONV, blank, tmp_path / "o.md", "html", "md")


def test_input_size_limit(tmp_path, inproc, monkeypatch):
    monkeypatch.setattr(_docjob, "MAX_TEXT_INPUT_BYTES", 10)
    src = tmp_path / "t.txt"
    src.write_text("слишком длинный текст", encoding="utf-8")
    with pytest.raises(ConversionError, match="Слишком большой файл"):
        run(CONV, src, tmp_path / "o.md", "txt", "md")


def test_output_size_and_disk_checks(tmp_path, inproc, monkeypatch):
    monkeypatch.setattr(_docjob, "MAX_OUTPUT_BYTES", 100)
    src = tmp_path / "t.txt"
    src.write_text("текст " * 200, encoding="utf-8")
    with pytest.raises(ConversionError, match="Слишком большой результат"):
        run(CONV, src, tmp_path / "o.md", "txt", "md")
    monkeypatch.setattr(_docjob, "MAX_OUTPUT_BYTES", 2**30)
    monkeypatch.setattr(_docjob, "DISK_RESERVE", 2**60)
    with pytest.raises(ConversionError, match="Недостаточно места"):
        run(CONV, src, tmp_path / "o2.md", "txt", "md")


def test_job_manager_end_to_end(tmp_path):
    folder = tmp_path / "документы"
    folder.mkdir()
    src = folder / "заметка.md"
    src.write_text(MD, encoding="utf-8")
    (folder / "заметка.html").write_text("занято", encoding="utf-8")
    manager = JobManager(max_workers=1)
    try:
        job = manager.submit([src], CONV, target(CONV, "md", "html"), Preset.MEDIUM, folder,
                             formats=["md"])
        wait_jobs([job])
    finally:
        manager.shutdown(timeout=10)
    assert job.status == JobStatus.DONE, job.error_message
    assert job.result_path == folder / "заметка (1).html"
    assert "<h1>Заголовок документа</h1>" in job.result_path.read_text("utf-8")
    assert (folder / "заметка.html").read_text("utf-8") == "занято"
    assert no_children()


def test_real_child_docx_to_md(tmp_path):
    src = make_docx(tmp_path / "отчёт с пробелами.docx")
    out = tmp_path / "o.md"
    out.touch()
    CONV.convert([src], out, target(CONV, "docx", "md"), Preset.MEDIUM, lambda _p: None,
                 CancelToken(), formats=["docx"])
    assert "# Главный заголовок" in out.read_bytes().decode("utf-8")
    assert re.search(r"\| \*\*Имя\*\* \| \*\*Город\*\* \||\| Имя \| Город \|",
                     out.read_bytes().decode("utf-8"))
    assert no_children()
