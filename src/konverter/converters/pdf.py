"""Конвертер PDF (этап 3): страницы в PNG/JPG, текст, объединение, разделение.

Варианты (вход — только код формата "pdf" из detect), в каноническом порядке:
* «Страницы в PNG» / «Страницы в JPG» (`pdf_pages_png`, `pdf_pages_jpg`) —
  результат ПАПКА (`имя/`, при конфликте `имя (1)/` — это делает ядро),
  в ней `стр-001.png` …; ширина номера — по числу страниц, но не меньше
  трёх цифр (`стр-001` … `стр-999`, `стр-0001` … для 1000+ страниц), так
  что имена сортируются по порядку и в Проводнике, и по алфавиту.
  Пресет — разрешение рендера: «Высокое» 300 dpi, «Среднее» 150, «Маленький»
  96; для JPG ещё и качество (92 / 85 / 75). Страница больше
  `MAX_PAGE_PIXELS` пикселей или длиннее `MAX_PAGE_SIDE` по стороне при
  выбранном dpi рендерится с МЕНЬШИМ разрешением (до предела), а не с
  ошибкой: так «огромный MediaBox» даёт картинку разумного размера.
* «Текст (TXT)» (`pdf_txt`) — один файл UTF-8 (без BOM, переводы строк
  Windows), перед каждой страницей строка «--- Страница N ---» (понятнее
  пользователю, чем невидимый `\\f`). Пресет не влияет. Если текста нет НИ
  НА ОДНОЙ странице — ошибка «В PDF нет текстового слоя (похоже на скан)»:
  распознавание текста (OCR) не поддерживается.
* «Объединить в один PDF» (`pdf_merge`, many_to_one) — все PDF группы в
  порядке группы → один файл (имя по `merged_stem`). Показывается только
  для группы из ≥ 2 файлов (`min_files`, читает `core.grouping.common_targets`):
  для одного файла это была бы просто копия.
* «Разделить на страницы» (`pdf_split`) — папка с `стр-001.pdf` …
Все варианты `is_long=True`: даже текст тысячи страниц извлекается заметное
время, а задача идёт в дочернем процессе — нужны прогресс и «Отмена».

Изоляция и устойчивость (входные файлы враждебны):
* ВЕСЬ разбор PDF (pdfium — рендер и текст, qpdf через pikepdf — объединение
  и разделение; оба на C/C++) идёт в ДОЧЕРНЕМ процессе через
  `_isolated.run_task`: падение нативного кода → «Файл повреждён»,
  зависание → kill по `STALL_TIMEOUT` без продвижения (ребёнок сообщает
  прогресс постранично, при объединении — ещё и проценты записи qpdf, при
  очистке — каждые `_WALK_REPORT_EVERY` шагов обхода: объектов и проверок
  элементов массивов и словарей), общий предел `MAX_TIME`,
  отмена — kill (≤ 2 с), лимит памяти процесса `PDF_MEMORY_LIMIT`.
* Очередь и слот: задачи PDF идут в свою очередь тяжёлых задач
  `core.jobs` (`queue = "pdf"`, один поток): много PDF не занимают потоки
  лёгкого пула (картинки не ждут). В `_isolated` — свой счётчик мест
  `pool="pdf"` (`PDF_MAX_CHILDREN`, 1). Не короткий (SVG/RAW): разбор PDF
  длится минуты и занял бы место SVG. Не общий с видео ("long"): рендер
  страниц не ждёт окончания часового видео — очереди "media" и "pdf"
  выполняются параллельно.
* Рендер учитывает общий бюджет пикселей `core.budget.IMAGE_BUDGET`: на
  время задачи (после получения места) резервируется `MAX_PAGE_PIXELS` —
  предел одной страницы; ребёнок рендерит страницы по одной.
* Лимиты: страниц ≤ `MAX_PAGES` (у объединения — в сумме); размер входа ≤
  `MAX_INPUT_BYTES` (объединение — сумма ≤ `MAX_MERGE_BYTES`, файлов ≤
  `MAX_MERGE_FILES`: все исходники открыты до конца записи); сумма пикселей
  рендера ≤ `MAX_TOTAL_PIXELS`; результат ≤ `MAX_OUTPUT_BYTES` (счётчик байт),
  свободное место — до записи (оценка + `DISK_RESERVE`; оценка — размер
  входа для объединения/разделения, 0 для рендера и текста) и во время
  записи каждые `DISK_CHECK_BYTES` (осталось < `MIN_FREE_BYTES` → ошибка).
* Шифрование: PDF только с паролем владельца (ограничения прав, пустой
  пароль пользователя) обрабатывается; с паролем на открытие —
  «PDF защищён паролем» (ввод пароля не поддерживается). Результат
  объединения/разделения НЕ зашифрован (ограничения прав не переносятся).
* Активное содержимое. pdfium: форм-окружение (`init_forms`) НЕ
  инициализируется, рендер с `may_draw_forms=False`; собранный pdfium в
  pypdfium2 без V8 и XFA (флаги сборки пусты) — JavaScript исполнять нечем.
  Внешние ресурсы pdfium не загружает (сети нет, файл открыт нашим
  дескриптором). pikepdf/qpdf: файл открывается нашим потоком
  (`open(..., "rb")`), qpdf внешних файлов не читает; повреждённый файл
  qpdf может «починить» (attempt_recovery) — время ограничено сторожем.
* Что УДАЛЯЕТСЯ при объединении/разделении (страницы копируются в НОВЫЙ
  документ, каталог исходника не переносится):
  - уровня документа: `/OpenAction`, `/AA`, `/Names` (в т.ч. `/JavaScript`,
    `/EmbeddedFiles`), `/AcroForm`, `/Outlines` (закладки), `/Metadata`
    (XMP), `/Info` (автор, программа, даты) — как EXIF у картинок;
  - во всех объектах, достижимых из результата: `/AA` (автодействия),
    `/Metadata`, `/PieceInfo` (личные данные программ), `/AF`
    (связанные файлы PDF 2.0), `/EF`, `/RF`, `/FS` (вложенные файлы и
    ссылки на них), `/PA` (действие ссылки «по ходу»); у потоков — `/F`,
    `/FFilter`, `/FDecodeParms` (данные во внешнем файле) и `/Ref`
    (внешняя форма); у страниц — `/B` (бусины статей); у виджетов форм —
    `/Parent` (дерево полей со значениями других полей); у остальных
    аннотаций — автор и служебные метки (`/T`, `/M`, `/NM`,
    `/CreationDate`; текст комментария `/Contents` остаётся);
  - объекты `/Type /Filespec`, `/EmbeddedFile`, `/Sound`, `/3D` и
    аннотации-вложения/мультимедиа (`FileAttachment`, `Sound`, `Movie`,
    `Screen`, `RichMedia`, `3D`) — ОТОВСЮДУ, а не только из `/Annots`:
    ссылка на такой объект (через `/Parent` у Popup, `/IRT`, любой
    нестандартный ключ) удаляется, в массиве — заменяется на null.
    Popup удалённой аннотации — тоже удаляется;
  - в `/Annots` остаются только аннотации из белого списка
    (`_ANNOT_SUBTYPES`: ссылки, комментарии и пометки, фигуры, штампы,
    Popup, виджеты с внешним видом), прочие подтипы удаляются;
  - действия `/A` кроме безопасных: остаются только `/GoTo` (переход по
    документу), `/URI` со схемой http/https/mailto (ссылка — это
    содержимое документа; `file:`, UNC, `javascript:`, `data:`,
    относительные и прочие — удаляются вместе с действием, сама ссылка
    остаётся без действия) и `/Named` с NextPage/PrevPage/FirstPage/
    LastPage; цепочки `/Next` у действий удаляются; JavaScript, Launch,
    SubmitForm, ImportData, GoToR/GoToE и прочие — удаляются. URI длиннее
    `_MAX_URI_LENGTH` удаляется; у `mailto:` остаются только параметры
    subject/body/cc/bcc/to (attach и т.п. — вложение локального файла —
    вырезаются, URI переписывается);
  - любой словарь-действие (`/S` из `_ACTION_TYPES`), кроме безопасных, —
    под ЛЮБЫМ ключом и в массивах (`/MyA`, ресурсы …); ключи `/JS`,
    `/JavaScript` — отовсюду, `/Names` — у страниц и аннотаций. Словари,
    где `/S` означает иное (`/Transparency`, `/D`, `/Alpha` …), остаются.
  Слои (Optional Content): `/OCProperties` собирается заново
  (`_PdfJob._layers`) — только слои, попавшие в результат (у слоя —
  `/Type`, `/Name`, `/Intent`), и конфигурация `/D` с выключенными слоями
  (`/OFF`), `/Order`, `/RBGroups`; без `/AS` и `/Configs`. Иначе
  выключенные (скрытые) слои стали бы видимыми. При объединении слои всех
  исходников объединяются.
  Ссылки на страницы, не попавшие в результат (при разделении), qpdf
  заменяет на null — содержимое чужих страниц в файл не попадает.
  Сохранение: `linearize=False`, без потоков объектов, без шифрования,
  `deterministic_id=True` (ID без времени и случайности).
* Рендер и текст (pdfium) метаданных не переносят: PNG/JPG пишет Pillow без
  EXIF, TXT — только текст.
* Ребёнок пишет ТОЛЬКО в `dst` (временный файл или папку ядра) — имена
  файлов внутри папки составляем сами (`стр-NNN.ext`, режим "xb", без
  перезаписи); при ошибке или отмене уборку делает ядро.
"""

