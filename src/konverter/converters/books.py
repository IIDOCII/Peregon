"""Конвертер книг (этап 4): EPUB, FB2, MOBI → EPUB, FB2, PDF, TXT.

Варианты (вход — коды "epub", "fb2", "mobi" из detect; «в тот же формат» не
предлагается), в каноническом порядке: EPUB, FB2, PDF, TXT. MOBI — только
чтение. Все варианты `is_long=True` (дочерний процесс, прогресс по главам и
блокам, «Отмена»). Пресет влияет только на PDF (размер и качество картинок,
см. `_pdfout`); в EPUB/FB2 картинки переносятся без перекодирования
(JPEG/PNG — как есть, прочие растровые — в PNG), в TXT — не переносятся.

Что сохраняется: название, авторы, язык, порядок глав, заголовки, абзацы,
жирный/курсив/код, ссылки http/https/mailto, списки, таблицы, цитаты,
картинки и обложка (EPUB ↔ FB2, MOBI → EPUB/FB2/PDF). Не сохраняются:
оформление (CSS), шрифты, сноски как ссылки (текст сносок остаётся),
внутренние ссылки между главами, SVG-картинки (могут содержать скрипты).

Чтение:
* EPUB 2/3 — `META-INF/container.xml` → OPF: метаданные (dc:title,
  dc:creator, dc:language), manifest, spine (порядок глав), обложка (EPUB 3
  `properties="cover-image"`, EPUB 2 `<meta name="cover">`), оглавление
  (EPUB 3 nav или EPUB 2 NCX) — подписи глав. Страница-обложка (только
  картинка обложки, без текста) как глава не переносится. DRM
  (`encryption.xml` с зашифрованными не-шрифтами) — ошибка.
* FB2 — `description/title-info` (название, авторы, язык, обложка), тела
  (`body`; тело примечаний — главой «Примечания»), секции верхнего уровня —
  главы, вложенные — заголовки; картинки — из `binary` (base64).
  Узлы текста больше 10 МБ (огромная картинка base64) libxml2 без
  `huge_tree` не разбирает: при строгом разборе это ошибка, затем идёт
  разбор с восстановлением (`recover`), в котором такая картинка теряется
  (или файл признаётся повреждённым).
* MOBI (MOBI 6, PalmDOC LZ77 или без сжатия; совмещённые MOBI 6 + KF8 —
  по части MOBI 6): заголовки PalmDB/PalmDOC/MOBI/EXTH (название 503 или
  полное имя, авторы 100, язык 524, обложка 201), текстовые записи с
  удалением хвостовых данных (флаги extra data), HTML разбирается
  санитайзером модели; главы — по `<mbp:pagebreak/>` (или по заголовкам
  верхнего уровня, если разрывов нет), картинки — по `recindex`. HUFF/CDIC,
  DRM (любое шифрование), KF8 без MOBI 6 (AZW3) — «Этот вариант MOBI не
  поддерживается».

Запись:
* EPUB 3 с NCX для читалок EPUB 2: `mimetype` первым и без сжатия,
  container.xml, content.opf (dc:identifier urn:uuid, dc:title, dc:creator,
  dc:language, dcterms:modified, обложка в обоих видах), nav.xhtml,
  toc.ncx, главы XHTML (только элементы модели — см. санитайзер в
  `_docmodel`), картинки, свой style.css без url()/@import.
* FB2 2.0 (UTF-8): title-info (жанр "unrecognised", автор — имя/фамилия по
  последнему слову), document-info, одна `body` с секцией на главу, картинки
  в `binary`. Переводы строк внутри абзаца → отдельные `<p>`; списки — абзацы
  с маркерами (в FB2 списков нет); подзаголовки → `<subtitle>`.
* TXT — UTF-8 без BOM, переводы строк Windows; в начале название и авторы.
* PDF — `_pdfout` (глава с новой страницы, закладки, обложка первой страницей).
"""

from __future__ import annotations

import base64
import datetime
import io
import posixpath
import struct
import uuid
import zipfile
from pathlib import Path

from konverter.strings import tr

from . import _docjob
from .base import CancelToken, Category, Preset, ProgressCallback, Target

INPUTS = frozenset({"epub", "fb2", "mobi"})
# (id, расширение, uses_preset)
_OUTPUTS = (
    ("epub", "epub", False),
    ("fb2", "fb2", False),
    ("pdf", "pdf", True),
    ("txt", "txt", False),
)
TARGET_ORDER = tuple(item[0] for item in _OUTPUTS)


class BookConverter:
    """Конвертер книг (см. докстринг модуля)."""

    category = Category.BOOK
    target_order = TARGET_ORDER
    queue = "docs"

    def targets(self, fmt: str) -> list[Target]:
        fmt = (fmt or "").lower()
        if fmt not in INPUTS:
            return []
        return [
            Target(id=tid, label=tr(f"target_book_{tid}"), extension=ext, uses_preset=uses,
                   is_long=True)
            for tid, ext, uses in _OUTPUTS
            if tid != fmt
        ]

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
        _docjob.run_conversion(
            "book", self.targets, sources, dst, target, preset, progress, cancel, formats,
            text_formats=frozenset({"fb2"}),
        )


# =============================================================================
# Дочерний процесс
# =============================================================================


