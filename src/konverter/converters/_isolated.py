"""Изолированное декодирование опасных форматов (SVG, RAW) в дочернем процессе.

Зачем: рендер resvg и `rawpy.postprocess` — долгие вызовы нативного кода,
которые нельзя прервать изнутри потока. «Бомба» SVG на 700 байт рендерится
минуты, а поток, застрявший в нативном коде, не даёт ни отменить задачу,
ни нормально завершить программу. Поэтому декодирование идёт в отдельном
процессе (`multiprocessing`, контекст `spawn`), который родитель может
убить при отмене или по таймауту.

Протокол (сообщения через `Pipe`, только простые кортежи):
1. ребёнок → ("size", w, h, …) — размеры известны, пиксели ещё не декодированы;
2. родитель проверяет размеры, резервирует бюджет памяти → ("go",);
3. ребёнок декодирует → ("pixels", mode, w, h, nbytes);
4. родитель проверяет размеры, создаёт блок общей памяти → ("shm", name);
5. ребёнок копирует пиксели в общую память → ("done",);
   при ошибке на любом шаге ребёнок шлёт ("error", код, тип исключения).
Пиксели передаются через `multiprocessing.shared_memory`: на Windows это
именованный объект в памяти (без файлов на диске), он исчезает, когда
закрыт последний дескриптор, — даже если процесс убит, мусора не остаётся.

Гарантии:
- родитель каждые `POLL_INTERVAL` с проверяет отмену и таймаут; при
  отмене — `Cancelled`, при таймауте — `ConversionError(MSG_TIMEOUT)`;
  процесс в обоих случаях убивается (`kill`) и дожидается (`join`), зомби
  не остаются;
- на Windows дочерние процессы помещаются в объект задания (Job Object)
  с флагом KILL_ON_JOB_CLOSE: если родитель аварийно завершится, система
  сама убьёт детей; кроме того, в ребёнке работает сторож, который
  завершает процесс, когда родитель исчез (если GIL свободен);
- в ребёнке отключены системные окна об ошибках (`SetErrorMode`), консоль
  `spawn` не открывает (ребёнок наследует консоль/её отсутствие у родителя);
- ребёнок не импортирует PySide6: модуль `konverter.converters` и его
  зависимости GUI не тянут (проверяется тестом через вид задачи "probe");
- в объекте задания стоит лимит памяти одного процесса
  (`PROCESS_MEMORY_LIMIT`, JOB_OBJECT_LIMIT_PROCESS_MEMORY): при превышении
  выделение памяти в ребёнке не удаётся (MemoryError в Python или аварийный
  выход нативного кода); родитель узнаёт об этом по сообщению порта
  завершения задания и сообщает `ChildFailed("memory")` — «нехватка памяти»,
  а не «файл повреждён». Общая память при этом освобождается как обычно;
- лимит выше — на ОДИН процесс; чтобы сумма не росла с числом потоков,
  одновременно работают не больше `MAX_CHILDREN` детей (по умолчанию 1,
  при ≥ 32 ГБ ОЗУ — 2), остальные задачи ждут места (с отменой, вне таймаута).

Что изоляция НЕ даёт (честно): это мера НАДЁЖНОСТИ — таймауты, отмена,
ограничение памяти, аварии нативного кода не роняют программу, — а не граница
безопасности. Дочерний процесс работает с теми же правами, что и программа
(без песочницы: он может читать и писать те же файлы и ходить в сеть), а его
ответы родитель принимает через `Pipe`, то есть распаковывает pickle. Если
враждебный файл сумеет выполнить код в ребёнке (через уязвимость resvg или
LibRaw), он получит те же возможности, что и сама программа, и сможет
прислать родителю вредоносный pickle. Защита от этого — только в том, что
декодеры не должны давать выполнить код; изоляция от этого не спасает.

Общий режим «задача с прогрессом» (`run_task`, для аудио/видео):
- ребёнок выполняет функцию из реестра `TASKS` (имя → «модуль:функция») или
  тестовую задачу из `_TEST_TASKS`; аргументы — простой словарь от родителя;
- сообщения ребёнка: ("progress", доля | None) — работа продвинулась
  (None — доля неизвестна); ("result", значение) — готово; ("error", код,
  подробность[, параметры]) — ошибка. Код "user" — ошибка, понятная
  пользователю (`TaskError` в ребёнке → `TaskFailed` у родителя): ключ
  сообщения и ЧИСЛОВЫЕ параметры, текст составляет родитель по своему
  шаблону (ребёнок не присылает готовых строк для показа);
- ребёнок сообщает прогресс, только когда работа реально продвинулась
  (выросли «отметки» — время/позиция в файле), не чаще `TASK_REPORT_INTERVAL`;
  родитель считает зависанием `stall_timeout` секунд без таких сообщений
  (→ kill, `ConversionError(MSG_STALLED)`), плюс общий предел `max_time`
  (→ `ConversionError(MSG_TIMEOUT)`); доля прогресса у родителя монотонна;
- отмена — kill процесса (опрос каждые `POLL_INTERVAL` с), затем `Cancelled`;
- результат ребёнок пишет сам (путь — в аргументах); родитель после
  kill/ошибки ничего не переименовывает и не удаляет — это делает ядро;
- у долгих задач СВОЙ счётчик мест (`LONG_MAX_CHILDREN`, по умолчанию 1),
  отдельный от `MAX_CHILDREN` для SVG/RAW: иначе видео на час заняло бы
  единственное место и SVG ждал бы его окончания. Цена — до
  `MAX_CHILDREN` + `LONG_MAX_CHILDREN` детей одновременно, т.е. в худшем
  случае (1 + 1) × 3 ГБ = 6 ГБ выделенной памяти (при ≥ 31 ГиБ ОЗУ —
  (2 + 1) × 3 ГБ = 9 ГБ). Это предел, а не обычный расход; лимит памяти
  задаётся на процесс (`memory_limit`, по умолчанию `PROCESS_MEMORY_LIMIT`).
- PDF (`converters/pdf.py`) идёт через тот же `run_task`, но в СВОЁМ счётчике
  мест (`pool="pdf"`, предел `PDF_MAX_CHILDREN`, по умолчанию 1): задачи PDF
  выполняются в общем пуле потоков (не в «тяжёлой» очереди), и при общем
  счётчике с видео рендер страниц ждал бы окончания часового видео. Лимит
  памяти PDF-ребёнка меньше (2 ГБ, см. `pdf.PDF_MEMORY_LIMIT`), поэтому
  худший случай растёт до 1·3 + 1·3 + 2 = 8 ГБ (при ≥ 31 ГиБ ОЗУ — 11 ГБ).
- Книги, документы и таблицы (`converters/books.py`, `documents.py`,
  `tables.py`, этап 4) — задача "docs" в СВОЁМ счётчике мест (`pool="docs"`,
  предел `DOCS_MAX_CHILDREN`, по умолчанию 1, лимит памяти 2 ГБ — см.
  `_docjob.DOCS_MEMORY_LIMIT`): разбор ZIP/XML/HTML не ждёт видео и PDF.
  Худший случай памяти растёт ещё на 2 ГБ.
- Окно «Фрагмент для GIF» (кадры начала/конца и предпросмотр,
  `media.fragment_frames` / `media.gif_preview`) — тоже в СВОЁМ счётчике
  (`pool="preview"`, предел `PREVIEW_MAX_CHILDREN`, по умолчанию 1, лимит
  памяти 2 ГБ — `media.PREVIEW_MEMORY_LIMIT`): окно не ждёт часового видео
  в очереди. Худший случай памяти растёт ещё на 2 ГБ.
- `hold` — необязательный контекстный менеджер (например, резервация бюджета
  пикселей), который берётся ПОСЛЕ получения места и отпускается после
  завершения процесса: задача, ждущая места, ничего не держит.

Для сборки PyInstaller в точке входа нужен `multiprocessing.freeze_support()`
(см. `konverter.main` и `konverter/__main__.py`).
"""