from __future__ import annotations

import io
import logging
import math
import os
import shutil
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from konverter.core import budget
from konverter.strings import tr

from . import _isolated
from .base import (
    CancelToken,
    Category,
    ConversionError,
    Preset,
    ProgressCallback,
    Target,
)

log = logging.getLogger(__name__)

# --- Лимиты -------------------------------------------------------------------

MAX_PAGES = 5000
MAX_INPUT_BYTES = 2**30
MAX_MERGE_BYTES = 2 * 2**30
MAX_MERGE_FILES = 500
# Пикселей на одну отрисованную страницу и длина стороны, не больше.
MAX_PAGE_PIXELS = 50_000_000
MAX_PAGE_SIDE = 20_000
# Сумма пикселей всех страниц одного рендера (≈ 5000 страниц A4 при 300 dpi).
MAX_TOTAL_PIXELS = 50_000_000_000
# Объектов PDF, которые просматривает очистка результата, не больше.
MAX_OBJECTS = 5_000_000
MAX_OUTPUT_BYTES = 20 * 2**30
DISK_RESERVE = 512 * 2**20
MIN_FREE_BYTES = 256 * 2**20
DISK_CHECK_BYTES = 32 * 2**20

# --- Изоляция -----------------------------------------------------------------

STALL_TIMEOUT = 120.0
MAX_TIME = 4 * 3600.0
PDF_MEMORY_LIMIT = 2 * 2**30
# Имя задачи в `_isolated.TASKS` (тесты подменяют на тестовые задачи).
_TASK = "pdf"
# Как часто обход сообщает о продвижении (шагов: посещённых объектов и
# проверок элементов массивов/словарей — одна проверка нового объекта может
# стоить до миллисекунды разбора URI).
_WALK_REPORT_EVERY = 200

# --- Тексты -------------------------------------------------------------------

MSG_DAMAGED = "Файл повреждён или не является PDF"
MSG_NO_MEMORY = "Недостаточно памяти для обработки PDF"

_USER_MESSAGES: dict[str, str] = {
    "password": "PDF защищён паролем: открытие PDF с паролем не поддерживается",
    "bad_encryption": "PDF зашифрован неподдерживаемым способом",
    "too_many_pages": "Слишком много страниц: {0:.0f}, максимум {1:.0f}",
    "no_pages": "В PDF нет страниц",
    "no_text": (
        "В PDF нет текстового слоя (похоже на скан); "
        "распознавание текста не поддерживается"
    ),
    "bad_page": "Повреждена страница {0:.0f}",
    "too_many_pixels": "Слишком большой объём рендера: больше {0:.0f} Мп в сумме",
    "too_complex": "Слишком сложный PDF",
    "no_space": "Недостаточно места на диске: нужно ~{0:.1f} ГБ, свободно {1:.1f} ГБ",
    "disk_full": "Недостаточно места на диске: свободно меньше {0:.1f} ГБ",
    "output_too_large": "Слишком большой результат: больше {0:.0f} ГБ",
    "damaged": MSG_DAMAGED,
}

