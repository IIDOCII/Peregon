"""Модуль LibreOffice: исправления по ревью 1 этапа 5 (без сети).

Откат на старую подписанную версию, ошибки цепочки Authenticode, сбои
http.client и диска, неверный хэш на одном зеркале, две копии программы,
отмена во время выбора версии и WinVerifyTrust, тексты из strings.py.
"""

from __future__ import annotations

import errno
import hashlib
import json
import time
from pathlib import Path

import pytest

from konverter import paths
from konverter.libreoffice import authenticode, download, module, msi
from konverter.strings import tr

from test_libreoffice import (  # noqa: F401 — фикстуры и помощники загрузчика
    HELPPACK,
    MSI_NAME,
    PAYLOAD,
    VERSION_DIR,
    _meta4,
    _policy,
    needs_helppack,
    server,
)


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(paths, "app_root", lambda: root)
    return root


def _release(version=(26, 2, 6)) -> download.Release:
    text = ".".join(map(str, version))
    return download.Release(version, "https://x/y.msi", f"LibreOffice_{text}_Win_x86-64.msi",
                            10, "0" * 64, "")


# --- 1. Версия и название внутри msi; устаревший модуль ---------------------------------


@pytest.mark.parametrize("name,version,ok", [
    ("LibreOffice 26.2", "26.2.6.3", True),
    ("LibreOffice", "26.2.6", True),
    ("LibreOffice 7.6", "7.6.4.1", False),          # откат на старую подписанную сборку
    ("LibreOffice 26.2", "26.2.5.2", False),        # не та версия, что выбрана
    ("LibreOffice 26.2", "26.2.60.1", False),       # совпадение префикса без точки
    ("LibreOffice 26.2 Help Pack (Russian)", "26.2.6.3", False),
    ("LibreOffice 26.2 SDK", "26.2.6.3", False),
    ("OpenOffice", "26.2.6.3", False),
    ("LibreOffice", "", False),
])
def test_check_product_version_inside_msi(name, version, ok):
    plan = msi.Plan(files=[], cabinets=[], product_version=version, product_name=name)
    if ok:
        module.check_product(plan, _release(), (26, 2, 6))
    else:
        with pytest.raises(module.InstallError):
            module.check_product(plan, _release(), (26, 2, 6))


def test_check_product_below_minimum():
    old = msi.Plan(files=[], cabinets=[], product_version="26.2.5.1", product_name="LibreOffice")
    with pytest.raises(module.InstallError):
        module.check_product(old, _release((26, 2, 5)), (26, 2, 6))


def test_install_rejects_old_signed_msi_before_unpack(app_dir, monkeypatch):
    """Подписи верны, но внутри msi старая версия — отказ до распаковки, файл удалён."""
    release = _release((26, 2, 7))
    monkeypatch.setattr(download, "resolve_release", lambda *a, **k: release)
    saved: list[Path] = []

    def fake_download(rel, folder, **kw):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / rel.file_name
        path.write_bytes(b"msi")
        saved.append(path)
        return path

    monkeypatch.setattr(download, "download_release", fake_download)
    monkeypatch.setattr(module, "verify_authenticity", lambda *a, **k: None)
    monkeypatch.setattr(msi, "read_plan", lambda path: msi.Plan(
        files=[], cabinets=["x.cab"], product_version="7.6.4.1", product_name="LibreOffice 7.6"))
    monkeypatch.setattr(msi, "extract_cabinets", lambda *a, **k: pytest.fail("распаковка"))
    with pytest.raises(module.InstallError) as info:
        module.install()
    assert str(info.value) == tr("office_err_product")
    assert saved and not saved[0].exists()


@needs_helppack
def test_real_helppack_is_not_main_product():
    plan = msi.read_plan(HELPPACK)
    assert plan.product_name.startswith("LibreOffice") and plan.product_version == "26.2.6.3"
    with pytest.raises(module.InstallError):
        module.check_product(plan, _release(), (26, 2, 6))


def fake_module(app_dir: Path, version: str) -> Path:
    root = app_dir / "data" / "modules" / module.MODULE_DIR_NAME
    (root / "program").mkdir(parents=True)
    (root / "program" / "soffice.exe").write_bytes(b"x")
    (root / module.MARKER_NAME).write_text(json.dumps({"version": version}), encoding="utf-8")
    return root


