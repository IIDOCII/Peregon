"""Длинные пути (> 260 символов) в деревьях, которые создаёт LibreOffice.

Ошибка с другого ПК: профиль LibreOffice содержит папки вида
`user/extensions/bundled/registry/...help.PackageRegistryBackend/luXXXX.tmp/da/caption/...`;
при длинном пути к программе `shutil.copytree` профиля-шаблона падал
с [WinError 3]. Тесты проверяют работу новых функций (расширенный путь
`\\\\?\\`), а не падение старых: при включённом LongPathsEnabled обычные
функции тоже могут работать.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from konverter import paths
from konverter.core import naming
from konverter.libreoffice import module, soffice
from konverter.strings import tr

windows_only = pytest.mark.skipif(os.name != "nt", reason="длинные пути — особенность Windows")

# Хвост, как в реальном профиле LibreOffice (относительно папки профиля).
LO_TAIL = (
    "user", "extensions", "bundled", "registry",
    "com.sun.star.comp.deployment.help.PackageRegistryBackend",
    "lu1379666b0zq.tmp", "da", "caption",
)
LO_FILE = "org.openoffice.da.hunspell.dictionaries%2Fpage1.xhp"


def _lp(path: Path | str) -> str:
    return paths.long_path(path)


def _exists(path: Path | str) -> bool:
    return os.path.lexists(_lp(path))


def _make_profile(root: Path, pad: int = 3) -> Path:
    """Профиль с путём файла > 300 символов; возвращает путь к файлу."""
    folder = Path(root)
    for index in range(pad):
        folder = folder / (f"pad{index}-" + "x" * 40)
    folder = folder.joinpath(*LO_TAIL)
    os.makedirs(_lp(folder), exist_ok=True)
    target = folder / LO_FILE
    with open(_lp(target), "w", encoding="utf-8") as fh:
        fh.write("<help/>")
    assert len(str(target)) > 300
    return target


# --- paths.long_path --------------------------------------------------------


@windows_only
def test_long_path_format():
    assert paths.long_path(r"C:\a\b\..\c/d") == "\\\\?\\C:\\a\\c\\d"
    assert paths.long_path("\\\\?\\C:\\x") == "\\\\?\\C:\\x"
    assert paths.long_path(r"\\server\share\dir") == "\\\\?\\UNC\\server\\share\\dir"
    assert paths.long_path(Path("rel")).startswith("\\\\?\\")
    assert os.path.isabs(paths.long_path("rel")[4:])


def test_long_path_not_windows(monkeypatch):
    monkeypatch.setattr(paths, "_IS_WINDOWS", False)
    assert paths.long_path(Path("a") / "b") == os.fspath(Path("a") / "b")


# --- копирование / удаление -------------------------------------------------


@windows_only
def test_copy_and_remove_long_tree(tmp_path):
    src = tmp_path / "src"
    deep = _make_profile(src)
    dst = tmp_path / ("dst-" + "y" * 30)
    soffice.copy_tree(src, dst)
    copied = dst / deep.relative_to(src)
    assert len(str(copied)) > 300
    with open(_lp(copied), encoding="utf-8") as fh:
        assert fh.read() == "<help/>"
    soffice.remove_tree(dst)
    soffice.remove_tree(src)
    assert not _exists(dst) and not _exists(src)


@windows_only
def test_cleanup_stale_removes_long_tree(tmp_path, monkeypatch):
    root = tmp_path / "app"
    monkeypatch.setattr(paths, "app_root", lambda: root)
    dead = root / "data" / "temp" / "lo-run-999999-abcdef123456"
    _make_profile(dead / "profile")
    assert module.cleanup_stale() == 1
    assert not _exists(dead)


@windows_only
def test_naming_own_temp_dir_long_tree(tmp_path):
    temp = naming.make_temp_dir(tmp_path)
    _make_profile(temp)
    assert naming.cleanup(temp)
    assert not _exists(temp)


@windows_only
def test_naming_orphan_long_tree(tmp_path):
    """Осиротевшая папка (из журнала другого процесса) с длинной вложенной папкой нашего формата."""
    orphan = tmp_path / naming._temp_name("")
    inner = orphan
    for _ in range(6):
        inner = inner / naming._temp_name("")
    os.makedirs(_lp(inner))
    assert len(str(inner)) > 260
    assert naming.remove_orphan(orphan)
    assert not _exists(orphan)


# --- профиль-шаблон ---------------------------------------------------------


def _fake_convert(make_long: bool):
    def fake(soffice_exe, work, source, fmt, kind, **kw):
        profile = Path(work) / "profile"
        if make_long and os.name == "nt":
            _make_profile(profile, pad=0)
        else:
            (profile / "user").mkdir(parents=True)
        (profile / "user" / "registrymodifications.xcu").write_text("<x/>", encoding="utf-8")
        for name in ("cache", "crash"):
            (profile / name).mkdir(exist_ok=True)
            (profile / name / "f.bin").write_bytes(b"1")
        (profile / ".lock").write_text("lock", encoding="utf-8")
        out = Path(work) / "out"
        out.mkdir(exist_ok=True)
        produced = out / "input.pdf"
        produced.write_bytes(b"%PDF-1.7\n")
        return produced

    return fake


def test_profile_template_skips_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(soffice, "convert", _fake_convert(make_long=False))
    temp = tmp_path / "temp"
    temp.mkdir()
    keep = tmp_path / "root" / module.PROFILE_TEMPLATE_NAME
    module.trial_conversion(Path("soffice.exe"), temp, lambda: None, keep_profile=keep)
    assert (keep / "user" / "registrymodifications.xcu").is_file()
    for skipped in ("cache", "crash", ".lock"):
        assert not (keep / skipped).exists(), skipped
    assert list(temp.iterdir()) == []


@windows_only
def test_trial_conversion_long_paths(tmp_path, monkeypatch):
    """Как на ПК пользователя: и папка работы, и папка шаблона — с путями > 260."""
    monkeypatch.setattr(soffice, "convert", _fake_convert(make_long=True))
    base = tmp_path / ("p" * 60) / ("q" * 60)
    temp = base / "data" / "temp"
    os.makedirs(_lp(temp))
    keep = base / "data" / "temp" / "lo-unpack-1-abcdef123456" / "root" / module.PROFILE_TEMPLATE_NAME
    module.trial_conversion(Path("soffice.exe"), temp, lambda: None, keep_profile=keep)
    copied = keep.joinpath(*LO_TAIL, LO_FILE)
    assert len(str(copied)) > 300 and os.path.isfile(_lp(copied))
    soffice.remove_tree(keep.parent.parent)
    assert os.listdir(_lp(temp)) == []


def test_profile_copy_error_is_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(soffice, "convert", _fake_convert(make_long=False))
    raw = "[WinError 3] Системе не удается найти указанный путь"

    def broken(src, dst, **kw):
        os.makedirs(dst)
        Path(dst, "partial.txt").write_text("x", encoding="utf-8")
        raise shutil.Error([(str(src) + "\\a", str(dst) + "\\a", raw)])

    monkeypatch.setattr(soffice.shutil, "copytree", broken)
    temp = tmp_path / "temp"
    temp.mkdir()
    keep = tmp_path / "root" / module.PROFILE_TEMPLATE_NAME
    with pytest.raises(module.InstallError) as info:
        module.trial_conversion(Path("soffice.exe"), temp, lambda: None, keep_profile=keep)
    text = str(info.value)
    assert text == tr("office_err_profile_copy")
    assert "WinError" not in text and "(" not in text.split(".")[0]
    assert not keep.exists()  # частичная копия убрана
    assert list(temp.iterdir()) == []


def test_unexpected_shutil_error_is_summarized():
    exc = shutil.Error([("a", "b", "[WinError 3] нет пути"), ("c", "d", "[WinError 3] нет пути")])
    text = module._error_text(exc)
    assert "WinError 3" in text and "2" in text and "('a'" not in text


def test_failed_install_leaves_no_garbage(tmp_path, monkeypatch):
    """Ошибка копирования профиля: в data/temp нет остатков, модуль не появился."""
    root = tmp_path / "app"
    monkeypatch.setattr(paths, "app_root", lambda: root)
    temp = module.temp_root()

    class Plan:
        files = [1]
        product_version = "26.2.7.1"

    def fake_unpack(msi_path, work, **kw):
        install = work / "root"
        (install / "program").mkdir(parents=True)
        (install / "program" / "soffice.exe").write_bytes(b"MZ")
        return install, Plan(), []

    def broken(src, dst, **kw):
        os.makedirs(dst)
        raise shutil.Error([("a", "b", "[WinError 3] нет пути")])

    monkeypatch.setattr(module, "unpack_msi", fake_unpack)
    monkeypatch.setattr(soffice, "convert", _fake_convert(make_long=True))
    monkeypatch.setattr(soffice.shutil, "copytree", broken)
    release = module.download.Release((26, 2, 7), "u", "x.msi", 1, "0" * 64, "")
    with pytest.raises(module.InstallError) as info:
        module._unpack_and_check(
            tmp_path / "x.msi", release, temp, module._Phases(None), lambda: False,
            module.download.Policy(),
        )
    assert str(info.value) == tr("office_err_profile_copy")
    assert os.listdir(_lp(temp)) == []
    assert not module.is_installed()
    assert not (root / "data" / "modules" / module.MODULE_DIR_NAME).exists()


def test_convert_survives_broken_template(tmp_path, monkeypatch):
    """Шаблон не скопировался — конвертация идёт с новым профилем, без остатков копии."""
    template = tmp_path / "template"
    (template / "user").mkdir(parents=True)
    (template / "user" / "keep.txt").write_text("x", encoding="utf-8")

    def broken(src, dst, **kw):
        os.makedirs(dst)
        Path(dst, "partial.txt").write_text("x", encoding="utf-8")
        raise shutil.Error([("a", "b", "[WinError 3] нет пути")])

    seen = {}

    def fake_run(args, **kw):
        work = Path(kw["cwd"])
        seen["profile"] = sorted(p.name for p in (work / "profile").iterdir())
        (work / "out" / "input.pdf").write_bytes(b"%PDF-1.7\n")

        class R:
            timed_out = False
            returncode = 0

        return R()

    monkeypatch.setattr(soffice.shutil, "copytree", broken)
    monkeypatch.setattr(soffice.jobobject, "run", fake_run)
    work = tmp_path / "work"
    work.mkdir()
    source = tmp_path / "a.txt"
    source.write_text("x", encoding="utf-8")
    soffice.convert(Path("soffice.exe"), work, source, "txt", "pdf", template=template)
    assert seen["profile"] == ["user"]
    assert not (work / "profile" / "partial.txt").exists()
