"""Самопроверка собранной программы без окна: `Peregon.exe --selftest <папка>`.

Зачем: проверить собранный exe на машине без участия человека — что в сборку
попали модель Magika, FFmpeg, pdfium, HEIF, LibRaw, resvg, плагины Qt и т.п.,
и что дочерние процессы (`multiprocessing`, `spawn`) запускаются из exe.

Что делает:
1. в `<папка>/in` генерирует маленькие файлы (PNG, SVG, WAV, MP4, PDF, CSV,
   EPUB, HEIC); файлы из `<папка>/extra` (например, RAW, положенный
   скриптом проверки) тоже конвертируются — в JPG;
2. определяет формат каждого файла (`core.detect`, как интерфейс) и ставит
   задачи в ту же очередь (`core.jobs.JobManager`) с теми же конвертерами —
   тяжёлые форматы идут через дочерние процессы `converters._isolated`;
3. результаты пишутся в `<папка>/out`; проверяется, что файл/папка
   результата есть и не пустые (картинки — ещё и открываются Pillow);
4. проверяет дочерний процесс (`probe`: PySide6 в нём не загружен — значит,
   второго окна/QApplication там нет), пути программы, иконку и плагины Qt
   (платформа `offscreen` — без окна);
5. пишет отчёт `<папка>/report.json` и возвращает код выхода: 0 — всё
   прошло, 1 — что-то не прошло, 2 — неверные аргументы/папка.

Безопасность аргумента: пишет только в указанную папку (in/, out/,
report.json) и в обычную папку данных программы (журнал, журнал временных
файлов). Папка должна не существовать или быть пустой (допускается только
подпапка `extra`) — так самопроверка не может что-то перезаписать или
перемешать с чужими файлами. Из `extra` файлы только читаются. В справке
аргумент не показывается (справки у программы нет).
"""

from __future__ import annotations

import json
import logging
import math
import os
import struct
import sys
import time
import wave
import zipfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SELFTEST_ARG = "--selftest"
# Что может уже лежать в папке самопроверки до запуска.
ALLOWED_EXISTING = frozenset({"extra"})
REPORT_NAME = "report.json"
# Общий предел ожидания всех задач, с.
JOBS_TIMEOUT = 600.0

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


class SelftestUsageError(ValueError):
    """Неверные аргументы или неподходящая папка самопроверки."""


def parse_selftest_args(argv: list[str]) -> Path | None:
    """Папка самопроверки из командной строки или None — обычный запуск.

    Самопроверка включается, только если `--selftest` — ПЕРВЫЙ аргумент
    (после имени программы) и за ним ровно один непустой путь. Иначе:
    `--selftest` где-то ещё или без пути — `SelftestUsageError`; без
    `--selftest` — None.
    """
    args = list(argv[1:])
    if SELFTEST_ARG not in args:
        return None
    if args[0] != SELFTEST_ARG or len(args) != 2 or not args[1].strip():
        raise SelftestUsageError(f"Использование: {SELFTEST_ARG} <папка>")
    return Path(args[1]).absolute()


def check_folder(folder: Path) -> None:
    """Проверяет папку самопроверки (не существует или пуста, кроме `extra`)."""
    if folder.exists():
        if not folder.is_dir() or folder.is_symlink():
            raise SelftestUsageError(f"{folder} — не папка")
        extra = sorted(p.name for p in folder.iterdir() if p.name not in ALLOWED_EXISTING)
        if extra:
            raise SelftestUsageError(f"Папка {folder} не пуста: {extra[:5]}")


# --- Генерация входных файлов --------------------------------------------


def _make_png(path: Path) -> None:
    from PIL import Image

    img = Image.new("RGB", (64, 48))
    img.putdata([(x * 4, y * 5, 128) for y in range(48) for x in range(64)])
    img.save(path, "PNG")


def _make_svg(path: Path) -> None:
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">'
        '<rect width="64" height="64" fill="#2fbf8f"/>'
        '<circle cx="32" cy="32" r="20" fill="#fff"/></svg>',
        encoding="utf-8",
    )


def _make_wav(path: Path) -> None:
    rate = 22050
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(rate)  # 1 с
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


