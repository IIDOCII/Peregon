"""Модуль LibreOffice без сети (этап 5): подпись OpenPGP, загрузчик, msi, профиль,
объект задания, Authenticode, установка (П20, П21).

Сеть не нужна: загрузчик проверяется на локальном HTTP-сервере (политика
`download.Policy` разрешает http только в тестах), ключи OpenPGP генерируются
в тесте. Тесты на настоящих файлах TDF (helppack_ru.msi и его .asc в
`data/testdata/`, скачанные загрузчиком программы) пропускаются, если файлов нет.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest
from Cryptodome.Hash import SHA1, SHA256, SHA512
from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import pkcs1_15
from defusedxml import ElementTree as SafeET

from konverter import paths
from konverter.libreoffice import (
    _tdf_key,
    authenticode,
    download,
    jobobject,
    module,
    msi,
    openpgp,
    soffice,
)

ROOT = Path(__file__).resolve().parents[1]
TESTDATA = ROOT / "data" / "testdata"
HELPPACK = TESTDATA / "LibreOffice_26.2.6_Win_x86-64_helppack_ru.msi"
HELPPACK_ASC = HELPPACK.with_name(HELPPACK.name + ".asc")
needs_helppack = pytest.mark.skipif(
    not (HELPPACK.is_file() and HELPPACK_ASC.is_file()),
    reason="нет data/testdata/…helppack_ru.msi (скачивается загрузчиком программы)",
)


# =============================================================================
# OpenPGP: построитель ключей и подписей для тестов
# =============================================================================

NOW = 1_790_000_000


def _mpi(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return struct.pack(">H", value.bit_length()) + raw


def _packet(tag: int, body: bytes) -> bytes:
    return bytes([0xC0 | tag, 255]) + struct.pack(">I", len(body)) + body


def _sub(kind: int, data: bytes, critical: bool = False) -> bytes:
    return bytes([len(data) + 1, kind | (0x80 if critical else 0)]) + data


def _key_body(key, created: int = NOW - 10_000) -> bytes:
    return b"\x04" + struct.pack(">I", created) + b"\x01" + _mpi(key.n) + _mpi(key.e)


def _fpr(body: bytes) -> bytes:
    return hashlib.sha1(b"\x99" + struct.pack(">H", len(body)) + body).digest()


def _key_prefix(body: bytes) -> bytes:
    return b"\x99" + struct.pack(">H", len(body)) + body


_HASH = {8: SHA256, 10: SHA512, 2: SHA1}


def _signature(priv, sig_type: int, prefix: bytes, *, issuer: bytes, hash_algo: int = 8,
               created: int = NOW - 100, extra: tuple[bytes, ...] = (), issuer_in_hashed=True) -> bytes:
    hashed = _sub(2, struct.pack(">I", created)) + b"".join(extra)
    unhashed = b""
    if issuer_in_hashed:
        hashed += _sub(33, b"\x04" + issuer)
    else:
        unhashed = _sub(16, issuer[-8:])
    head = bytes([4, sig_type, 1, hash_algo]) + struct.pack(">H", len(hashed)) + hashed
    h = _HASH[hash_algo].new()
    h.update(prefix)
    h.update(head)
    h.update(b"\x04\xff" + struct.pack(">I", len(head)))
    sig = pkcs1_15.new(priv).sign(h)
    body = head + struct.pack(">H", len(unhashed)) + unhashed + h.digest()[:2]
    body += _mpi(int.from_bytes(sig, "big"))
    return _packet(2, body)


def _armor(kind: str, data: bytes) -> str:
    text = base64.b64encode(data).decode()
    lines = [text[i:i + 64] for i in range(0, len(text), 64)]
    crc = base64.b64encode(openpgp._crc24(data).to_bytes(3, "big")).decode()
    return f"-----BEGIN PGP {kind}-----\nComment: test\n\n" + "\n".join(lines) + \
        f"\n={crc}\n-----END PGP {kind}-----\n"


@pytest.fixture(scope="module")
def rsa_keys():
    return [RSA.generate(2048) for _ in range(3)]


def _public_block(primary, subkeys=()) -> tuple[str, bytes]:
    """(броня открытого ключа, отпечаток основного). subkeys — [(ключ, флаги|None, revoke)]."""
    pbody = _key_body(primary)
    pfpr = _fpr(pbody)
    data = _packet(6, pbody)
    for sub, flags, revoked in subkeys:
        sbody = _key_body(sub)
        data += _packet(14, sbody)
        prefix = _key_prefix(pbody) + _key_prefix(sbody)
        if flags is not None:
            data += _signature(primary, 0x18, prefix, issuer=pfpr, extra=(_sub(27, bytes([flags])),))
        if revoked:
            data += _signature(primary, 0x28, prefix, issuer=pfpr)
    return _armor("PUBLIC KEY BLOCK", data), pfpr


def _sign_doc(priv, data: bytes, issuer: bytes, **kw) -> str:
    return _armor("SIGNATURE", _signature(priv, 0x00, data, issuer=issuer, **kw))


DOC = b"LibreOffice_test.msi contents " * 1000


def test_pgp_own_key_signature_verifies_with_injected_key(rsa_keys):
    armored, fpr = _public_block(rsa_keys[0])
    keys = openpgp.load_keyring(armored, fpr.hex())
    sig = _sign_doc(rsa_keys[0], DOC, fpr)
    assert openpgp.verify_bytes(DOC, sig, keys, now=NOW).fingerprint == fpr
    # Только ID ключа в нехэшированной части — тоже находится.
    sig2 = _sign_doc(rsa_keys[0], DOC, fpr, issuer_in_hashed=False)
    assert openpgp.verify_bytes(DOC, sig2, keys, now=NOW).fingerprint == fpr
    # SHA-512 поддерживается.
    sig3 = _sign_doc(rsa_keys[0], DOC, fpr, hash_algo=10)
    assert openpgp.verify_bytes(DOC, sig3, keys, now=NOW).fingerprint == fpr


def test_pgp_foreign_key_rejected_by_tdf_keyring(rsa_keys):
    """Подпись своим ключом НЕ принимается: доверен только зашитый ключ TDF."""
    tdf = openpgp.load_keyring(_tdf_key.TDF_PUBLIC_KEY, _tdf_key.TDF_FINGERPRINT)
    _armored, fpr = _public_block(rsa_keys[0])
    with pytest.raises(openpgp.PgpError):
        openpgp.verify_bytes(DOC, _sign_doc(rsa_keys[0], DOC, fpr), tdf, now=NOW)
    # Подделка «издателя»: отпечаток TDF в подписи, а подписано своим ключом.
    forged = _sign_doc(rsa_keys[0], DOC, bytes.fromhex(_tdf_key.TDF_FINGERPRINT))
    with pytest.raises(openpgp.PgpError):
        openpgp.verify_bytes(DOC, forged, tdf, now=NOW)


def test_pgp_tampered_data_and_signature_rejected(rsa_keys):
    armored, fpr = _public_block(rsa_keys[0])
    keys = openpgp.load_keyring(armored, fpr.hex())
    sig = _sign_doc(rsa_keys[0], DOC, fpr)
    with pytest.raises(openpgp.PgpError):
        openpgp.verify_bytes(DOC + b"x", sig, keys, now=NOW)
    # Испорченная контрольная сумма брони.
    lines = sig.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("="))
    lines[index] = "=AAAA"
    with pytest.raises(openpgp.PgpError):
        openpgp.verify_bytes(DOC, "\n".join(lines), keys, now=NOW)


def test_pgp_rules(rsa_keys):
    armored, fpr = _public_block(rsa_keys[0])
    keys = openpgp.load_keyring(armored, fpr.hex())
    # SHA-1 не принимается.
    with pytest.raises(openpgp.PgpError):
        openpgp.verify_bytes(DOC, _sign_doc(rsa_keys[0], DOC, fpr, hash_algo=2), keys, now=NOW)
    # Неизвестный критичный подпакет.
    crit = _sign_doc(rsa_keys[0], DOC, fpr, extra=(_sub(100, b"x", critical=True),))
    with pytest.raises(openpgp.PgpError):
        openpgp.verify_bytes(DOC, crit, keys, now=NOW)
    # Истёкшая подпись (срок 10 с).
    expired = _sign_doc(rsa_keys[0], DOC, fpr, extra=(_sub(3, struct.pack(">I", 10)),))
    with pytest.raises(openpgp.PgpError):
        openpgp.verify_bytes(DOC, expired, keys, now=NOW)
    # Подпись не двоичного документа (класс 0x01 — текст).
    text_sig = _armor("SIGNATURE", _signature(rsa_keys[0], 0x01, DOC, issuer=fpr))
    with pytest.raises(openpgp.PgpError):
        openpgp.verify_bytes(DOC, text_sig, keys, now=NOW)
    # Неверный ожидаемый отпечаток ключа.
    with pytest.raises(openpgp.PgpError):
        openpgp.load_keyring(armored, "00" * 20)


def test_pgp_signing_subkey_support(rsa_keys):
    primary, good, bad = rsa_keys
    # Подключ с флагом «подпись» и привязкой — принимается.
    armored, pfpr = _public_block(primary, [(good, 0x02, False)])
    keys = openpgp.load_keyring(armored, pfpr.hex())
    assert len(keys) == 2
    gfpr = keys[1].fingerprint
    assert openpgp.verify_bytes(DOC, _sign_doc(good, DOC, gfpr), keys, now=NOW).fingerprint == gfpr
    # Без флага «подпись» (только шифрование), без привязки и отозванный — нет.
    for subkeys in ([(bad, 0x0C, False)], [(bad, None, False)], [(bad, 0x02, True)]):
        armored, pfpr = _public_block(primary, subkeys)
        keys = openpgp.load_keyring(armored, pfpr.hex())
        assert len(keys) == 1
        bfpr = _fpr(_key_body(bad))
        with pytest.raises(openpgp.PgpError):
            openpgp.verify_bytes(DOC, _sign_doc(bad, DOC, bfpr), keys, now=NOW)


def test_tdf_key_fingerprint_checked_by_code():
    keys = openpgp.load_keyring(_tdf_key.TDF_PUBLIC_KEY, _tdf_key.TDF_FINGERPRINT)
    assert keys[0].hex == "C2839ECAD9408FBE9531C3E9F434A1EFAFEEAEA3"
    assert keys[0].n.bit_length() == 4096
    assert len(keys) == 1  # подключ блока — для шифрования, к подписям не допущен


@needs_helppack
def test_real_tdf_signature_on_helppack():
    keys = openpgp.load_keyring(_tdf_key.TDF_PUBLIC_KEY, _tdf_key.TDF_FINGERPRINT)
    sig = HELPPACK_ASC.read_text(encoding="ascii")
    assert openpgp.verify_file(HELPPACK, sig, keys).hex == _tdf_key.TDF_FINGERPRINT
    data = bytearray(HELPPACK.read_bytes())
    data[len(data) // 2] ^= 0xFF
    with pytest.raises(openpgp.PgpError):
        openpgp.verify_bytes(bytes(data), sig, keys)


# =============================================================================
# Authenticode
# =============================================================================


@needs_helppack
def test_authenticode_helppack():
    assert authenticode.verify(HELPPACK) == "The Document Foundation"


def test_authenticode_rejects_unsigned_and_foreign(tmp_path):
    plain = tmp_path / "plain.msi"
    plain.write_bytes(b"not signed")
    with pytest.raises(authenticode.AuthenticodeError):
        authenticode.verify(plain)
    explorer = Path(r"C:\Windows\explorer.exe")
    if explorer.is_file():
        with pytest.raises(authenticode.AuthenticodeError):
            authenticode.verify(explorer)  # подписан, но не TDF


# =============================================================================
# Загрузчик: локальный HTTP-сервер
# =============================================================================

VERSION_DIR = "/libreoffice/stable/26.2.7/win/x86_64/"
MSI_NAME = "LibreOffice_26.2.7_Win_x86-64.msi"
PAYLOAD = bytes(range(256)) * 4096  # 1 МБ «msi»


class _Server:
    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.requests: list[tuple[str, dict]] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                server.requests.append((self.path, dict(self.headers)))
                route = server.routes.get(self.path)
                if route is None:
                    self.send_error(404)
                    return
                if callable(route):
                    route(self)
                    return
                status, headers, body = route
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def file_route(self, data: bytes):
        """Отдаёт `data` с поддержкой Range."""

        def handler(h) -> None:
            rng = h.headers.get("Range")
            if rng and rng.startswith("bytes="):
                start = int(rng[6:].split("-")[0])
                body = data[start:]
                h.send_response(206)
                h.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            else:
                body = data
                h.send_response(200)
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            try:
                h.wfile.write(body)
            except OSError:
                pass

        return handler

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _meta4(name: str, size: int, sha: str, urls: list[str]) -> bytes:
    url_xml = "".join(f'<url priority="{i + 1}">{u}</url>' for i, u in enumerate(urls))
    return (
        '<?xml version="1.0" encoding="UTF-8"?><metalink xmlns="urn:ietf:params:xml:ns:metalink">'
        f'<file name="{name}"><size>{size}</size><hash type="sha-256">{sha}</hash>{url_xml}</file>'
        "</metalink>"
    ).encode()


@pytest.fixture
def server():
    srv = _Server()
    listing = "".join(
        f'<a href="{v}/">{v}/</a>' for v in ("25.8.7", "26.2.5", "26.2.7", "26.8.0", "26.8.1")
    ).encode()
    srv.routes["/libreoffice/stable/"] = (200, {}, listing)
    sha = hashlib.sha256(PAYLOAD).hexdigest()
    base = VERSION_DIR + MSI_NAME
    srv.routes[base + ".sha256"] = (200, {}, f"{sha}  {MSI_NAME}\n".encode())
    srv.routes[base + ".meta4"] = (
        200, {}, _meta4(MSI_NAME, len(PAYLOAD), sha, [srv.base + "/mirror/" + MSI_NAME]),
    )
    srv.routes[base + ".asc"] = (200, {}, b"-----BEGIN PGP SIGNATURE-----\n...\n")
    # Сервер «TDF» отвечает редиректом на зеркало, как mirrorbrain.
    srv.routes[base] = (302, {"Location": srv.base + "/mirror/" + MSI_NAME}, b"")
    srv.routes["/mirror/" + MSI_NAME] = srv.file_route(PAYLOAD)
    yield srv
    srv.close()


def _policy(srv: _Server, **kw) -> download.Policy:
    values = dict(
        stable_url=srv.base + "/libreoffice/stable/",
        metadata_origin=srv.base,
        mirror_schemes=("http",),
        min_version=(26, 2, 6),
        timeout=5.0,
    )
    values.update(kw)
    return download.Policy(**values)


def test_choose_version_branches():
    versions = download.parse_versions(
        '<a href="25.8.7/"> <a href="26.2.5/"> <a href="26.2.6/"> <a href="26.8.0/"> <a href="26.8.1/">'
    )
    assert download.choose_version(versions, "still", (26, 2, 6)) == (26, 2, 6)
    assert download.choose_version(versions, "fresh", (26, 2, 6)) == (26, 8, 1)
    with pytest.raises(download.DownloadError):
        download.choose_version(versions, "still", (26, 2, 7))  # ниже минимальной — отказ
    assert download.BRANCH == "still" and download.MIN_VERSION == (26, 2, 6)


def test_download_ok_through_redirect(server, tmp_path):
    policy = _policy(server)
    release = download.resolve_release(policy)
    assert release.version == (26, 2, 7) and release.size == len(PAYLOAD)
    progress: list[float] = []
    path = download.download_release(release, tmp_path, policy=policy, progress=progress.append)
    assert path.read_bytes() == PAYLOAD
    assert progress[-1] == pytest.approx(1.0)
    assert sorted(p.name for p in tmp_path.iterdir()) == [MSI_NAME]


def test_http_redirect_rejected(server, tmp_path):
    """Редирект на http:// при политике «только https» — отказ, файл не создаётся."""
    policy = _policy(server, mirror_schemes=("https",))
    release = download.resolve_release(policy)
    assert release.mirrors == []  # http-зеркала из .meta4 отброшены
    with pytest.raises(download.DownloadError):
        download.download_release(release, tmp_path, policy=policy)
    assert not any(p.suffix in (".msi", ".part") for p in tmp_path.iterdir())
    assert any(path.startswith(VERSION_DIR) for path, _h in server.requests)
    assert not any(path.startswith("/mirror/") for path, _h in server.requests)