from __future__ import annotations

import importlib
import logging
import math
import multiprocessing
import os
import sys
import threading
import time
from contextlib import ExitStack
from multiprocessing.shared_memory import SharedMemory
from typing import Any, Callable, ContextManager

from PIL import Image

from .base import CancelToken, Cancelled, ConversionError

log = logging.getLogger(__name__)

# Предельное время обработки в дочернем процессе, с (время ожидания
# бюджета памяти не считается).
SVG_TIMEOUT = 120.0
RAW_TIMEOUT = 300.0
# Как часто родитель проверяет отмену/таймаут, с.
POLL_INTERVAL = 0.1
# Сколько ждать штатного завершения ребёнка после ответа / после kill, с.
JOIN_TIMEOUT = 5.0
# Лимит выделенной памяти одного дочернего процесса (Job Object,
# JOB_OBJECT_LIMIT_PROCESS_MEMORY), байт. Последний рубеж против «бомб»,
# которые не поймали проверки заголовков: превышение → «нехватка памяти».
PROCESS_MEMORY_LIMIT = 3 * 2**30
# Сколько дочерних процессов могут работать одновременно (на всю программу).
# Лимит памяти выше — на ОДИН процесс, а задачи SVG идут в общий пул
# потоков: без общего ограничения 8 детей × 3 ГБ = 24 ГБ. None — выбрать
# автоматически по объёму ОЗУ (`_max_children`): 2 при ≥ 32 ГБ, иначе 1.
# Число (например, 1) — жёстко; в тестах подменяется.
MAX_CHILDREN: int | None = None
# Порог ОЗУ для двух детей. Чуть меньше 32 ГиБ: на машине «32 ГБ» система
# видит немного меньше (память, зарезервированная оборудованием).
_TWO_CHILDREN_MIN_RAM = 31 * 2**30

# Сколько долгих задач (`run_task`: аудио, видео) могут работать одновременно
# в дочерних процессах. Отдельно от MAX_CHILDREN (см. докстринг модуля).
LONG_MAX_CHILDREN = 1
# Сколько задач PDF (`run_task(..., pool="pdf")`) работают одновременно.
PDF_MAX_CHILDREN = 1
# Сколько задач книг/документов/таблиц (`pool="docs"`) работают одновременно.
DOCS_MAX_CHILDREN = 1
# Сколько задач окна «Фрагмент для GIF» (кадры, предпросмотр; `pool="preview"`)
# работают одновременно. Свой счётчик: окно не ждёт идущих конвертаций видео.
PREVIEW_MAX_CHILDREN = 1
# Как часто ребёнок шлёт сообщения о прогрессе (не чаще), с.
TASK_REPORT_INTERVAL = 0.1

