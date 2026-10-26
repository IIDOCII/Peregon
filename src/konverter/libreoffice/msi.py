"""Чтение базы msi без установщика Windows и раскладка файлов (Ф16).

Ничего не исполняется и не регистрируется: `msi.dll` открывает базу ТОЛЬКО
для чтения (`MsiOpenDatabaseW(..., MSIDBOPEN_READONLY)` — это чтение файла
базы; служба msiserver не участвует, custom actions не выполняются). Читаются
таблицы Media, File, Component, Directory и встроенные CAB-потоки
(`_Streams`). Распаковка CAB — системным `%SystemRoot%\\System32\\expand.exe`
в объекте задания (отмена = убить), раскладка по путям — своим кодом:

* путь файла = цепочка Directory от `Component.Directory_` вверх до корня;
  из `DefaultDir` берётся целевая часть (до `:`) и длинное имя (после `|`),
  `.` — тот же каталог; цикл или глубина > `MAX_DEPTH` — отказ;
* корень установки — каталог `INSTALL_ROOT` (`INSTALLLOCATION`): файлы под
  ним кладутся относительно него; файлы под `System64Folder`/`SystemFolder`
  (там лежат библиотеки Visual C++) — отдельно, их забирает вызывающий;
  прочие корни (ярлыки, меню «Пуск» и т.п.) пропускаются;
* каждая часть пути проверяется: без `..`, без `:`/`\\`/`/`, без
  управляющих символов, без зарезервированных имён Windows; итоговый путь
  обязан лежать внутри папки назначения (защита от обхода путей и
  абсолютных путей из таблиц, хотя msi и проверен подписью);
* лимиты: файлов ≤ `MAX_FILES`, сумма размеров ≤ `MAX_TOTAL_BYTES`;
* файлы вне CAB (некомпрессированные, внешние CAB) не поддерживаются —
  отказ (в msi LibreOffice их нет).
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterator

MAX_FILES = 40_000
MAX_TOTAL_BYTES = 3 * 2**30
MAX_DEPTH = 64
MAX_STREAM_BYTES = 1 * 2**30
INSTALL_ROOT = "INSTALLLOCATION"
# 64-битные системные библиотеки (Visual C++ x64); ключ каталога в msi
# LibreOffice — «System64Folder.<GUID>». 32-битные (SystemFolder) не нужны.
SYSTEM64_ROOT = "System64Folder"
# Шрифты, которые установщик кладёт в системную папку Fonts, — в папку
# шрифтов самого LibreOffice (её он подключает как частные шрифты процесса).
FONTS_ROOT = "FontsFolder"
FONTS_SUBDIR = ("share", "fonts", "truetype")
# Компоненты с такими словами в условии не нужны (интеграция с системой:
# реестр, ассоциации, ярлыки, автозапуск, проверка обновлений).
_SKIP_CONDITION_WORDS = (
    "ISCHECKFORPRODUCTUPDATES", "CREATEDESKTOPLINK", "QUICKSTART", "WRITE_REGISTRY",
    "REGISTER_", "SELECT_",
)
_ARM64_ONLY = re.compile(r'ARM64CHECK[\w.]*\s*=\s*"ARM64"')

_ERROR_NO_MORE_ITEMS = 259
_ERROR_MORE_DATA = 234
_MSIDBOPEN_READONLY = 0
_FILE_ATTR_NONCOMPRESSED = 0x2000
_KEY_RE = re.compile(r"^[A-Za-z0-9_.]{1,72}$")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
             *(f"LPT{i}" for i in range(10))}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class MsiError(Exception):
    """База msi не прочитана или содержит недопустимые данные."""


class Cancelled(Exception):
    """Распаковка отменена."""


# --- msi.dll ------------------------------------------------------------------


def _msi():
    if os.name != "nt":
        raise MsiError("msi.dll есть только в Windows")
    dll = ctypes.WinDLL("msi", use_last_error=True)
    dll.MsiOpenDatabaseW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE)]
    dll.MsiOpenDatabaseW.restype = wintypes.UINT
    dll.MsiDatabaseOpenViewW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.POINTER(wintypes.HANDLE)]
    dll.MsiDatabaseOpenViewW.restype = wintypes.UINT
    dll.MsiViewExecute.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    dll.MsiViewExecute.restype = wintypes.UINT
    dll.MsiViewFetch.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE)]
    dll.MsiViewFetch.restype = wintypes.UINT
    dll.MsiRecordGetStringW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    dll.MsiRecordGetStringW.restype = wintypes.UINT
    dll.MsiRecordGetInteger.argtypes = [wintypes.HANDLE, wintypes.UINT]
    dll.MsiRecordGetInteger.restype = ctypes.c_int
    dll.MsiRecordIsNull.argtypes = [wintypes.HANDLE, wintypes.UINT]
    dll.MsiRecordIsNull.restype = wintypes.BOOL
    dll.MsiRecordReadStream.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_char_p, ctypes.POINTER(wintypes.DWORD)]
    dll.MsiRecordReadStream.restype = wintypes.UINT
    dll.MsiCloseHandle.argtypes = [wintypes.HANDLE]
    dll.MsiCloseHandle.restype = wintypes.UINT
    return dll


_MSI_NULL_INTEGER = -0x80000000


class MsiDatabase:
    """База msi, открытая только для чтения."""

    def __init__(self, path: Path | str) -> None:
        self._dll = _msi()
        self._handle = wintypes.HANDLE()
        # MSIDBOPEN_READONLY — это (LPCWSTR)0, передаётся как указатель.
        rc = self._dll.MsiOpenDatabaseW(str(path), ctypes.c_void_p(_MSIDBOPEN_READONLY), ctypes.byref(self._handle))
        if rc != 0:
            raise MsiError(f"MsiOpenDatabaseW: {rc}")

    def close(self) -> None:
        if self._handle:
            self._dll.MsiCloseHandle(self._handle)
            self._handle = wintypes.HANDLE()

    def __enter__(self) -> "MsiDatabase":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _rows(self, query: str) -> Iterator[wintypes.HANDLE]:
        view = wintypes.HANDLE()
        rc = self._dll.MsiDatabaseOpenViewW(self._handle, query, ctypes.byref(view))
        if rc != 0:
            raise MsiError(f"MsiDatabaseOpenViewW({query}): {rc}")
        try:
            rc = self._dll.MsiViewExecute(view, None)
            if rc != 0:
                raise MsiError(f"MsiViewExecute: {rc}")
            while True:
                record = wintypes.HANDLE()
                rc = self._dll.MsiViewFetch(view, ctypes.byref(record))
                if rc == _ERROR_NO_MORE_ITEMS:
                    return
                if rc != 0:
                    raise MsiError(f"MsiViewFetch: {rc}")
                try:
                    yield record
                finally:
                    self._dll.MsiCloseHandle(record)
        finally:
            self._dll.MsiCloseHandle(view)

    def _string(self, record, field: int) -> str | None:
        if self._dll.MsiRecordIsNull(record, field):
            return None
        size = wintypes.DWORD(256)
        while True:
            buf = ctypes.create_unicode_buffer(size.value + 1)
            needed = wintypes.DWORD(size.value + 1)
            rc = self._dll.MsiRecordGetStringW(record, field, buf, ctypes.byref(needed))
            if rc == _ERROR_MORE_DATA:
                size = wintypes.DWORD(needed.value + 1)
                continue
            if rc != 0:
                raise MsiError(f"MsiRecordGetStringW: {rc}")
            return buf.value

    def _integer(self, record, field: int) -> int | None:
        value = self._dll.MsiRecordGetInteger(record, field)
        return None if value == _MSI_NULL_INTEGER else int(value)

    def table(self, query: str, kinds: str) -> list[tuple]:
        """Строки запроса; `kinds` — по букве на столбец: s — строка, i — число."""
        result = []
        for record in self._rows(query):
            row = []
            for index, kind in enumerate(kinds, start=1):
                row.append(self._string(record, index) if kind == "s" else self._integer(record, index))
            result.append(tuple(row))
        return result

    def has_table(self, name: str) -> bool:
        rows = self.table(f"SELECT `Name` FROM `_Tables` WHERE `Name`='{name}'", "s")
        return bool(rows)

    def read_stream(self, name: str, out: Path, check: Callable[[], None] | None = None) -> int:
        """Выгружает поток `_Streams[name]` в файл `out` (создаётся новым)."""
        if not _KEY_RE.match(name):
            raise MsiError("недопустимое имя потока")
        total = 0
        found = False
        for record in self._rows(f"SELECT `Data` FROM `_Streams` WHERE `Name`='{name}'"):
            found = True
            with open(out, "xb") as fh:
                buf = ctypes.create_string_buffer(1 << 20)
                while True:
                    if check is not None:
                        check()
                    size = wintypes.DWORD(len(buf))
                    rc = self._dll.MsiRecordReadStream(record, 1, buf, ctypes.byref(size))
                    if rc != 0:
                        raise MsiError(f"MsiRecordReadStream: {rc}")
                    if size.value == 0:
                        break
                    total += size.value
                    if total > MAX_STREAM_BYTES:
                        raise MsiError("слишком большой поток")
                    fh.write(buf.raw[: size.value])
        if not found:
            raise MsiError(f"нет потока {name}")
        return total


# --- План раскладки -----------------------------------------------------------


@dataclass(frozen=True)
class PlannedFile:
    key: str  # ключ таблицы File = имя файла в CAB
    parts: tuple[str, ...]  # путь относительно корня (корень установки или системный)
    size: int
    system: bool  # файл из System64Folder/SystemFolder
    component: str


@dataclass
class Plan:
    files: list[PlannedFile]
    cabinets: list[str]  # имена встроенных потоков CAB
    product_version: str = ""
    product_name: str = ""


def _long_name(value: str) -> str:
    """Directory.DefaultDir: «цель[:источник]», каждая часть «короткое|длинное»."""
    target = value.split(":", 1)[0]
    return target.split("|", 1)[-1]


def _file_long_name(value: str) -> str:
    """File.FileName: «короткое|длинное» (двоеточия в имени файла нет — его отвергнет safe_part)."""
    return value.split("|", 1)[-1]


def safe_part(name: str) -> str:
    """Проверяет одну часть пути из таблиц msi; при нарушении — MsiError."""
    if not name or name in (".", "..") or len(name) > 255:
        raise MsiError(f"недопустимое имя в таблицах msi: {name!r}")
    if _BAD_CHARS.search(name) or name.endswith((" ", ".")):
        raise MsiError(f"недопустимое имя в таблицах msi: {name!r}")
    if name.split(".", 1)[0].upper() in _RESERVED:
        raise MsiError(f"зарезервированное имя в таблицах msi: {name!r}")
    return name


def component_wanted(condition: str | None) -> bool:
    """Нужен ли компонент с таким условием установки (см. `_SKIP_CONDITION_WORDS`)."""
    if not condition:
        return True
    if any(word in condition for word in _SKIP_CONDITION_WORDS):
        return False
    return not _ARM64_ONLY.search(condition)


def build_plan(
    directories: list[tuple[str, str | None, str]],
    components: list[tuple[str, str]],
    files: list[tuple[str, str, str, int, int]],
    media: list[tuple[int, int, str | None]],
) -> Plan:
    """Строит план раскладки по строкам таблиц (чистая функция, без msi.dll).

    `directories` — (Directory, Directory_Parent, DefaultDir);
    `components` — (Component, Directory_, Condition);
    `files` — (File, Component_, FileName, FileSize, Attributes);
    `media` — (DiskId, LastSequence, Cabinet).
    """
    parent: dict[str, str | None] = {}
    names: dict[str, str] = {}
    for key, par, default in directories:
        parent[key] = par if par and par != key else None
        names[key] = _long_name(default or ".")

    def chain(directory: str) -> tuple[str | None, tuple[str, ...]]:
        """(корень: INSTALL_ROOT / "system" / None, части пути под ним)."""
        parts: list[str] = []
        current: str | None = directory
        seen: set[str] = set()
        while current is not None:
            if current in seen or len(seen) > MAX_DEPTH:
                raise MsiError("цикл в таблице Directory")
            seen.add(current)
            if current == INSTALL_ROOT:
                return INSTALL_ROOT, tuple(reversed(parts))
            if current == FONTS_ROOT:
                return INSTALL_ROOT, FONTS_SUBDIR + tuple(reversed(parts))
            if current == SYSTEM64_ROOT or current.startswith(SYSTEM64_ROOT + "."):
                return "system", tuple(reversed(parts))
            if current not in parent:
                raise MsiError(f"неизвестный каталог {current!r}")
            name = names[current]
            if name != ".":
                parts.append(safe_part(name))
            current = parent[current]
        return None, ()

    component_dir = {
        name: directory for name, directory, condition in components
        if component_wanted(condition)
    }
    cabinets: list[str] = []
    for _disk, _last, cabinet in media:
        if not cabinet:
            continue
        if not cabinet.startswith("#"):
            raise MsiError("внешние CAB не поддерживаются")
        cabinets.append(cabinet[1:])
    if len(files) > MAX_FILES:
        raise MsiError("слишком много файлов в msi")
    planned: list[PlannedFile] = []
    total = 0
    targets: set[tuple[bool, tuple[str, ...]]] = set()
    for key, component, file_name, size, attributes in files:
        if not _KEY_RE.match(key or ""):
            raise MsiError(f"недопустимый ключ файла {key!r}")
        if attributes and attributes & _FILE_ATTR_NONCOMPRESSED:
            raise MsiError("некомпрессированные файлы не поддерживаются")
        directory = component_dir.get(component)
        if directory is None:
            continue  # компонент не нужен (см. component_wanted) или его нет
        root, parts = chain(directory)
        if root is None:
            continue
        name = safe_part(_file_long_name(file_name or ""))
        size = max(0, int(size or 0))
        total += size
        if total > MAX_TOTAL_BYTES:
            raise MsiError("слишком большой объём распаковки")
        entry = PlannedFile(key, parts + (name,), size, root == "system", component)
        target = (entry.system, tuple(p.casefold() for p in entry.parts))
        if target in targets:
            # Тот же путь у двух компонентов (варианты под разные условия) —
            # берём первый: одинаковые по смыслу файлы.
            continue
        targets.add(target)
        planned.append(entry)
    return Plan(files=planned, cabinets=cabinets)


def read_plan(path: Path | str) -> Plan:
    """Читает таблицы msi и строит план раскладки."""
    with MsiDatabase(path) as db:
        directories = db.table("SELECT `Directory`, `Directory_Parent`, `DefaultDir` FROM `Directory`", "sss")
        components = db.table("SELECT `Component`, `Directory_`, `Condition` FROM `Component`", "sss")
        files = db.table("SELECT `File`, `Component_`, `FileName`, `FileSize`, `Attributes` FROM `File`", "sssii")
        media = db.table("SELECT `DiskId`, `LastSequence`, `Cabinet` FROM `Media`", "iis")
        props = dict(db.table(
            "SELECT `Property`, `Value` FROM `Property` "
            "WHERE `Property`='ProductVersion' OR `Property`='ProductName'", "ss"))
    plan = build_plan(directories, components, files, media)
    plan.product_version = props.get("ProductVersion") or ""
    plan.product_name = props.get("ProductName") or ""
    return plan


def extract_cabinets(path: Path | str, plan: Plan, work: Path, check: Callable[[], None]) -> list[Path]:
    """Выгружает встроенные CAB в `work` (папка должна существовать)."""
    result = []
    with MsiDatabase(path) as db:
        for index, name in enumerate(plan.cabinets):
            out = work / f"cab{index}.cab"
            db.read_stream(name, out, check)
            result.append(out)
    return result


# --- Распаковка CAB ------------------------------------------------------------


def expand_exe() -> Path:
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return Path(root) / "System32" / "expand.exe"


def expand_cabinet(
    cab: Path,
    out_dir: Path,
    *,
    expected_files: int,
    check: Callable[[], None],
    progress: Callable[[float], None] | None = None,
    timeout: float = 1800.0,
) -> None:
    """Распаковывает CAB системным expand.exe в объекте задания.

    `check()` бросает исключение при отмене — процесс убивается вместе с
    объектом задания. Прогресс — по числу появившихся файлов.
    """
    from . import jobobject

    out_dir.mkdir(parents=True, exist_ok=True)
    args = [str(expand_exe()), str(cab), "-F:*", str(out_dir)]
    last = [0.0]

    def poll() -> None:
        check()
        now = time.monotonic()
        if progress is not None and now - last[0] >= 0.5 and expected_files:
            last[0] = now
            try:
                count = sum(1 for _ in os.scandir(out_dir))
            except OSError:
                count = 0
            progress(min(1.0, count / expected_files))

    result = jobobject.run(
        args, timeout=timeout, poll=poll, cwd=out_dir, memory_limit=1 * 2**30,
        stdout=subprocess.DEVNULL,
    )
    if result.timed_out:
        raise MsiError("распаковка CAB не уложилась во время")
    if result.returncode != 0:
        raise MsiError(f"expand.exe завершился с кодом {result.returncode}")


def place_files(
    plan: Plan,
    flat_dir: Path,
    install_dir: Path,
    *,
    check: Callable[[], None],
    system_whitelist: Callable[[str], bool] = lambda name: False,
) -> list[str]:
    """Переносит файлы из плоской папки распаковки по путям плана.

    Файлы установки → `install_dir/<части>`; системные файлы, имя которых
    проходит `system_whitelist`, → `install_dir/program/<имя>`, если там
    такого ещё нет. Возвращает имена перенесённых системных файлов.
    """
    root = install_dir.resolve()
    copied_system: list[str] = []
    for entry in plan.files:
        check()
        source = flat_dir / entry.key
        if entry.system:
            name = entry.parts[-1]
            if len(entry.parts) != 1 or not system_whitelist(name):
                continue
            target = root / "program" / name
            if target.exists():
                continue
        else:
            target = root.joinpath(*entry.parts)
        resolved = Path(os.path.abspath(target))
        if os.path.commonpath([str(resolved), str(root)]) != str(root) or resolved == root:
            raise MsiError("путь из msi выходит за папку назначения")
        if PureWindowsPath(str(resolved)).drive.upper() != PureWindowsPath(str(root)).drive.upper():
            raise MsiError("путь из msi на другом диске")
        if not source.is_file():
            raise MsiError(f"в CAB нет файла {entry.key}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, resolved)
        if entry.system:
            copied_system.append(name)
    return copied_system
