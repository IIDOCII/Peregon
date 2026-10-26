"""Проверка подписи Authenticode файла (WinVerifyTrust через ctypes).

Это вторая, независимая от OpenPGP проверка скачанного msi: другая PKI
(сертификат кода от Certum, корни — хранилище Windows). «Подпись верна» сама
по себе значит лишь «подписано кем-то с доверенным сертификатом», поэтому
дополнительно сверяется субъект сертификата подписавшего: и простое имя (CN),
и организация (O) должны быть ровно `EXPECTED_SIGNER`. Отпечаток сертификата
не зашивается: он меняется при перевыпуске.

Сеть: загрузка по URL РАЗРЕШЕНА — на «чистой» Windows корневой сертификат
Certum может отсутствовать в хранилище, и Windows догружает его (автообновление
корней) и промежуточные сертификаты; мы и так в сети во время скачивания.
Проверка ОТЗЫВА выключена (`WTD_REVOKE_NONE`): она ходит к CRL/OCSP издателя и
может надолго зависать или падать за прокси, а подлинность файла и так держится
на подписи OpenPGP закреплённым ключом TDF — Authenticode здесь вторая линия.
Ошибки цепочки (нет доверенного корня, цепочка не строится) отделены от
«подпись неверна» (`ChainError`): это проблема хранилища сертификатов
Windows, а не признак подмены.

Только чтение: ничего не устанавливается и не пишется в систему.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

EXPECTED_SIGNER = "The Document Foundation"

_WTD_UI_NONE = 2
_WTD_REVOKE_NONE = 0
_WTD_REVOCATION_CHECK_NONE = 0x10
_WTD_CHOICE_FILE = 1
_WTD_STATEACTION_VERIFY = 1
_WTD_STATEACTION_CLOSE = 2
# Коды WinVerifyTrust, означающие «не удалось построить/доверить цепочку».
CHAIN_ERRORS = frozenset({
    0x800B0109,  # CERT_E_UNTRUSTEDROOT
    0x800B010A,  # CERT_E_CHAINING
    0x800B010D,  # CERT_E_UNTRUSTEDTESTROOT
    0x800B0112,  # CERT_E_UNTRUSTEDCA
    0x80096019,  # TRUST_E_BASIC_CONSTRAINTS (обычно — неполная цепочка)
})
_CERT_NAME_ATTR_TYPE = 3
_CERT_NAME_SIMPLE_DISPLAY_TYPE = 4
_OID_ORGANIZATION = b"2.5.4.10"


class AuthenticodeError(Exception):
    """Подпись отсутствует, неверна или выдана не тому субъекту."""


class ChainError(AuthenticodeError):
    """Подпись есть, но Windows не смогла построить доверенную цепочку сертификатов."""


def error_for_status(status: int) -> AuthenticodeError:
    """Исключение для ненулевого кода WinVerifyTrust."""
    code = status & 0xFFFFFFFF
    cls = ChainError if code in CHAIN_ERRORS else AuthenticodeError
    return cls(f"WinVerifyTrust: 0x{code:08X}")


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


# WINTRUST_ACTION_GENERIC_VERIFY_V2 = {00AAC56B-CD44-11d0-8CC2-00C04FC295EE}
_ACTION_VERIFY_V2 = _GUID(
    0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE)
)


class _WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pcwszFilePath", wintypes.LPCWSTR),
        ("hFile", wintypes.HANDLE),
        ("pgKnownSubject", ctypes.POINTER(_GUID)),
    ]


class _WINTRUST_DATA(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("pPolicyCallbackData", ctypes.c_void_p),
        ("pSIPClientData", ctypes.c_void_p),
        ("dwUIChoice", wintypes.DWORD),
        ("fdwRevocationChecks", wintypes.DWORD),
        ("dwUnionChoice", wintypes.DWORD),
        ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)),
        ("dwStateAction", wintypes.DWORD),
        ("hWVTStateData", wintypes.HANDLE),
        ("pwszURLReference", wintypes.LPCWSTR),
        ("dwProvFlags", wintypes.DWORD),
        ("dwUIContext", wintypes.DWORD),
        ("pSignatureSettings", ctypes.c_void_p),
    ]


class _CRYPT_PROVIDER_CERT(ctypes.Structure):
    # Нужны только первые поля структуры.
    _fields_ = [("cbStruct", wintypes.DWORD), ("pCert", ctypes.c_void_p)]


class _CRYPT_PROVIDER_SGNR(ctypes.Structure):
    _fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("sftVerifyAsOf", wintypes.FILETIME),
        ("csCertChain", wintypes.DWORD),
        ("pasCertChain", ctypes.POINTER(_CRYPT_PROVIDER_CERT)),
        ("dwSignerType", wintypes.DWORD),
        ("psSigner", ctypes.c_void_p),
        ("dwError", wintypes.DWORD),
        ("csCounterSigners", wintypes.DWORD),
        ("pasCounterSigners", ctypes.c_void_p),
        ("pChainContext", ctypes.c_void_p),
    ]


def _cert_name(crypt32, cert: int, kind: int, para) -> str:
    size = crypt32.CertGetNameStringW(cert, kind, 0, para, None, 0)
    if size <= 1:
        return ""
    buf = ctypes.create_unicode_buffer(size)
    crypt32.CertGetNameStringW(cert, kind, 0, para, buf, size)
    return buf.value


def signer_name(path: Path | str) -> str:
    """Проверяет Authenticode файла и возвращает CN подписавшего.

    Любая неудача (нет подписи, подпись неверна, нет цепочки) —
    `AuthenticodeError`. Субъект НЕ сверяет — см. `verify`.
    """
    if os.name != "nt":
        raise AuthenticodeError("Authenticode проверяется только в Windows")
    wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    wintrust.WinVerifyTrust.restype = ctypes.c_long
    wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(_GUID), ctypes.c_void_p]
    wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
    wintrust.WTHelperProvDataFromStateData.argtypes = [wintypes.HANDLE]
    wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.POINTER(_CRYPT_PROVIDER_SGNR)
    wintrust.WTHelperGetProvSignerFromChain.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    crypt32.CertGetNameStringW.restype = wintypes.DWORD
    crypt32.CertGetNameStringW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.LPWSTR, wintypes.DWORD,
    ]

    file_info = _WINTRUST_FILE_INFO(
        ctypes.sizeof(_WINTRUST_FILE_INFO), str(Path(path).absolute()), None, None
    )
    data = _WINTRUST_DATA()
    data.cbStruct = ctypes.sizeof(_WINTRUST_DATA)
    data.dwUIChoice = _WTD_UI_NONE
    data.fdwRevocationChecks = _WTD_REVOKE_NONE
    # Для GENERIC_VERIFY_V2 отзыв отключается ещё и флагом провайдера,
    # иначе возможны ожидание CRL за прокси и CERT_E_REVOCATION_FAILURE.
    data.dwProvFlags = _WTD_REVOCATION_CHECK_NONE
    data.dwUnionChoice = _WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = _WTD_STATEACTION_VERIFY
    action = _GUID.from_buffer_copy(_ACTION_VERIFY_V2)
    status = wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    try:
        if status != 0:
            raise error_for_status(status)
        provider = wintrust.WTHelperProvDataFromStateData(data.hWVTStateData)
        if not provider:
            raise AuthenticodeError("нет данных проверки")
        signer = wintrust.WTHelperGetProvSignerFromChain(provider, 0, False, 0)
        if not signer or signer.contents.csCertChain < 1 or not signer.contents.pasCertChain:
            raise AuthenticodeError("нет цепочки сертификатов подписавшего")
        cert = signer.contents.pasCertChain[0].pCert
        if not cert:
            raise AuthenticodeError("нет сертификата подписавшего")
        common = _cert_name(crypt32, cert, _CERT_NAME_SIMPLE_DISPLAY_TYPE, None)
        org = _cert_name(crypt32, cert, _CERT_NAME_ATTR_TYPE, ctypes.c_char_p(_OID_ORGANIZATION))
        return common if common == org else f"{common} (O={org})"
    finally:
        data.dwStateAction = _WTD_STATEACTION_CLOSE
        wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))


def verify(path: Path | str, expected: str = EXPECTED_SIGNER) -> str:
    """Authenticode верна и выдана `expected` (CN и O); иначе `AuthenticodeError`."""
    name = signer_name(path)
    if name != expected:
        raise AuthenticodeError(f"подписано не тем субъектом: {name!r}")
    return name
