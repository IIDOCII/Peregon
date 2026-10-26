# -*- mode: python ; coding: utf-8 -*-
"""Сборка программы Peregon PyInstaller'ом: папка `dist/Peregon/` (--onedir), без консоли.

Видимое название — Peregon (автор XClassiK); имя пакета Python — `konverter`.

Запуск — через `packaging/build.ps1` (или `uv run pyinstaller packaging/konverter.spec`
из корня проекта). Версия берётся из pyproject.toml.

Что важно:
- data/ рабочих данных в сборку НЕ кладётся: программа создаёт её рядом
  с Peregon.exe при первом запуске (`konverter.paths`, режим `sys.frozen`);
  модуль LibreOffice (data/modules) скачивается по требованию;
- иконка окна — `konverter/ui/assets` (рядом с модулями в `_internal`),
  значок exe — `packaging/icon.ico`;
- конвертеры дочерних процессов задаются строками «модуль:функция»
  (`converters/_isolated.TASKS`), поэтому все модули пакета konverter
  подключаются явно (collect_submodules);
- лишнее (py7zr и его зависимости, tkinter, тесты, ненужные части Qt)
  исключается — см. EXCLUDES и фильтр бинарников ниже.
"""

import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 — SPECPATH задаёт PyInstaller
SRC = ROOT / "src"
# Видимое имя программы: Peregon.exe, dist/Peregon/.
APP_NAME = "Peregon"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

# Модули, которых в сборке быть не должно.
EXCLUDES = [
    # py7zr в коде не используется (остаётся в зависимостях проекта) и его зависимости
    "py7zr", "pyppmd", "bcj", "inflate64", "multivolumefile", "texttable",
    "backports.zstd", "brotli", "_brotli",
    # графика/среды, которые программе не нужны
    "tkinter", "_tkinter", "turtle", "turtledemo", "idlelib",
    "IPython", "matplotlib", "pandas", "scipy",
    # тесты
    "pytest", "_pytest", "pytestqt", "pluggy", "iniconfig", "pygments",
    "numpy.tests", "PIL.ImageQt", "PIL.ImageTk",
    "lib2to3", "pydoc_data",
    # Qt: только QtCore/QtGui/QtWidgets
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtDBus", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtXml",
    "PySide6.QtConcurrent", "PySide6.QtPrintSupport", "PySide6.QtHelp",
    "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtMultimedia",
    "PySide6.QtNetworkAuth", "PySide6.QtSvgWidgets",
]

datas = [(str(SRC / "konverter" / "ui" / "assets"), "konverter/ui/assets")]
# Модель Magika (.onnx) и её конфиги.
datas += collect_data_files("magika")

hiddenimports = collect_submodules("konverter")
# Модули, которые библиотеки импортируют динамически.
hiddenimports += ["pillow_heif", "rawpy", "resvg_py", "pikepdf", "pypdfium2", "pypdfium2_raw"]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)


def _keep(dest: str) -> bool:
    """Отбрасывает ненужные бинарники и данные Qt."""
    name = dest.replace("\\", "/").lower()
    if not name.startswith("pyside6/"):
        return True
    base = name.rsplit("/", 1)[-1]
    # Программный OpenGL (~20 МБ) — виджетам не нужен.
    if base == "opengl32sw.dll":
        return False
    # Переводы Qt не используются (у программы свои строки, ui/common.py).
    if "/translations/" in name:
        return False
    plugin_dir = name.split("/plugins/", 1)[1].split("/", 1)[0] if "/plugins/" in name else None
    if plugin_dir is not None:
        # Нужны: платформа (windows; offscreen — для самопроверки без окна),
        # стили (windows11), форматы картинок (svg, gif, ico, …), значки SVG.
        if plugin_dir not in {"platforms", "styles", "imageformats", "iconengines"}:
            return False
        if plugin_dir == "platforms" and base not in {"qwindows.dll", "qoffscreen.dll"}:
            return False
        # qpdf тянет Qt6Pdf (~10 МБ) — PDF программа рендерит pypdfium2.
        if plugin_dir == "imageformats" and base == "qpdf.dll":
            return False
    # Модули Qt, которые программа не использует.
    for unused in ("qt6pdf", "qt6qml", "qt6quick", "qt6network", "qt6opengl", "qt6virtualkeyboard"):
        if base.startswith(unused):
            return False
    return True


a.binaries = [entry for entry in a.binaries if _keep(entry[0])]
a.datas = [entry for entry in a.datas if _keep(entry[0])]

pyz = PYZ(a.pure)  # noqa: F821

_numbers = tuple(int(part) for part in (VERSION.split(".") + ["0", "0", "0"])[:4])
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_numbers, prodvers=_numbers),
    kids=[
        StringFileInfo([
            StringTable("041904B0", [
                StringStruct("CompanyName", "XClassiK"),
                StringStruct("FileDescription", "Peregon — конвертер файлов"),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", APP_NAME),
                StringStruct("LegalCopyright", "© XClassiK, GPL-3.0-or-later"),
                StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", VERSION),
            ])
        ]),
        VarFileInfo([VarStruct("Translation", [0x0419, 1200])]),
    ],
)

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "packaging" / "icon.ico"),
    version=version_info,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