def test_metadata_redirect_rejected(server):
    base = VERSION_DIR + MSI_NAME
    server.routes[base + ".sha256"] = (302, {"Location": server.base + "/elsewhere"}, b"")
    with pytest.raises(download.DownloadError):
        download.resolve_release(_policy(server))


def test_metadata_only_from_tdf_origin(server):
    with pytest.raises(download.DownloadError):
        download.fetch_text("https://example.invalid/x.sha256", download.Policy())
    with pytest.raises(download.DownloadError):
        download.resolve_release(_policy(server, metadata_origin="https://download.documentfoundation.org"))


def test_tampered_payload_rejected(server, tmp_path):
    """П21: подменённый файл на зеркале (неверный SHA-256) — отказ, файл удалён."""
    tampered = bytearray(PAYLOAD)
    tampered[1000] ^= 1
    server.routes["/mirror/" + MSI_NAME] = server.file_route(bytes(tampered))
    policy = _policy(server)
    release = download.resolve_release(policy)
    with pytest.raises(download.DownloadError) as info:
        download.download_release(release, tmp_path, policy=policy)
    assert "подменён" in info.value.user
    assert list(tmp_path.iterdir()) == []


def test_sha_mismatch_between_metadata_rejected(server):
    base = VERSION_DIR + MSI_NAME
    server.routes[base + ".sha256"] = (200, {}, f"{'0' * 64}  {MSI_NAME}\n".encode())
    with pytest.raises(download.DownloadError):
        download.resolve_release(_policy(server))


