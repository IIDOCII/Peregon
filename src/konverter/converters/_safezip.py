"""Безопасное чтение ZIP-контейнеров (EPUB, DOCX, XLSX) — только в дочернем процессе.

Входные файлы враждебны, поэтому архив читается с ограничениями:
* число записей ≤ `max_entries` (сначала — по счётчику в конце архива,
  EOCD, ещё до разбора центрального каталога: 100 000 записей отвергаются
  без построения 100 000 объектов);
* распакованный размер считается ПО ФАКТУ (поток читается кусками, счётчик —
  по реально полученным байтам; размерам из заголовков не верим): одна
  запись ≤ `max_entry`, сумма всех прочитанных ≤ `max_total`;
* коэффициент сжатия записи: распаковано больше `ratio_min_size` и больше
  `max_ratio` × сжатый размер → «ZIP-бомба» (отказ сразу, не дочитывая);
* только способы сжатия STORED и DEFLATED; зашифрованные записи — отказ;
* повторяющиеся имена записей — отказ (разные программы взяли бы разные копии);
* имена записей никогда не превращаются в пути на диске: архив читается
  в память, имя — только ключ словаря. Ссылки внутри документа (`../img.png`)
  разрешаются функцией `resolve`, которая не выпускает за корень архива.

Ошибки — `TaskError` с ключами из `_docjob.MESSAGES`.
"""

from __future__ import annotations

import os
import posixpath
import struct
import zipfile
import zlib
from urllib.parse import unquote, urlsplit

from ._isolated import TaskError

# Лимиты по умолчанию (родитель передаёт свои в аргументах задачи).
MAX_ENTRIES = 10_000
MAX_TOTAL = 2**30
MAX_ENTRY = 512 * 2**20
MAX_RATIO = 200
RATIO_MIN_SIZE = 32 * 2**20

_CHUNK = 1 << 16
_ALLOWED_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