MSG_TIMEOUT = "Слишком сложный файл: превышено время обработки"
MSG_STALLED = "Обработка зависла"

# Задачи общего режима: имя → "модуль:функция". Функция вызывается в
# дочернем процессе как func(args: dict, report) и возвращает простое
# значение (оно уходит родителю через pickle).
TASKS: dict[str, str] = {
    "media": "konverter.converters.media:child_task",
    "pdf": "konverter.converters.pdf:child_task",
    "docs": "konverter.converters._docjob:child_task",
    "office": "konverter.converters._office:child_task",
}

_ALLOWED_MODES = {"RGB": 3, "RGBA": 4}


class ChildFailed(Exception):
    """Ребёнок сообщил об ошибке или аварийно завершился.

    `code`: "damaged" (файл не разобран), "memory" (нехватка памяти),
    "crashed" (процесс умер без ответа), "protocol" (неожиданный ответ),
    "user" (понятная пользователю ошибка задачи — см. `TaskFailed`).
    `detail` — только тип исключения, без содержимого файла.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class TaskError(Exception):
    """Бросается В РЕБЁНКЕ: ошибка, понятная пользователю.

    `key` — ключ сообщения (шаблон знает родитель), `params` — только числа
    (их подставляют в шаблон). Готовых строк ребёнок не передаёт.
    """

    def __init__(self, key: str, *params: int | float) -> None:
        super().__init__(key, *params)
        self.key = key
        self.params = params


class TaskFailed(ChildFailed):
    """У родителя: ребёнок сообщил `TaskError` (ключ и числовые параметры)."""

    def __init__(self, key: str, params: tuple[int | float, ...] = ()) -> None:
        super().__init__("user", key)
        self.key = key
        self.params = params


def _clean_params(raw: Any) -> tuple[int | float, ...]:
    """Параметры ошибки от ребёнка: только конечные числа, не больше 8 штук."""
    if not isinstance(raw, (tuple, list)):
        return ()
    result: list[int | float] = []
    for value in list(raw)[:8]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        result.append(value)
    return tuple(result)


# --- Объект задания Windows (убить детей вместе с родителем) ------------------

# Сообщение порта завершения объекта задания: процесс упёрся в лимит памяти.
_JOB_OBJECT_MSG_PROCESS_MEMORY_LIMIT = 9


class _Job:
    """Объект задания + порт завершения, по которому узнаём о нехватке памяти.

    Когда процесс задания пытается выделить память сверх `ProcessMemoryLimit`,
    выделение не удаётся, а система кладёт в порт сообщение с PID процесса.
    Python в ребёнке получит MemoryError, а нативный код (resvg на Rust)
    обычно аварийно завершает процесс — тогда только по этому сообщению
    и можно отличить «не хватило памяти» от «файл повреждён».
    """

    def __init__(self, handle: int, port: int | None) -> None:
        self.handle = handle
        self.port = port
        self._lock = threading.Lock()
        self._oom_pids: set[int] = set()

    def drain(self) -> None:
        """Забирает все сообщения порта (иначе очередь растёт с каждым ребёнком)."""
        if self.port is None:
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetQueuedCompletionStatus.restype = wintypes.BOOL
        kernel32.GetQueuedCompletionStatus.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
        ]
        with self._lock:
            while True:
                msg = wintypes.DWORD()
                key = ctypes.c_size_t()
                overlapped = ctypes.c_void_p()
                if not kernel32.GetQueuedCompletionStatus(
                    self.port, ctypes.byref(msg), ctypes.byref(key), ctypes.byref(overlapped), 0
                ):
                    return
                if msg.value == _JOB_OBJECT_MSG_PROCESS_MEMORY_LIMIT and overlapped.value:
                    self._oom_pids.add(int(overlapped.value))

    def hit_memory_limit(self, pid: int | None) -> bool:
        """Упирался ли процесс `pid` в лимит памяти (сведения забираются один раз)."""
        if pid is None:
            return False
        try:
            self.drain()
        except Exception:  # noqa: BLE001
            log.warning("Не удалось прочитать порт объекта задания", exc_info=True)
        with self._lock:
            if pid in self._oom_pids:
                self._oom_pids.discard(pid)
                return True
            return False

    def forget(self, pid: int | None) -> None:
        with self._lock:
            self._oom_pids.discard(pid)


_job_lock = threading.Lock()
# Объекты задания по значению лимита памяти (обычно один; другой лимит — в тестах).
_jobs: dict[int, _Job] = {}
_jobs_failed: set[int] = set()


def _windows_job(limit: int | None = None) -> _Job | None:
    """Объект задания с KILL_ON_JOB_CLOSE и лимитом памяти процесса `limit`
    (по умолчанию `PROCESS_MEMORY_LIMIT`; создаётся один раз на значение
    лимита); None — не вышло.
    """
    if os.name != "nt":
        return None
    limit = int(PROCESS_MEMORY_LIMIT if limit is None else limit)
    with _job_lock:
        if limit in _jobs or limit in _jobs_failed:
            return _jobs.get(limit)
        try:
            import ctypes
            from ctypes import wintypes

            class _Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _Io(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
                )]

            class _Extended(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _Basic),
                    ("IoInfo", _Io),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ]
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
            info = _Extended()
            # KILL_ON_JOB_CLOSE (0x2000) | DIE_ON_UNHANDLED_EXCEPTION (0x400)
            # | PROCESS_MEMORY (0x100): лимит выделенной (commit) памяти процесса.
            info.BasicLimitInformation.LimitFlags = 0x2000 | 0x400 | 0x100
            info.ProcessMemoryLimit = limit
            if not kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                error = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                raise OSError(error, "SetInformationJobObject")
            _jobs[limit] = _Job(handle, _job_completion_port(kernel32, handle))
        except Exception:  # noqa: BLE001 — без задания работаем дальше
            log.warning("Не удалось создать объект задания Windows", exc_info=True)
            _jobs_failed.add(limit)
    return _jobs.get(limit)


def _job_completion_port(kernel32, job: int) -> int | None:
    """Порт завершения для сообщений объекта задания; None — не вышло (не критично)."""
    import ctypes
    from ctypes import wintypes

    class _Port(ctypes.Structure):
        _fields_ = [("CompletionKey", ctypes.c_void_p), ("CompletionPort", wintypes.HANDLE)]

    try:
        kernel32.CreateIoCompletionPort.restype = wintypes.HANDLE
        kernel32.CreateIoCompletionPort.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE, ctypes.c_size_t, wintypes.DWORD,
        ]
        port = kernel32.CreateIoCompletionPort(wintypes.HANDLE(-1), None, 0, 1)
        if not port:
            raise OSError(ctypes.get_last_error(), "CreateIoCompletionPort")
        assoc = _Port(None, port)
        # 7 = JobObjectAssociateCompletionPortInformation
        if not kernel32.SetInformationJobObject(job, 7, ctypes.byref(assoc), ctypes.sizeof(assoc)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(port)
            raise OSError(error, "SetInformationJobObject(port)")
        return port
    except Exception:  # noqa: BLE001
        log.warning("Не удалось привязать порт к объекту задания", exc_info=True)
        return None


def _assign_to_job(
    process: multiprocessing.process.BaseProcess, limit: int | None = None
) -> _Job | None:
    """Помещает процесс в объект задания (лимит памяти `limit`, по умолчанию
    `PROCESS_MEMORY_LIMIT`); возвращает задание (или None)."""
    job = _windows_job(limit)
    if job is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        # На Windows `sentinel` — дескриптор процесса из CreateProcess.
        if not kernel32.AssignProcessToJobObject(job.handle, process.sentinel):
            log.warning("AssignProcessToJobObject: ошибка %s", ctypes.get_last_error())
            return None
        return job
    except Exception:  # noqa: BLE001
        log.warning("Не удалось поместить процесс в объект задания", exc_info=True)
        return None


# --- Ограничение числа одновременно работающих детей ----------------------------


def _total_ram() -> int | None:
    """Объём физической памяти, байт (`GlobalMemoryStatusEx`); None — не узнали."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
        kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullTotalPhys)
    except Exception:  # noqa: BLE001
        log.warning("Не удалось узнать объём памяти", exc_info=True)
        return None