def child_convert(work) -> None:
    from ._isolated import TaskError

    args = work.args
    fmt = args["fmt"]
    target = args["target"]
    src = args["src"]
    if fmt == "epub":
        doc = read_epub(work, src)
    elif fmt == "fb2":
        doc = read_fb2(work, _docjob.read_input(src, _docjob.MAX_TEXT_INPUT_BYTES))
    elif fmt == "mobi":
        doc = read_mobi(work, _docjob.read_input(src, _docjob.MAX_INPUT_BYTES))
    else:
        raise TaskError("damaged")
    if not doc.title:
        doc.title = str(args.get("stem") or "")
    if not doc.chapters:
        raise TaskError("empty")
    work.tick(0.4)
    if target == "epub":
        data = write_epub(doc, work)
    elif target == "fb2":
        data = write_fb2(doc, work)
    elif target == "txt":
        from ._docmodel import doc_to_text

        data = doc_to_text(doc, work, header=True).replace("\n", "\r\n").encode("utf-8")
    elif target == "pdf":
        from ._pdfout import doc_to_pdf

        data = doc_to_pdf(doc, work, book=True, preset=str(args.get("preset", "medium")))
    else:
        raise ValueError("unknown target")
    _docjob.write_bytes(args["dst"], data, work.limits)


def _text_of(element) -> str:
    from ._docmodel import clean_text

    return " ".join(clean_text("".join(t for t in element.itertext() if isinstance(t, str))).split())


def _check_chapters(work, count: int) -> None:
    from ._isolated import TaskError

    if count > work.max_chapters:
        raise TaskError("too_many_chapters", work.max_chapters)


# --- EPUB: чтение --------------------------------------------------------------

_XHTML_TYPES = frozenset({"application/xhtml+xml", "text/html", "application/xml", "text/xml"})
_FONT_EXT = (".ttf", ".otf", ".woff", ".woff2")


def _parse_markup(data: bytes, work):
    """XHTML главы: строгий безопасный XML, при ошибке — HTML-парсер."""
    from ._isolated import TaskError
    from ._safexml import parse_html, parse_xml
    from ._text import decode_text

    try:
        return parse_xml(data)
    except TaskError as exc:
        if exc.key != "damaged":
            raise
    return parse_html(decode_text(data))


def read_epub(work, path: str):
    from ._docmodel import Chapter, Doc, HtmlReader, Img, Pictures, clean_text, plain_text
    from ._isolated import TaskError
    from ._safexml import localname, parse_xml
    from ._safezip import SafeZip, resolve

    max_xml = work.lim("max_xml_bytes", 128 * 2**20)
    doc = Doc()
    with SafeZip(path, work.limits, tick=work.tick) as archive:

        def read(name: str, limit: int = max_xml) -> bytes:
            try:
                return archive.read(name, limit)
            except KeyError:
                raise TaskError("damaged") from None

        if archive.has("META-INF/encryption.xml"):
            enc = parse_xml(read("META-INF/encryption.xml"), recover=True)
            for element in enc.iter():
                if localname(element) == "cipherreference":
                    uri = (element.get("URI") or "").lower()
                    if not uri.endswith(_FONT_EXT):
                        raise TaskError("epub_drm")
        container = parse_xml(read("META-INF/container.xml"), recover=True)
        opf_path = None
        for element in container.iter():
            if localname(element) == "rootfile":
                full = resolve("", element.get("full-path") or "")
                if full and archive.has(full):
                    opf_path = full
                    break
        if opf_path is None:
            raise TaskError("damaged")
        opf = parse_xml(read(opf_path), recover=True)
        opf_dir = posixpath.dirname(opf_path)

        manifest: dict[str, tuple[str, str, str]] = {}
        meta_cover = None
        spine: list[str] = []
        spine_toc = None
        for element in opf.iter():
            name = localname(element)
            if name == "title" and not doc.title:
                doc.title = _text_of(element)[:1000]
            elif name == "creator":
                author = _text_of(element)[:300]
                if author and author not in doc.authors and len(doc.authors) < 50:
                    doc.authors.append(author)
            elif name == "language" and not doc.language:
                doc.language = _text_of(element)[:35]
            elif name == "meta" and (element.get("name") or "").lower() == "cover":
                meta_cover = element.get("content")
            elif name == "item":
                item_id = element.get("id") or ""
                href = resolve(opf_dir, element.get("href") or "")
                if item_id and href:
                    manifest[item_id] = (href, (element.get("media-type") or "").lower(),
                                         (element.get("properties") or "").lower())
            elif name == "spine":
                spine_toc = element.get("toc")
            elif name == "itemref":
                idref = element.get("idref")
                if idref and (element.get("linear") or "yes").lower() != "no":
                    spine.append(idref)
        _check_chapters(work, len(spine))

        pictures = Pictures(work, doc.images)
        max_image = work.max_image_bytes

        def loader(base_dir: str):
            def load(src: str) -> str | None:
                full = resolve(base_dir, src)
                if full is None or not archive.has(full):
                    return None
                if pictures.known(full):  # повтор — из кэша, без новой распаковки
                    return pictures.add(b"", key=full)
                return pictures.add(archive.read(full, max_image), key=full)

            return load

        # Обложка.
        cover_path = None
        for item_id, (href, _media, props) in manifest.items():
            if "cover-image" in props.split():
                cover_path = href
                break
        if cover_path is None and meta_cover and meta_cover in manifest:
            cover_path = manifest[meta_cover][0]
        if cover_path and archive.has(cover_path) and not pictures.known(cover_path):
            doc.cover = pictures.add(archive.read(cover_path, max_image), key=cover_path)

        labels = _epub_toc(archive, manifest, spine_toc, read)

        total = max(1, len(spine))
        for index, idref in enumerate(spine):
            item = manifest.get(idref)
            if item is None or item[1] not in _XHTML_TYPES or not archive.has(item[0]):
                continue
            root = _parse_markup(read(item[0]), work)
            blocks = HtmlReader(work, loader(posixpath.dirname(item[0]))).blocks(root)
            if not blocks:
                continue
            if (doc.cover and all(isinstance(b, Img) and b.id == doc.cover for b in blocks)
                    and not plain_text(blocks).strip()):
                continue  # страница-обложка
            doc.chapters.append(Chapter(clean_text(labels.get(item[0], "")), blocks))
            work.tick(0.4 * (index + 1) / total)
    return doc


