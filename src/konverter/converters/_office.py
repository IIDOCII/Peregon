"""Конвертации через модуль LibreOffice (этап 5, Ф17) — общая часть конвертеров.

Родитель (поток очереди "office", по одной задаче):
1. проверки входа (один файл, код формата, размер ≤ `MAX_INPUT_BYTES`);
   модуля нет → `ConversionError` с понятным текстом (Ф15/Ф19);
2. `libreoffice.soffice.convert` — копия входа под ASCII-именем во временной
   папке `data/temp/lo-run-…`, свой профиль, объект задания, отмена (опрос
   ≤ 0,25 с) и таймаут `soffice.TIMEOUT` (10 мин) → всё дерево процессов убито;
3. дообработка результата LibreOffice:
   * PDF → удаляются метаданные документа (`/Info`: автор, название, программа,
     даты; XMP `/Metadata` каталога) — как у всех результатов программы
     «без метаданных пользователя»; делается pikepdf в ДОЧЕРНЕМ процессе
     (`_isolated.run_task`, задача "office");
   * DOCX/XLSX → свойства документа (`docProps/core.xml`, `app.xml`,
     `custom.xml`) заменяются нейтральными (без автора, компании, шаблона),
     тоже в дочернем процессе;
   * PNG слайдов → PDF от LibreOffice рендерится существующим конвертером
     PDF (`pdf.PdfConverter`, вариант «Страницы в PNG», результат — папка);
   * CSV → XLSX от LibreOffice разбирается существующим кодом таблиц
     (`tables`, «первый лист» / «каждый лист в папку»).

Временная папка удаляется в любом случае (`soffice.work_dir`).
"""

from __future__ import annotations

import logging
import math
import os
import time
from pathlib import Path
from typing import Any

from . import _isolated
from .base import CancelToken, ConversionError, Preset, ProgressCallback, Target

log = logging.getLogger(__name__)

MODULE = "libreoffice"
QUEUE = "office"
MAX_INPUT_BYTES = 512 * 2**20
STALL_TIMEOUT = 120.0
MAX_TIME = 1800.0
CLEAN_MEMORY_LIMIT = 2 * 2**30

MSG_FAILED = (
    "LibreOffice не смог открыть или сконвертировать файл "
    "(возможно, он повреждён или защищён паролем)"
)
MSG_TIMEOUT = "LibreOffice не справился с файлом за отведённое время (10 минут)"
MSG_DAMAGED = "Файл повреждён или имеет неверный формат"

# Как пост-обработать результат LibreOffice.
POST_PDF = "pdf"
POST_OOXML = "ooxml"
POST_PAGES_PNG = "pages_png"
POST_CSV = "csv"
POST_CSV_SHEETS = "csv_sheets"


def _file_size(path: Path) -> int:
    try:
        return int(os.stat(path).st_size)
    except OSError as exc:
        raise ConversionError(f"Не удалось прочитать файл: {exc.strerror or exc}") from exc


def convert(
    sources: list[Path],
    dst: Path,
    target: Target,
    preset: Preset,
    progress: ProgressCallback,
    cancel: CancelToken,
    formats: list[str],
    *,
    kind: str,
    post: str,
) -> None:
    """Конвертация одного файла через LibreOffice (см. докстринг модуля).

    `kind` — что просить у LibreOffice ("pdf", "docx", "xlsx"), `post` — что
    сделать с его результатом (`POST_*`).
    """
    from konverter.core import naming
    from konverter.libreoffice import module, soffice

    cancel.raise_if_cancelled()
    sources = [Path(s) for s in sources]
    formats = [str(f or "").lower() for f in (formats or [])]
    if len(sources) != 1 or len(formats) != 1:
        raise ConversionError("Этот вариант обрабатывает один файл за раз")
    fmt = formats[0]
    if fmt not in soffice.INPUT_EXTENSIONS or fmt == "txt":
        raise ConversionError(MSG_DAMAGED)
    src = naming.os_path(sources[0])
    size = _file_size(src)
    if size == 0:
        raise ConversionError("В файле нет содержимого для конвертации")
    if size > MAX_INPUT_BYTES:
        raise ConversionError(
            f"Слишком большой файл: {size / 2**20:.0f} МБ, максимум {MAX_INPUT_BYTES / 2**20:.0f} МБ"
        )
    dst = naming.os_path(Path(dst))
    if (not target.extension) != os.path.isdir(dst):
        raise ConversionError("Внутренняя ошибка: неверный путь результата")
    if not module.is_installed():
        raise ConversionError(module.missing_message())
    try:
        temp = module.temp_root()
    except module.InstallError as exc:
        raise ConversionError(str(exc)) from exc

    progress(0.02)
    started = time.monotonic()
    lo_share = 0.6 if post == POST_PAGES_PNG else 0.8

    def poll() -> None:
        cancel.raise_if_cancelled()
        # Доля LibreOffice неизвестна: плавное приближение к lo_share.
        elapsed = time.monotonic() - started
        progress(0.02 + (lo_share - 0.02) * (1 - math.exp(-elapsed / 15)))

    try:
        with soffice.work_dir(temp) as work:
            try:
                produced = soffice.convert(
                    module.soffice_path(), work, src, fmt, kind, poll=poll,
                    timeout=soffice.TIMEOUT, template=module.profile_template(),
                )
            except soffice.SofficeTimeout as exc:
                raise ConversionError(MSG_TIMEOUT) from exc
            except soffice.SofficeError as exc:
                log.info("LibreOffice: %s", exc)
                raise ConversionError(MSG_FAILED) from exc
            progress(lo_share)
            cancel.raise_if_cancelled()
            _post_process(produced, dst, post, preset, progress, cancel, lo_share)
    except OSError as exc:
        log.warning("Конвертация через LibreOffice: %s", exc)
        raise ConversionError(f"Ошибка при работе с временными файлами: {exc.strerror or exc}") from exc
    progress(1.0)


