"""Безопасный разбор XML и HTML (lxml) — только в дочернем процессе.

XML (FB2, OPF, NCX, XHTML глав, container.xml):
* парсер lxml с `resolve_entities=False` (сущности не подставляются вообще —
  ни внешние, ни внутренние), `load_dtd=False` (внешний DTD не читается),
  `no_network=True`, `huge_tree=False` (встроенные пределы libxml2 на
  глубину и размер текстовых узлов), без комментариев и инструкций;
* документ, в DTD которого ОБЪЯВЛЕНЫ сущности (`<!ENTITY …>`: XXE на
  локальный файл, «миллиард смешков»), отвергается целиком (`xml_entities`):
  в книгах и офисных файлах они не нужны, а «вырезать и продолжить» —
  риск получить искажённый текст. Отказ быстрый: сущности не раскрываются.
* размер одного XML ограничен вызывающим (данные уже прочитаны с лимитом).

HTML (вход документа, главы MOBI, запасной путь для «грязного» XHTML EPUB):
* `lxml.html.HTMLParser(no_network=True, huge_tree=False, …)`; парсер HTML
  libxml2 не обрабатывает объявления сущностей DTD, а именованные сущности
  HTML (`&nbsp;`) знает сам.

OOXML (DOCX, XLSX) разбирают сами python-docx и openpyxl (их парсеры lxml с
`resolve_entities=False`, openpyxl ещё и через defusedxml); до передачи им
файла каждая XML-часть проверяется `prolog_has_dtd`: DOCTYPE в OOXML не
бывает, любой — отказ.
"""

from __future__ import annotations

import re

from lxml import etree

from ._isolated import TaskError

_XML_PARSER_ARGS = dict(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    dtd_validation=False,
    huge_tree=False,
    remove_comments=True,
    remove_pis=True,
    collect_ids=False,
)


def xml_parser(recover: bool = False) -> etree.XMLParser:
    return etree.XMLParser(recover=recover, **_XML_PARSER_ARGS)


def _check_entities(tree) -> None:
    try:
        dtd = tree.docinfo.internalDTD
    except Exception:  # noqa: BLE001
        dtd = None
    if dtd is not None:
        for _entity in dtd.iterentities():
            raise TaskError("xml_entities")


def _declares_entities(data: bytes) -> bool:
    head = data[: 1 << 20]
    return b"<!ENTITY" in head or "<!ENTITY".encode("utf-16-le") in head or (
        "<!ENTITY".encode("utf-16-be") in head)


def parse_xml(data: bytes, *, recover: bool = False):
    """Корень безопасно разобранного XML; ошибка разбора — TaskError("damaged")."""
    if not data or not data.strip():
        raise TaskError("damaged")
    try:
        root = etree.fromstring(data, xml_parser(recover))
    except (etree.XMLSyntaxError, ValueError):
        # Неразобранный документ с объявлением сущностей — тоже отказ «по
        # сущностям» (а не «повреждён», после которого вызывающий может
        # попробовать HTML-парсер).
        if _declares_entities(data):
            raise TaskError("xml_entities") from None
        raise TaskError("damaged") from None
    if root is None:
        raise TaskError("damaged")
    _check_entities(root.getroottree())
    return root


_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")


def html_parser():
    from lxml import html

    return html.HTMLParser(
        no_network=True, huge_tree=False, remove_comments=True, remove_pis=True,
        default_doctype=False,
    )


def parse_html(text: str):
    """Корень HTML-документа (lxml.html, с восстановлением); пусто — TaskError."""
    from lxml import html

    if not text or not text.strip():
        raise TaskError("empty")
    # Строка с объявлением кодировки lxml не принимает (ValueError) — убираем его.
    text = _XML_DECL.sub("", text, count=1)
    if not text.strip():
        raise TaskError("empty")
    try:
        return html.document_fromstring(text, parser=html_parser())
    except (etree.ParserError, etree.XMLSyntaxError, ValueError):
        raise TaskError("empty") from None


def localname(element) -> str:
    """Локальное имя тега без пространства имён, в нижнем регистре ("" — не элемент)."""
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        tag = tag.rsplit("}", 1)[1]
    elif ":" in tag:  # HTML-парсер оставляет префикс («mbp:pagebreak»)
        tag = tag.rsplit(":", 1)[1] if not tag.startswith("mbp:") else tag
    return tag.lower()


# Пролог XML: объявление, комментарии, инструкции, пробелы — до корня.
_PROLOG_SKIP = re.compile(r"\s+|<\?.*?\?>|<!--.*?-->", re.S)


def _xml_encoding(chunk: bytes) -> tuple[str, int]:
    """(кодировка, длина BOM) начала XML: BOM UTF-8/16/32 или узор нулевых байтов.

    Без BOM кодировка определяется по первым 4 байтам (XML 1.0, прил. F):
    `X\\0\\0\\0` — UTF-32 LE, `\\0\\0\\0X` — UTF-32 BE, `X\\0Y\\0` — UTF-16 LE,
    `\\0X\\0Y` — UTF-16 BE (X, Y — ASCII: «<» или пробельный символ).
    """
    head = chunk[:4]
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8", 3
    if head == b"\xff\xfe\x00\x00":  # раньше UTF-16 LE: BOM UTF-32 LE начинается так же
        return "utf-32-le", 4
    if head == b"\x00\x00\xfe\xff":
        return "utf-32-be", 4
    if head.startswith(b"\xff\xfe"):
        return "utf-16-le", 2
    if head.startswith(b"\xfe\xff"):
        return "utf-16-be", 2
    if len(head) == 4:
        if head[0] and not head[1] and not head[2] and not head[3]:
            return "utf-32-le", 0
        if not head[0] and not head[1] and not head[2] and head[3]:
            return "utf-32-be", 0
        if head[0] and not head[1] and head[2] and not head[3]:
            return "utf-16-le", 0
        if not head[0] and head[1] and not head[2] and head[3]:
            return "utf-16-be", 0
    elif len(head) >= 2:
        if head[0] and not head[1]:
            return "utf-16-le", 0
        if not head[0] and head[1]:
            return "utf-16-be", 0
    return "utf-8", 0


def _decode_prefix(chunk: bytes, errors: str = "ignore") -> str:
    enc, bom = _xml_encoding(chunk)
    chunk = chunk[bom:]
    unit = 4 if enc.startswith("utf-32") else 2 if enc.startswith("utf-16") else 1
    if len(chunk) % unit:
        chunk = chunk[: len(chunk) - len(chunk) % unit]
    return chunk.decode(enc, errors=errors).lstrip("﻿")


def looks_like_xml(chunk: bytes) -> bool:
    """Начало данных — разметка: после BOM и пробельных символов идёт «<»."""
    return _decode_prefix(chunk[:256], errors="replace").lstrip()[:1] == "<"


def prolog_has_dtd(chunk: bytes) -> bool:
    """В начале XML (первый кусок) есть DOCTYPE — или пролог подозрительно длинный."""
    text = _decode_prefix(chunk[:65536])
    pos = 0
    while True:
        match = _PROLOG_SKIP.match(text, pos)
        if not match or match.end() == pos:
            break
        pos = match.end()
    rest = text[pos:]
    if rest.upper().startswith("<!DOCTYPE") or rest.startswith("<!"):
        return True
    if not rest and len(chunk) >= 65536:
        return True  # пролог не уместился в 64 КБ — так не бывает в OOXML
    return False
