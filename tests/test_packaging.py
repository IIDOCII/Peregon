"""Этап 6: сборка — самопроверка `--selftest`, пути в собранной программе, иконка."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import konverter
from konverter import logging_setup, paths, selftest

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"


# --- Разбор аргумента --selftest -------------------------------------------


def test_selftest_args_absent():
    assert selftest.parse_selftest_args(["Peregon.exe"]) is None
    assert selftest.parse_selftest_args(["Peregon.exe", "file.png"]) is None


def test_selftest_args_folder(tmp_path):
    folder = selftest.parse_selftest_args(["Peregon.exe", "--selftest", str(tmp_path / "st")])
    assert folder == (tmp_path / "st").absolute()


def test_selftest_relative_folder_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    folder = selftest.parse_selftest_args(["Peregon.exe", "--selftest", "st"])
    assert folder.is_absolute() and folder == tmp_path / "st"


@pytest.mark.parametrize(
    "args",
    [
        ["--selftest"],  # без папки
        ["--selftest", ""],  # пустая папка
        ["--selftest", "   "],
        ["--selftest", "a", "b"],  # лишний аргумент
        ["x.png", "--selftest", "a"],  # не первым аргументом
    ],
)
def test_selftest_args_invalid(args):
    with pytest.raises(selftest.SelftestUsageError):
        selftest.parse_selftest_args(["Peregon.exe", *args])


def test_selftest_folder_checks(tmp_path):
    selftest.check_folder(tmp_path / "missing")  # не существует — можно
    selftest.check_folder(tmp_path)  # пустая — можно
    (tmp_path / "extra").mkdir()
    selftest.check_folder(tmp_path)  # только extra — можно
    (tmp_path / "user.txt").write_text("x", encoding="utf-8")
    with pytest.raises(selftest.SelftestUsageError):
        selftest.check_folder(tmp_path)
    with pytest.raises(selftest.SelftestUsageError):
        selftest.check_folder(tmp_path / "user.txt")  # файл, а не папка


def test_selftest_nonempty_folder_untouched(tmp_path):
    (tmp_path / "keep.txt").write_text("мой файл", encoding="utf-8")
    assert selftest.main(tmp_path) == selftest.EXIT_USAGE
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt"]


def _patch_main(monkeypatch, calls):
    import multiprocessing

    import konverter.core.temp_journal as tj
    import konverter.ui.main_window as mw

    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: calls.append("freeze"))
    monkeypatch.setattr(logging_setup, "setup_logging", lambda: calls.append("log"))
    monkeypatch.setattr(tj, "cleanup_orphans", lambda: calls.append("orphans") or 0)
    monkeypatch.setattr(mw, "run", lambda: calls.append("gui") or 0)


def test_main_selftest_skips_gui(monkeypatch, tmp_path):
    calls: list = []
    _patch_main(monkeypatch, calls)
    monkeypatch.setattr(selftest, "main", lambda folder: calls.append(("selftest", folder)) or 0)
    monkeypatch.setattr(sys, "argv", ["Peregon.exe", "--selftest", str(tmp_path)])
    with pytest.raises(SystemExit) as info:
        konverter.main()
    assert info.value.code == 0
    assert calls == ["freeze", "log", ("selftest", tmp_path.absolute())]


def test_main_selftest_bad_args_exit_2(monkeypatch):
    calls: list = []
    _patch_main(monkeypatch, calls)
    monkeypatch.setattr(sys, "argv", ["Peregon.exe", "--selftest"])
    with pytest.raises(SystemExit) as info:
        konverter.main()
    assert info.value.code == selftest.EXIT_USAGE
    assert "gui" not in calls


def test_selftest_end_to_end(tmp_path, monkeypatch, qapp):
    """Самопроверка из исходников на двух файлах (PNG → JPG, WAV → MP3, дочерний процесс)."""
    monkeypatch.setattr(paths, "app_root", lambda: tmp_path / "app")
    monkeypatch.setattr(
        selftest, "CASES", [c for c in selftest.CASES if c[0] in {"picture.png", "tone.wav"}]
    )
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    folder = tmp_path / "st"
    code = selftest.run_selftest(folder)
    import json

    report = json.loads((folder / selftest.REPORT_NAME).read_text(encoding="utf-8"))
    assert [c["status"] for c in report["cases"]] == ["done", "done"], report["cases"]
    assert report["child"]["ok"] and report["child"]["qt_gui_modules_in_child"] == []
    assert report["paths"]["ok"] and report["qt"]["icon_ok"]
    assert code == selftest.EXIT_OK, report
    assert sorted(p.name for p in folder.iterdir()) == ["in", "out", "report.json"]


# --- Пути в собранной программе (sys.frozen) -------------------------------


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """Имитация собранной программы: exe в tmp_path/Peregon/Peregon.exe."""
    app = tmp_path / "Peregon"
    (app / "_internal").mkdir(parents=True)
    exe = app / "Peregon.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(app / "_internal"), raising=False)
    return app


def test_frozen_app_root_is_exe_folder(frozen):
    assert paths.app_root() == frozen.resolve()
    assert paths.data_dir_path() == frozen.resolve() / "data"
    assert "_internal" not in paths.data_dir_path().parts


def test_frozen_data_created_on_demand(frozen):
    assert not (frozen / "data").exists()
    assert paths.logs_dir() == frozen.resolve() / "data" / "logs"
    assert (frozen / "data" / "logs").is_dir()
    assert paths.temp_dir() == frozen.resolve() / "data" / "temp"


def test_frozen_libreoffice_module_next_to_exe(frozen):
    from konverter.libreoffice import module

    assert module.module_path() == frozen.resolve() / "data" / "modules" / "libreoffice"
    assert not module.is_installed()


# --- Иконка ----------------------------------------------------------------


def test_icon_ico_sizes():
    from PIL import Image

    ico = PACKAGING / "icon.ico"
    assert ico.is_file()
    with Image.open(ico) as img:
        sizes = set(img.info["sizes"])
    assert {(s, s) for s in (16, 24, 32, 48, 64, 128, 256)} <= sizes


def test_make_icon_renders_each_size(tmp_path, monkeypatch):
    import importlib.util

    from PIL import Image

    spec = importlib.util.spec_from_file_location("make_icon", PACKAGING / "make_icon.py")
    make_icon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_icon)
    out = make_icon.make_icon(ico_path=tmp_path / "icon.ico")
    with Image.open(out) as img:
        assert set(img.info["sizes"]) == {(s, s) for s in make_icon.SIZES}
        small = img.ico.getimage((16, 16)).convert("RGBA")
        assert small.size == (16, 16)
        assert small.getpixel((8, 8))[3] == 255  # центр значка непрозрачен


def test_app_icon_found_relative_to_module(qapp):
    from konverter.ui import common

    path = common.app_icon_path()
    assert path is not None and path.name == "icon.svg"
    # В сборке модули лежат в _internal/konverter/..., и assets кладутся туда же.
    assert path.parent == Path(common.__file__).resolve().parent / "assets"
    icon = common.load_app_icon()
    assert icon is not None and not icon.pixmap(32, 32).isNull()


def test_spec_bundles_assets_and_icon():
    text = (PACKAGING / "konverter.spec").read_text(encoding="utf-8")
    assert '"konverter/ui/assets"' in text
    assert "icon.ico" in text and "console=False" in text and 'APP_NAME = "Peregon"' in text
    assert '"py7zr"' in text and 'collect_data_files("magika")' in text


def test_entry_calls_freeze_support_first():
    text = (PACKAGING / "entry.py").read_text(encoding="utf-8")
    code = text.split('"""', 2)[2]  # без строки документации
    assert code.index("multiprocessing.freeze_support()") < code.index("from konverter import main")