def _epub_toc(archive, manifest, spine_toc, read) -> dict[str, str]:
    """Подписи глав из оглавления: путь файла → подпись (первая встреченная)."""
    from ._isolated import TaskError
    from ._safexml import localname, parse_xml
    from ._safezip import resolve

    labels: dict[str, str] = {}

    def put(base: str, href: str | None, label: str) -> None:
        if not href or not label:
            return
        full = resolve(base, href)
        if full and full not in labels:
            labels[full] = label[:300]

    try:
        nav = next((h for h, _m, p in manifest.values() if "nav" in p.split()), None)
        if nav and archive.has(nav):
            root = _parse_markup(read(nav), None)
            for element in root.iter():
                if localname(element) == "nav":
                    kind = "".join(v for k, v in element.attrib.items() if k.endswith("type"))
                    if "toc" not in kind:
                        continue
                    for link in element.iter():
                        if localname(link) == "a":
                            put(posixpath.dirname(nav), link.get("href"), _text_of(link))
        ncx = manifest.get(spine_toc or "", (None,))[0]
        if ncx is None:
            ncx = next((h for h, m, _p in manifest.values() if m == "application/x-dtbncx+xml"),
                       None)
        if ncx and archive.has(ncx):
            root = parse_xml(read(ncx), recover=True)
            for point in root.iter():
                if localname(point) != "navpoint":
                    continue
                label = ""
                src = None
                for child in point:
                    if localname(child) == "navlabel":
                        label = _text_of(child)
                    elif localname(child) == "content":
                        src = child.get("src")
                put(posixpath.dirname(ncx), src, label)
    except TaskError as exc:
        if exc.key not in ("damaged", "empty"):
            raise
    return labels


# --- EPUB: запись --------------------------------------------------------------

_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
_OPS_NS = "http://www.idpf.org/2007/ops"
_EPUB_CSS = (
    "body{margin:0 .5em;line-height:1.45}h1,h2,h3{text-align:center}"
    "p{margin:.3em 0;text-indent:1.2em}li p,td p{text-indent:0}"
    "table{border-collapse:collapse}td,th{border:1px solid #888;padding:.15em .4em}"
    "pre{white-space:pre-wrap}div.image{text-align:center;margin:.5em 0}"
    "img{max-width:100%}blockquote{margin:.5em 1.5em;font-style:italic}"
)


def _xml_bytes(root, doctype: str | None = None) -> bytes:
    from lxml import etree

    return etree.tostring(root, xml_declaration=True, encoding="utf-8", doctype=doctype)


def _zip_info(name: str, compress: bool = True) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    info.external_attr = 0o644 << 16
    return info