_auto_children: int | None = None


def _max_children() -> int:
    """Действующий предел одновременных детей (см. `MAX_CHILDREN`)."""
    global _auto_children
    if MAX_CHILDREN is not None:
        return max(1, int(MAX_CHILDREN))
    if _auto_children is None:
        ram = _total_ram()
        _auto_children = 2 if ram is not None and ram >= _TWO_CHILDREN_MIN_RAM else 1
        log.info("Одновременно дочерних процессов: %d", _auto_children)
    return _auto_children


# Счётчики занятых мест (а не threading.Semaphore): предел читается при каждом
# ожидании, поэтому его можно поменять (в тестах) без пересоздания объекта.
# "short" — SVG/RAW (`run_isolated`, предел `_max_children()`),
# "long" — долгие задачи `run_task` (предел `LONG_MAX_CHILDREN`),
# "pdf" — задачи PDF (предел `PDF_MAX_CHILDREN`),
# "docs" — книги, документы, таблицы (предел `DOCS_MAX_CHILDREN`),
# "preview" — кадры и предпросмотр окна GIF (предел `PREVIEW_MAX_CHILDREN`).
_slots_cond = threading.Condition()
_slots_used: dict[str, int] = {"short": 0, "long": 0, "pdf": 0, "docs": 0, "preview": 0}
POOLS = frozenset(_slots_used)


def _slot_limit(pool: str) -> int:
    if pool == "long":
        return max(1, int(LONG_MAX_CHILDREN))
    if pool == "pdf":
        return max(1, int(PDF_MAX_CHILDREN))
    if pool == "docs":
        return max(1, int(DOCS_MAX_CHILDREN))
    if pool == "preview":
        return max(1, int(PREVIEW_MAX_CHILDREN))
    return _max_children()


def _acquire_slot(cancel: CancelToken, pool: str = "short") -> None:
    """Ждёт свободного места для дочернего процесса; при отмене — `Cancelled`.

    Порядок без гарантии FIFO (как у обычного семафора). Взаимоблокировки с
    бюджетом пикселей нет: место берётся ДО резервации бюджета (в `on_header`),
    а задача, уже держащая резервацию, место не ждёт (каждый источник
    закрывает свой ExitStack до следующего).
    """
    with _slots_cond:
        while _slots_used[pool] >= _slot_limit(pool):
            if cancel.cancelled:
                raise Cancelled()
            _slots_cond.wait(POLL_INTERVAL)
        if cancel.cancelled:
            raise Cancelled()
        _slots_used[pool] += 1


