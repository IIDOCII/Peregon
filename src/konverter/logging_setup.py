"""Настройка журнала работы программы (НФ «Логи», Ф18).

- файл `data/logs/konverter.log` с ротацией (`LOG_MAX_BYTES` × `LOG_BACKUPS`),
  уровень INFO, формат: время, уровень, поток, модуль, сообщение;
- если папка недоступна для записи — журнал идёт в stderr (если он есть,
  у pythonw/собранного exe его нет) или никуда (NullHandler); программа
  при этом не падает;
- в журнал пишутся только служебные сведения (пути, статусы, тексты
  ошибок), но НЕ содержимое файлов пользователя — модули программы так
  и пишут, здесь это правило зафиксировано.

Две копии программы (ревью 2, п.5): общий файл с ротацией ломается, когда
одна копия переименовывает его, пока другая пишет. Поэтому при старте
берётся эксклюзивная блокировка `data/logs/konverter.lock` (блокировка байта,
система снимает её при завершении процесса, даже аварийном):
- кто взял — пишет в `konverter.log` с ротацией;
- остальные — в `konverter-<pid>.log` без ротации, но не больше
  `LOG_MAX_BYTES` (дальше записи отбрасываются);
- при старте удаляются такие журналы старше `EXTRA_LOG_MAX_AGE_DAYS` дней —
  только по точному шаблону имени `konverter-<цифры>.log`.

Повторный вызов `setup_logging` заменяет ранее поставленный им обработчик.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from konverter import paths

LOG_FILE_NAME = "konverter.log"
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3
LOG_FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"

LOCK_FILE_NAME = "konverter.lock"
# Журналы второй и следующих копий: имя и срок хранения.
EXTRA_LOG_RE = re.compile(r"^konverter-\d{1,10}\.log$")
EXTRA_LOG_MAX_AGE_DAYS = 7

# Метка «обработчик поставлен нами» — чтобы при повторном вызове заменить его.
_OUR_HANDLER_ATTR = "_konverter_handler"


class _CappedFileHandler(logging.FileHandler):
    """Файл без ротации; после `max_bytes` записи отбрасываются (одна пометка)."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        super().__init__(path, mode="a", encoding="utf-8")
        self.max_bytes = max_bytes
        self._full = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._full:
            return
        try:
            stream = self.stream if self.stream is not None else self._open()
            self.stream = stream
            if stream.tell() >= self.max_bytes:
                self._full = True
                stream.write("… журнал достиг предельного размера, дальше не пишется\n")
                self.flush()
                return
        except (OSError, ValueError):
            pass
        super().emit(record)


# Блокировка «я — первая копия»: (папка, дескриптор) на время жизни процесса.
_lock: tuple[Path, int] | None = None


def _take_primary_lock(folder: Path) -> bool:
    """Пытается стать «первой копией» (держать `konverter.lock`); True — удалось."""
    global _lock
    from konverter.core.temp_journal import _try_lock

    if _lock is not None:
        if _lock[0] == folder:
            return True
        _release_lock()
    try:
        fd = os.open(folder / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    except OSError:
        return False
    if _try_lock(fd):
        _lock = (folder, fd)
        return True
    os.close(fd)
    return False


def _release_lock() -> None:
    global _lock
    from konverter.core.temp_journal import _unlock

    if _lock is None:
        return
    _, fd = _lock
    _lock = None
    _unlock(fd)
    try:
        os.close(fd)
    except OSError:
        pass


def _remove_old_extra_logs(folder: Path) -> None:
    """Удаляет журналы `konverter-<pid>.log` старше срока (только точный шаблон)."""
    limit = time.time() - EXTRA_LOG_MAX_AGE_DAYS * 86400
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        if not EXTRA_LOG_RE.fullmatch(entry.name):
            continue
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < limit:
                os.remove(entry.path)
        except OSError:
            pass  # занят живой копией или нет прав — не страшно


def _open_handler(folder: Path) -> tuple[logging.Handler, Path]:
    if _take_primary_lock(folder):
        path = folder / LOG_FILE_NAME
        handler: logging.Handler = RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
    else:
        path = folder / f"konverter-{os.getpid()}.log"
        handler = _CappedFileHandler(path, LOG_MAX_BYTES)
    _remove_old_extra_logs(folder)
    return handler, path


def shutdown_logging() -> None:
    """Снимает наш обработчик и блокировку «первой копии» (для тестов и выхода)."""
    root = logging.getLogger()
    for old in list(root.handlers):
        if getattr(old, _OUR_HANDLER_ATTR, False):
            root.removeHandler(old)
            old.close()
    _release_lock()


def setup_logging(level: int = logging.INFO) -> Path | None:
    """Настраивает корневой логгер; возвращает путь к файлу журнала или None."""
    handler: logging.Handler
    log_path: Path | None = None
    try:
        folder = paths.logs_dir()
        if folder is None:
            raise OSError("папка логов недоступна")
        handler, log_path = _open_handler(folder)
    except OSError:
        log_path = None
        handler = logging.StreamHandler(sys.stderr) if sys.stderr else logging.NullHandler()

    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    setattr(handler, _OUR_HANDLER_ATTR, True)
    root = logging.getLogger()
    for old in list(root.handlers):
        if getattr(old, _OUR_HANDLER_ATTR, False):
            root.removeHandler(old)
            old.close()
    root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(True)
    if log_path is None:
        logging.getLogger(__name__).warning("Журнал в файл не ведётся: папка недоступна")
    return log_path