def write_epub(doc, work) -> bytes:
    from lxml import etree

    from ._docmodel import XHTML_NS, HtmlWriter, chapter_label

    lang = doc.language or "ru"
    title = doc.title or "Без названия"
    book_id = f"urn:uuid:{uuid.uuid4()}"
    modified = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    image_names = {image_id: f"images/{image_id}.{pic.ext}" for image_id, pic in doc.images.items()}
    labels = [chapter_label(ch, i) for i, ch in enumerate(doc.chapters)]
    chapter_names = [f"text/ch{i + 1:04d}.xhtml" for i in range(len(doc.chapters))]

    def page(page_title: str, css_href: str):
        html = etree.Element(f"{{{XHTML_NS}}}html", nsmap={None: XHTML_NS, "epub": _OPS_NS})
        html.set("lang", lang)
        html.set("{http://www.w3.org/XML/1998/namespace}lang", lang)
        head = etree.SubElement(html, f"{{{XHTML_NS}}}head")
        etree.SubElement(head, f"{{{XHTML_NS}}}meta", charset="utf-8")
        title_el = etree.SubElement(head, f"{{{XHTML_NS}}}title")
        title_el.text = page_title
        etree.SubElement(head, f"{{{XHTML_NS}}}link", rel="stylesheet", type="text/css",
                         href=css_href)
        body = etree.SubElement(html, f"{{{XHTML_NS}}}body")
        return html, body

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(_zip_info("mimetype", compress=False), b"application/epub+zip")
        archive.writestr(_zip_info("META-INF/container.xml"), (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            b'<rootfiles><rootfile full-path="OEBPS/content.opf" '
            b'media-type="application/oebps-package+xml"/></rootfiles></container>'
        ))
        archive.writestr(_zip_info("OEBPS/style.css"), _EPUB_CSS.encode("utf-8"))
        for image_id, name in image_names.items():
            archive.writestr(_zip_info("OEBPS/" + name, compress=False), doc.images[image_id].data)

        cover_page = None
        if doc.cover and doc.cover in image_names:
            cover_page = "cover.xhtml"
            html, body = page(title, "style.css")
            div = etree.SubElement(body, f"{{{XHTML_NS}}}div")
            div.set("class", "image")
            etree.SubElement(div, f"{{{XHTML_NS}}}img", src=image_names[doc.cover], alt=title)
            archive.writestr(_zip_info("OEBPS/" + cover_page),
                             _xml_bytes(html, "<!DOCTYPE html>"))

        total = max(1, len(doc.chapters))
        for index, chapter in enumerate(doc.chapters):
            html, body = page(labels[index], "../style.css")

            def image_src(image_id: str) -> str | None:
                name = image_names.get(image_id)
                return f"../{name}" if name else None

            HtmlWriter(image_src, XHTML_NS, work).blocks(body, chapter.blocks)
            archive.writestr(_zip_info("OEBPS/" + chapter_names[index]),
                             _xml_bytes(html, "<!DOCTYPE html>"))
            work.tick(0.4 + 0.5 * (index + 1) / total)

        # nav.xhtml
        html, body = page(title, "style.css")
        nav = etree.SubElement(body, f"{{{XHTML_NS}}}nav", id="toc")
        nav.set(f"{{{_OPS_NS}}}type", "toc")
        heading = etree.SubElement(nav, f"{{{XHTML_NS}}}h1")
        heading.text = "Оглавление"
        ol = etree.SubElement(nav, f"{{{XHTML_NS}}}ol")
        for name, label in zip(chapter_names, labels):
            li = etree.SubElement(ol, f"{{{XHTML_NS}}}li")
            link = etree.SubElement(li, f"{{{XHTML_NS}}}a", href=name)
            link.text = label
        archive.writestr(_zip_info("OEBPS/nav.xhtml"), _xml_bytes(html, "<!DOCTYPE html>"))

        # toc.ncx
        ncx = etree.Element(f"{{{_NCX_NS}}}ncx", nsmap={None: _NCX_NS}, version="2005-1")
        head = etree.SubElement(ncx, f"{{{_NCX_NS}}}head")
        for meta_name, content in (("dtb:uid", book_id), ("dtb:depth", "1"),
                                   ("dtb:totalPageCount", "0"), ("dtb:maxPageNumber", "0")):
            etree.SubElement(head, f"{{{_NCX_NS}}}meta", name=meta_name, content=content)
        doc_title = etree.SubElement(ncx, f"{{{_NCX_NS}}}docTitle")
        etree.SubElement(doc_title, f"{{{_NCX_NS}}}text").text = title
        nav_map = etree.SubElement(ncx, f"{{{_NCX_NS}}}navMap")
        for number, (name, label) in enumerate(zip(chapter_names, labels), 1):
            point = etree.SubElement(nav_map, f"{{{_NCX_NS}}}navPoint", id=f"np{number}",
                                     playOrder=str(number))
            nav_label = etree.SubElement(point, f"{{{_NCX_NS}}}navLabel")
            etree.SubElement(nav_label, f"{{{_NCX_NS}}}text").text = label
            etree.SubElement(point, f"{{{_NCX_NS}}}content", src=name)
        archive.writestr(_zip_info("OEBPS/toc.ncx"), _xml_bytes(ncx))

        # content.opf
        package = etree.Element(f"{{{_OPF_NS}}}package", nsmap={None: _OPF_NS},
                                version="3.0")
        package.set("unique-identifier", "bookid")
        package.set("{http://www.w3.org/XML/1998/namespace}lang", lang)
        metadata = etree.SubElement(package, f"{{{_OPF_NS}}}metadata", nsmap={"dc": _DC_NS})
        etree.SubElement(metadata, f"{{{_DC_NS}}}identifier", id="bookid").text = book_id
        etree.SubElement(metadata, f"{{{_DC_NS}}}title").text = title
        for author in doc.authors:
            etree.SubElement(metadata, f"{{{_DC_NS}}}creator").text = author
        etree.SubElement(metadata, f"{{{_DC_NS}}}language").text = lang
        etree.SubElement(metadata, f"{{{_OPF_NS}}}meta", property="dcterms:modified").text = modified
        if cover_page:
            etree.SubElement(metadata, f"{{{_OPF_NS}}}meta", name="cover", content=doc.cover)
        manifest = etree.SubElement(package, f"{{{_OPF_NS}}}manifest")

        def item(item_id: str, href: str, media: str, props: str | None = None) -> None:
            element = etree.SubElement(manifest, f"{{{_OPF_NS}}}item", id=item_id, href=href)
            element.set("media-type", media)
            if props:
                element.set("properties", props)

        item("nav", "nav.xhtml", "application/xhtml+xml", "nav")
        item("ncx", "toc.ncx", "application/x-dtbncx+xml")
        item("css", "style.css", "text/css")
        for image_id, name in image_names.items():
            item(image_id, name, doc.images[image_id].media_type,
                 "cover-image" if image_id == doc.cover else None)
        if cover_page:
            item("coverpage", cover_page, "application/xhtml+xml")
        for number, name in enumerate(chapter_names, 1):
            item(f"ch{number:04d}", name, "application/xhtml+xml")
        spine = etree.SubElement(package, f"{{{_OPF_NS}}}spine", toc="ncx")
        if cover_page:
            etree.SubElement(spine, f"{{{_OPF_NS}}}itemref", idref="coverpage")
        for number in range(1, len(chapter_names) + 1):
            etree.SubElement(spine, f"{{{_OPF_NS}}}itemref", idref=f"ch{number:04d}")
        archive.writestr(_zip_info("OEBPS/content.opf"), _xml_bytes(package))
    return buffer.getvalue()


