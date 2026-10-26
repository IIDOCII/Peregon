"""Конвертер таблиц: CSV ↔ XLSX своим кодом (этап 4); XLS, ODS и PDF — через LibreOffice (этап 5).

Варианты (вход — коды detect; `.tsv` detect даёт как "csv"):
* csv → «Excel (XLSX)» (`xlsx`);
* xlsx → «CSV (первый лист)» (`csv`, один файл) и «CSV (каждый лист —
  отдельный файл)» (`csv_sheets`, результат — ПАПКА `имя/` с файлами
  `<имя листа>.csv`). Два варианта, потому что ядро заранее, до чтения файла,
  решает, будет результат файлом или папкой (`Target.extension`), а число
  листов известно только после разбора. «Первый лист» — первый видимый лист
  (скрытые пропускаются; если видимых нет — просто первый);
* через модуль LibreOffice (`_office`, очередь "office"): xlsx → PDF;
  xls, ods → XLSX, CSV (первый лист / каждый лист в папку — цепочка
  LibreOffice → XLSX → код ниже), PDF. Варианты показываются всегда; если
  модуля нет, интерфейс спрашивает разрешение на скачивание (Ф15), а сама
  задача без модуля завершается понятной ошибкой.
Все варианты `is_long=True` (дочерний процесс), пресет не влияет.

CSV → XLSX:
* кодировка — `_text.decode_text` (UTF-8, UTF-8 BOM, UTF-16, cp1251 …),
  разделитель — `_text.sniff_delimiter` (`;` `\\t` `,` `|`), кавычки `"`;
* типы: целые (до 15 цифр, без ведущих нулей: «007» и телефоны остаются
  текстом) и десятичные дроби (точка; запятая — только если разделитель не
  запятая: «3,14» в CSV с `;`) — числами; всё остальное, включая даты, —
  ТЕКСТОМ как есть (угадывание дат портит данные: «01.02» — 1 февраля или
  номер раздела?);
* ВНЕДРЕНИЕ ФОРМУЛ невозможно: XLSX пишется своим потоковым писателем,
  который умеет только два вида ячеек — число (`<v>`) и строку
  (`t="inlineStr"`); элемента `<f>` в нём нет вовсе. Ячейка
  `=cmd|' /C calc'!A0`, `+1+1`, `@SUM(1)`, `-2+3` остаётся строкой.
  Свой писатель вместо openpyxl write-only — ещё и потому, что openpyxl
  складывает листы во временные файлы в %TEMP%, которые остаются, если
  процесс убит (отмена) — а программа не должна мусорить вне своей папки;
* лимиты: ячеек ≤ `max_cells` (5 млн), строк ≤ 1 048 576, столбцов ≤ 16 384
  (пределы Excel), длина ячейки ≤ 32 767 символов (предел Excel) — больше →
  понятная ошибка, а не молчаливая обрезка;
* архив — без ZIP64 (Excel на ZIP64-запись потокового писателя предлагает
  «восстановить содержимое»): XML листа ≤ `MAX_SHEET_XML_BYTES` (≈2 ГБ,
  предел записи без ZIP64), больше — ошибка «Слишком большой результат».
  При входе ≤ 128 МБ предел недостижим на практике.

XLSX → CSV:
* до openpyxl архив проверяется `_safezip.precheck_ooxml` (ZIP-бомбы,
  DOCTYPE/сущности в XML-частях); openpyxl сам разбирает XML через
  defusedxml и lxml с `resolve_entities=False`; читаем `read_only=True,
  data_only=True` (потоково), внешние ссылки не загружаются (`keep_links=False`);
* формулы — ЗНАЧЕНИЯМИ из кэша файла; если кэша нет (файл записан
  программой, которая не считала формулы, например openpyxl), ячейка пустая —
  пересчитывать формулы нам нечем;
* результат — UTF-8 с BOM, разделитель `;`, переводы строк Windows,
  десятичный разделитель — запятая: так файл правильно открывается двойным
  щелчком в Excel с русской локалью (там разделитель списка — `;`, а
  «UTF-8 без BOM» Excel читает как cp1251);
* даты — ISO (`2024-01-31`, `2024-01-31 12:30:00`; время 00:00:00 не пишется —
  openpyxl не отличает «дату» от «даты-времени в полночь»), логические — TRUE/FALSE;
* ЗАЩИТА ОТ ФОРМУЛ в CSV: текстовое значение, начинающееся с `=`, `+`, `-`,
  `@`, `|` (DDE), табуляции или `\\r` — в том числе после ведущих пробельных
  символов (пробел, U+00A0, U+3000, переводы строк) и в полноширинном
  варианте `＝ ＋ － ＠` — получает префикс `'` (рекомендация OWASP —
  иначе Excel, открыв CSV, выполнит его как формулу). Это меняет данные
  (`'-текст`), зато безопасно; числа (в т.ч. отрицательные) не трогаются;
* пустые строки в конце листа и пустые ячейки в конце строки отбрасываются.
"""

