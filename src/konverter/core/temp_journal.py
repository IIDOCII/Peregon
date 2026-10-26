"""Журнал временных файлов: уборка «осиротевших» `.part` после аварийного выхода.

Ядро пишет результат во временный файл/папку рядом с исходником
(`naming.make_temp_*`). Если программа упадёт или будет убита, такой объект
останется. Поэтому каждый созданный временный объект записывается в журнал
в `data/temp/`, а при переносе в конечное имя или удалении — вычёркивается.
При следующем запуске `cleanup_orphans()` удаляет то, что осталось в
журналах ЗАВЕРШИВШИХСЯ процессов программы.

Устройство:
- у каждого процесса свой журнал `active-<pid>-<случайное>.txt`
  (дописывание строк «+путь» / «-путь», UTF-8). Когда активных записей не
  остаётся, файл усекается до нуля, так что он не растёт;
- пока процесс жив, он держит блокировку байта журнала далеко за его концом
  (`msvcrt.locking` на Windows, `fcntl.lockf` в прочих ОС). Блокировка
  снимается системой при завершении процесса, даже аварийном. Поэтому
  `cleanup_orphans()` трогает только журналы, блокировку которых удалось
  взять, — вторая запущенная копия программы не удалит рабочие файлы первой;
- журнал создаётся новым файлом (O_EXCL) и сразу блокируется; если
  блокировку перехватила уборка другой копии — файл бросается и создаётся
  другой; без блокировки журнал не ведётся;
- журналу не доверяем: удаляются только объекты по АБСОЛЮТНОМУ пути с именем
  точно нашего формата (`naming.is_temp_name`), через `naming.remove_orphan`;
  папки — только пустые или содержащие лишь объекты нашего формата, без
  следования ссылкам;
- если объект удалить не удалось (`remove_orphan` → False: занят, нет прав,
  в папке чужие файлы), журнал НЕ удаляется, а перезаписывается только
  такими записями с временем первой неудачи («~время\\tпуть»); следующий
  запуск пробует снова. Запись старше `ORPHAN_RETRY_DAYS` дней
  отбрасывается (с записью в лог), записи не нашего формата не хранятся;
- если `data/` недоступна для записи, журнал просто не ведётся;
- потокобезопасно (одна блокировка на журнал процесса); ошибки записи
  журнала не мешают конвертации (только запись в лог).
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from konverter import paths

log = logging.getLogger(__name__)

JOURNAL_PREFIX = "active-"
JOURNAL_SUFFIX = ".txt"
# Смещение байта, который блокирует живой процесс (за концом любого журнала).
_LOCK_OFFSET = 1 << 30
# Сколько раз пробовать создать и заблокировать новый журнал.
_OPEN_ATTEMPTS = 3
# Сколько дней повторять удаление объекта, который не удаётся удалить
# (занят, нет прав); потом запись отбрасывается с записью в лог.
ORPHAN_RETRY_DAYS = 30
# Текущее время для срока повторов (в тестах подменяется).
_now = time.time


def _try_lock(fd: int) -> bool:
    """Неблокирующая блокировка байта `_LOCK_OFFSET`; True — взята."""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, _LOCK_OFFSET)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.lockf(fd, fcntl.LOCK_UN, 1, _LOCK_OFFSET)
    except OSError:
        pass


def _replay(text: str) -> list[str]:
    """Активные пути по строкам «+путь» / «-путь» (в порядке добавления)."""
    return list(_replay_with_age(text))


def _replay_with_age(text: str) -> dict[str, float | None]:
    """Активные пути → время первой неудачной попытки уборки (или None).

    Строки: «+путь» — объект создан; «-путь» — вычеркнут;
    «~<секунды с эпохи>\\tпуть» — запись, перенесённая уборкой, которой не
    удалось удалить объект (см. `TempJournal.cleanup_orphans`).
    """
    active: dict[str, float | None] = {}
    for line in text.splitlines():
        if len(line) < 2:
            continue
        sign, value = line[0], line[1:]
        if sign == "+":
            active[value] = None
        elif sign == "-":
            active.pop(value, None)
        elif sign == "~":
            stamp, sep, path = value.partition("\t")
            if not sep or not path:
                continue
            try:
                since = float(stamp)
            except ValueError:
                continue
            active[path] = since if since == since else None  # NaN → неизвестно
    return active


def _key(path: Path | str) -> str:
    return str(path)


class TempJournal:
    """Журнал временных объектов одного процесса."""

    def __init__(self, directory: Callable[[], Path | None] = paths.temp_dir) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._fd: int | None = None
        self._path: Path | None = None
        self._disabled = False
        self._active: set[str] = set()

    @property
    def path(self) -> Path | None:
        """Файл журнала этого процесса (None — ещё не создан или журнал отключён)."""
        return self._path

    # --- запись ---

    def _ensure_open(self) -> bool:
        """Открывает журнал при первой записи; False — журнал не ведётся."""
        if self._fd is not None:
            return True
        if self._disabled:
            return False
        try:
            folder = self._directory()
            if folder is None:
                raise OSError("папка data/temp недоступна")
        except OSError as exc:
            log.info("Журнал временных файлов не ведётся: %s", exc)
            self._disabled = True
            return False
        # Между созданием файла и его блокировкой уборка другой копии
        # программы может успеть взять блокировку сама (файл новый и ничей).
        # Поэтому: создаём НОВЫЙ файл (O_EXCL), блокируем; не вышло — бросаем
        # его и пробуем с другим именем. Журнал без блокировки не ведём: его
        # содержимое могла бы удалить уборка другой копии.
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND | getattr(os, "O_BINARY", 0)
        for _ in range(_OPEN_ATTEMPTS):
            path = folder / f"{JOURNAL_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}{JOURNAL_SUFFIX}"
            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError:
                continue
            except OSError as exc:
                log.info("Журнал временных файлов не ведётся: %s", exc)
                self._disabled = True
                return False
            if _try_lock(fd):
                self._fd, self._path = fd, path
                return True
            log.warning("Не удалось заблокировать журнал %s, пробую другое имя", path)
            os.close(fd)
            try:
                os.remove(path)
            except OSError:
                pass  # пустой файл; его уберёт уборка при следующем запуске
        log.warning("Журнал временных файлов не ведётся: не удалось взять блокировку")
        self._disabled = True
        return False

    def _write(self, line: str) -> None:
        try:
            os.write(self._fd, (line + "\n").encode("utf-8", "surrogatepass"))
        except OSError as exc:
            log.warning("Не удалось записать журнал временных файлов: %s", exc)

    def add(self, path: Path | str) -> None:
        """Отмечает созданный временный объект."""
        key = _key(path)
        with self._lock:
            if not self._ensure_open():
                return
            self._active.add(key)
            self._write("+" + key)

    def remove(self, path: Path | str) -> None:
        """Вычёркивает объект (перенесён в конечное имя или удалён)."""
        key = _key(path)
        with self._lock:
            if self._fd is None or key not in self._active:
                return
            self._active.discard(key)
            if self._active:
                self._write("-" + key)
                return
            try:  # активных не осталось — журнал можно начать с нуля
                os.ftruncate(self._fd, 0)
            except OSError:
                self._write("-" + key)

    def close(self) -> None:
        """Закрывает журнал; пустой — удаляет (вызывается при выходе)."""
        with self._lock:
            if self._fd is None:
                return
            fd, path, empty = self._fd, self._path, not self._active
            self._fd = None
            _unlock(fd)
            try:
                os.close(fd)
            except OSError:
                pass
            if empty and path is not None:
                try:
                    os.remove(path)
                except OSError:
                    pass

    # --- уборка после аварийного выхода ---

    def cleanup_orphans(self) -> int:
        """Удаляет временные объекты из журналов завершившихся процессов.

        Возвращает число удалённых объектов. Журналы живых процессов
        (включая этот) не трогаются.
        """
        from konverter.core import naming  # здесь, чтобы не было цикла импортов

        try:
            folder = self._directory()
        except OSError:
            folder = None
        if folder is None:
            return 0
        try:
            candidates = sorted(folder.glob(f"{JOURNAL_PREFIX}*{JOURNAL_SUFFIX}"))
        except OSError as exc:
            log.warning("Не удалось прочитать папку журналов: %s", exc)
            return 0
        removed = 0
        for journal in candidates:
            if self._path is not None and journal == self._path:
                continue
            try:
                fd = os.open(journal, os.O_RDWR | getattr(os, "O_BINARY", 0))
            except OSError:
                continue
            try:
                if not _try_lock(fd):
                    continue  # процесс-владелец жив
                os.lseek(fd, 0, os.SEEK_SET)
                chunks = []
                while chunk := os.read(fd, 1 << 16):
                    chunks.append(chunk)
                text = b"".join(chunks).decode("utf-8", "surrogatepass")
                keep: list[tuple[str, float]] = []
                now = _now()
                for entry, since in _replay_with_age(text).items():
                    # Журналу не доверяем (его мог подделать кто угодно с
                    # доступом к data/): только абсолютные пути, только точно
                    # наш формат имени, папки — без рекурсии по чужому.
                    if naming.remove_orphan(Path(entry)):
                        removed += 1
                        continue
                    path = Path(entry)
                    if not (path.is_absolute() and naming.is_temp_name(path.name)):
                        continue  # подделка/мусор: повторять бессмысленно
                    # Будущее время (часы перевели назад) не продлевает срок.
                    first = now if since is None else min(since, now)
                    if now - first > ORPHAN_RETRY_DAYS * 86400:
                        log.warning(
                            "Запись журнала отброшена: объект не удаётся удалить "
                            "больше %d дней: %s", ORPHAN_RETRY_DAYS, entry,
                        )
                        continue
                    keep.append((entry, first))
                if keep:
                    # Не удалось удалить (файл занят, нет прав…): журнал
                    # остаётся, но только с этими записями — следующий запуск
                    # попробует снова. Блокировку он не держит (владельца
                    # нет), поэтому его возьмёт уборка любой копии программы.
                    self._rewrite_orphan_journal(fd, journal, keep)
                    _unlock(fd)
                    continue
                if os.name != "nt":
                    # На POSIX удаляем журнал, ещё держа блокировку: иначе
                    # между снятием блокировки и удалением его мог бы успеть
                    # заблокировать новый владелец (см. `_ensure_open`).
                    try:
                        os.remove(journal)
                    except OSError as exc:
                        log.warning("Не удалось удалить журнал %s: %s", journal, exc)
                _unlock(fd)
            finally:
                os.close(fd)
            if os.name == "nt":
                # На Windows открытый файл не удалить; удаление после закрытия
                # безопасно: файл, открытый своим создателем, не удалится.
                try:
                    os.remove(journal)
                except OSError as exc:
                    log.warning("Не удалось удалить журнал %s: %s", journal, exc)
        if removed:
            log.info("Удалено осиротевших временных объектов: %d", removed)
        return removed

    @staticmethod
    def _rewrite_orphan_journal(fd: int, journal: Path, keep: list[tuple[str, float]]) -> None:
        """Перезаписывает журнал (под его блокировкой) только записями `keep`.

        Формат — строки «~время\\tпуть» (время первой неудачи, для срока
        `ORPHAN_RETRY_DAYS`). Пишем на месте, а не через временный файл с
        переименованием: переименование открытого файла на Windows не
        работает, а на POSIX между ним и блокировкой могла бы вклиниться
        уборка другой копии. Сбой записи — только в лог (файл не удаляется).
        """
        data = "".join(f"~{since:.0f}\t{entry}\n" for entry, since in keep)
        payload = data.encode("utf-8", "surrogatepass")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            log.warning(
                "Не удалось удалить осиротевших временных объектов: %d "
                "(повтор при следующем запуске)", len(keep),
            )
        except OSError as exc:
            log.warning("Не удалось перезаписать журнал %s: %s", journal, exc)


# Журнал процесса. Код берёт его через атрибут модуля (`temp_journal.JOURNAL`),
# поэтому в тестах его можно подменить.
JOURNAL = TempJournal()


def cleanup_orphans() -> int:
    """Уборка при старте программы (см. `TempJournal.cleanup_orphans`)."""
    try:
        return JOURNAL.cleanup_orphans()
    except Exception:  # noqa: BLE001 — уборка не должна мешать запуску
        log.exception("Сбой уборки осиротевших временных файлов")
        return 0


@atexit.register
def _close_at_exit() -> None:
    JOURNAL.close()