def test_outdated_module_is_not_installed(app_dir):
    fake_module(app_dir, "7.6.4")
    assert not module.is_installed() and module.is_outdated()
    assert module.availability() == "outdated"
    assert "7.6.4" in module.missing_message() and "26.2.6" in module.missing_message()
    assert "7.6.4" in module.ask_prefix()


def test_broken_marker_is_outdated(app_dir):
    root = fake_module(app_dir, "26.2.6")
    (root / module.MARKER_NAME).write_text("{не json", encoding="utf-8")
    assert not module.is_installed() and module.availability() == "outdated"


def test_valid_module_installed(app_dir):
    fake_module(app_dir, "26.2.6")
    assert module.is_installed() and module.availability() == "installed"
    assert module.ask_prefix() == ""


# --- 2. Authenticode: ошибки цепочки отдельно ------------------------------------------


def test_authenticode_chain_errors_separate():
    assert isinstance(authenticode.error_for_status(0x800B0109), authenticode.ChainError)
    assert isinstance(authenticode.error_for_status(-2146762487), authenticode.ChainError)
    assert isinstance(authenticode.error_for_status(0x800B010A), authenticode.ChainError)
    bad = authenticode.error_for_status(0x80096010)  # TRUST_E_BAD_DIGEST — подмена
    assert not isinstance(bad, authenticode.ChainError)
    assert not hasattr(authenticode, "_WTD_CACHE_ONLY_URL_RETRIEVAL")


def _pgp_ok(monkeypatch) -> None:
    monkeypatch.setattr(module.openpgp, "load_keyring", lambda *a: [])
    monkeypatch.setattr(module.openpgp, "verify_file",
                        lambda *a, **k: type("Key", (), {"hex": "X"})())


def test_chain_error_message_differs_from_tampering(tmp_path, monkeypatch):
    _pgp_ok(monkeypatch)

    def chain(path):
        raise authenticode.ChainError("WinVerifyTrust: 0x800B0109")

    monkeypatch.setattr(authenticode, "verify", chain)
    with pytest.raises(module.InstallError) as info:
        module.verify_authenticity(tmp_path / "f.msi", "sig")
    assert str(info.value) == tr("office_err_chain") != tr("office_err_authenticode")


# --- 6. Отмена во время WinVerifyTrust и выбора версии ---------------------------------


def test_authenticode_cancel_is_prompt(tmp_path, monkeypatch):
    _pgp_ok(monkeypatch)
    target = tmp_path / "f.msi"
    target.write_bytes(b"x")

    def slow(path):
        time.sleep(1.5)
        return "The Document Foundation"

    monkeypatch.setattr(authenticode, "verify", slow)
    t0 = time.monotonic()
    with pytest.raises(module.InstallCancelled):
        module.verify_authenticity(target, "sig", lambda: time.monotonic() - t0 > 0.2)
    assert time.monotonic() - t0 < 1.0
    deadline = time.monotonic() + 5
    while target.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not target.exists()  # поток удалил файл, дождавшись WinVerifyTrust


def test_resolve_release_cancel_before_requests(server):
    with pytest.raises(download.DownloadCancelled):
        download.resolve_release(_policy(server), cancelled=lambda: True)
    assert server.requests == []


def test_metadata_timeout_shorter():
    assert download.Policy().metadata_timeout < download.Policy().timeout


# --- 3, 4. Сбои зеркал, диска и неверный хэш -------------------------------------------


def _two_mirrors(server, first_handler):
    sha = hashlib.sha256(PAYLOAD).hexdigest()
    base = VERSION_DIR + MSI_NAME
    m1, m2 = server.base + "/m1/" + MSI_NAME, server.base + "/m2/" + MSI_NAME
    server.routes[base + ".meta4"] = (200, {}, _meta4(MSI_NAME, len(PAYLOAD), sha, [m1, m2]))
    server.routes[base] = (302, {"Location": m1}, b"")
    server.routes["/m1/" + MSI_NAME] = first_handler
    server.routes["/m2/" + MSI_NAME] = server.file_route(PAYLOAD)