def _release_slot(pool: str = "short") -> None:
    with _slots_cond:
        _slots_used[pool] = max(0, _slots_used[pool] - 1)
        _slots_cond.notify_all()


# --- Родительская сторона -----------------------------------------------------


def run_isolated(
    kind: str,
    payload: dict[str, Any],
    *,
    cancel: CancelToken,
    timeout: float,
    on_header: Callable[..., None] | None = None,
    check_pixels: Callable[[int, int], None] | None = None,
) -> Image.Image | Any:
    """Выполняет задачу `kind` в дочернем процессе и возвращает изображение.

    `on_header(*размеры)` вызывается после шага 1 (проверка размеров,
    резервирование бюджета; может ждать — это время в таймаут не входит).
    `check_pixels(w, h)` — проверка итоговых размеров перед приёмом пикселей.
    Исключения: `Cancelled`, `ConversionError` (таймаут и ошибки
    колбэков), `ChildFailed` (ошибка в ребёнке).

    Одновременно работают не больше `_max_children()` дочерних процессов:
    сначала ждём свободного места (с проверкой отмены; это время в таймаут
    не входит — отсчёт начинается после), место освобождается только после
    того, как процесс завершён или убит (на всех путях, включая отмену).
    """
    cancel.raise_if_cancelled()
    _acquire_slot(cancel)
    try:
        return _run_child(
            kind, payload, cancel=cancel, timeout=timeout,
            on_header=on_header, check_pixels=check_pixels,
        )
    finally:
        _release_slot()


def _run_child(
    kind: str,
    payload: dict[str, Any],
    *,
    cancel: CancelToken,
    timeout: float,
    on_header: Callable[..., None] | None,
    check_pixels: Callable[[int, int], None] | None,
) -> Image.Image | Any:
    """Тело `run_isolated` (место для процесса уже взято)."""
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=True)
    process = ctx.Process(
        target=_child_main,
        args=(child_conn, kind, payload),
        name=f"konverter-{kind}",
        daemon=True,
    )
    shm: SharedMemory | None = None
    finished = False  # ребёнок всё сделал — даём ему выйти самому
    deadline = time.monotonic() + timeout
    job: _Job | None = None
    pid: int | None = None

    def failed(code: str, detail: str) -> ChildFailed:
        # Упёрся в лимит памяти задания → «нехватка памяти», что бы ни сообщил
        # ребёнок (нативный код при нехватке памяти обычно просто падает).
        if job is not None and code != "memory" and job.hit_memory_limit(pid):
            log.warning("Дочерний процесс %s превысил лимит памяти (%s)", kind, detail)
            return ChildFailed("memory", "process memory limit")
        return ChildFailed(code, detail)

    def receive() -> tuple:
        while True:
            if cancel.cancelled:
                raise Cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("Дочерний процесс %s: превышено время %.0f с", kind, timeout)
                raise ConversionError(MSG_TIMEOUT)
            try:
                if parent_conn.poll(min(POLL_INTERVAL, remaining)):
                    message = parent_conn.recv()
                    break
            except (EOFError, OSError) as exc:
                raise failed("crashed", type(exc).__name__) from exc
            if not process.is_alive():
                # Последнее сообщение могло прийти перед самым выходом.
                try:
                    if parent_conn.poll(0):
                        message = parent_conn.recv()
                        break
                except (EOFError, OSError):
                    pass
                raise failed("crashed", f"exitcode={process.exitcode}")
        if not isinstance(message, tuple) or not message:
            raise ChildFailed("protocol")
        if message[0] == "error":
            raise failed(str(message[1]), str(message[2]) if len(message) > 2 else "")
        return message

    try:
        process.start()
        pid = process.pid
        child_conn.close()
        job = _assign_to_job(process)

        message = receive()
        if message[0] == "probe":
            finished = True
            return message[1]
        if message[0] != "size":
            raise ChildFailed("protocol", str(message[0]))
        if on_header is not None:
            waited_from = time.monotonic()
            on_header(*message[1:])
            deadline += time.monotonic() - waited_from
        cancel.raise_if_cancelled()
        parent_conn.send(("go",))

        message = receive()
        if message[0] != "pixels" or len(message) != 5:
            raise ChildFailed("protocol", str(message[0]))
        _, mode, width, height, nbytes = message
        if mode not in _ALLOWED_MODES or not (
            isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0
        ):
            raise ChildFailed("protocol", "bad pixels header")
        if nbytes != width * height * _ALLOWED_MODES[mode]:
            raise ChildFailed("protocol", "bad size")
        if check_pixels is not None:
            check_pixels(width, height)
        shm = SharedMemory(create=True, size=nbytes)
        parent_conn.send(("shm", shm.name))

        message = receive()
        if message[0] != "done":
            raise ChildFailed("protocol", str(message[0]))
        finished = True
        view = shm.buf[:nbytes]
        try:
            return Image.frombytes(mode, (width, height), view)
        finally:
            view.release()
    finally:
        _stop_process(process, graceful=finished)
        child_conn.close()  # повторное закрытие безопасно
        parent_conn.close()
        if job is not None:
            try:
                job.drain()  # сообщения о выходе процесса и т.п. — не копим
                job.forget(pid)
            except Exception:  # noqa: BLE001
                log.warning("Не удалось прочитать порт объекта задания", exc_info=True)
        if shm is not None:
            try:
                shm.close()
                shm.unlink()  # на Windows ничего не делает, на POSIX удаляет имя
            except Exception:  # noqa: BLE001
                log.warning("Не удалось освободить общую память", exc_info=True)