from __future__ import annotations

import csv
import datetime
import io
import re
import zipfile
from pathlib import Path

from konverter.strings import tr

from . import _docjob, _office
from .base import CancelToken, Category, Preset, ProgressCallback, Target

# (вход, id, расширение)
_OUTPUTS = (
    ("csv", "xlsx", "xlsx"),
    ("xlsx", "csv", "csv"),
    ("xlsx", "csv_sheets", ""),
)
TARGET_ORDER = ("xlsx", "csv", "csv_sheets", "pdf")
# Варианты через LibreOffice: вход → [(id, расширение)].
_OFFICE_OUTPUTS = {
    "xlsx": (("pdf", "pdf"),),
    "xls": (("xlsx", "xlsx"), ("csv", "csv"), ("csv_sheets", ""), ("pdf", "pdf")),
    "ods": (("xlsx", "xlsx"), ("csv", "csv"), ("csv_sheets", ""), ("pdf", "pdf")),
}
# Как получить вариант через LibreOffice: id → (что просит LO, дообработка).
_OFFICE_PLAN = {
    "pdf": ("pdf", "pdf"),
    "xlsx": ("xlsx", "ooxml"),
    "csv": ("xlsx", "csv"),
    "csv_sheets": ("xlsx", "csv_sheets"),
}


def _target(tid: str, ext: str) -> Target:
    return Target(id=tid, label=tr(f"target_table_{tid}"), extension=ext, uses_preset=False,
                  is_long=True)


def BASE_TARGETS(fmt: str) -> list[Target]:  # noqa: N802 — «константа-функция» для _office
    """Варианты своим кодом (без LibreOffice)."""
    fmt = (fmt or "").lower()
    return [_target(tid, ext) for src, tid, ext in _OUTPUTS if src == fmt]

CSV_DELIMITER = ";"
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
# Первый значимый символ (после пробельных), из-за которого Excel/LibreOffice
# могут счесть значение формулой или DDE: обычные и полноширинные = + - @, и |.
_FORMULA_FIRST = frozenset("=+-@|＝＋－＠")


def needs_formula_guard(text: str) -> bool:
    """Текст CSV, который надо защитить префиксом «'» (см. докстринг модуля)."""
    if text.startswith(FORMULA_PREFIXES) or text.startswith("|"):
        return True
    rest = text.lstrip()  # str.lstrip снимает и U+3000, U+00A0, переводы строк
    return bool(rest) and rest[0] in _FORMULA_FIRST


class SpreadsheetConverter:
    """Конвертер таблиц без LibreOffice (см. докстринг модуля)."""

    category = Category.SPREADSHEET
    target_order = TARGET_ORDER
    queue = "docs"

    def targets(self, fmt: str) -> list[Target]:
        fmt = (fmt or "").lower()
        return BASE_TARGETS(fmt) + [_target(tid, ext) for tid, ext in _OFFICE_OUTPUTS.get(fmt, ())]

    def module_for(self, target_id: str, fmt: str) -> str | None:
        """"libreoffice", если вариант для этого формата делается через LibreOffice."""
        ids = {tid for tid, _ext in _OFFICE_OUTPUTS.get((fmt or "").lower(), ())}
        return _office.MODULE if target_id in ids else None

    def queue_for(self, target_id: str, formats: list[str]) -> str | None:
        if any(self.module_for(target_id, fmt) for fmt in formats):
            return _office.QUEUE
        return self.queue

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
        fmt = str(formats[0] if formats else "").lower()
        if len(formats) == 1 and self.module_for(target.id, fmt):
            kind, post = _OFFICE_PLAN[target.id]
            _office.convert(
                sources, dst, target, preset, progress, cancel, formats, kind=kind, post=post
            )
            return
        _docjob.run_conversion(
            "table", BASE_TARGETS, sources, dst, target, preset, progress, cancel, formats,
            text_formats=frozenset({"csv"}), images=False,
        )