# --- FB2: чтение ---------------------------------------------------------------

_MAX_SECTION_DEPTH = 40


def read_fb2(work, data: bytes):
    from ._docmodel import Chapter, Doc, Pictures
    from ._isolated import TaskError
    from ._safexml import localname, parse_xml

    try:
        root = parse_xml(data)
    except TaskError as exc:
        if exc.key != "damaged":
            raise
        root = parse_xml(data, recover=True)
    if localname(root) != "fictionbook":
        raise TaskError("damaged")
    doc = Doc()
    binaries = {}
    for child in root:
        if localname(child) == "binary" and child.get("id"):
            binaries.setdefault(child.get("id"), child)
    pictures = Pictures(work, doc.images)

    def image(href: str) -> str | None:
        if not href.startswith("#"):
            return None  # внешние ссылки не загружаются
        if pictures.known(href[1:]):  # повтор — из кэша, без повторного декодирования
            return pictures.add(b"", key=href[1:])
        element = binaries.get(href[1:])
        if element is None:
            return None
        payload = "".join(element.itertext())
        if len(payload) > work.max_image_bytes * 4 // 3 + 1024:
            return None
        try:
            raw = base64.b64decode("".join(payload.split()), validate=False)
        except (ValueError, TypeError):
            return None
        return pictures.add(raw, key=href[1:])

    reader = _Fb2Reader(work, image)
    for child in root:
        if localname(child) == "description":
            reader.description(child, doc)
    for body in root:
        if localname(body) != "body":
            continue
        notes = (body.get("name") or "").lower() in ("notes", "comments", "footnotes")
        if notes:
            blocks = reader.blocks(body, 2)
            if blocks:
                doc.chapters.append(Chapter("Примечания", reader.title_heading("Примечания", 1)
                                            + blocks))
            continue
        pending: list = []
        for child in body:
            name = localname(child)
            if name == "section":
                blocks = pending + reader.section(child, 1)
                pending = []
                if blocks:
                    doc.chapters.append(Chapter(reader.section_title(child), blocks))
                    _check_chapters(work, len(doc.chapters))
                    work.tick()
            elif name != "title":
                pending.extend(reader.block(child, 1))
        if pending:
            if doc.chapters:
                doc.chapters[-1].blocks.extend(pending)
            else:
                doc.chapters.append(Chapter("", pending))
    return doc