def _eocd_entries(path: str) -> int | None:
    """Число записей из конца архива (EOCD) или None, если не найдено/ZIP64."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            tail_size = min(size, 65_557)  # 22 байта EOCD + комментарий ≤ 65535
            fh.seek(size - tail_size)
            tail = fh.read(tail_size)
    except OSError:
        return None
    pos = tail.rfind(b"PK\x05\x06")
    if pos < 0 or pos + 22 > len(tail):
        return None
    total = struct.unpack("<H", tail[pos + 10:pos + 12])[0]
    return None if total == 0xFFFF else total


class SafeZip:
    """ZIP-архив для чтения с лимитами (см. докстринг модуля)."""

    def __init__(self, path: str, limits: dict | None = None, tick=None) -> None:
        limits = limits or {}
        self.max_entries = int(limits.get("zip_max_entries", MAX_ENTRIES))
        self.max_total = int(limits.get("zip_max_total", MAX_TOTAL))
        self.max_entry = int(limits.get("zip_max_entry", MAX_ENTRY))
        self.max_ratio = float(limits.get("zip_max_ratio", MAX_RATIO))
        self.ratio_min_size = int(limits.get("zip_ratio_min_size", RATIO_MIN_SIZE))
        self.total = 0
        self._tick = tick
        count = _eocd_entries(path)
        if count is not None and count > self.max_entries:
            raise TaskError("zip_entries", self.max_entries)
        try:
            self._zip = zipfile.ZipFile(path, "r")
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError,
                NotImplementedError, struct.error):
            raise TaskError("damaged") from None
        try:
            infos = self._zip.infolist()
            if len(infos) > self.max_entries:
                raise TaskError("zip_entries", self.max_entries)
            self._infos: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                if info.filename in self._infos:
                    raise TaskError("zip_duplicate")
                self._infos[info.filename] = info
        except BaseException:
            self._zip.close()
            raise

    # -- контекстный менеджер --

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "SafeZip":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- доступ --

    def names(self) -> list[str]:
        return list(self._infos)

    def has(self, name: str) -> bool:
        info = self._infos.get(name)
        return info is not None and not info.is_dir()

    def _stream(self, name: str, limit: int | None):
        """Генератор кусков распакованных данных записи с проверкой лимитов."""
        info = self._infos.get(name)
        if info is None or info.is_dir():
            raise KeyError(name)
        if info.flag_bits & 0x1:
            raise TaskError("zip_encrypted")
        if info.compress_type not in _ALLOWED_METHODS:
            raise TaskError("zip_method")
        entry_limit = self.max_entry if limit is None else min(self.max_entry, int(limit))
        compressed = max(1, int(info.compress_size))
        got = 0
        try:
            with self._zip.open(info, "r") as stream:
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    got += len(chunk)
                    self.total += len(chunk)
                    if got > self.ratio_min_size and got > self.max_ratio * compressed:
                        raise TaskError("zip_bomb")
                    if got > entry_limit:
                        raise TaskError("zip_too_big", entry_limit / 2**20)
                    if self.total > self.max_total:
                        raise TaskError("zip_too_big", self.max_total / 2**20)
                    if self._tick is not None and got % (_CHUNK * 64) < len(chunk):
                        self._tick()
                    yield chunk
        except TaskError:
            raise
        except (zipfile.BadZipFile, zlib.error, OSError, EOFError, ValueError,
                NotImplementedError, RuntimeError, struct.error):
            raise TaskError("damaged") from None

    def read(self, name: str, limit: int | None = None) -> bytes:
        """Содержимое записи целиком (с лимитами); нет записи — KeyError."""
        return b"".join(self._stream(name, limit))

    def scan(self, check=None) -> None:
        """Распаковывает ВСЕ записи «вхолостую» (проверка до передачи в библиотеку).

        `check(name, first_chunk)` вызывается для первого куска каждой записи
        (например, проверка пролога XML на DTD).
        """
        for name, info in self._infos.items():
            if info.is_dir():
                continue
            first = True
            for chunk in self._stream(name, None):
                if first and check is not None:
                    check(name, chunk)
                first = False
            if first and check is not None:
                check(name, b"")


def resolve(base_dir: str, href: str) -> str | None:
    """Ссылка документа → имя записи архива или None (внешняя/выход за корень).

    `base_dir` — папка документа внутри архива ("OEBPS/text"); ссылка
    процентно декодируется, фрагмент и параметры отбрасываются. Схемы
    (`http:`, `file:`, `data:` …), абсолютные пути и выход выше корня —
    None: такие ссылки не читаются никогда.
    """
    if not href:
        return None
    href = href.strip()
    try:
        parts = urlsplit(href)
    except ValueError:
        return None
    if parts.scheme or parts.netloc:
        return None
    path = unquote(parts.path)
    if not path or path.startswith("/") or "\\" in path or "\x00" in path:
        return None
    joined = posixpath.normpath(posixpath.join(base_dir, path)) if base_dir else posixpath.normpath(path)
    if joined == "." or joined.startswith("../") or joined == ".." or joined.startswith("/"):
        return None
    return joined


# Сигнатуры заведомо двоичных частей OOXML: их пролог XML не проверяется.
_BINARY_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"\xff\xd8\xff",  # JPEG
    b"GIF87a", b"GIF89a",  # GIF
    b"BM",  # BMP
    b"II*\x00", b"MM\x00*",  # TIFF
    b"RIFF",  # WEBP, WAV, AVI
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",  # OLE (vbaProject.bin, внедрённые объекты)
    b"PK\x03\x04",  # вложенный ZIP (внедрённый документ)
    b"%PDF",
    b"\x01\x00\x00\x00",  # EMF
    b"\xd7\xcd\xc6\x9a",  # WMF (placeable)
    b"\x00\x01\x00\x00", b"OTTO", b"true", b"wOFF", b"wOF2",  # шрифты
)


def precheck_ooxml(path: str, limits: dict | None = None, tick=None) -> None:
    """Проверка OOXML (DOCX, XLSX) ДО передачи в python-docx/openpyxl.

    Все части распаковываются «вхолостую» с лимитами (`SafeZip.scan`: число
    записей, размер по факту, коэффициент сжатия, повторы имён), у XML-частей
    проверяется пролог: DOCTYPE (а с ним и объявления сущностей) в OOXML не
    бывает — любой означает отказ (`xml_entities`).
    """
    from ._safexml import looks_like_xml, prolog_has_dtd

    def check(name: str, chunk: bytes) -> None:
        if not chunk or chunk.startswith(_BINARY_SIGNATURES):
            return  # явно двоичная часть (картинка, OLE …)
        # Расширению не верим: XML-ли часть, решает тип содержимого, который
        # библиотека берёт из [Content_Types].xml, — поэтому проверяется
        # каждая часть, похожая на разметку (в любой кодировке: UTF-8 с BOM,
        # UTF-16, UTF-32), а части с XML-расширением — всегда.
        if name.lower().endswith((".xml", ".rels", ".vml")) or looks_like_xml(chunk):
            if prolog_has_dtd(chunk):
                raise TaskError("xml_entities")

    with SafeZip(path, limits, tick=tick) as archive:
        archive.scan(check)
