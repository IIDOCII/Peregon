"""Определение кодировки текста и разделителя CSV (П19) — только в ребёнке.

Кодировка (`decode_text`), по порядку:
1. BOM: UTF-8 с BOM, UTF-32 LE/BE, UTF-16 LE/BE;
2. UTF-16 без BOM — по «старшим» байтам (0x00/0x04) через один;
3. строгий UTF-8 (самый частый случай; ASCII — тоже UTF-8);
4. charset-normalizer по первым `DETECT_SAMPLE` байтам; русскоязычным
   пользователям чаще всего попадается cp1251, поэтому, если в образце
   есть байты 0xC0–0xFF и разбор как cp1251 даёт в основном кириллицу,
   а charset-normalizer предложил не кириллическую кодировку, — cp1251;
5. ничего не подошло — cp1251 с заменой битых байтов.
Весь текст декодируется с `errors="replace"` (битый байт → «�», а не ошибка).

Разделитель CSV (`sniff_delimiter`): своя проверка согласованности по первым
строкам (для каждого кандидата `;` `\\t` `,` `|` — число полей в строках,
разобранных `csv.reader`: побеждает кандидат, дающий ≥ 2 поля одинаково в
большинстве строк; при равенстве — порядок `;` → `\\t` → `,` → `|`, потому что
в русской локали `,` — десятичный разделитель: «1,5;2,5» — это две ячейки,
а не три). Не определилось — `csv.Sniffer`, затем запятая.
"""

from __future__ import annotations

import csv
import io

DETECT_SAMPLE = 1 << 20
_CYRILLIC_ENCODINGS = ("cp1251", "koi8_r", "koi8_u", "cp866", "mac_cyrillic", "iso8859_5",
                       "utf_8", "utf_16", "utf_32")


def _cyrillic_share(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if "Ѐ" <= ch <= "ӿ") / len(letters)


def detect_encoding(data: bytes) -> tuple[str, int]:
    """(кодировка, длина BOM)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8", 3
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return ("utf-32-le" if data[0] == 0xFF else "utf-32-be"), 4
    if data.startswith(b"\xff\xfe"):
        return "utf-16-le", 2
    if data.startswith(b"\xfe\xff"):
        return "utf-16-be", 2
    sample = data[:DETECT_SAMPLE]
    if len(sample) >= 4:
        # UTF-16 без BOM: «старшие» байты (у латиницы — 0x00, у кириллицы — 0x04)
        # стоят через один.
        half = len(sample) // 2
        odd = sum(1 for b in sample[1::2] if b in (0x00, 0x04))
        even = sum(1 for b in sample[0::2] if b in (0x00, 0x04))
        for encoding, share in (("utf-16-le", odd), ("utf-16-be", even)):
            if half and share / half > 0.7:
                try:
                    sample[: len(sample) // 2 * 2].decode(encoding)
                    return encoding, 0
                except UnicodeDecodeError:
                    pass
    if _utf8_ok(data):
        return "utf-8", 0
    guess = None
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(sample).best()
        if best is not None:
            guess = best.encoding
    except Exception:  # noqa: BLE001
        guess = None
    high = sum(1 for b in sample if b >= 0xC0)
    if high and (guess is None or guess not in _CYRILLIC_ENCODINGS):
        if _cyrillic_share(sample.decode("cp1251", errors="replace")) > 0.6:
            return "cp1251", 0
    return (guess or "cp1251"), 0


def _utf8_ok(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def decode_text(data: bytes) -> str:
    """Байты текста → строка (кодировка — см. докстринг модуля), без BOM."""
    encoding, bom = detect_encoding(data)
    try:
        text = data[bom:].decode(encoding, errors="replace")
    except LookupError:
        text = data[bom:].decode("cp1251", errors="replace")
    return text.lstrip("﻿")


CANDIDATES = (";", "\t", ",", "|")


def sniff_delimiter(sample: str) -> str:
    """Разделитель CSV по образцу текста (см. докстринг модуля)."""
    lines = sample.splitlines()[:50]
    if len(sample) >= 65536 and len(lines) > 1:
        lines = lines[:-1]  # последняя строка образца может быть оборвана
    text = "\n".join(lines)
    best: tuple[float, int] | None = None
    choice = None
    for priority, delimiter in enumerate(CANDIDATES):
        if delimiter not in text:
            continue
        try:
            counts = [len(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter) if row]
        except csv.Error:
            continue
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        if mode < 2:
            continue
        share = counts.count(mode) / len(counts)
        score = (share, -priority)
        if share >= 0.6 and (best is None or score > best):
            best = score
            choice = delimiter
    if choice is not None:
        return choice
    try:
        return csv.Sniffer().sniff(text[:65536], delimiters="".join(CANDIDATES)).delimiter
    except csv.Error:
        return ","