# =============================================================================
# Дочерний процесс
# =============================================================================


def child_convert(work) -> None:
    from ._isolated import TaskError

    args = work.args
    fmt = args["fmt"]
    target = args["target"]
    if fmt == "csv" and target == "xlsx":
        csv_to_xlsx(work, args["src"], args["dst"])
    elif fmt == "xlsx" and target in ("csv", "csv_sheets"):
        xlsx_to_csv(work, args["src"], args["dst"], every_sheet=target == "csv_sheets")
    else:
        raise TaskError("damaged")


# --- CSV → XLSX ----------------------------------------------------------------

_INT = re.compile(r"-?(?:0|[1-9]\d{0,14})")
_DEC_DOT = re.compile(r"-?(?:0|[1-9]\d{0,14})\.\d{1,15}")
_DEC_COMMA = re.compile(r"-?(?:0|[1-9]\d{0,14}),\d{1,15}")


def number_value(text: str, delimiter: str) -> str | None:
    """Текст ячейки → число для `<v>` (строкой с точкой) или None — оставить текстом."""
    if not text or len(text) > 32:
        return None
    if _INT.fullmatch(text):
        return text
    if _DEC_DOT.fullmatch(text):
        return text
    if delimiter != "," and _DEC_COMMA.fullmatch(text):
        return text.replace(",", ".")
    return None


def column_letters(index: int) -> str:
    """0 → "A", 25 → "Z", 26 → "AA" …"""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

_CONTENT_TYPES = (
    _XML_DECL
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.spreadsheetml.styles+xml"/>'
    '</Types>'
)
_ROOT_RELS = (
    _XML_DECL + f'<Relationships xmlns="{_NS_PKG_REL}">'
    f'<Relationship Id="rId1" Type="{_NS_REL}/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>'
)
_WORKBOOK = (
    _XML_DECL + f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
    '<sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS = (
    _XML_DECL + f'<Relationships xmlns="{_NS_PKG_REL}">'
    f'<Relationship Id="rId1" Type="{_NS_REL}/worksheet" Target="worksheets/sheet1.xml"/>'
    f'<Relationship Id="rId2" Type="{_NS_REL}/styles" Target="styles.xml"/>'
    '</Relationships>'
)
_STYLES = (
    _XML_DECL + f'<styleSheet xmlns="{_NS_MAIN}">'
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)