def _raw(data: bytes):
    def handler(h) -> None:
        h.close_connection = True
        try:
            h.wfile.write(data)
            h.wfile.flush()
        except OSError:
            pass

    return handler


@pytest.mark.parametrize("raw", [
    b"GARBAGE\r\n\r\n",                                                     # BadStatusLine
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\nabc",      # IncompleteRead
    b"HTTP/1.1 200 " + b"x" * 70000 + b"\r\n\r\n",                          # LineTooLong
], ids=["bad_status", "incomplete", "line_too_long"])
def test_http_client_errors_go_to_next_mirror(server, tmp_path, raw):
    _two_mirrors(server, _raw(raw))
    policy = _policy(server)
    release = download.resolve_release(policy)
    path = download.download_release(release, tmp_path, policy=policy)
    assert path.read_bytes() == PAYLOAD


def test_http_client_error_in_metadata_is_download_error(server):
    server.routes["/libreoffice/stable/"] = _raw(b"GARBAGE\r\n\r\n")
    with pytest.raises(download.DownloadError) as info:
        download.resolve_release(_policy(server))
    assert info.value.user == tr("office_err_no_network")


def test_disk_full_is_clear_error(server, tmp_path, monkeypatch):
    class Full:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(download, "_open_part", lambda path, mode: Full())
    policy = _policy(server)
    release = download.resolve_release(policy)
    with pytest.raises(download.DownloadError) as info:
        download.download_release(release, tmp_path, policy=policy)
    assert info.value.user == tr("office_err_disk_full")
    assert list(tmp_path.iterdir()) == []


def _tampered() -> bytes:
    data = bytearray(PAYLOAD)
    data[5] ^= 1
    return bytes(data)


def test_bad_hash_tries_next_mirror(server, tmp_path):
    _two_mirrors(server, server.file_route(_tampered()))
    policy = _policy(server)
    release = download.resolve_release(policy)
    assert download.download_release(release, tmp_path, policy=policy).read_bytes() == PAYLOAD


def test_bad_hash_everywhere_limited(server, tmp_path):
    _two_mirrors(server, server.file_route(_tampered()))
    server.routes["/m2/" + MSI_NAME] = server.file_route(_tampered())
    policy = _policy(server)
    release = download.resolve_release(policy)
    with pytest.raises(download.DownloadError) as info:
        download.download_release(release, tmp_path, policy=policy)
    assert info.value.user == tr("office_err_hash")
    assert list(tmp_path.iterdir()) == []
    fetched = [p for p, _h in server.requests if p.startswith(("/m1/", "/m2/"))]
    assert len(fetched) == download.MAX_HASH_FAILURES


# --- 5. Две копии программы ------------------------------------------------------------


def test_install_blocked_by_other_process_lock(app_dir, monkeypatch):
    monkeypatch.setattr(download, "resolve_release", lambda *a, **k: pytest.fail("сеть"))
    with module._process_lock(module.temp_root()):
        with pytest.raises(module.InstallError) as info:
            module.install()
    assert str(info.value) == tr("office_err_busy_other")


def test_activate_keeps_valid_module(app_dir):
    target = fake_module(app_dir, "26.2.6")
    (target / "busy.txt").write_text("используется другой копией", encoding="utf-8")
    new = app_dir / "data" / "temp" / "new-root"
    (new / "program").mkdir(parents=True)
    module._activate(new, app_dir / "data" / "temp")
    assert (target / "busy.txt").exists() and not new.exists()


def test_activate_replaces_outdated_module(app_dir):
    target = fake_module(app_dir, "7.6.4")
    new = app_dir / "data" / "temp" / "new-root"
    (new / "program").mkdir(parents=True)
    (new / "program" / "marker").write_text("new", encoding="utf-8")
    module._activate(new, app_dir / "data" / "temp")
    assert (target / "program" / "marker").exists()
    assert not (target / module.MARKER_NAME).exists()


# --- 8. Тексты --------------------------------------------------------------------------


def test_user_texts_come_from_strings():
    assert module.MSG_NOT_INSTALLED == tr("office_refused")
    assert module.MSG_NOT_WRITABLE == tr("office_not_writable")
    assert download.DownloadError("журнал").user == tr("office_err_bad_data")