def _make_mp4(path: Path) -> None:
    import av
    from PIL import Image

    with av.open(str(path), "w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for i in range(20):  # 2 с
            img = Image.new("RGB", (64, 48), ((i * 12) % 256, 60, 160))
            img.putpixel(((i * 3) % 64, 24), (255, 255, 255))
            frame = av.VideoFrame.from_image(img)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def _make_pdf(path: Path) -> None:
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.add_page()
    pdf.set_font("helvetica", size=14)
    pdf.cell(text="Peregon selftest")
    pdf.output(str(path))


def _make_csv(path: Path) -> None:
    path.write_text("name;value\nalpha;1\nbeta;2,5\ngamma;=1+1\n", encoding="utf-8")


def _make_epub(path: Path) -> None:
    container = (
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="id">selftest</dc:identifier><dc:title>Selftest</dc:title>'
        "<dc:language>ru</dc:language></metadata>"
        '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )
    chapter = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Глава</title></head>'
        "<body><h1>Глава 1</h1><p>Проверка сборки.</p></body></html>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/c1.xhtml", chapter, compress_type=zipfile.ZIP_DEFLATED)


def _make_heic(path: Path) -> None:
    import pillow_heif
    from PIL import Image

    img = Image.new("RGB", (64, 48), (200, 80, 40))
    heif = pillow_heif.from_pillow(img)
    heif.save(str(path), quality=80)


# (имя файла, генератор, вариант конвертации, доп. параметры задачи)
CASES: list[tuple[str, Any, str, dict]] = [
    ("picture.png", _make_png, "jpg", {}),
    ("vector.svg", _make_svg, "png", {}),
    ("tone.wav", _make_wav, "mp3", {}),
    ("clip.mp4", _make_mp4, "gif", {"fragment": (0.0, 1.0)}),
    ("doc.pdf", _make_pdf, "pdf_pages_png", {}),
    ("table.csv", _make_csv, "xlsx", {}),
    ("book.epub", _make_epub, "txt", {}),
    ("photo.heic", _make_heic, "jpg", {}),
]
# Вариант для файлов из extra/ (RAW и др. картинки).
EXTRA_TARGET = "jpg"


# --- Проверки ------------------------------------------------------------


def _check_result(path: Path | None) -> str | None:
    """None — результат в порядке, иначе текст проблемы."""
    if path is None or not path.exists():
        return "нет результата"
    files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    if not files or any(f.stat().st_size == 0 for f in files):
        return "пустой результат"
    for f in files:
        if f.suffix.lower() in {".jpg", ".png", ".gif"}:
            from PIL import Image

            try:
                with Image.open(f) as img:
                    img.load()
            except Exception as exc:  # noqa: BLE001
                return f"{f.name}: не открывается ({type(exc).__name__})"
    return None


def _check_child() -> dict[str, Any]:
    """Дочерний процесс из exe запускается и не грузит PySide6 (окна в нём нет)."""
    from konverter.converters import _isolated
    from konverter.converters.base import CancelToken

    try:
        info = _isolated.run_isolated("probe", {}, cancel=CancelToken(), timeout=60)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    gui_modules = [
        m for m in info.get("qt_modules", [])
        if m.startswith("PySide6.Qt") and m != "PySide6.QtCore"
    ]
    # В сборке runtime-хук PyInstaller грузит в каждом процессе PySide6.QtCore
    # (см. `_isolated._probe_info`); окна нет, если нет QtGui/QtWidgets и приложения Qt.
    return {
        "ok": not gui_modules and not info.get("qt_app", True),
        "qt_core_in_child": "PySide6.QtCore" in info.get("qt_modules", []),
        "qt_gui_modules_in_child": gui_modules,
        "qt_app_in_child": info.get("qt_app"),
    }


def _check_paths() -> dict[str, Any]:
    from konverter import paths
    from konverter.libreoffice import module

    root = paths.app_root()
    exe_dir = Path(sys.executable).resolve().parent
    frozen = bool(getattr(sys, "frozen", False))
    lo = module.module_path()
    info = {
        "frozen": frozen,
        "app_root": str(root),
        "data_dir": str(paths.data_dir_path()),
        "libreoffice_module": str(lo),
        "libreoffice_installed": module.is_installed(),
        "data_writable": paths.is_data_writable(),
    }
    ok = paths.data_dir_path() == root / paths.DATA_DIR_NAME
    if frozen:
        # В сборке data/ — рядом с exe, а не в _internal.
        ok = ok and root == exe_dir and "_internal" not in paths.data_dir_path().parts
    info["ok"] = ok and lo.is_relative_to(paths.data_dir_path())
    return info


def _check_qt() -> dict[str, Any]:
    """Иконка и плагины Qt (платформа offscreen — без окна)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImageReader
    from PySide6.QtWidgets import QApplication, QStyleFactory

    from konverter.ui.common import app_icon_path, load_app_icon

    app = QApplication.instance() or QApplication([sys.argv[0]])
    icon = load_app_icon()
    styles = [s.lower() for s in QStyleFactory.keys()]
    formats = sorted(bytes(f).decode() for f in QImageReader.supportedImageFormats())
    path = app_icon_path()
    info = {
        "icon_path": str(path) if path else None,
        "icon_ok": icon is not None and not icon.pixmap(32, 32).isNull(),
        "styles": styles,
        "image_formats": formats,
        "platform": app.platformName(),
    }
    info["ok"] = bool(
        info["icon_ok"] and "windows11" in styles and {"svg", "gif", "ico"} <= set(formats)
    )
    return info


# --- Основной ход --------------------------------------------------------


def run_selftest(folder: Path) -> int:
    """Выполняет самопроверку в `folder`; возвращает код выхода."""
    check_folder(folder)
    started = time.monotonic()
    in_dir, out_dir = folder / "in", folder / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(exist_ok=True)

    import konverter.converters  # noqa: F401 — регистрирует конвертеры
    from konverter.converters.base import REGISTRY, Preset
    from konverter.core.detect import detect
    from konverter.core.jobs import FINISHED_STATUSES, JobManager

    report: dict[str, Any] = {"python": sys.version, "executable": sys.executable}
    cases: list[dict[str, Any]] = []
    planned: list[tuple[dict[str, Any], Path, str, dict]] = []

    for name, make, target_id, options in CASES:
        path = in_dir / name
        entry: dict[str, Any] = {"source": name, "target": target_id}
        cases.append(entry)
        try:
            make(path)
        except Exception as exc:  # noqa: BLE001
            entry.update(status="generate_failed", error=f"{type(exc).__name__}: {exc}")
            continue
        planned.append((entry, path, target_id, options))

    extra_dir = folder / "extra"
    if extra_dir.is_dir():
        for path in sorted(p for p in extra_dir.iterdir() if p.is_file()):
            entry = {"source": f"extra/{path.name}", "target": EXTRA_TARGET}
            cases.append(entry)
            planned.append((entry, path, EXTRA_TARGET, {}))

    manager = JobManager()
    jobs = []
    for entry, path, target_id, options in planned:
        try:
            detected = detect(path)
        except Exception as exc:  # noqa: BLE001
            entry.update(status="detect_failed", error=f"{type(exc).__name__}: {exc}")
            continue
        if detected is None:
            entry.update(status="detect_failed", error="формат не определён")
            continue
        entry["detected"] = detected.fmt
        converter = REGISTRY.get(detected.category)
        target = next(
            (t for t in (converter.targets(detected.fmt) if converter else []) if t.id == target_id),
            None,
        )
        if target is None:
            entry.update(status="no_target", error=f"нет варианта {target_id}")
            continue
        job = manager.submit(
            [path], converter, target, Preset.MEDIUM, out_dir,
            formats=[detected.fmt], options=options,
        )
        jobs.append((entry, job))

    deadline = time.monotonic() + JOBS_TIMEOUT
    while time.monotonic() < deadline and any(j.status not in FINISHED_STATUSES for _, j in jobs):
        time.sleep(0.2)
    for entry, job in jobs:
        snap = job.snapshot()
        entry["status"] = snap.status.value
        if snap.error_message:
            entry["error"] = snap.error_message
        if snap.result_path is not None:
            entry["result"] = snap.result_path.name
        if snap.started_at and snap.finished_at:
            entry["seconds"] = round(snap.finished_at - snap.started_at, 2)
        if snap.status.value == "done":
            problem = _check_result(snap.result_path)
            if problem:
                entry.update(status="bad_result", error=problem)
    manager.shutdown(wait=True, timeout=10, cancel=True)

    report["cases"] = cases
    for key, check in (("child", _check_child), ("paths", _check_paths), ("qt", _check_qt)):
        try:
            report[key] = check()
        except Exception as exc:  # noqa: BLE001
            report[key] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    ok = all(c.get("status") == "done" for c in cases) and all(
        report[k].get("ok") for k in ("child", "paths", "qt")
    )
    report["ok"] = ok
    report["seconds"] = round(time.monotonic() - started, 2)
    (folder / REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Самопроверка: %s", "успешно" if ok else "есть ошибки")
    return EXIT_OK if ok else EXIT_FAILED


def main(folder: Path) -> int:
    """Обёртка: неверная папка — код 2 (и запись в журнал), иначе `run_selftest`."""
    try:
        return run_selftest(folder)
    except SelftestUsageError as exc:
        log.warning("Самопроверка: %s", exc)
        return EXIT_USAGE
    except Exception:  # noqa: BLE001 — без консоли трассировка ушла бы в модальное окно
        log.exception("Самопроверка: непредвиденная ошибка")
        return EXIT_FAILED


__all__ = ["SELFTEST_ARG", "SelftestUsageError", "main", "parse_selftest_args", "run_selftest"]