def test_size_limit(server, tmp_path):
    with pytest.raises(download.DownloadError):
        download.resolve_release(_policy(server, max_bytes=len(PAYLOAD) - 1))
    # Сервер шлёт больше объявленного: отказ, файл удалён.
    policy = _policy(server)
    release = download.resolve_release(policy)
    server.routes["/mirror/" + MSI_NAME] = (200, {}, PAYLOAD + b"extra")
    with pytest.raises(download.DownloadError):
        download.download_release(release, tmp_path, policy=policy)
    assert not any(p.suffix in (".msi", ".part") for p in tmp_path.iterdir())


def test_cancel_removes_partial(server, tmp_path):
    policy = _policy(server)
    release = download.resolve_release(policy)
    state = {"chunks": 0}

    def cancelled() -> bool:
        return state["chunks"] >= 2

    def progress(_fraction: float) -> None:
        state["chunks"] += 1

    with pytest.raises(download.DownloadCancelled):
        download.download_release(release, tmp_path, policy=policy, progress=progress,
                                  cancelled=cancelled)
    assert list(tmp_path.iterdir()) == []


def test_resume_partial_download(server, tmp_path):
    policy = _policy(server)
    release = download.resolve_release(policy)
    part, info = download.part_paths(tmp_path, release)
    half = len(PAYLOAD) // 2
    part.write_bytes(PAYLOAD[:half])
    info.write_text(json.dumps({"file": release.file_name, "sha256": release.sha256,
                                "size": release.size}), encoding="utf-8")
    path = download.download_release(release, tmp_path, policy=policy)
    assert path.read_bytes() == PAYLOAD
    ranges = [h.get("Range") for p, h in server.requests if p.startswith("/mirror/")]
    assert f"bytes={half}-" in ranges
    assert not part.exists() and not info.exists()


