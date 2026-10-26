"""Общая часть конвертеров книг, документов и таблиц (этап 4).

Родитель (`run_conversion`): проверяет аргументы и размер входа, запускает
задачу "docs" в дочернем процессе (`_isolated.run_task`, свой счётчик мест
`pool="docs"`), переводит ключи ошибок ребёнка в тексты (`MESSAGES`).

Почему дочерний процесс, хотя разбор — Python + lxml: враждебный файл может
вызвать долгий разбор (регулярные выражения Markdown, патологический HTML,
огромные таблицы) или съесть память; в отдельном процессе это ограничено
сторожем зависания (`STALL_TIMEOUT` без продвижения), общим временем
(`MAX_TIME`), лимитом памяти процесса (`DOCS_MEMORY_LIMIT`) и мгновенной
отменой (kill), а авария нативного кода (lxml/libxml2, Pillow, zlib) не
роняет программу. Своя очередь тяжёлых задач `core.jobs` ("docs", один поток)
и свой счётчик мест: книги не ждут видео и PDF и не занимают лёгкий пул.
Цена — запуск процесса (~0,5–1 с) на каждый файл.

Ребёнок (`child_task`): выбирает модуль по категории и пишет результат ТОЛЬКО
в `dst` через `Output` (счётчик байт: результат ≤ `MAX_OUTPUT_BYTES`,
свободное место — до записи и каждые `DISK_CHECK_BYTES`).
"""

from __future__ import annotations

import io
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from konverter.core import budget

from . import _isolated
from .base import CancelToken, ConversionError, Preset, ProgressCallback, Target

log = logging.getLogger(__name__)

# --- Лимиты (передаются ребёнку в аргументах) -----------------------------------

MAX_INPUT_BYTES = 512 * 2**20
# Текстовые входы (TXT, MD, HTML, CSV) разбираются целиком в память.
MAX_TEXT_INPUT_BYTES = 128 * 2**20
MAX_OUTPUT_BYTES = 4 * 2**30
DISK_RESERVE = 256 * 2**20
MIN_FREE_BYTES = 256 * 2**20
DISK_CHECK_BYTES = 32 * 2**20

STALL_TIMEOUT = 120.0
MAX_TIME = 2 * 3600.0
DOCS_MEMORY_LIMIT = 2 * 2**30
# Резерв в общем бюджете пикселей на время задачи (одна картинка за раз).
IMAGE_HOLD_PIXELS = 40_000_000

LIMITS: dict[str, int] = {
    "zip_max_entries": 10_000,
    "zip_max_total": 2**30,
    "zip_max_entry": 512 * 2**20,
    "zip_max_ratio": 200,
    "zip_ratio_min_size": 32 * 2**20,
    "max_xml_bytes": 128 * 2**20,
    "max_chars": 200_000_000,
    "max_blocks": 2_000_000,
    "max_chapters": 10_000,
    "max_images": 2_000,
    "max_image_bytes": 256 * 2**20,
    "max_image_pixels": IMAGE_HOLD_PIXELS,
    "max_table_cells": 1_000_000,
    "max_pages": 5000,
    "max_cells": 5_000_000,
    "max_rows": 1_048_576,
    "max_columns": 16_384,
    "max_cell_chars": 32_767,
    "max_sheets": 1000,
    "max_html_embed_bytes": 64 * 2**20,
}

# Имя задачи в `_isolated.TASKS` (тесты могут подменить).
_TASK = "docs"

# --- Тексты ошибок -------------------------------------------------------------

MSG_DAMAGED = "Файл повреждён или имеет неверный формат"
MSG_NO_MEMORY = "Недостаточно памяти для обработки файла"