class _Fb2Reader:
    def __init__(self, work, image) -> None:
        self.work = work
        self.image = image

    def description(self, element, doc) -> None:
        from ._safexml import localname

        for info in element:
            if localname(info) != "title-info":
                continue
            for child in info:
                name = localname(child)
                if name == "book-title" and not doc.title:
                    doc.title = _text_of(child)[:1000]
                elif name == "author" and len(doc.authors) < 50:
                    parts = {localname(p): _text_of(p) for p in child}
                    full = " ".join(p for p in (parts.get("first-name"), parts.get("middle-name"),
                                                parts.get("last-name")) if p)
                    full = full or parts.get("nickname", "")
                    if full and full not in doc.authors:
                        doc.authors.append(full[:300])
                elif name == "lang" and not doc.language:
                    doc.language = _text_of(child)[:35]
                elif name == "coverpage" and doc.cover is None:
                    for img in child.iter():
                        if localname(img) == "image":
                            href = _href(img)
                            if href:
                                doc.cover = self.image(href)
                                break

    def inline(self, element, style=None) -> list:
        from ._docmodel import Span, clean_text, safe_href
        from ._safexml import localname

        bold, italic, code, href = style or (False, False, False, None)
        spans = []

        def text(value):
            if value:
                spans.append(Span(clean_text(value).replace("\n", " "), bold, italic, code, href))

        text(element.text)
        for child in element:
            if isinstance(child.tag, str):
                name = localname(child)
                if name == "strong":
                    spans.extend(self.inline(child, (True, italic, code, href)))
                elif name == "emphasis":
                    spans.extend(self.inline(child, (bold, True, code, href)))
                elif name == "code":
                    spans.extend(self.inline(child, (bold, italic, True, href)))
                elif name == "a":
                    spans.extend(self.inline(child, (bold, italic, code, safe_href(_href(child)))))
                elif name != "image":
                    spans.extend(self.inline(child, (bold, italic, code, href)))
            text(child.tail)
        return spans

    def para(self, element):
        from ._docmodel import Para, normalize_spans, spans_text

        spans = normalize_spans(self.inline(element))
        if not spans:
            return []
        self.work.add_text(len(spans_text(spans)))
        self.work.add_block()
        return [Para(spans)]

    def title_heading(self, text: str, level: int) -> list:
        from ._docmodel import Heading, Span

        return [Heading(level, [Span(text)])] if text else []

    def section_title(self, section) -> str:
        from ._safexml import localname

        for child in section:
            if localname(child) == "title":
                return _text_of(child)[:300]
        return ""

    def heading(self, element, level: int) -> list:
        from ._docmodel import Heading, Span, normalize_spans
        from ._safexml import localname

        spans = []
        for child in element:
            if localname(child) == "p":
                if spans:
                    spans.append(Span(" "))
                spans.extend(self.inline(child))
        if not spans:
            spans = self.inline(element)
        spans = normalize_spans(spans)
        if not spans:
            return []
        self.work.add_block()
        return [Heading(max(1, min(6, level)), spans)]

    def section(self, element, level: int) -> list:
        if level > _MAX_SECTION_DEPTH:
            return self.flat(element)
        return self.blocks(element, level)

    def flat(self, element) -> list:
        from ._docmodel import Para, Span

        text = _text_of(element)
        return [Para([Span(text)])] if text else []

    def blocks(self, element, level: int) -> list:
        out = []
        for child in element:
            if isinstance(child.tag, str):
                out.extend(self.block(child, level))
        return out

    def block(self, element, level: int) -> list:
        from ._docmodel import Img, Para, Quote, Span, Table, normalize_spans
        from ._safexml import localname

        name = localname(element)
        if name == "p":
            return self.para(element)
        if name == "title":
            return self.heading(element, level)
        if name == "subtitle":
            return self.heading(element, min(6, level + 1))
        if name == "section":
            return self.section(element, level + 1)
        if name in ("epigraph", "annotation", "cite"):
            inner = self.blocks(element, level)
            return [Quote(inner)] if inner else []
        if name == "text-author":
            spans = normalize_spans(self.inline(element, (False, True, False, None)))
            return [Para(spans)] if spans else []
        if name == "poem":
            out = []
            for child in element:
                child_name = localname(child)
                if child_name == "title":
                    out.extend(self.heading(child, min(6, level + 1)))
                elif child_name == "stanza":
                    lines = []
                    for verse in child:
                        if localname(verse) == "v":
                            spans = normalize_spans(self.inline(verse))
                            if spans:
                                if lines:
                                    lines.append(Span("\n"))
                                lines.extend(spans)
                        elif localname(verse) == "title":
                            out.extend(self.heading(verse, min(6, level + 2)))
                    if lines:
                        out.append(Para(lines))
                else:
                    out.extend(self.block(child, level))
            return out
        if name == "image":
            href = _href(element)
            image_id = self.image(href) if href else None
            return [Img(image_id, element.get("alt") or "")] if image_id else []
        if name == "table":
            rows = []
            header = 0
            for tr in element:
                if localname(tr) != "tr":
                    continue
                cells = [normalize_spans(self.inline(td)) for td in tr
                         if localname(td) in ("td", "th")]
                if not cells:
                    continue
                self.work.add_cells(len(cells))
                if len(rows) == header and all(localname(td) == "th" for td in tr
                                               if localname(td) in ("td", "th")):
                    header += 1
                rows.append(cells)
            return [Table(rows, header)] if rows else []
        if name in ("empty-line", "binary"):
            return []
        return self.blocks(element, level) or self.para(element)


def _href(element) -> str:
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower() == "href":
            return (value or "").strip()
    return ""


# --- FB2: запись ---------------------------------------------------------------

_FB2_NS = "http://www.gribuser.ru/xml/fictionbook/2.0"
_XLINK_NS = "http://www.w3.org/1999/xlink"


def write_fb2(doc, work) -> bytes:
    from lxml import etree

    from ._docmodel import Heading

    def el(parent, name: str, text: str | None = None, **attrs):
        element = etree.SubElement(parent, f"{{{_FB2_NS}}}{name}")
        for key, value in attrs.items():
            element.set(key, value)
        if text:
            element.text = text
        return element

    root = etree.Element(f"{{{_FB2_NS}}}FictionBook", nsmap={None: _FB2_NS, "l": _XLINK_NS})
    description = el(root, "description")
    info = el(description, "title-info")
    el(info, "genre", "unrecognised")
    for author in doc.authors or [""]:
        author_el = el(info, "author")
        words = author.split()
        if len(words) >= 2:
            el(author_el, "first-name", " ".join(words[:-1]))
            el(author_el, "last-name", words[-1])
        else:
            el(author_el, "nickname", author or "Неизвестный автор")
    el(info, "book-title", doc.title or "Без названия")
    lang = doc.language or "ru"
    if doc.cover and doc.cover in doc.images:
        coverpage = el(info, "coverpage")
        el(coverpage, "image").set(f"{{{_XLINK_NS}}}href", f"#{doc.cover}")
    el(info, "lang", lang)
    doc_info = el(description, "document-info")
    el(el(doc_info, "author"), "nickname", "Peregon")
    el(doc_info, "program-used", "Peregon")
    el(doc_info, "date", datetime.date.today().isoformat())
    el(doc_info, "id", str(uuid.uuid4()))
    el(doc_info, "version", "1.0")

    body = el(root, "body")
    writer = _Fb2Writer(el, doc)
    total = max(1, len(doc.chapters))
    for index, chapter in enumerate(doc.chapters):
        section = el(body, "section")
        blocks = list(chapter.blocks)
        if blocks and isinstance(blocks[0], Heading):
            title = el(section, "title")
            writer.inline(el(title, "p"), blocks.pop(0).spans)
        elif chapter.title:
            title = el(section, "title")
            el(title, "p", chapter.title)
        for block in blocks:
            work.add_block()
            writer.block(section, block)
        if not any(child.tag != f"{{{_FB2_NS}}}title" for child in section):
            el(section, "empty-line")
        work.tick(0.4 + 0.5 * (index + 1) / total)
    for image_id, picture in doc.images.items():
        binary = el(root, "binary", base64.b64encode(picture.data).decode("ascii"), id=image_id)
        binary.set("content-type", picture.media_type)
    return etree.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=False)