def test_stale_partial_is_discarded(server, tmp_path):
    policy = _policy(server)
    release = download.resolve_release(policy)
    part, info = download.part_paths(tmp_path, release)
    part.write_bytes(b"garbage from another version")
    info.write_text(json.dumps({"file": "old.msi", "sha256": "0" * 64, "size": 5}), encoding="utf-8")
    assert download.download_release(release, tmp_path, policy=policy).read_bytes() == PAYLOAD


# =============================================================================
# Установка: проверки до разбора msi (П20, П21)
# =============================================================================


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr(paths, "app_root", lambda: root)
    return root


def test_install_rejects_foreign_signature_and_deletes_msi(app_dir, monkeypatch, rsa_keys):
    """Файл с верным SHA-256, но подписью не TDF — удаляется, msi не разбирается."""
    armored, fpr = _public_block(rsa_keys[0])
    signature = _sign_doc(rsa_keys[0], PAYLOAD, fpr)
    release = download.Release((26, 2, 7), "https://x/y.msi", MSI_NAME, len(PAYLOAD),
                               hashlib.sha256(PAYLOAD).hexdigest(), signature)
    monkeypatch.setattr(download, "resolve_release", lambda *a, **k: release)
    saved: list[Path] = []

    def fake_download(rel, folder, **kw):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / rel.file_name
        path.write_bytes(PAYLOAD)
        saved.append(path)
        return path

    monkeypatch.setattr(download, "download_release", fake_download)
    monkeypatch.setattr(msi, "read_plan", lambda path: pytest.fail("msi разбирается до проверки подписи"))
    with pytest.raises(module.InstallError) as info:
        module.install()
    assert "подпис" in str(info.value)
    assert saved and not saved[0].exists()
    assert not module.is_installed()