def _sub_progress(progress: ProgressCallback, start: float) -> ProgressCallback:
    def report(fraction: float) -> None:
        progress(start + (1.0 - start) * min(1.0, max(0.0, float(fraction))))

    return report


def _post_process(
    produced: Path, dst: Path, post: str, preset: Preset, progress: ProgressCallback,
    cancel: CancelToken, start: float,
) -> None:
    sub = _sub_progress(progress, start)
    if post in (POST_PDF, POST_OOXML):
        _run_clean(post, produced, dst, cancel, sub)
    elif post == POST_PAGES_PNG:
        from .pdf import TARGET_PNG, PdfConverter

        converter = PdfConverter()
        target = next(t for t in converter.targets("pdf") if t.id == TARGET_PNG)
        converter.convert([produced], dst, target, preset, sub, cancel, formats=["pdf"])
    elif post in (POST_CSV, POST_CSV_SHEETS):
        from . import _docjob
        from .tables import BASE_TARGETS

        target = next(t for t in BASE_TARGETS("xlsx") if t.id == post)
        _docjob.run_conversion(
            "table", BASE_TARGETS, [produced], dst, target, preset, sub, cancel, ["xlsx"],
            text_formats=frozenset({"csv"}), images=False,
        )
    else:
        raise ConversionError("Внутренняя ошибка: неизвестная дообработка")


def _run_clean(op: str, src: Path, dst: Path, cancel: CancelToken, progress: ProgressCallback) -> None:
    try:
        _isolated.run_task(
            "office", {"op": op, "src": str(src), "dst": str(dst)}, cancel=cancel,
            progress=progress, stall_timeout=STALL_TIMEOUT, max_time=MAX_TIME,
            memory_limit=CLEAN_MEMORY_LIMIT, pool="pdf",
        )
    except _isolated.TaskFailed as exc:
        log.info("Очистка результата LibreOffice: %s", exc.key)
        raise ConversionError(MSG_FAILED) from exc
    except _isolated.ChildFailed as exc:
        log.info("Очистка результата LibreOffice: %s", exc)
        raise ConversionError(MSG_FAILED) from exc


# =============================================================================
# Дочерний процесс: очистка метаданных результата LibreOffice.
# =============================================================================

# Нейтральные свойства документа OOXML (без автора, компании, шаблона).
_CORE_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
    'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
    '<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
    "</cp:coreProperties>"
)
_APP_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
    'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
    "<Application>Peregon</Application></Properties>"
)
_CUSTOM_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
    'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"/>'
)
_MAX_ENTRIES = 20_000
_MAX_TOTAL = 4 * 2**30


def child_task(args: dict[str, Any], report) -> dict[str, Any]:
    """Точка входа задачи "office" в дочернем процессе."""
    op = args.get("op")
    if op == POST_PDF:
        _clean_pdf(str(args["src"]), str(args["dst"]), report)
    elif op == POST_OOXML:
        _clean_ooxml(str(args["src"]), str(args["dst"]), report)
    else:
        raise ValueError("unknown op")
    return {"ok": True}


def _clean_pdf(src: str, dst: str, report) -> None:
    import pikepdf

    with pikepdf.open(src) as pdf:
        report(0.3, 1)
        if "/Info" in pdf.trailer:
            del pdf.trailer["/Info"]
        if "/Metadata" in pdf.Root:
            del pdf.Root["/Metadata"]
        report(0.4, 2)

        def on_progress(percent: int) -> None:
            report(0.4 + 0.6 * percent / 100, 3 + percent)

        pdf.save(dst, progress=on_progress, deterministic_id=True)


def _clean_ooxml(src: str, dst: str, report) -> None:
    import datetime
    import zipfile

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    replacements = {
        "docProps/core.xml": _CORE_XML.format(now=now).encode("utf-8"),
        "docProps/app.xml": _APP_XML.encode("utf-8"),
        "docProps/custom.xml": _CUSTOM_XML.encode("utf-8"),
    }
    with zipfile.ZipFile(src) as zin:
        infos = zin.infolist()
        if len(infos) > _MAX_ENTRIES or sum(i.file_size for i in infos) > _MAX_TOTAL:
            raise _isolated.TaskError("too_big")
        with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for index, info in enumerate(infos):
                data = replacements.get(info.filename)
                if data is None:
                    data = zin.read(info)
                out_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                out_info.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(out_info, data)
                report((index + 1) / len(infos), index + 1)