def test_app_user_model_id(monkeypatch):
    from konverter.ui import main_window

    if sys.platform != "win32":
        assert main_window.set_app_user_model_id() is False
        return
    seen = []

    class _Shell:
        class SetCurrentProcessExplicitAppUserModelID:  # noqa: N801 — имя функции WinAPI
            argtypes = None

            def __new__(cls, app_id):
                seen.append(app_id)
                return 0

    import ctypes

    monkeypatch.setattr(ctypes, "WinDLL", lambda name: _Shell)
    assert main_window.set_app_user_model_id() is True
    assert seen == [main_window.APP_USER_MODEL_ID]


def test_probe_reports_no_qt_in_child():
    from konverter.converters import _isolated
    from konverter.converters.base import CancelToken

    info = _isolated.run_isolated("probe", {}, cancel=CancelToken(), timeout=60)
    assert info["pyside"] is False and info["qt_modules"] == [] and info["qt_app"] is False


def test_visible_name_is_peregon():
    from konverter.strings import tr
    from konverter.ui import main_window

    assert tr("app_title") == "Peregon"
    assert "Peregon" in main_window.APP_USER_MODEL_ID
    text = (PACKAGING / "konverter.spec").read_text(encoding="utf-8")
    assert '"CompanyName", "XClassiK"' in text and '"ProductName", APP_NAME' in text
    assert '"FileDescription", "Peregon — конвертер файлов"' in text
