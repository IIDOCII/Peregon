"""Имена файлов-результатов и временные файлы рядом с ними (Ф11–Ф13).

Правила:
- результат никогда не перезаписывает существующий файл: при занятом имени
  берётся `имя (1).ext`, `имя (2).ext`…;
- конвертер пишет во временный файл/папку со скрытым именем
  `.~konverter-<uuid>.part[.ext]` в папке результата; по окончании ядро
  переносит его в конечное имя функцией `finalize`;
- перенос атомарный и без перезаписи: на Windows `os.rename`
  (MoveFileExW без флага замены) сам падает с FileExistsError, если имя
  занято, — тогда пробуем следующее имя. Окна гонки «проверили — записали»
  нет. На других ОС для файлов используется `os.link` + удаление временного
  (тоже без перезаписи), для папок — `os.rename` (может заменить ПУСТУЮ
  папку с тем же именем; программа рассчитана на Windows);
- каждый созданный временный объект записывается в журнал
  (`temp_journal`), чтобы после аварийного выхода его убрал следующий запуск;
- длинные пути Windows (> ~260 символов): для системных вызовов к пути
  добавляется префикс `\\\\?\\`, а наружу (для показа) возвращается обычный путь.

Имя результата для варианта «много файлов → один» (например, несколько
картинок → один PDF) — стем ПЕРВОГО файла группы (функция `merged_stem`):
так результат легко найти рядом с исходниками, а конфликт имён решает
`finalize`.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import threading
import uuid
from pathlib import Path

from konverter import paths
from konverter.core import temp_journal
from konverter.strings import tr

log = logging.getLogger(__name__)

# Префикс и метка имён наших временных объектов.
TEMP_PREFIX = ".~konverter-"
TEMP_MARK = ".part"
# Точный формат временного имени: `.~konverter-<uuid4.hex>.part[.ext]`.
# Только такие имена программа считает своими и удаляет.
_TEMP_NAME_RE = re.compile(r"^\.~konverter-[0-9a-f]{32}\.part(\.[A-Za-z0-9]{1,16})?$")
_TEMP_EXT_RE = re.compile(r"[A-Za-z0-9]{1,16}")
# Временные папки, созданные `make_temp_dir` ЭТИМ процессом (ключ — str пути
# в виде `os_path`). Только их `cleanup` удаляет вместе с содержимым.
_own_temp_dirs: set[str] = set()
_own_lock = threading.Lock()
# Глубина вложенности при осторожном удалении «осиротевшей» папки.
_ORPHAN_MAX_DEPTH = 8

# Сколько вариантов «имя (N)» перебирать, прежде чем сдаться.
MAX_NAME_ATTEMPTS = 10000

# Предельная длина одного компонента пути (NTFS).
MAX_COMPONENT_LENGTH = 255

# Длина пути, начиная с которой добавляем префикс длинного пути Windows.
_LONG_PATH_THRESHOLD = 240

# Недопустимые в именах Windows символы и управляющие символы.
_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Зарезервированные имена устройств Windows (без учёта расширения и регистра).
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_DEFAULT_STEM = "result"


# --- Вспомогательное --------------------------------------------------------


def os_path(path: Path) -> Path:
    """Путь для системных вызовов: с префиксом `\\\\?\\` для длинных путей Windows."""
    if os.name != "nt":
        return path
    text = str(path)
    if text.startswith("\\\\?\\") or len(text) < _LONG_PATH_THRESHOLD:
        return path
    text = os.path.abspath(text)
    if text.startswith("\\\\"):  # UNC: \\server\share → \\?\UNC\server\share
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def display_path(path: Path) -> Path:
    """Обратное к `os_path`: убирает префикс длинного пути для показа."""
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + text[8:])
    if text.startswith("\\\\?\\"):
        return Path(text[4:])
    return path


def sanitize_stem(stem: str) -> str:
    """Делает стем допустимым именем файла Windows.

    Недопустимые символы заменяются на «_», завершающие точки и пробелы
    убираются, имена устройств (CON, NUL…) дополняются «_», длина
    ограничивается с запасом под « (9999).расширение».
    """
    cleaned = _INVALID_CHARS_RE.sub("_", stem).rstrip(" .").lstrip(" ")
    if not cleaned:
        cleaned = _DEFAULT_STEM
    if cleaned.split(".", 1)[0].upper() in _RESERVED_NAMES:
        cleaned = cleaned + "_"
    # Запас: " (9999)" + "." + расширение до 16 символов.
    limit = MAX_COMPONENT_LENGTH - 24
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip(" .") or _DEFAULT_STEM
    return cleaned


def source_stem(source: Path) -> str:
    """Стем исходника для имени результата («архив.tar.gz» → «архив»)."""
    name = source.name
    if name.lower().endswith(".tar.gz"):
        return name[: -len(".tar.gz")] or _DEFAULT_STEM
    return source.stem or name or _DEFAULT_STEM


def merged_stem(sources: list[Path]) -> str:
    """Стем результата «много файлов → один»: стем первого файла группы."""
    if not sources:
        return _DEFAULT_STEM
    return source_stem(sources[0])


def _candidate_name(stem: str, extension: str, index: int) -> str:
    base = stem if index == 0 else f"{stem} ({index})"
    return f"{base}.{extension}" if extension else base


def _normalize_extension(extension: str) -> str:
    return extension.lstrip(".")


# --- Публичные функции ------------------------------------------------------


def can_write(directory: Path) -> bool:
    """Можно ли писать в папку (проверка на деле, см. `paths.is_writable`)."""
    return paths.is_writable(os_path(Path(directory)))


def unique_path(directory: Path, stem: str, extension: str) -> Path:
    """Свободное имя `имя.ext`, `имя (1).ext`, `имя (2).ext`… в папке.

    Для папок `extension` — пустая строка. Это только подсказка: между
    проверкой и записью имя может занять кто-то другой, поэтому для
    сохранения результата используйте `finalize` (он не перезаписывает).
    """
    directory = Path(directory)
    stem = sanitize_stem(stem)
    extension = _normalize_extension(extension)
    for index in range(MAX_NAME_ATTEMPTS):
        candidate = directory / _candidate_name(stem, extension, index)
        if not os.path.lexists(os_path(candidate)):
            return candidate
    raise FileExistsError(tr("err_no_free_name"))


def _temp_extension(extension: str) -> str:
    """Расширение временного имени: одна часть из латиницы/цифр, до 16 символов.

    Составное («tar.gz») → последняя часть («gz»), неподходящее → без
    расширения: так имя всегда соответствует строгому `_TEMP_NAME_RE`.
    """
    extension = _normalize_extension(extension)
    if _TEMP_EXT_RE.fullmatch(extension):
        return extension
    last = extension.rsplit(".", 1)[-1]
    return last if _TEMP_EXT_RE.fullmatch(last) else ""


def _temp_name(extension: str) -> str:
    extension = _temp_extension(extension)
    name = f"{TEMP_PREFIX}{uuid.uuid4().hex}{TEMP_MARK}"
    return f"{name}.{extension}" if extension else name


def is_temp_name(name: str) -> bool:
    """Точно ли это имя нашего временного объекта (формат `_temp_name`)."""
    return _TEMP_NAME_RE.fullmatch(name) is not None


def make_temp_file(directory: Path, extension: str) -> Path:
    """Создаёт пустой временный файл в папке (эксклюзивно) и возвращает путь.

    Возвращённый путь пригоден для передачи конвертеру (для длинных путей
    Windows он уже содержит префикс `\\\\?\\`). Ошибки ОС — OSError.
    """
    directory = Path(directory)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    for _ in range(100):
        path = os_path(directory / _temp_name(extension))
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            continue  # совпадение uuid практически невозможно, но не перезаписываем
        os.close(fd)
        temp_journal.JOURNAL.add(path)
        # Атрибут «скрытый» НЕ ставим: на Windows открытие скрытого файла
        # на перезапись (open(..., "wb") → CREATE_ALWAYS) завершается
        # отказом в доступе, а конвертеры пишут именно так. «Скрытость» —
        # только точка в начале имени.
        return path
    raise FileExistsError(tr("err_no_free_name"))


def make_temp_dir(directory: Path) -> Path:
    """Создаёт временную папку в папке (эксклюзивно) и возвращает путь."""
    directory = Path(directory)
    for _ in range(100):
        path = os_path(directory / _temp_name(""))
        try:
            os.mkdir(path)
        except FileExistsError:
            continue
        with _own_lock:
            _own_temp_dirs.add(str(path))
        temp_journal.JOURNAL.add(path)
        return path
    raise FileExistsError(tr("err_no_free_name"))


def _move_no_replace(src: Path, dst: Path, is_dir: bool) -> None:
    """Переносит src в dst; если dst существует — FileExistsError, без перезаписи."""
    if os.name == "nt":
        # MoveFileExW без MOVEFILE_REPLACE_EXISTING: атомарно, занятое имя → ошибка.
        os.rename(src, dst)
        return
    if is_dir:
        if os.path.lexists(dst):
            raise FileExistsError(dst)
        os.rename(src, dst)
        return
    os.link(src, dst)  # падает с FileExistsError, если имя занято
    os.unlink(src)


def finalize(temp: Path, directory: Path, stem: str, extension: str) -> Path:
    """Переносит временный файл/папку в свободное конечное имя и возвращает его.

    Никогда не перезаписывает существующий файл/папку: при занятом имени
    (в том числе если его заняли в момент переноса) пробует следующее.
    Временный объект должен лежать в той же папке (тот же том).
    """
    temp = os_path(Path(temp))
    directory = Path(directory)
    stem = sanitize_stem(stem)
    extension = _normalize_extension(extension)
    is_dir = os.path.isdir(temp) and not os.path.islink(temp)
    for index in range(MAX_NAME_ATTEMPTS):
        candidate = directory / _candidate_name(stem, extension, index)
        target = os_path(candidate)
        try:
            _move_no_replace(temp, target, is_dir)
        except FileExistsError:
            continue
        except PermissionError:
            # На Windows занятое папкой/удаляемым файлом имя иногда даёт
            # «отказано в доступе» — если имя занято, пробуем следующее.
            if os.path.lexists(target):
                continue
            raise
        temp_journal.JOURNAL.remove(temp)
        with _own_lock:
            _own_temp_dirs.discard(str(temp))
        return display_path(candidate)
    raise FileExistsError(tr("err_no_free_name"))


def _is_link(st: os.stat_result) -> bool:
    """Символическая ссылка или любая точка повторной обработки (junction и т.п.)."""
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _remove_link(path: Path, st: os.stat_result) -> None:
    """Удаляет саму ссылку, не трогая то, на что она указывает."""
    if stat.S_ISDIR(st.st_mode) or (
        getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
    ):
        os.rmdir(path)  # junction / ссылка на папку: rmdir удаляет только ссылку
    else:
        os.unlink(path)


def _remove_orphan_dir(path: Path, depth: int = 0) -> bool:
    """Удаляет папку, только если в ней пусто или лишь объекты нашего формата.

    По ссылкам не идёт: ссылка с нашим именем удаляется как ссылка, цель не
    трогается. Встретив хоть одно чужое имя, ничего в этой ветке не удаляет
    (проверка — до удаления). True — удалено.
    """
    if depth > _ORPHAN_MAX_DEPTH:
        return False
    with os.scandir(path) as it:
        entries = list(it)
    if any(not is_temp_name(entry.name) for entry in entries):
        log.warning("Во временной папке есть чужие объекты, не удаляю: %s", path)
        return False
    for entry in entries:
        child = Path(entry.path)
        st = os.lstat(child)
        if _is_link(st):
            _remove_link(child, st)
        elif stat.S_ISDIR(st.st_mode):
            if not _remove_orphan_dir(child, depth + 1):
                return False
        else:
            os.unlink(child)
    os.rmdir(path)
    return True


def _remove(temp: Path, own_dir: bool) -> bool:
    """Удаляет временный объект. True — удалён (или его уже нет)."""
    try:
        st = os.lstat(temp)
    except FileNotFoundError:
        return True
    if _is_link(st):
        _remove_link(temp, st)
    elif stat.S_ISDIR(st.st_mode):
        if own_dir:
            # Папка из make_temp_dir этого процесса (например, распаковка
            # архива): содержимое наше, удаляем целиком. rmtree по ссылкам
            # и junction не ходит.
            shutil.rmtree(temp)
        else:
            return _remove_orphan_dir(temp)
    else:
        os.unlink(temp)
    return True


def cleanup(temp: Path | None, *, forget: bool = True) -> bool:
    """Удаляет НАШ временный файл/папку, не падая. True — удалено или уже нет.

    Удаляется только объект с именем точно нашего формата (`is_temp_name`);
    всё остальное не трогается (возвращается False). Папка удаляется вместе
    с содержимым, только если её создал `make_temp_dir` ЭТОГО процесса;
    иначе — только пустая или содержащая лишь объекты нашего формата
    (см. `remove_orphan`).
    `forget` — после удаления вычеркнуть объект из журнала временных файлов.
    False нужен, когда объект может появиться снова (его ещё может
    пересоздать зависший рабочий поток): запись остаётся, и объект уберёт
    `temp_journal.cleanup_orphans()` при следующем запуске.
    """
    if temp is None:
        return True
    temp = os_path(Path(temp))
    if not is_temp_name(temp.name):
        log.warning("Отказ удалять не наш временный объект: %s", temp)
        return False
    with _own_lock:
        own_dir = str(temp) in _own_temp_dirs
    try:
        if not _remove(temp, own_dir):
            return False
    except OSError as exc:
        log.warning("Не удалось удалить временный объект %s: %s", temp, exc)
        return False
    if own_dir and forget:
        with _own_lock:
            _own_temp_dirs.discard(str(temp))
    if forget:
        temp_journal.JOURNAL.remove(temp)
    return True


def remove_orphan(temp: Path) -> bool:
    """Удаляет объект из журнала ДРУГОГО (завершившегося) процесса.

    Строже `cleanup`: путь обязан быть абсолютным, имя — точно нашего
    формата; папка удаляется, только если пустая или содержит лишь объекты
    нашего формата (рекурсивно, без следования ссылкам). Журнал мог быть
    подделан, поэтому ему не доверяем. True — удалено или уже нет.
    """
    path = Path(temp)
    if not path.is_absolute() or not is_temp_name(path.name):
        log.warning("Запись журнала отклонена (не абсолютный путь или не наше имя): %s", temp)
        return False
    path = os_path(path)
    try:
        return _remove(path, own_dir=False)
    except OSError as exc:
        log.warning("Не удалось удалить временный объект %s: %s", path, exc)
        return False