def test_install_unavailable_when_data_not_writable(app_dir, monkeypatch):
    monkeypatch.setattr(paths, "is_data_writable", lambda: False)
    assert module.availability() == "unavailable"
    monkeypatch.setattr(download, "resolve_release", lambda *a, **k: pytest.fail("сеть"))
    with pytest.raises(module.InstallError):
        module.install()
    assert "недоступен" in module.missing_message()


def test_install_cancel_maps_to_cancelled(app_dir, monkeypatch):
    from konverter.converters.base import CancelToken, Cancelled

    monkeypatch.setattr(download, "resolve_release", lambda *a, **k: download.Release(
        (26, 2, 7), "u", MSI_NAME, 10, "0" * 64, ""))

    def fake_download(*a, **k):
        raise download.DownloadCancelled()

    monkeypatch.setattr(download, "download_release", fake_download)
    token = CancelToken()
    with pytest.raises(Cancelled):
        module.install_job(lambda f: None, token)


def test_cleanup_stale_removes_only_dead_own_dirs(app_dir):
    temp = app_dir / "data" / "temp"
    dead = temp / "lo-unpack-999999-abcdef123456"
    alive = temp / f"lo-run-{__import__('os').getpid()}-abcdef123456"
    foreign = temp / "something-else"
    for folder in (dead, alive, foreign):
        (folder / "sub").mkdir(parents=True)
    assert module.cleanup_stale() == 1
    assert not dead.exists() and alive.exists() and foreign.exists()