class _Fb2Writer:
    def __init__(self, el, doc) -> None:
        self.el = el
        self.doc = doc

    def inline(self, parent, spans) -> None:
        for span in spans:
            target = parent
            if span.href:
                target = self.el(target, "a")
                target.set(f"{{{_XLINK_NS}}}href", span.href)
            if span.bold:
                target = self.el(target, "strong")
            if span.italic:
                target = self.el(target, "emphasis")
            if span.code:
                target = self.el(target, "code")
            if len(target):
                last = target[-1]
                last.tail = (last.tail or "") + span.text
            else:
                target.text = (target.text or "") + span.text

    def lines(self, spans) -> list:
        """Отрезки абзаца → строки (по "\\n")."""
        from ._docmodel import Span

        result = [[]]
        for span in spans:
            parts = span.text.split("\n")
            for index, part in enumerate(parts):
                if index:
                    result.append([])
                if part:
                    result[-1].append(Span(part, span.bold, span.italic, span.code, span.href))
        return result

    def paragraph(self, parent, spans, name: str = "p") -> None:
        for line in self.lines(spans):
            if line:
                self.inline(self.el(parent, name), line)
            else:
                self.el(parent, "empty-line")

    def block(self, parent, block, prefix: str = "") -> None:
        from ._docmodel import (
            Heading, Img, ListBlock, Para, Pre, Quote, Rule, Span, Table)

        if isinstance(block, Heading):
            self.paragraph(parent, block.spans, "subtitle")
        elif isinstance(block, Para):
            spans = ([Span(prefix)] if prefix else []) + list(block.spans)
            self.paragraph(parent, spans)
        elif isinstance(block, ListBlock):
            for number, item in enumerate(block.items, 1):
                marker = f"{number}. " if block.ordered else "• "
                for index, sub in enumerate(item):
                    self.block(parent, sub, (prefix + marker) if index == 0 else "")
        elif isinstance(block, Table):
            table = self.el(parent, "table")
            for index, row in enumerate(block.rows):
                tr = self.el(table, "tr")
                for cell in row:
                    flat = [Span(s.text.replace("\n", " "), s.bold, s.italic, s.code, s.href)
                            for s in cell]
                    self.inline(self.el(tr, "th" if index < block.header else "td"), flat)
        elif isinstance(block, Img):
            if block.id in self.doc.images:
                image = self.el(parent, "image")
                image.set(f"{{{_XLINK_NS}}}href", f"#{block.id}")
        elif isinstance(block, Pre):
            for line in block.text.split("\n"):
                if line:
                    self.el(self.el(parent, "p"), "code", line)
                else:
                    self.el(parent, "empty-line")
        elif isinstance(block, Quote):
            cite = self.el(parent, "cite")
            for sub in block.blocks:
                if isinstance(sub, (Para, Pre, ListBlock, Heading)):
                    self.block(cite, sub)
            if not len(cite):
                parent.remove(cite)
        elif isinstance(block, Rule):
            self.el(parent, "subtitle", "* * *")


# --- MOBI: чтение --------------------------------------------------------------

_MAX_RECORD_OUT = 16384


def palmdoc_decompress(data: bytes) -> bytes:
    """Распаковка PalmDOC (LZ77); ошибка структуры — TaskError("damaged")."""
    from ._isolated import TaskError

    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        i += 1
        if c == 0 or 0x09 <= c <= 0x7F:
            out.append(c)
        elif c <= 0x08:
            if i + c > n:
                raise TaskError("damaged")
            out += data[i:i + c]
            i += c
        elif c >= 0xC0:
            out.append(0x20)
            out.append(c ^ 0x80)
        else:
            if i >= n:
                raise TaskError("damaged")
            pair = (c << 8) | data[i]
            i += 1
            distance = (pair >> 3) & 0x07FF
            length = (pair & 0x07) + 3
            if distance == 0 or distance > len(out):
                raise TaskError("damaged")
            for _ in range(length):
                out.append(out[-distance])
        if len(out) > _MAX_RECORD_OUT:
            raise TaskError("damaged")
    return bytes(out)


def _trailing_size(record: bytes, flags: int) -> int:
    """Размер хвостовых данных текстовой записи MOBI (extra data flags)."""
    size = len(record)
    num = 0
    bits = flags >> 1
    while bits:
        if bits & 1:
            pos = size - num
            value = 0
            shift = 0
            while pos > 0:
                byte = record[pos - 1]
                value |= (byte & 0x7F) << shift
                shift += 7
                pos -= 1
                if byte & 0x80 or shift >= 28:
                    break
            num += value
        bits >>= 1
    if flags & 1 and size - num - 1 >= 0:
        num += (record[size - num - 1] & 0x3) + 1
    return min(num, size)