def run_task(
    task: str,
    args: dict[str, Any],
    *,
    cancel: CancelToken,
    progress: Callable[[float], None] | None = None,
    stall_timeout: float,
    max_time: float,
    memory_limit: int | None = None,
    pool: str = "long",
    hold: Callable[[], ContextManager[Any]] | None = None,
) -> Any:
    """Выполняет задачу `task` (из `TASKS`) в дочернем процессе и возвращает её результат.

    `progress(доля)` вызывается в потоке вызывающего, доля монотонна и в
    [0, 1]; сообщения «доля неизвестна» продлевают ожидание, но колбэк не
    вызывают. Зависание — `stall_timeout` с без продвижения работы →
    `ConversionError(MSG_STALLED)`; общий предел `max_time` →
    `ConversionError(MSG_TIMEOUT)`; отмена → `Cancelled`. В этих случаях и
    при любой ошибке процесс убивается и дожидается. Ошибки ребёнка —
    `TaskFailed` (понятная пользователю, ключ + числа) или `ChildFailed`
    ("damaged", "memory", "crashed", "protocol").

    Место берётся из счётчика `pool` ("long" — `LONG_MAX_CHILDREN`, "pdf" —
    `PDF_MAX_CHILDREN`, "docs" — `DOCS_MAX_CHILDREN`); время ожидания места в таймауты не входит.
    `hold()` — контекстный менеджер, который держится, пока работает ребёнок
    (берётся после получения места; ожидание внутри него тоже вне таймаутов).
    """
    if pool not in POOLS or pool == "short":
        raise ValueError(f"неизвестный счётчик мест: {pool}")
    cancel.raise_if_cancelled()
    _acquire_slot(cancel, pool)
    try:
        with ExitStack() as stack:
            if hold is not None:
                stack.enter_context(hold())
            cancel.raise_if_cancelled()
            return _run_task_child(
                task, args, cancel=cancel, progress=progress, stall_timeout=stall_timeout,
                max_time=max_time, memory_limit=memory_limit,
            )
    finally:
        _release_slot(pool)


def _run_task_child(
    task: str,
    args: dict[str, Any],
    *,
    cancel: CancelToken,
    progress: Callable[[float], None] | None,
    stall_timeout: float,
    max_time: float,
    memory_limit: int | None,
) -> Any:
    """Тело `run_task` (место для процесса уже взято)."""
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=_child_main,
        args=(child_conn, "task", {"task": task, "args": args}),
        name=f"konverter-{task}",
        daemon=True,
    )
    finished = False
    job: _Job | None = None
    pid: int | None = None
    best = 0.0

    def failed(code: str, detail: str) -> ChildFailed:
        if job is not None and code != "memory" and job.hit_memory_limit(pid):
            log.warning("Дочерний процесс %s превысил лимит памяти (%s)", task, detail)
            return ChildFailed("memory", "process memory limit")
        return ChildFailed(code, detail)

    try:
        process.start()
        pid = process.pid
        child_conn.close()
        job = _assign_to_job(process, memory_limit)
        started = last_advance = time.monotonic()
        while True:
            if cancel.cancelled:
                raise Cancelled()
            now = time.monotonic()
            if now - started > max_time:
                log.warning("Задача %s: превышено время %.0f с", task, max_time)
                raise ConversionError(MSG_TIMEOUT)
            if now - last_advance > stall_timeout:
                log.warning("Задача %s: нет продвижения %.0f с", task, stall_timeout)
                raise ConversionError(MSG_STALLED)
            message = None
            try:
                if parent_conn.poll(POLL_INTERVAL):
                    message = parent_conn.recv()
            except (EOFError, OSError) as exc:
                raise failed("crashed", type(exc).__name__) from exc
            if message is None:
                if not process.is_alive():
                    try:
                        if parent_conn.poll(0):
                            message = parent_conn.recv()
                    except (EOFError, OSError):
                        pass
                    if message is None:
                        raise failed("crashed", f"exitcode={process.exitcode}")
                else:
                    continue
            if not isinstance(message, tuple) or not message:
                raise ChildFailed("protocol")
            kind = message[0]
            if kind == "progress" and len(message) == 2:
                last_advance = time.monotonic()
                value = message[1]
                if (
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value)
                ):
                    value = min(1.0, max(0.0, float(value)))
                    if value > best:
                        best = value
                        if progress is not None:
                            progress(best)
                continue
            if kind == "result" and len(message) == 2:
                finished = True
                return message[1]
            if kind == "error" and len(message) >= 2:
                code = str(message[1])
                detail = str(message[2]) if len(message) > 2 else ""
                if code == "user":
                    raise TaskFailed(detail, _clean_params(message[3] if len(message) > 3 else ()))
                raise failed(code, detail)
            raise ChildFailed("protocol", str(kind)[:40])
    finally:
        _stop_process(process, graceful=finished)
        child_conn.close()
        parent_conn.close()
        if job is not None:
            try:
                job.drain()
                job.forget(pid)
            except Exception:  # noqa: BLE001
                log.warning("Не удалось прочитать порт объекта задания", exc_info=True)