def test_cleanup_stale_old_partial_download(app_dir):
    import os

    folder = app_dir / "data" / "temp" / module.DOWNLOAD_DIR_NAME
    folder.mkdir(parents=True)
    old = folder / (MSI_NAME + ".part")
    fresh = folder / ("LibreOffice_26.8.1_Win_x86-64.msi.part")
    foreign = folder / "notes.txt"
    for path in (old, fresh, foreign):
        path.write_bytes(b"x")
    stamp = time.time() - (module.STALE_DOWNLOAD_DAYS + 1) * 86400
    os.utime(old, (stamp, stamp))
    os.utime(foreign, (stamp, stamp))
    module.cleanup_stale()
    assert not old.exists() and fresh.exists() and foreign.exists()


# =============================================================================
# msi: план раскладки (обход путей, лимиты) и распаковка настоящего helppack
# =============================================================================

DIRS = [
    ("TARGETDIR", None, "SourceDir"),
    ("ProgramFiles64Folder", "TARGETDIR", "."),
    ("INSTALLLOCATION", "ProgramFiles64Folder", "LIBREO~1|LibreOffice:."),
    ("program", "INSTALLLOCATION", "program"),
    ("System64Folder.ABC", "TARGETDIR", "System64"),
    ("FontsFolder", "TARGETDIR", "Fonts"),
    ("DesktopFolder", "TARGETDIR", "."),
]


def _plan(files, dirs=DIRS, components=None):
    components = components or [
        ("c_prog", "program", None), ("c_sys", "System64Folder.ABC", None),
        ("c_font", "FontsFolder", None), ("c_desk", "DesktopFolder", None),
        ("c_arm", "System64Folder.ABC", '(ARM64CHECK.X = "ARM64") AND (VC_REDIST=1)'),
        ("c_upd", "program", "ISCHECKFORPRODUCTUPDATES=1"),
    ]
    return msi.build_plan(dirs, components, files, [(1, len(files), "#data1.cab")])


def test_msi_plan_layout():
    plan = _plan([
        ("f1", "c_prog", "SOFFICE.EXE|soffice.exe", 10, 0),
        ("f2", "c_sys", "abc.dll|vcruntime140.dll", 5, 0),
        ("f3", "c_font", "FONT~1.TTF|Font.ttf", 7, 0),
        ("f4", "c_desk", "lnk.lnk", 1, 0),
        ("f5", "c_arm", "x|vcruntime140.dll", 9, 0),
        ("f6", "c_upd", "updchk.dll", 9, 0),
    ])
    layout = {f.key: (f.parts, f.system) for f in plan.files}
    assert layout == {
        "f1": (("program", "soffice.exe"), False),
        "f2": (("vcruntime140.dll",), True),
        "f3": (("share", "fonts", "truetype", "Font.ttf"), False),
    }
    assert plan.cabinets == ["data1.cab"]