MESSAGES: dict[str, str] = {
    "damaged": MSG_DAMAGED,
    "empty": "В файле нет содержимого для конвертации",
    "zip_entries": "Слишком много частей внутри файла: больше {0:.0f}",
    "zip_bomb": "Подозрительно сильно сжатые данные внутри файла (похоже на «ZIP-бомбу»)",
    "zip_too_big": "Слишком большой объём данных внутри файла: больше {0:.0f} МБ",
    "zip_encrypted": "Файл зашифрован или защищён паролем",
    "zip_method": "Неподдерживаемый способ сжатия внутри файла",
    "zip_duplicate": "Файл повреждён: повторяющиеся имена частей внутри",
    "xml_entities": (
        "Файл содержит XML с объявлениями сущностей (DTD); "
        "такие файлы не обрабатываются из соображений безопасности"
    ),
    "xml_too_big": "Слишком большая XML-часть внутри файла: больше {0:.0f} МБ",
    "mobi_drm": "Этот вариант MOBI не поддерживается: книга защищена DRM",
    "mobi_huff": "Этот вариант MOBI не поддерживается: сжатие HUFF/CDIC",
    "mobi_kf8": "Этот вариант MOBI не поддерживается: формат KF8 (AZW3)",
    "mobi_compression": "Этот вариант MOBI не поддерживается: неизвестный способ сжатия",
    "epub_drm": "Книга EPUB защищена DRM: такие книги не поддерживаются",
    "too_many_chapters": "Слишком много глав: больше {0:.0f}",
    "too_much_text": "Слишком много текста: больше {0:.0f} млн символов",
    "too_complex": "Слишком сложный документ",
    "too_many_cells": "Слишком большая таблица: больше {0:.0f} ячеек",
    "too_many_rows": "Слишком много строк: больше {0:.0f}",
    "too_many_columns": "Слишком много столбцов: больше {0:.0f}",
    "cell_too_long": "Слишком длинное значение в ячейке: больше {0:.0f} символов",
    "too_many_sheets": "Слишком много листов: больше {0:.0f}",
    "no_sheets": "В таблице нет листов",
    "too_many_pages": "Слишком много страниц в PDF: больше {0:.0f}",
    "no_font": (
        "Не найден шрифт с кириллицей для PDF: нужен Arial, Segoe UI, Calibri "
        "или Times New Roman в папке шрифтов Windows"
    ),
    "no_space": "Недостаточно места на диске: нужно ~{0:.1f} ГБ, свободно {1:.1f} ГБ",
    "disk_full": "Недостаточно места на диске: свободно меньше {0:.1f} ГБ",
    "output_too_large": "Слишком большой результат: больше {0:.0f} ГБ",
}


def user_message(key: str, params: tuple) -> str:
    template = MESSAGES.get(key)
    if template is None:
        return MSG_DAMAGED
    try:
        return template.format(*params)
    except (IndexError, ValueError, TypeError):
        return MSG_DAMAGED


def _file_size(path: Path) -> int:
    try:
        return int(os.stat(path).st_size)
    except OSError as exc:
        raise ConversionError(f"Не удалось прочитать файл: {exc.strerror or exc}") from exc


def run_conversion(
    category: str,
    targets_for,
    sources: list[Path],
    dst: Path,
    target: Target,
    preset: Preset,
    progress: ProgressCallback,
    cancel: CancelToken,
    formats: list[str],
    *,
    text_formats: frozenset[str] = frozenset(),
    images: bool = True,
) -> None:
    """Родительская часть `convert` для категорий книг, документов и таблиц.

    `targets_for(fmt)` — `targets` конвертера (вариант должен быть в списке
    для формата файла); `text_formats` — форматы, читаемые целиком в память
    (для них предел входа меньше); `images` — держать ли резерв бюджета
    пикселей (если вариант может декодировать картинки).
    """
    from konverter.core import naming  # только у родителя (журнал временных файлов)

    cancel.raise_if_cancelled()
    sources = [Path(s) for s in sources]
    if not sources:
        raise ConversionError("Нет файлов для конвертации")
    formats = [str(f or "").lower() for f in (formats or [])]
    if len(formats) != len(sources):
        raise ConversionError("Внутренняя ошибка: не переданы форматы исходных файлов")
    if len(sources) != 1:
        raise ConversionError("Этот вариант обрабатывает один файл за раз")
    fmt = formats[0]
    if target.id not in {t.id for t in targets_for(fmt)}:
        raise ConversionError("Внутренняя ошибка: неизвестный вариант конвертации")
    src = naming.os_path(sources[0])
    size = _file_size(src)
    limit = MAX_TEXT_INPUT_BYTES if fmt in text_formats else MAX_INPUT_BYTES
    if size > limit:
        raise ConversionError(
            f"Слишком большой файл: {size / 2**20:.0f} МБ, максимум {limit / 2**20:.0f} МБ"
        )
    if size == 0:
        raise ConversionError(MESSAGES["empty"])
    dst = naming.os_path(Path(dst))
    if (not target.extension) != os.path.isdir(dst):
        raise ConversionError("Внутренняя ошибка: неверный путь результата")
    args: dict[str, Any] = {
        "category": category,
        "fmt": fmt,
        "target": target.id,
        "src": str(src),
        "dst": str(dst),
        "stem": sources[0].stem,
        "preset": Preset(preset).value,
        "limits": dict(LIMITS, max_output_bytes=MAX_OUTPUT_BYTES, disk_reserve=DISK_RESERVE,
                       min_free_bytes=MIN_FREE_BYTES, disk_check_bytes=DISK_CHECK_BYTES),
    }
    hold = None
    if images:
        def hold():
            return budget.IMAGE_BUDGET.reserve(IMAGE_HOLD_PIXELS, cancel)
    try:
        _isolated.run_task(
            _TASK, args, cancel=cancel, progress=progress, stall_timeout=STALL_TIMEOUT,
            max_time=MAX_TIME, memory_limit=DOCS_MEMORY_LIMIT, pool="docs", hold=hold,
        )
    except _isolated.TaskFailed as exc:
        raise ConversionError(user_message(exc.key, exc.params)) from exc
    except _isolated.ChildFailed as exc:
        log.info("Дочерний процесс docs: %s", exc)
        raise ConversionError(MSG_NO_MEMORY if exc.code == "memory" else MSG_DAMAGED) from exc
    progress(1.0)