def _stop_process(process: multiprocessing.process.BaseProcess, graceful: bool) -> None:
    """Дожидается или убивает дочерний процесс; зомби не оставляет.

    `graceful` — ребёнок закончил работу: даём ему до секунды на выход.
    Иначе (отмена, таймаут, ошибка) — убиваем сразу.
    """
    try:
        if process.pid is None:  # не успел запуститься
            return
        process.join(1.0 if graceful else 0)
        if process.is_alive():
            process.kill()
            process.join(JOIN_TIMEOUT)
        if process.is_alive():
            log.error("Дочерний процесс %s не завершился после kill", process.pid)
            return
        process.close()
    except Exception:  # noqa: BLE001
        log.warning("Ошибка при остановке дочернего процесса", exc_info=True)


# --- Дочерняя сторона ---------------------------------------------------------


def _child_setup() -> None:
    """Без окон об ошибках; выход, если родитель исчез."""
    if os.name == "nt":
        try:
            import ctypes

            # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        except Exception:  # noqa: BLE001
            pass

    parent = multiprocessing.parent_process()
    if parent is not None:
        def watchdog() -> None:
            parent.join()
            os._exit(3)

        threading.Thread(target=watchdog, name="parent-watchdog", daemon=True).start()


def _probe_info() -> dict[str, Any]:
    """Что загружено в ребёнке (для тестов и самопроверки сборки).

    `pyside` — загружен ли PySide6 вообще; `qt_modules` — какие модули Qt;
    `qt_app` — создан ли объект приложения Qt (окно без него невозможно).
    В собранной программе runtime-хук PyInstaller для PySide6 сам загружает
    `PySide6.QtCore` в КАЖДОМ процессе (регистрирует qt.conf), поэтому там
    `pyside` истинно, но QtGui/QtWidgets и приложения Qt в ребёнке нет.
    """
    qt_modules = sorted(m for m in sys.modules if m.startswith("PySide6"))
    qt_app = False
    core = sys.modules.get("PySide6.QtCore")
    if core is not None:
        try:
            qt_app = core.QCoreApplication.instance() is not None
        except Exception:  # noqa: BLE001
            qt_app = True  # не смогли проверить — считаем, что есть
    return {"pyside": bool(qt_modules), "qt_modules": qt_modules, "qt_app": qt_app}


def _child_main(conn, kind: str, payload: dict[str, Any]) -> None:
    """Точка входа дочернего процесса."""
    try:
        _child_setup()
        if kind == "svg":
            _child_svg(conn, payload)
        elif kind == "raw":
            _child_raw(conn, payload)
        elif kind == "task":
            _child_task(conn, payload)
        elif kind == "probe":
            conn.send(("probe", _probe_info()))
        elif kind == "sleep":  # только для тестов: «зависший» декодер
            conn.send(("size", 1, 1))
            if _wait_go(conn):
                time.sleep(float(payload.get("seconds", 3600)))
        elif kind == "alloc":  # только для тестов: «прожорливый» декодер
            conn.send(("size", 1, 1))
            if _wait_go(conn):
                _child_alloc(int(payload["bytes"]), str(payload.get("how", "python")))
        else:
            conn.send(("error", "protocol", "unknown kind"))
    except TaskError as exc:
        try:
            conn.send(("error", "user", str(exc.key), tuple(exc.params)))
        except Exception:  # noqa: BLE001
            pass
    except MemoryError:
        _send_error(conn, "memory", "MemoryError")
    except Exception as exc:  # noqa: BLE001 — враждебный файл: любая ошибка = «повреждён»
        _send_error(conn, "damaged", type(exc).__name__)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _child_alloc(nbytes: int, how: str) -> None:
    """Для тестов: выделить `nbytes` памяти.

    "python" — обычный объект Python (при нехватке — MemoryError);
    "native" — как нативный код: VirtualAlloc, при неудаче процесс
    завершается без сообщения родителю (как Rust при нехватке памяти).
    """
    if how == "native" and os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.VirtualAlloc.restype = ctypes.c_void_p
        kernel32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
        # MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
        if not kernel32.VirtualAlloc(None, nbytes, 0x1000 | 0x2000, 0x04):
            os._exit(0xC0000409 & 0x7FFFFFFF)
        return
    block = bytearray(nbytes)
    del block


def _send_error(conn, code: str, detail: str) -> None:
    try:
        conn.send(("error", code, detail))
    except Exception:  # noqa: BLE001
        pass


def _wait_go(conn) -> bool:
    message = conn.recv()
    return isinstance(message, tuple) and message[:1] == ("go",)


def _send_pixels(conn, mode: str, width: int, height: int, data) -> None:
    """Шаги 3–5: объявить пиксели, получить имя общей памяти, скопировать."""
    view = memoryview(data).cast("B")
    nbytes = view.nbytes
    conn.send(("pixels", mode, width, height, nbytes))
    message = conn.recv()
    if not (isinstance(message, tuple) and message[:1] == ("shm",)):
        return
    shm = SharedMemory(name=message[1])
    try:
        shm.buf[:nbytes] = view
    finally:
        view.release()
        shm.close()
    conn.send(("done",))