@pytest.mark.parametrize("name", ["..", "a\\..\\b", "C:evil.dll", "/abs", "con.txt", "x\x01", "trail."])
def test_msi_plan_rejects_bad_file_names(name):
    with pytest.raises(msi.MsiError):
        _plan([("f1", "c_prog", f"short|{name}", 1, 0)])


def test_msi_plan_rejects_bad_directories_and_limits(monkeypatch):
    bad_dirs = DIRS + [("evil", "INSTALLLOCATION", "..")]
    with pytest.raises(msi.MsiError):
        _plan([("f1", "c_evil", "a.txt", 1, 0)], dirs=bad_dirs,
              components=[("c_evil", "evil", None)])
    cyclic = [("A", "B", "a"), ("B", "A", "b")]
    with pytest.raises(msi.MsiError):
        _plan([("f1", "c", "a.txt", 1, 0)], dirs=cyclic, components=[("c", "A", None)])
    with pytest.raises(msi.MsiError):
        msi.build_plan(DIRS, [("c", "program", None)], [("f", "c", "a", 1, 0)], [(1, 1, "external.cab")])
    with pytest.raises(msi.MsiError):
        _plan([("bad/key", "c_prog", "a.txt", 1, 0)])
    monkeypatch.setattr(msi, "MAX_TOTAL_BYTES", 10)
    with pytest.raises(msi.MsiError):
        _plan([("f1", "c_prog", "a", 6, 0), ("f2", "c_prog", "b", 6, 0)])
    monkeypatch.setattr(msi, "MAX_FILES", 1)
    with pytest.raises(msi.MsiError):
        _plan([("f1", "c_prog", "a", 1, 0), ("f2", "c_prog", "b", 1, 0)])


def test_place_files_refuses_escape(tmp_path):
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "f1").write_bytes(b"x")
    root = tmp_path / "root"
    root.mkdir()
    plan = msi.Plan(files=[msi.PlannedFile("f1", ("..", "escaped.txt"), 1, False, "c")], cabinets=[])
    with pytest.raises(msi.MsiError):
        msi.place_files(plan, flat, root, check=lambda: None)
    assert not (tmp_path / "escaped.txt").exists()
    plan = msi.Plan(files=[msi.PlannedFile("f1", ("C:\\Windows", "x.dll"), 1, False, "c")], cabinets=[])
    with pytest.raises(msi.MsiError):
        msi.place_files(plan, flat, root, check=lambda: None)


def test_place_files_system_whitelist(tmp_path):
    flat = tmp_path / "flat"
    flat.mkdir()
    for key in ("a", "b", "c"):
        (flat / key).write_bytes(key.encode())
    root = tmp_path / "root"
    (root / "program").mkdir(parents=True)
    (root / "program" / "msvcp140.dll").write_bytes(b"own")
    plan = msi.Plan(files=[
        msi.PlannedFile("a", ("vcruntime140.dll",), 1, True, "c"),
        msi.PlannedFile("b", ("msvcp140.dll",), 1, True, "c"),
        msi.PlannedFile("c", ("evil.dll",), 1, True, "c"),
    ], cabinets=[])
    copied = msi.place_files(plan, flat, root, check=lambda: None,
                             system_whitelist=lambda n: n.lower() != "evil.dll")
    assert copied == ["vcruntime140.dll"]
    assert (root / "program" / "msvcp140.dll").read_bytes() == b"own"  # своя не заменяется
    assert not (root / "program" / "evil.dll").exists()


@needs_helppack
def test_unpack_real_helppack(tmp_path):
    plan = msi.read_plan(HELPPACK)
    assert plan.cabinets == ["libreoffice1.cab"] and len(plan.files) > 1000
    work = tmp_path / "work"
    work.mkdir()
    progress: list[float] = []
    root, plan2, copied = module.unpack_msi(HELPPACK, work, progress=progress.append)
    assert copied == []
    placed = [p for p in root.rglob("*") if p.is_file()]
    assert len(placed) == len(plan2.files)
    assert (root / "help").is_dir()
    assert progress[-1] == pytest.approx(1.0)
    assert sorted(p.name for p in work.iterdir()) == ["root"]  # CAB и плоская папка удалены