def _info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def csv_to_xlsx(work, src: str, dst: str) -> None:
    from ._docmodel import clean_text
    from ._isolated import TaskError
    from ._text import decode_text, sniff_delimiter

    max_cells = work.lim("max_cells", 5_000_000)
    max_rows = work.lim("max_rows", 1_048_576)
    max_columns = work.lim("max_columns", 16_384)
    max_cell_chars = work.lim("max_cell_chars", 32_767)

    text = decode_text(_docjob.read_input(src, _docjob.MAX_TEXT_INPUT_BYTES))
    work.tick(0.05)
    delimiter = sniff_delimiter(text[:65536])
    total_lines = max(1, text.count("\n") + 1)
    csv.field_size_limit(max_cell_chars + 1)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, quotechar='"',
                        doublequote=True, strict=False)
    out = _docjob.Output(dst, work.limits)
    cells = 0
    try:
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr(_info("[Content_Types].xml"), _CONTENT_TYPES)
            archive.writestr(_info("_rels/.rels"), _ROOT_RELS)
            archive.writestr(_info("xl/workbook.xml"), _WORKBOOK.replace("{name}", "Лист1"))
            archive.writestr(_info("xl/_rels/workbook.xml.rels"), _WORKBOOK_RELS)
            archive.writestr(_info("xl/styles.xml"), _STYLES)
            # Без ZIP64 (см. MAX_SHEET_XML_BYTES): у записи с force_zip64 в
            # локальном заголовке ZIP64-extra и размеры 0xFFFFFFFF, а в
            # центральном каталоге — нет; Excel такое предлагает «восстановить».
            with archive.open(_info("xl/worksheets/sheet1.xml"), "w") as raw_sheet:
                sheet = _SheetStream(raw_sheet)
                sheet.write((_XML_DECL + f'<worksheet xmlns="{_NS_MAIN}"><sheetData>')
                            .encode("utf-8"))
                parts: list[str] = []
                size = 0
                row_number = 0
                try:
                    for row in reader:
                        row_number += 1
                        if row_number > max_rows:
                            raise TaskError("too_many_rows", max_rows)
                        if len(row) > max_columns:
                            raise TaskError("too_many_columns", max_columns)
                        cells += len(row)
                        if cells > max_cells:
                            raise TaskError("too_many_cells", max_cells)
                        row_xml = [f'<row r="{row_number}">']
                        for col, value in enumerate(row):
                            if not value:
                                continue
                            if len(value) > max_cell_chars:
                                raise TaskError("cell_too_long", max_cell_chars)
                            ref = f"{column_letters(col)}{row_number}"
                            number = number_value(value, delimiter)
                            if number is not None:
                                row_xml.append(f'<c r="{ref}"><v>{number}</v></c>')
                            else:
                                value = clean_text(value)
                                row_xml.append(
                                    f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
                                    f'{_xml_escape(value)}</t></is></c>')
                        row_xml.append("</row>")
                        chunk = "".join(row_xml)
                        parts.append(chunk)
                        size += len(chunk)
                        if size > 1 << 20:
                            sheet.write("".join(parts).encode("utf-8"))
                            parts.clear()
                            size = 0
                        if row_number % 2000 == 0:
                            work.tick(min(0.95, reader.line_num / total_lines))
                except csv.Error:
                    raise TaskError("cell_too_long", max_cell_chars) from None
                parts.append("</sheetData></worksheet>")
                sheet.write("".join(parts).encode("utf-8"))
    except RuntimeError:
        # zipfile: «File size too large» — запись переросла предел без ZIP64
        # (страховка: MAX_SHEET_XML_BYTES проверяется раньше).
        raise TaskError("output_too_large", MAX_SHEET_XML_BYTES / 2**30) from None
    finally:
        out.close()


# Предел распакованного XML листа: запись ZIP без ZIP64 (`zipfile.ZIP64_LIMIT`
# = 2 ГБ − 1) с запасом на последний кусок. Вход CSV ≤ 128 МБ, ячеек ≤ 5 млн —
# реальный лист на порядок меньше; больше — честная ошибка.
MAX_SHEET_XML_BYTES = zipfile.ZIP64_LIMIT - 4 * 2**20


class _SheetStream:
    """Поток записи листа с подсчётом байт и пределом MAX_SHEET_XML_BYTES."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.size = 0

    def write(self, data: bytes) -> None:
        from ._isolated import TaskError

        self.size += len(data)
        if self.size > MAX_SHEET_XML_BYTES:
            raise TaskError("output_too_large", MAX_SHEET_XML_BYTES / 2**30)
        self.stream.write(data)


# --- XLSX → CSV ----------------------------------------------------------------


def format_value(value) -> str:
    """Значение ячейки openpyxl → текст CSV (см. докстринг модуля)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value).replace(".", ",")
    if isinstance(value, datetime.datetime):
        # openpyxl отдаёт даты как datetime с 00:00:00 — тогда пишем только дату.
        if value.time() == datetime.time(0, 0):
            return value.date().isoformat()
        if value.microsecond:
            value = value.replace(microsecond=0)
        return value.isoformat(sep=" ")
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    text = str(value)
    if needs_formula_guard(text):
        return "'" + text
    return text