# --- Варианты и пресеты -------------------------------------------------------

TARGET_PNG = "pdf_pages_png"
TARGET_JPG = "pdf_pages_jpg"
TARGET_TXT = "pdf_txt"
TARGET_MERGE = "pdf_merge"
TARGET_SPLIT = "pdf_split"

# (id, расширение ("" — папка), uses_preset, many_to_one, операция в ребёнке)
_OUTPUTS = (
    (TARGET_PNG, "", True, False, "render"),
    (TARGET_JPG, "", True, False, "render"),
    (TARGET_TXT, "txt", False, False, "text"),
    (TARGET_MERGE, "pdf", False, True, "merge"),
    (TARGET_SPLIT, "", False, False, "split"),
)
TARGET_ORDER: tuple[str, ...] = tuple(item[0] for item in _OUTPUTS)
_OPS = {item[0]: item[4] for item in _OUTPUTS}

DPI = {Preset.HIGH: 300, Preset.MEDIUM: 150, Preset.SMALL: 96}
JPEG_QUALITY = {Preset.HIGH: 92, Preset.MEDIUM: 85, Preset.SMALL: 75}

PAGE_PREFIX = "стр-"
TEXT_PAGE_HEADER = "--- Страница {0} ---"


def _user_message(key: str, params: tuple) -> str:
    template = _USER_MESSAGES.get(key)
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