@needs_helppack
def test_unpack_cancel(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(msi.Cancelled):
        module.unpack_msi(HELPPACK, work, cancelled=lambda: True)


# =============================================================================
# Профиль LibreOffice
# =============================================================================


def test_xcu_contains_security_settings():
    text = soffice.registry_xcu()
    root = SafeET.fromstring(text.encode("utf-8"))
    oor = "{http://openoffice.org/2001/registry}"
    values = {}
    for item in root:
        prop = item[0]
        values[(item.get(oor + "path"), prop.get(oor + "name"))] = prop[0].text or ""
    scripting = "/org.openoffice.Office.Common/Security/Scripting"
    assert values[(scripting, "DisableMacrosExecution")] == "true"
    assert values[(scripting, "MacroSecurityLevel")] == "3"
    assert values[(scripting, "DisableActiveContent")] == "true"
    assert values[(scripting, "DisablePythonRuntime")] == "true"
    assert values[(scripting, "DisableOLEAutomation")] == "true"
    assert values[(scripting, "BlockUntrustedRefererLinks")] == "true"
    assert values[("/org.openoffice.Office.Common/Security", "LoadExoticFileFormats")] == "0"
    assert values[("/org.openoffice.Office.Common/Misc", "CrashReport")] == "false"
    assert values[("/org.openoffice.Office.Calc/Content/Update", "Link")] == "1"  # никогда
    assert values[("/org.openoffice.Office.Writer/Content/Update", "Link")] == "0"  # никогда
    assert values[("/org.openoffice.Inet/Settings", "ooInetProxyType")] == "2"


def test_soffice_args_and_profile(tmp_path):
    profile = tmp_path / "Профиль с пробелом"
    soffice.write_profile(profile)
    assert (profile / "user" / "registrymodifications.xcu").is_file()
    args = soffice.build_args(Path("C:/lo/program/soffice.exe"), profile, "pdf:writer_pdf_Export",
                              tmp_path / "out", tmp_path / "in" / "input.docx")
    env_arg = next(a for a in args if a.startswith("-env:UserInstallation="))
    assert env_arg.endswith(profile.as_uri()[len("file:///"):]) and "%20" in env_arg
    for flag in ("--headless", "--invisible", "--norestore", "--nologo", "--nodefault",
                 "--nolockcheck"):
        assert flag in args
    assert args[args.index("--convert-to") + 1] == "pdf:writer_pdf_Export"


def test_soffice_rejects_unknown_pair(tmp_path):
    with pytest.raises(soffice.SofficeError):
        soffice.convert(Path("x.exe"), tmp_path, tmp_path / "a", "pdf", "pdf")
    with pytest.raises(soffice.SofficeError):
        soffice.convert(Path("x.exe"), tmp_path, tmp_path / "a", "pptx", "docx")


# =============================================================================
# Объект задания: дерево процессов убивается
# =============================================================================

_TREE_SCRIPT = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(120)"])
with open(sys.argv[1], "w") as fh:
    fh.write(str(child.pid))
time.sleep(120)
"""


class _Stop(Exception):
    pass


def _tree(tmp_path):
    script = tmp_path / "tree.py"
    script.write_text(_TREE_SCRIPT, encoding="utf-8")
    pid_file = tmp_path / "grandchild.pid"
    return [sys.executable, "-I", str(script), str(pid_file)], pid_file


def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.Error:
            return True
        time.sleep(0.05)
    return False


def test_job_object_kills_tree_on_cancel(tmp_path):
    args, pid_file = _tree(tmp_path)
    started: list[int] = []

    def poll() -> None:
        if pid_file.exists() and pid_file.read_text():
            raise _Stop()

    def on_started(job, process) -> None:
        started.append(process.pid)

    t0 = time.monotonic()
    with pytest.raises(_Stop):
        jobobject.run(args, timeout=60, poll=poll, on_started=on_started)
    grandchild = int(pid_file.read_text())
    assert _gone(grandchild) and _gone(started[0])
    assert time.monotonic() - t0 < 30


def test_job_object_timeout(tmp_path):
    args, pid_file = _tree(tmp_path)
    deadline = time.monotonic() + 30
    result = None

    def poll() -> None:
        pass

    result = jobobject.run(args, timeout=3, poll=poll)
    assert result.timed_out and not result.leftover
    if pid_file.exists() and pid_file.read_text():
        assert _gone(int(pid_file.read_text()))
    assert time.monotonic() < deadline


def test_job_object_normal_exit():
    result = jobobject.run([sys.executable, "-I", "-c", "print('ok')"], timeout=30,
                           stdout=subprocess.DEVNULL)
    assert result.returncode == 0 and not result.timed_out and not result.leftover