def _child_svg(conn, payload: dict[str, Any]) -> None:
    import io

    import resvg_py

    width, height = int(payload["width"]), int(payload["height"])
    conn.send(("size", width, height))
    if not _wait_go(conn):
        return
    png = resvg_py.svg_to_bytes(svg_string=payload["svg"], width=width, height=height)
    with Image.open(io.BytesIO(bytes(png)), formats=["PNG"]) as rendered:
        rendered.load()
        rgba = rendered.convert("RGBA")
    del png
    _send_pixels(conn, "RGBA", rgba.width, rgba.height, rgba.tobytes())


def _child_raw(conn, payload: dict[str, Any]) -> None:
    import numpy as np
    import rawpy

    # Файл передаётся объектом (а не путём), чтобы LibRaw не зависел от
    # юникодных путей Windows: rawpy читает его в память целиком.
    with open(payload["path"], "rb") as fh, rawpy.imread(fh) as raw:
        sizes = raw.sizes
        conn.send(("size", int(sizes.width), int(sizes.height),
                   int(sizes.raw_width), int(sizes.raw_height)))
        if not _wait_go(conn):
            return
        rgb = raw.postprocess(
            use_camera_wb=True, output_bps=8, half_size=bool(payload.get("half_size"))
        )
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("unexpected RAW output shape")
    height, width = int(rgb.shape[0]), int(rgb.shape[1])
    _send_pixels(conn, "RGB", width, height, rgb)


# --- Общий режим задач: дочерняя сторона ----------------------------------------


class _Reporter:
    """Колбэк прогресса задачи в ребёнке: `report(доля | None, *отметки)`.

    Отметки — неубывающие числа, показывающие продвижение работы (время
    в файле в мкс, позиция в байтах …). Сообщение уходит родителю, только
    если хотя бы одна отметка выросла, и не чаще `TASK_REPORT_INTERVAL`.
    `report.final()` — отправить последнее значение без прореживания.
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        self._marks: list[float] = []
        self._last_sent = 0.0
        self._pending: tuple | None = None

    def __call__(self, fraction: float | None, *marks: float) -> None:
        advanced = False
        for index, mark in enumerate(marks):
            if index >= len(self._marks):
                self._marks.append(mark)
                advanced = True
            elif mark > self._marks[index]:
                self._marks[index] = mark
                advanced = True
        if not marks:
            advanced = True
        if not advanced:
            return
        self._pending = ("progress", None if fraction is None else float(fraction))
        now = time.monotonic()
        if now - self._last_sent >= TASK_REPORT_INTERVAL:
            self._flush(now)

    def _flush(self, now: float) -> None:
        if self._pending is not None:
            self._conn.send(self._pending)
            self._pending = None
            self._last_sent = now

    def final(self) -> None:
        self._flush(time.monotonic())


def _resolve_task(name: str) -> Callable[[dict[str, Any], _Reporter], Any]:
    if name in _TEST_TASKS:
        return _TEST_TASKS[name]
    target = TASKS.get(name)
    if target is None:
        raise ValueError("unknown task")
    module_name, _, func_name = target.partition(":")
    return getattr(importlib.import_module(module_name), func_name)


def _child_task(conn, payload: dict[str, Any]) -> None:
    func = _resolve_task(str(payload.get("task")))
    reporter = _Reporter(conn)
    result = func(dict(payload.get("args") or {}), reporter)
    reporter.final()
    conn.send(("result", result))


# Тестовые задачи (только для тестов; аргументы задаёт сам тест).


def _task_test_progress(args: dict[str, Any], report: _Reporter) -> Any:
    """Ровный прогресс: `steps` шагов по `delay` с."""
    steps = int(args.get("steps", 10))
    for index in range(1, steps + 1):
        time.sleep(float(args.get("delay", 0.01)))
        report(index / steps, index)
    return {"steps": steps}


def _task_test_abort(args: dict[str, Any], report: _Reporter) -> Any:
    """Аварийное завершение процесса (как segfault в нативном коде)."""
    report(0.1, 1)
    report.final()
    os.abort()


def _task_test_hang(args: dict[str, Any], report: _Reporter) -> Any:
    """Зависание без прогресса."""
    report(0.1, 1)
    report.final()
    time.sleep(float(args.get("seconds", 3600)))
    return None


def _task_test_spin(args: dict[str, Any], report: _Reporter) -> Any:
    """Работа «вхолостую»: зовёт report с теми же отметками — это не продвижение."""
    deadline = time.monotonic() + float(args.get("seconds", 3600))
    while time.monotonic() < deadline:
        report(0.2, 5)
        time.sleep(0.001)
    return None


def _task_test_error(args: dict[str, Any], report: _Reporter) -> Any:
    raise TaskError(str(args.get("key", "test")), 1, 2.5)


_TEST_TASKS: dict[str, Callable[[dict[str, Any], _Reporter], Any]] = {
    "test_progress": _task_test_progress,
    "test_abort": _task_test_abort,
    "test_hang": _task_test_hang,
    "test_spin": _task_test_spin,
    "test_error": _task_test_error,
}