class PdfConverter:
    """Конвертер PDF (см. докстринг модуля)."""

    category = Category.PDF
    target_order = TARGET_ORDER
    # Своя очередь тяжёлых задач core.jobs (см. докстринг модуля, «Слот»).
    queue = "pdf"
    # Для `core.grouping.common_targets`: объединение — от двух файлов.
    min_files = {TARGET_MERGE: 2}

    def targets(self, fmt: str) -> list[Target]:
        if (fmt or "").lower() != "pdf":
            return []
        return [
            Target(
                id=tid, label=tr(f"target_{tid}"), extension=ext, uses_preset=uses,
                many_to_one=many, is_long=True,
            )
            for tid, ext, uses, many, _op in _OUTPUTS
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
        from konverter.core import naming  # только у родителя (журнал временных файлов)

        cancel.raise_if_cancelled()
        sources = [Path(s) for s in sources]
        if not sources:
            raise ConversionError("Нет файлов для конвертации")
        formats = [str(f or "").lower() for f in (formats or [])]
        if len(formats) != len(sources):
            raise ConversionError("Внутренняя ошибка: не переданы форматы исходных файлов")
        if any(fmt != "pdf" for fmt in formats):
            raise ConversionError(MSG_DAMAGED)
        op = _OPS.get(target.id)
        if op is None:
            raise ConversionError("Внутренняя ошибка: неизвестный вариант конвертации")
        if op == "merge":
            if len(sources) > MAX_MERGE_FILES:
                raise ConversionError(
                    f"Слишком много файлов для объединения: {len(sources)}, "
                    f"максимум {MAX_MERGE_FILES}"
                )
        elif len(sources) != 1:
            raise ConversionError("Этот вариант обрабатывает один файл за раз")

        src_paths = [naming.os_path(s) for s in sources]
        sizes = [_file_size(p) for p in src_paths]
        for size in sizes:
            if size > MAX_INPUT_BYTES:
                raise ConversionError(
                    f"Слишком большой файл: {size / 2**30:.1f} ГБ, "
                    f"максимум {MAX_INPUT_BYTES / 2**30:.0f} ГБ"
                )
        if sum(sizes) > MAX_MERGE_BYTES:
            raise ConversionError(
                f"Слишком большие файлы для объединения: {sum(sizes) / 2**30:.1f} ГБ, "
                f"максимум {MAX_MERGE_BYTES / 2**30:.0f} ГБ"
            )
        dst = naming.os_path(Path(dst))
        if (not target.extension) != os.path.isdir(dst):
            raise ConversionError("Внутренняя ошибка: неверный путь результата")

        preset = Preset(preset)
        args = {
            "op": op,
            "sources": [str(p) for p in src_paths],
            "dst": str(dst),
            "image_format": "jpg" if target.id == TARGET_JPG else "png",
            "dpi": DPI[preset],
            "quality": JPEG_QUALITY[preset],
            "estimate": sum(sizes) if op in ("merge", "split") else 0,
            "limits": {
                "max_pages": MAX_PAGES,
                "max_page_pixels": MAX_PAGE_PIXELS,
                "max_page_side": MAX_PAGE_SIDE,
                "max_total_pixels": MAX_TOTAL_PIXELS,
                "max_objects": MAX_OBJECTS,
                "max_output_bytes": MAX_OUTPUT_BYTES,
                "disk_reserve": DISK_RESERVE,
                "min_free_bytes": MIN_FREE_BYTES,
                "disk_check_bytes": DISK_CHECK_BYTES,
            },
        }
        hold = None
        if op == "render":
            def hold():
                return budget.IMAGE_BUDGET.reserve(MAX_PAGE_PIXELS, cancel)
        try:
            _isolated.run_task(
                _TASK, args, cancel=cancel, progress=progress,
                stall_timeout=STALL_TIMEOUT, max_time=MAX_TIME,
                memory_limit=PDF_MEMORY_LIMIT, pool="pdf", hold=hold,
            )
        except _isolated.TaskFailed as exc:
            raise ConversionError(_user_message(exc.key, exc.params)) from exc
        except _isolated.ChildFailed as exc:
            log.info("Дочерний процесс PDF: %s", exc)
            raise ConversionError(MSG_NO_MEMORY if exc.code == "memory" else MSG_DAMAGED) from exc
        progress(1.0)


# =============================================================================
# Дочерний процесс: весь разбор PDF ниже выполняется ТОЛЬКО в ребёнке.
# =============================================================================

TaskError = _isolated.TaskError

# Действия, которые остаются в результате объединения/разделения.
_SAFE_ACTIONS = frozenset({"/GoTo", "/URI", "/Named"})
# Имена типов действий (ISO 32000-2, 12.6.4, плюс расширения Adobe): словарь
# с `/S` из этого списка — действие, где бы он ни оказался; если оно не из
# `_SAFE_ACTIONS`, словарь удаляется. Другие значения `/S` (группа прозрачности
# `/Transparency`, нумерация страниц `/D`, маски `/Alpha`/`/Luminosity`,
# стиль рамки, переходы страниц) не трогаются.
_ACTION_TYPES = frozenset({
    "/GoTo", "/GoToR", "/GoToE", "/GoToDp", "/Launch", "/Thread", "/URI", "/Sound",
    "/Movie", "/Hide", "/Named", "/SubmitForm", "/ResetForm", "/ImportData",
    "/JavaScript", "/ECMAScript", "/SetOCGState", "/Rendition", "/Trans",
    "/GoTo3DView", "/RichMediaExecute",
})
# Ключи, удаляемые из любых словарей: текст скрипта и словарь имён скриптов.
_SCRIPT_KEYS = ("/JS", "/JavaScript")
# Параметры `mailto:`, которые остаются (без учёта регистра); прочие
# (attach, attachment и т.п. — вложение локального файла) удаляются.
_MAILTO_PARAMS = frozenset({"subject", "body", "cc", "bcc", "to"})
# Длина URI ссылки, не больше (символов); длиннее — действие удаляется.
_MAX_URI_LENGTH = 8192
_SAFE_NAMED = frozenset({"/NextPage", "/PrevPage", "/FirstPage", "/LastPage"})
# Схемы URI, которые остаются в действиях `/URI` (без учёта регистра).
_SAFE_URI_SCHEMES = ("http:", "https:", "mailto:")
# Пробелы и управляющие символы, обрезаемые по краям URI; табуляцию и
# переводы строк браузеры выбрасывают и из середины («java\tscript:»).
_URI_STRIP = "".join(chr(code) for code in range(0x21)) + "\x7f"
_URI_INNER_DROP = str.maketrans("", "", "\t\n\r")
# Белый список подтипов аннотаций: в `/Annots` остаются только они.
_ANNOT_SUBTYPES = frozenset({
    "/Link", "/Text", "/Highlight", "/Underline", "/StrikeOut", "/Squiggly", "/FreeText",
    "/Square", "/Circle", "/Line", "/Polygon", "/PolyLine", "/Ink", "/Stamp", "/Caret",
    "/Popup", "/Widget",
})
# Подтипы, которые удаляются ОТОВСЮДУ (вложения и мультимедиа), а не только
# из `/Annots`: такая аннотация может быть достижима через /Parent, /IRT и
# любые нестандартные ключи.
_DROP_SUBTYPES = frozenset({"/FileAttachment", "/Sound", "/Movie", "/Screen", "/RichMedia", "/3D"})
# Типы объектов, которые не переносятся никогда (ссылка на них → удаляется).
_DROP_TYPES = frozenset({"/Filespec", "/EmbeddedFile", "/Sound", "/3D"})
# Ключи, удаляемые из любых словарей/потоков результата: автодействия,
# метаданные, связанные и вложенные файлы, действие ссылки «по ходу» (/PA).
_DROP_KEYS = ("/AA", "/Metadata", "/PieceInfo", "/AF", "/EF", "/RF", "/FS", "/PA")
# Ключи потоков: данные во внешнем файле и внешняя форма.
_DROP_STREAM_KEYS = ("/F", "/FFilter", "/FDecodeParms", "/Ref")
# Автор и служебные метки аннотаций (кроме виджетов: там /T — имя поля).
_ANNOT_META_KEYS = ("/T", "/M", "/NM", "/CreationDate")
# Ключи слоя (OCG), которые переносятся; прочее (/Usage — автор, язык,
# состояния для печати/экспорта и т.п.) удаляется.
_OCG_KEYS = frozenset({"/Type", "/Name", "/Intent"})
# Глубина вложенности `/Order` (дерево слоёв в панели), не больше.
_MAX_ORDER_DEPTH = 32


def _unquote_all(text: str) -> str:
    """Процентное декодирование до неподвижной точки (`%2561` → `%61` → `a`)."""
    from urllib.parse import unquote

    for _ in range(8):
        decoded = unquote(text)
        if decoded == text:
            return decoded
        text = decoded
    return text


def _clean_mailto(text: str) -> str | None:
    """`mailto:` только с параметрами из `_MAILTO_PARAMS`; None — разобрать нельзя.

    `text` — URI без пробелов по краям, начинается с «mailto:» (любой регистр).
    Возвращает `text` без изменений, если удалять нечего.
    """
    scheme, rest = text[:7], text[7:]
    address, sep, query = rest.partition("?")
    decoded_address = _unquote_all(address)
    if "?" in decoded_address or "&" in decoded_address:
        return None  # скрытые параметры в адресе
    if not sep:
        return text
    kept = []
    for part in query.split("&"):
        name, _eq, value = part.partition("=")
        if _unquote_all(name).strip(_URI_STRIP).lower() not in _MAILTO_PARAMS:
            continue
        # Значение не должно само по себе нести новые параметры
        # («body=a%26attach%3Dz» у почтовика, декодирующего до разбора).
        # «;» тоже: некоторые разборщики считают его разделителем
        # («subject=x;attach=C:/x»).
        decoded_value = _unquote_all(value)
        if "&" in decoded_value or "?" in decoded_value or ";" in decoded_value:
            continue
        kept.append(part)
    if len(kept) == len(query.split("&")):
        return text
    return scheme + address + ("?" + "&".join(kept) if kept else "")


def _clean_uri(value) -> str | None:
    """URI ссылки, который можно оставить, или None — действие удаляется.

    Допустима строка не длиннее `_MAX_URI_LENGTH` со схемой http/https/mailto;
    у `mailto:` остаются только параметры из `_MAILTO_PARAMS` (URI
    переписывается). Относительные URI удаляются: `/URI /Base` каталога не
    переносится, и их смысл в новом документе не определён.
    """
    from pikepdf import String

    if not isinstance(value, String):
        return None
    try:
        text = str(value)
    except (UnicodeDecodeError, ValueError):
        return None
    if len(text) > _MAX_URI_LENGTH:
        return None
    clean = text.translate(_URI_INNER_DROP).strip(_URI_STRIP)
    lower = clean.lower()
    if not lower.startswith(_SAFE_URI_SCHEMES):
        return None
    if lower.startswith("mailto:"):
        mailto = _clean_mailto(clean)
        if mailto is None:
            return None
        if mailto != clean:
            return mailto
    return text


def child_task(args: dict[str, Any], report) -> dict[str, Any]:
    """Точка входа задачи "pdf" в дочернем процессе."""
    import warnings

    # Предупреждения pikepdf/pypdfium2 о строении файла пользователю не нужны
    # (и не должны засорять консоль родителя).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return _PdfJob(args, report).run()


class _CountingWriter(io.RawIOBase):
    """Поток записи для qpdf: считает байты и проверяет место (через задачу).

    Ошибку из `write` qpdf может завернуть в своё исключение, поэтому она
    запоминается в `error` и пробрасывается после `save`.
    """

    def __init__(self, fh, job: "_PdfJob") -> None:
        super().__init__()
        self._fh = fh
        self._job = job
        self.error: BaseException | None = None

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        if self.error is not None:
            raise self.error
        try:
            size = len(data)
            self._job.account(size)
            self._fh.write(data)
            return size
        except BaseException as exc:
            self.error = exc
            raise


class _PdfJob:
    """Одна задача PDF в дочернем процессе."""

    def __init__(self, args: dict[str, Any], report) -> None:
        self.op = str(args["op"])
        self.sources = [str(s) for s in args["sources"]]
        self.dst = str(args["dst"])
        self.image_format = "jpg" if args.get("image_format") == "jpg" else "png"
        self.dpi = float(args.get("dpi", DPI[Preset.MEDIUM]))
        self.quality = int(args.get("quality", JPEG_QUALITY[Preset.MEDIUM]))
        self.estimate = max(0, int(args.get("estimate", 0)))
        limits = dict(args.get("limits") or {})
        self.max_pages = int(limits.get("max_pages", MAX_PAGES))
        self.max_page_pixels = int(limits.get("max_page_pixels", MAX_PAGE_PIXELS))
        self.max_page_side = int(limits.get("max_page_side", MAX_PAGE_SIDE))
        self.max_total_pixels = int(limits.get("max_total_pixels", MAX_TOTAL_PIXELS))
        self.max_objects = int(limits.get("max_objects", MAX_OBJECTS))
        self.max_output_bytes = int(limits.get("max_output_bytes", MAX_OUTPUT_BYTES))
        self.disk_reserve = int(limits.get("disk_reserve", DISK_RESERVE))
        self.min_free_bytes = int(limits.get("min_free_bytes", MIN_FREE_BYTES))
        self.disk_check_bytes = max(1, int(limits.get("disk_check_bytes", DISK_CHECK_BYTES)))
        self.report = report
        self.written = 0
        self.checked_at = 0
        self.total_pixels = 0
        # Отметка продвижения для сторожа зависания: только растёт.
        self.mark = 0

    # -- прогресс, диск --

    def _advance(self, fraction: float | None) -> None:
        self.mark += 1
        self.report(fraction, self.mark)

    def _folder(self) -> str:
        if os.path.isdir(self.dst):
            return self.dst
        return os.path.dirname(os.path.abspath(self.dst)) or "."

    def _free_space(self) -> int | None:
        try:
            return int(shutil.disk_usage(self._folder()).free)
        except (OSError, ValueError):
            return None

    def check_space(self) -> None:
        """До записи: оценка не больше предела и место под неё есть."""
        if self.estimate > self.max_output_bytes:
            raise TaskError("output_too_large", self.max_output_bytes / 2**30)
        need = self.estimate + self.disk_reserve
        free = self._free_space()
        if free is not None and free < need:
            raise TaskError("no_space", need / 2**30, free / 2**30)

    def account(self, size: int) -> None:
        """Учёт записанных байт: предел размера и периодическая проверка места."""
        self.written += max(0, int(size))
        if self.written > self.max_output_bytes:
            raise TaskError("output_too_large", self.max_output_bytes / 2**30)
        if self.written - self.checked_at >= self.disk_check_bytes:
            self.checked_at = self.written
            free = self._free_space()
            if free is not None and free < self.min_free_bytes:
                raise TaskError("disk_full", self.min_free_bytes / 2**30)

    def _check_pages(self, count: int) -> None:
        if count > self.max_pages:
            raise TaskError("too_many_pages", count, self.max_pages)

    def _page_path(self, index: int, count: int, ext: str) -> str:
        width = max(3, len(str(count)))
        return os.path.join(self.dst, f"{PAGE_PREFIX}{index + 1:0{width}d}.{ext}")

    # -- запуск --

    def run(self) -> dict[str, Any]:
        if self.op in ("render", "split") and not os.path.isdir(self.dst):
            raise TaskError("damaged")
        self.check_space()
        if self.op == "render":
            return self._render()
        if self.op == "text":
            return self._text()
        if self.op == "merge":
            return self._merge()
        if self.op == "split":
            return self._split()
        raise ValueError("unknown op")

    # -- pdfium: рендер и текст --

    def _open_pdfium(self, stack: ExitStack, path: str):
        """Документ pdfium из нашего дескриптора файла (без форм и JS)."""
        import pypdfium2 as pdfium
        import pypdfium2.raw as pdfium_c

        fh = stack.enter_context(open(path, "rb"))
        try:
            pdf = pdfium.PdfDocument(fh)
        except pdfium.PdfiumError as exc:
            code = getattr(exc, "err_code", None)
            if code == pdfium_c.FPDF_ERR_PASSWORD:
                raise TaskError("password") from None
            if code == pdfium_c.FPDF_ERR_SECURITY:
                raise TaskError("bad_encryption") from None
            raise TaskError("damaged") from None
        stack.callback(pdf.close)
        count = len(pdf)
        self._check_pages(count)
        if count <= 0:
            raise TaskError("no_pages")
        return pdf, count

    def _scale(self, width: float, height: float, number: int) -> float:
        """Масштаб рендера: dpi пресета, но не больше пределов страницы."""
        if not (math.isfinite(width) and math.isfinite(height)) or width <= 0 or height <= 0:
            raise TaskError("bad_page", number)
        scale = self.dpi / 72.0
        # При уменьшении — запас 0,1 % на округление размеров вверх (ceil) в render.
        if math.ceil(width * scale) * math.ceil(height * scale) > self.max_page_pixels:
            scale = math.sqrt(self.max_page_pixels / (width * height)) * 0.999
        if math.ceil(max(width, height) * scale) > self.max_page_side:
            scale = self.max_page_side / max(width, height) * 0.999
        return scale

    def _render(self) -> dict[str, Any]:
        with ExitStack() as stack:
            pdf, count = self._open_pdfium(stack, self.sources[0])
            for index in range(count):
                page = pdf[index]
                try:
                    width, height = page.get_size()
                    scale = self._scale(float(width), float(height), index + 1)
                    pixels = math.ceil(width * scale) * math.ceil(height * scale)
                    if self.total_pixels + pixels > self.max_total_pixels:
                        raise TaskError("too_many_pixels", self.max_total_pixels / 1e6)
                    self.total_pixels += pixels
                    # Формы не рисуем (и не инициализируем): только содержимое
                    # страницы и внешний вид аннотаций.
                    bitmap = page.render(scale=scale, may_draw_forms=False, draw_annots=True)
                    try:
                        image = bitmap.to_pil()
                        image.load()
                        if image.mode != "RGB":
                            image = image.convert("RGB")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                path = self._page_path(index, count, self.image_format)
                with open(path, "xb") as fh:
                    if self.image_format == "jpg":
                        image.save(
                            fh, format="JPEG", quality=self.quality,
                            subsampling=0 if self.quality >= 90 else 2,
                        )
                    else:
                        image.save(fh, format="PNG", compress_level=6)
                    size = fh.tell()
                del image
                self.account(size)
                self._advance((index + 1) / count)
        return {"pages": count}

    @staticmethod
    def _clean_text(text: str) -> str:
        """Текст страницы: переводы строк — `\\n`, без управляющих символов."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return "".join(
            ch for ch in text
            if ch in "\n\t" or (ch >= " " and ch not in "\x7f￾￿"
                                and not ("\ud800" <= ch <= "\udfff"))
        )

    def _text(self) -> dict[str, Any]:
        has_text = False
        with ExitStack() as stack:
            pdf, count = self._open_pdfium(stack, self.sources[0])
            out = stack.enter_context(open(self.dst, "wb"))
            for index in range(count):
                page = pdf[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                text = self._clean_text(text)
                if text.strip():
                    has_text = True
                chunk = TEXT_PAGE_HEADER.format(index + 1) + "\n" + text.rstrip("\n") + "\n\n"
                data = chunk.replace("\n", "\r\n").encode("utf-8")
                self.account(len(data))
                out.write(data)
                self._advance((index + 1) / count)
        if not has_text:
            raise TaskError("no_text")
        return {"pages": count}

    # -- pikepdf (qpdf): объединение и разделение --

    def _open_pike(self, stack: ExitStack, path: str):
        import pikepdf

        fh = stack.enter_context(open(path, "rb"))
        try:
            pdf = pikepdf.open(
                fh, password="", suppress_warnings=True, attempt_recovery=True,
                inherit_page_attributes=True,
            )
        except pikepdf.PasswordError:
            raise TaskError("password") from None
        except pikepdf.PdfError:
            raise TaskError("damaged") from None
        stack.callback(pdf.close)
        return pdf

    def _save(self, out, fh, fraction: tuple[float, float] | None) -> None:
        import pikepdf

        writer = _CountingWriter(fh, self)

        def on_progress(percent: int) -> None:
            if fraction is not None:
                start, end = fraction
                self._advance(start + (end - start) * max(0, min(100, int(percent))) / 100)
            else:
                self._advance(None)

        try:
            out.save(
                writer,
                linearize=False,
                object_stream_mode=pikepdf.ObjectStreamMode.disable,
                compress_streams=True,
                encryption=False,
                deterministic_id=True,
                fix_metadata_version=False,
                preserve_pdfa=False,
                progress=on_progress,
            )
        except Exception:
            if writer.error is not None:
                raise writer.error from None
            raise
        if writer.error is not None:
            raise writer.error

    def _merge(self) -> dict[str, Any]:
        import pikepdf

        with ExitStack() as stack:
            sources = []
            for path in self.sources:
                sources.append(self._open_pike(stack, path))
                # Открытие сотен файлов (с «починкой» qpdf) — тоже продвижение:
                # иначе сторож STALL счёл бы его зависанием.
                self._advance(None)
            counts = [len(pdf.pages) for pdf in sources]
            total = sum(counts)
            self._check_pages(total)
            if total <= 0:
                raise TaskError("no_pages")
            out = pikepdf.new()
            stack.callback(out.close)
            done = 0
            for pdf in sources:
                for page in pdf.pages:
                    out.pages.append(page)
                    done += 1
                    self._advance(0.4 * done / total)
            self._sanitize(out, (0.4, 0.5), sources)
            with open(self.dst, "wb") as fh:
                self._save(out, fh, (0.5, 1.0))
        return {"pages": total}

    def _split(self) -> dict[str, Any]:
        import pikepdf

        with ExitStack() as stack:
            pdf = self._open_pike(stack, self.sources[0])
            count = len(pdf.pages)
            self._check_pages(count)
            if count <= 0:
                raise TaskError("no_pages")
            for index, page in enumerate(pdf.pages):
                with pikepdf.new() as out:
                    out.pages.append(page)
                    self._sanitize(out, None, [pdf])
                    with open(self._page_path(index, count, "pdf"), "xb") as fh:
                        self._save(out, fh, None)
                self._advance((index + 1) / count)
        return {"pages": count}

    # -- очистка результата --

    def _sanitize(self, out, fraction: tuple[float, float] | None, sources: list) -> None:
        """Убирает из нового документа активное содержимое и метаданные.

        См. докстринг модуля. Каталог нового документа очищается до `/Type`
        и `/Pages`; затем обходятся страницы и всё, что из них достижимо;
        в конце из настроек слоёв исходников `sources` собирается безопасный
        `/OCProperties` (только для слоёв, попавших в результат).
        """
        from pikepdf import Array, Dictionary, Name, Stream, String

        if "/Info" in out.trailer:
            del out.trailer["/Info"]
        for key in list(out.Root.keys()):
            if key not in ("/Type", "/Pages"):
                del out.Root[key]

        def is_dict(obj) -> bool:
            return isinstance(obj, (Dictionary, Stream))

        def name_of(obj, key: str) -> str:
            value = obj.get(key)
            return str(value) if isinstance(value, Name) else ""

        def dropped_base(obj) -> bool:
            """Вложение, мультимедиа или аннотация не из белого списка."""
            if name_of(obj, "/Type") in _DROP_TYPES:
                return True
            subtype = name_of(obj, "/Subtype")
            if subtype in _DROP_SUBTYPES:
                return True
            # Аннотация — по /Type /Annot или (тип необязателен) по /Rect с /Subtype.
            annot = name_of(obj, "/Type") == "/Annot" or (bool(subtype) and "/Rect" in obj)
            return annot and subtype not in _ANNOT_SUBTYPES

        # Решения по косвенным объектам — один раз на объект (по `objgen`):
        # на одно действие может ссылаться очень много мест (массив из
        # десятков тысяч ссылок на `mailto:` с сотнями параметров).
        uri_cache: dict[tuple[int, int], str | None] = {}
        dropped_cache: dict[tuple[int, int], bool] = {}

        def clean_uri_of(action) -> str | None:
            """`_clean_uri` для `/URI` словаря-действия (с кэшем для косвенных)."""
            if not action.is_indirect:
                return _clean_uri(action.get("/URI"))
            key = action.objgen
            if key not in uri_cache:
                uri_cache[key] = _clean_uri(action.get("/URI"))
            return uri_cache[key]

        def bad_action(obj) -> bool:
            """Словарь-действие (по `/S` из `_ACTION_TYPES`), которое не переносится."""
            kind = name_of(obj, "/S")
            if kind not in _ACTION_TYPES:
                return False
            return (
                kind not in _SAFE_ACTIONS
                or (kind == "/Named" and name_of(obj, "/N") not in _SAFE_NAMED)
                or (kind == "/URI" and clean_uri_of(obj) is None)
            )

        def dropped(obj) -> bool:
            """Объект не переносится: ссылка на него удаляется (в массиве → null)."""
            if not is_dict(obj):
                return False
            if not obj.is_indirect:
                return dropped_uncached(obj)
            key = obj.objgen
            if key not in dropped_cache:
                dropped_cache[key] = dropped_uncached(obj)
            return dropped_cache[key]

        def dropped_uncached(obj) -> bool:
            if dropped_base(obj):
                return True
            # Опасное действие под любым ключом или в массиве (/MyA, /Names …).
            # Узлы дерева страниц не удаляются: у них ниже чистятся ключи.
            if name_of(obj, "/Type") not in ("/Page", "/Pages") and bad_action(obj):
                return True
            if name_of(obj, "/Subtype") == "/Popup":
                # Всплывающее окно удалённой аннотации показывало бы её текст.
                parent = obj.get("/Parent")
                if is_dict(parent) and (
                    name_of(parent, "/Subtype") == "/Popup" or dropped_base(parent)
                ):
                    return True
            return False

        def is_annot(obj, subtype: str) -> bool:
            return name_of(obj, "/Type") == "/Annot" or (
                subtype in _ANNOT_SUBTYPES and "/Rect" in obj
            )

        def clean_action(holder, key: str) -> None:
            action = holder.get(key)
            if action is None:
                return
            if not is_dict(action):
                del holder[key]
                return
            kind = name_of(action, "/S")
            if kind not in _SAFE_ACTIONS or bad_action(action):
                del holder[key]
                return
            if "/Next" in action:
                del action["/Next"]

        seen: set[tuple[int, int]] = set()
        # Слои (OCG), попавшие в результат, — в порядке обнаружения.
        layers: list = []
        layer_ids: set[tuple[int, int]] = set()
        stack: list = [out.Root]
        visited = 0
        steps = [0]

        def tick() -> None:
            """Шаг обхода; периодически — отметка продвижения для сторожа."""
            steps[0] += 1
            if steps[0] % _WALK_REPORT_EVERY == 0:
                self._advance(fraction[0] if fraction else None)

        while stack:
            obj = stack.pop()
            if obj.is_indirect:
                key = obj.objgen
                if key in seen:
                    continue
                seen.add(key)
            visited += 1
            if visited > self.max_objects:
                raise TaskError("too_complex")
            tick()
            if isinstance(obj, Array):
                for index, item in enumerate(obj):
                    tick()
                    if dropped(item):
                        obj[index] = None
                    elif isinstance(item, (Array, Dictionary, Stream)):
                        stack.append(item)
                continue
            if not is_dict(obj):
                continue
            for name in _DROP_KEYS:
                if name in obj:
                    del obj[name]
            if isinstance(obj, Stream):
                for name in _DROP_STREAM_KEYS:
                    if name in obj:
                        del obj[name]
            obj_type = name_of(obj, "/Type")
            subtype = name_of(obj, "/Subtype")
            if obj_type == "/Page" and "/B" in obj:
                del obj["/B"]
            if obj_type == "/OCG":
                # Слой: только тип, имя и назначение; дальше не обходим.
                for name in list(obj.keys()):
                    if name not in _OCG_KEYS:
                        del obj[name]
                if not isinstance(obj.get("/Name"), String):
                    obj["/Name"] = String("")
                intent = obj.get("/Intent")
                if intent is not None and not (
                    isinstance(intent, Name)
                    or (isinstance(intent, Array) and all(isinstance(i, Name) for i in intent))
                ):
                    del obj["/Intent"]
                if obj.is_indirect and obj.objgen not in layer_ids:
                    layer_ids.add(obj.objgen)
                    layers.append(obj)
                continue
            if subtype == "/Widget" and "/Parent" in obj:
                del obj["/Parent"]
            if subtype and subtype != "/Widget" and is_annot(obj, subtype):
                for name in _ANNOT_META_KEYS:
                    if name in obj:
                        del obj[name]
            if "/A" in obj:
                clean_action(obj, "/A")
            for name in _SCRIPT_KEYS:
                if name in obj:
                    del obj[name]
            if "/Names" in obj and (obj_type == "/Page" or is_annot(obj, subtype)):
                del obj["/Names"]  # словарь имён (скрипты) у страницы/аннотации
            if "/S" in obj:  # словарь-действие, где бы он ни оказался
                if "/Next" in obj:
                    del obj["/Next"]
                if name_of(obj, "/S") in _ACTION_TYPES and bad_action(obj):
                    # Сюда доходит только узел дерева страниц (прочее удалено
                    # у родителя): убираем признак действия.
                    del obj["/S"]
                elif name_of(obj, "/S") == "/URI" and "/URI" in obj:
                    uri = clean_uri_of(obj)
                    if uri is None:
                        del obj["/URI"]
                    elif uri != str(obj.get("/URI")):
                        obj["/URI"] = String(uri)  # mailto: без лишних параметров
            if "/Annots" in obj:
                annots = obj.get("/Annots")
                if not isinstance(annots, Array):
                    del obj["/Annots"]
                else:
                    keep = []
                    for annot in annots:
                        tick()
                        if (
                            isinstance(annot, Dictionary)
                            and name_of(annot, "/Subtype") in _ANNOT_SUBTYPES
                            and not dropped(annot)
                        ):
                            keep.append(annot)
                    if len(keep) != len(annots):
                        obj["/Annots"] = Array(keep)
            for name in list(obj.keys()):
                if name == "/Parent" and obj_type in ("/Page", "/Pages"):
                    continue  # дерево страниц и так обходится от корня
                tick()
                value = obj.get(name)
                if dropped(value):
                    # /Parent и /IRT удалённой аннотации, /MyKey → Filespec …
                    del obj[name]
                elif isinstance(value, (Array, Dictionary, Stream)):
                    stack.append(value)
        if layers:
            self._layers(out, sources, layers)
        if fraction is not None:
            self._advance(fraction[1])

    def _layers(self, out, sources: list, layers: list) -> None:
        """`/OCProperties` результата: слои `layers` и их видимость из исходников.

        Переносится только то, что нужно для показа: список слоёв (`/OCGs`) и
        конфигурация по умолчанию `/D` с `/OFF` (выключенные слои — из
        `/OFF` или, при `/BaseState /OFF`, все слои исходника, кроме `/ON`),
        `/Order` (дерево в панели слоёв) и `/RBGroups` (группы «один из»).
        Не переносятся `/AS` (автосмена видимости по событиям), `/Configs`,
        `/Locked`, имена и авторы конфигураций. Ссылки исходников переводятся
        в объекты результата через `copy_foreign` (уже скопированный объект
        qpdf возвращает тот же); слой, не попавший в результат, отбрасывается.
        """
        from pikepdf import Array, Dictionary, Name, String

        layer_ids = {layer.objgen for layer in layers}
        budget_left = [self.max_objects]

        def spend() -> None:
            budget_left[0] -= 1
            if budget_left[0] < 0:
                raise TaskError("too_complex")
            if budget_left[0] % _WALK_REPORT_EVERY == 0:
                self._advance(None)  # для сторожа зависания

        def local(ref):
            if not isinstance(ref, Dictionary) or not ref.is_indirect:
                return None
            value = ref.get("/Type")
            if not (isinstance(value, Name) and str(value) == "/OCG"):
                return None
            try:
                copy = out.copy_foreign(ref)
            except Exception:  # noqa: BLE001 — битая ссылка: слой без настроек
                return None
            return copy if copy.objgen in layer_ids else None

        def refs(array) -> list:
            result = []
            if isinstance(array, Array):
                for item in array:
                    spend()
                    mapped = local(item)
                    if mapped is not None:
                        result.append(mapped)
            return result

        # Косвенные массивы `/Order`, уже пройденные в текущем исходнике: цикл
        # или повторная ссылка на ветку обрезается (иначе ветвление 2^глубина).
        order_seen: set[tuple[int, int]] = set()

        def order(array, depth: int):
            """Ветка дерева слоёв; None — в ней не осталось слоёв."""
            if depth > _MAX_ORDER_DEPTH:
                return None
            if array.is_indirect:
                if array.objgen in order_seen:
                    return None
                order_seen.add(array.objgen)
            result = []
            for item in array:
                spend()
                if isinstance(item, Array):
                    nested = order(item, depth + 1)
                    if nested is not None:
                        result.append(nested)
                elif isinstance(item, String):
                    result.append(String(str(item)))  # подпись ветки
                else:
                    mapped = local(item)
                    if mapped is not None:
                        result.append(mapped)
            if all(isinstance(item, String) for item in result):
                return None
            return Array(result)

        off: list = []
        off_ids: set[tuple[int, int]] = set()
        orders: list = []
        groups: list = []
        for pdf in sources:
            props = pdf.Root.get("/OCProperties")
            if not isinstance(props, Dictionary):
                continue
            config = props.get("/D")
            if not isinstance(config, Dictionary):
                continue
            base = config.get("/BaseState")
            if isinstance(base, Name) and str(base) == "/OFF":
                on_ids = {layer.objgen for layer in refs(config.get("/ON"))}
                hidden = [
                    layer for layer in refs(props.get("/OCGs")) if layer.objgen not in on_ids
                ]
            else:
                hidden = refs(config.get("/OFF"))
            for layer in hidden:
                if layer.objgen not in off_ids:
                    off_ids.add(layer.objgen)
                    off.append(layer)
            tree = config.get("/Order")
            order_seen.clear()  # номера объектов — свои у каждого исходника
            if isinstance(tree, Array):
                mapped = order(tree, 0)
                if mapped is not None:
                    orders.extend(mapped)
            rb = config.get("/RBGroups")
            if isinstance(rb, Array):
                for group in rb:
                    members = refs(group)
                    if len(members) >= 2:
                        groups.append(Array(members))
        config = Dictionary(BaseState=Name.ON, OFF=Array(off))
        if orders:
            config["/Order"] = Array(orders)
        if groups:
            config["/RBGroups"] = Array(groups)
        out.Root["/OCProperties"] = Dictionary(OCGs=Array(layers), D=config)