_SHEET_NAME_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Зарезервированные имена устройств Windows: COM0–9, LPT0–9 (COM0/LPT0
# Microsoft тоже относит к зарезервированным) и с надстрочными цифрами ¹ ² ³
# (Windows считает их устройствами COM1–3/LPT1–3; надстрочного «⁰» в списке
# Microsoft нет); проверка — по части до первой точки, без учёта регистра.
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"{dev}{i}" for dev in ("COM", "LPT") for i in (*range(10), "¹", "²", "³"))}


def sheet_file_name(name: str, index: int, used: set[str]) -> str:
    """Имя файла CSV для листа: без запрещённых символов, уникальное (без учёта регистра)."""
    stem = _SHEET_NAME_BAD.sub("_", name or "").strip(" .")[:100] or f"Лист{index + 1}"
    if stem.split(".", 1)[0].rstrip(" ").upper() in _RESERVED:
        stem = "_" + stem
    candidate = stem
    number = 2
    while candidate.lower() in used:
        candidate = f"{stem} ({number})"
        number += 1
    used.add(candidate.lower())
    return candidate + ".csv"


def xlsx_to_csv(work, src: str, dst: str, *, every_sheet: bool) -> None:
    import os

    from ._isolated import TaskError
    from ._safezip import precheck_ooxml

    precheck_ooxml(src, work.limits, work.tick)
    work.tick(0.1)
    import openpyxl

    try:
        book = openpyxl.load_workbook(src, read_only=True, data_only=True, keep_links=False)
    except TaskError:
        raise
    except Exception:  # noqa: BLE001 — враждебный файл
        raise TaskError("damaged") from None
    try:
        sheets = list(book.worksheets)
        if not sheets:
            raise TaskError("no_sheets")
        max_sheets = work.lim("max_sheets", 1000)
        if len(sheets) > max_sheets:
            raise TaskError("too_many_sheets", max_sheets)
        if not every_sheet:
            visible = [s for s in sheets if getattr(s, "sheet_state", "visible") == "visible"]
            sheets = [(visible or sheets)[0]]
        counter = _Cells(work)
        used: set[str] = set()
        for index, sheet in enumerate(sheets):
            if every_sheet:
                path = os.path.join(dst, sheet_file_name(sheet.title, index, used))
                mode = "xb"
            else:
                path = dst
                mode = "wb"
            out = _docjob.Output(path, work.limits, mode)
            try:
                _write_sheet(sheet, out, counter)
            finally:
                out.close()
            work.tick(0.1 + 0.85 * (index + 1) / len(sheets))
    except TaskError:
        raise
    except Exception:  # noqa: BLE001
        raise TaskError("damaged") from None
    finally:
        book.close()


class _Cells:
    def __init__(self, work) -> None:
        self.work = work
        self.count = 0
        self.max_cells = work.lim("max_cells", 5_000_000)
        self.max_columns = work.lim("max_columns", 16_384)

    def add(self, count: int) -> None:
        from ._isolated import TaskError

        if count > self.max_columns:
            raise TaskError("too_many_columns", self.max_columns)
        self.count += count
        if self.count > self.max_cells:
            raise TaskError("too_many_cells", self.max_cells)


def _write_sheet(sheet, out, counter: _Cells) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=CSV_DELIMITER, quotechar='"',
                        quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    out.write(b"\xef\xbb\xbf")
    pending_empty = 0
    rows = 0
    for row in sheet.iter_rows(values_only=True):
        counter.add(len(row))
        values = [format_value(v) for v in row]
        while values and values[-1] == "":
            values.pop()
        rows += 1
        if rows % 2000 == 0:
            counter.work.tick()
        if not values:
            pending_empty += 1
            continue
        for _ in range(pending_empty):
            writer.writerow([])
        pending_empty = 0
        writer.writerow(values)
        if buffer.tell() > 1 << 20:
            out.write(buffer.getvalue().encode("utf-8"))
            buffer.seek(0)
            buffer.truncate()
    out.write(buffer.getvalue().encode("utf-8"))