# =============================================================================
# Дочерний процесс
# =============================================================================


class Output(io.RawIOBase):
    """Файл результата с учётом байт: предел размера и проверка места на диске."""

    def __init__(self, path: str, limits: dict, mode: str = "wb") -> None:
        super().__init__()
        self.path = path
        self.max_bytes = int(limits.get("max_output_bytes", MAX_OUTPUT_BYTES))
        self.reserve = int(limits.get("disk_reserve", DISK_RESERVE))
        self.min_free = int(limits.get("min_free_bytes", MIN_FREE_BYTES))
        self.check_every = max(1, int(limits.get("disk_check_bytes", DISK_CHECK_BYTES)))
        self.written = 0
        self.checked_at = 0
        self._fh = open(path, mode)

    def _free(self) -> int | None:
        folder = os.path.dirname(os.path.abspath(self.path)) or "."
        try:
            return int(shutil.disk_usage(folder).free)
        except (OSError, ValueError):
            return None

    def expect(self, size: int) -> None:
        """До записи известного объёма: предел и место (объём + запас)."""
        if size > self.max_bytes:
            raise _isolated.TaskError("output_too_large", self.max_bytes / 2**30)
        free = self._free()
        if free is not None and free < size + self.reserve:
            raise _isolated.TaskError("no_space", (size + self.reserve) / 2**30, free / 2**30)

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._fh.seek(offset, whence)

    def tell(self) -> int:
        return self._fh.tell()

    def write(self, data) -> int:
        size = len(data)
        self.written += size
        if self.written > self.max_bytes:
            raise _isolated.TaskError("output_too_large", self.max_bytes / 2**30)
        if self.written - self.checked_at >= self.check_every:
            self.checked_at = self.written
            free = self._free()
            if free is not None and free < self.min_free:
                raise _isolated.TaskError("disk_full", self.min_free / 2**30)
        self._fh.write(data)
        return size

    def flush(self) -> None:
        if not self._fh.closed:
            self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
        super().close()


def write_bytes(path: str, data: bytes, limits: dict) -> None:
    """Записать готовый результат целиком (с проверкой предела и места)."""
    out = Output(path, limits)
    try:
        out.expect(len(data))
        view = memoryview(data)
        for start in range(0, len(view), 8 * 2**20):
            out.write(view[start:start + 8 * 2**20])
    finally:
        out.close()


def read_input(path: str, limit: int) -> bytes:
    """Входной файл целиком (не больше `limit` байт, иначе ошибка «слишком большой»)."""
    with open(path, "rb") as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise _isolated.TaskError("zip_too_big", limit / 2**20)
    return data


def child_task(args: dict[str, Any], report) -> dict[str, Any]:
    """Точка входа задачи "docs" в дочернем процессе."""
    import warnings

    from ._docmodel import Work

    work = Work(args, report)
    category = str(args.get("category"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if category == "book":
            from . import books

            books.child_convert(work)
        elif category == "document":
            from . import documents

            documents.child_convert(work)
        elif category == "table":
            from . import tables

            tables.child_convert(work)
        else:
            raise ValueError("unknown category")
    work.tick(1.0)
    return {"ok": True}