def read_mobi(work, data: bytes):
    from ._docmodel import (
        Chapter, Doc, HtmlReader, Pictures, clean_text, split_by_breaks, split_by_headings)
    from ._isolated import TaskError
    from ._safexml import parse_html

    if len(data) < 86 or data[60:68] != b"BOOKMOBI":
        raise TaskError("damaged")
    count = struct.unpack(">H", data[76:78])[0]
    if count < 2 or 78 + 8 * count > len(data):
        raise TaskError("damaged")
    offsets = [struct.unpack(">I", data[78 + 8 * i:82 + 8 * i])[0] for i in range(count)]
    if any(b < a for a, b in zip(offsets, offsets[1:])) or offsets[-1] > len(data):
        raise TaskError("damaged")

    def record(index: int) -> bytes:
        if not 0 <= index < count:
            raise TaskError("damaged")
        end = offsets[index + 1] if index + 1 < count else len(data)
        return data[offsets[index]:end]

    rec0 = record(0)
    if len(rec0) < 16:
        raise TaskError("damaged")
    compression, = struct.unpack(">H", rec0[0:2])
    text_length, = struct.unpack(">I", rec0[4:8])
    text_count, = struct.unpack(">H", rec0[8:10])
    encryption, = struct.unpack(">H", rec0[12:14])
    if encryption != 0:
        raise TaskError("mobi_drm")
    if compression == 17480:
        raise TaskError("mobi_huff")
    if compression not in (1, 2):
        raise TaskError("mobi_compression")
    if rec0[16:20] != b"MOBI" or len(rec0) < 0x84:
        raise TaskError("damaged")
    header_len, = struct.unpack(">I", rec0[20:24])
    encoding, = struct.unpack(">I", rec0[28:32])
    version, = struct.unpack(">I", rec0[36:40])
    if version >= 8:
        raise TaskError("mobi_kf8")
    name_off, name_len = struct.unpack(">II", rec0[0x54:0x5C])
    first_image, = struct.unpack(">I", rec0[0x6C:0x70])
    exth_flags, = struct.unpack(">I", rec0[0x80:0x84])
    extra_flags = 0
    if header_len >= 0xE4 and len(rec0) >= 0xF4:
        extra_flags, = struct.unpack(">H", rec0[0xF2:0xF4])
    codec = "utf-8" if encoding == 65001 else "cp1252"

    doc = Doc()
    exth: dict[int, list[bytes]] = {}
    if exth_flags & 0x40:
        pos = 16 + header_len
        if rec0[pos:pos + 4] == b"EXTH" and pos + 12 <= len(rec0):
            items, = struct.unpack(">I", rec0[pos + 8:pos + 12])
            pos += 12
            for _ in range(min(items, 10_000)):
                if pos + 8 > len(rec0):
                    break
                kind, size = struct.unpack(">II", rec0[pos:pos + 8])
                if size < 8 or pos + size > len(rec0):
                    break
                exth.setdefault(kind, []).append(rec0[pos + 8:pos + size])
                pos += size

    def exth_text(kind: int) -> list[str]:
        return [" ".join(clean_text(v.decode(codec, errors="replace")).split())
                for v in exth.get(kind, [])]

    doc.title = next(iter(t for t in exth_text(503) if t), "")
    if not doc.title and name_len and name_off + name_len <= len(rec0):
        doc.title = " ".join(clean_text(rec0[name_off:name_off + name_len]
                                        .decode(codec, errors="replace")).split())
    doc.title = doc.title[:1000]
    for author in exth_text(100):
        if author and author not in doc.authors and len(doc.authors) < 50:
            doc.authors.append(author[:300])
    doc.language = next(iter(t for t in exth_text(524) if t), "")[:35]

    if text_count < 1 or text_count >= count:
        raise TaskError("damaged")
    max_text = min(work.max_chars * 4, 512 * 2**20)
    parts = []
    total = 0
    for index in range(1, text_count + 1):
        raw = record(index)
        raw = raw[:len(raw) - _trailing_size(raw, extra_flags)]
        chunk = palmdoc_decompress(raw) if compression == 2 else raw
        total += len(chunk)
        if total > max_text:
            raise TaskError("too_much_text", work.max_chars / 1e6)
        parts.append(chunk)
        if index % 200 == 0:
            work.tick(0.2 * index / text_count)
    text = b"".join(parts)
    if 0 < text_length < len(text):
        text = text[:text_length]
    markup = text.decode(codec, errors="replace")

    pictures = Pictures(work, doc.images)
    has_images = first_image not in (0, 0xFFFFFFFF) and first_image < count

    def image_record(number: int) -> str | None:
        if not has_images or number < 0:
            return None
        index = first_image + number
        if index >= count:
            return None
        if pictures.known(index):  # повтор — из кэша
            return pictures.add(b"", key=index)
        return pictures.add(record(index), key=index)

    def load(src: str) -> str | None:
        if not src.startswith("recindex:"):
            return None
        try:
            number = int(src[9:].strip())
        except ValueError:
            return None
        return image_record(number - 1)

    if 201 in exth and len(exth[201][0]) >= 4:
        offset, = struct.unpack(">I", exth[201][0][:4])
        if offset != 0xFFFFFFFF:
            doc.cover = image_record(offset)

    root = parse_html(markup)
    blocks = HtmlReader(work, load).blocks(root)
    parts_list = split_by_breaks(blocks)
    if len(parts_list) <= 1:
        parts_list = split_by_headings(parts_list[0] if parts_list else [])
    _check_chapters(work, len(parts_list))
    for part in parts_list:
        doc.chapters.append(Chapter("", part))
    return doc
