"""Минимальный проверяльщик отделённых подписей OpenPGP (RFC 4880 / RFC 9580, v4).

Зачем свой: готовые библиотеки не годятся (PGPy не работает на Python 3.13,
python-gnupg требует внешний gpg.exe, pysequoia — версия 0.1.x с нативным
бинарником). Нужен ровно один случай — подпись `.asc` к файлу LibreOffice,
сделанная ключом The Document Foundation, — поэтому код намеренно узкий:

* ключи: пакеты открытого ключа v4 (тег 6) и подключа v4 (тег 14), алгоритм
  RSA (1 — «шифрование и подпись» или 3 — «только подпись»);
* подписи: v4, класс 0x00 (двоичный документ), RSA, PKCS#1 v1.5, хэш
  SHA-256 или SHA-512 (8 и 10); хэшируется документ ‖ версия…hashed-подпакеты ‖
  трейлер `04 FF <длина, 4 байта>`;
* кем подписано: подпакет «отпечаток издателя» (33) или «ID ключа издателя»
  (16); ключ ищется ТОЛЬКО среди доверенных (переданных вызывающим);
* подключ доверенного ключа считается пригодным, только если у него есть
  действительная подпись привязки (0x18) основным ключом с флагом «подпись»
  (0x02) в подпакете флагов ключа, не истёкшая к моменту подписи, и нет
  действительного отзыва (0x28). Обратную подпись (0x19 внутри привязки) не
  проверяем: она защищает чужой подключ от присвоения ДРУГИМ основным ключом,
  а мы доверяем ровно одному закреплённому основному ключу;
* неизвестный подпакет с флагом «критичный» в hashed-части → отказ (RFC 4880,
  5.2.3.1); истёкшая подпись (подпакет 3) → отказ.

Проверка RSA — `Cryptodome.Signature.pkcs1_15`: она заново кодирует EM целиком
и сравнивает побайтно, поэтому устойчива к подделкам в духе Bleichenbacher'06.

Ничего не читает из сети и не обращается к связке ключей системы.
"""

from __future__ import annotations

import base64
import hashlib
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

# Алгоритмы открытого ключа (RFC 4880, 9.1).
_RSA_ALGOS = (1, 3)
# Хэши (RFC 4880, 9.4): код → (имя hashlib, OID для DigestInfo).
_HASHES = {
    8: ("sha256", "2.16.840.1.101.3.4.2.1"),
    10: ("sha512", "2.16.840.1.101.3.4.2.3"),
}
# Подпакеты подписи, которые мы понимаем (критичность для них допустима).
_SUB_CREATED = 2
_SUB_SIG_EXPIRES = 3
_SUB_KEY_EXPIRES = 9
_SUB_ISSUER = 16
_SUB_KEY_FLAGS = 27
_SUB_EMBEDDED = 32
_SUB_ISSUER_FPR = 33
_KNOWN_SUBPACKETS = frozenset({
    _SUB_CREATED, _SUB_SIG_EXPIRES, _SUB_KEY_EXPIRES, _SUB_ISSUER, _SUB_KEY_FLAGS,
    _SUB_EMBEDDED, _SUB_ISSUER_FPR,
    4,   # exportable
    5,   # trust signature
    7,   # revocable
    11,  # preferred symmetric
    12,  # revocation key
    20,  # notation data
    21,  # preferred hash
    22,  # preferred compression
    23,  # key server preferences
    24,  # preferred key server
    25,  # primary user id
    26,  # policy URI
    28,  # signer's user id
    29,  # reason for revocation
    30,  # features
    31,  # signature target
    34,  # preferred AEAD (черновик)
    35,  # intended recipient
    39,  # preferred AEAD ciphersuites
})
_FLAG_SIGN = 0x02
_SIG_BINARY = 0x00
_SIG_SUBKEY_BINDING = 0x18
_SIG_SUBKEY_REVOCATION = 0x28

# Пределы разбора (входные данные — из сети, хоть и с сервера TDF).
MAX_ARMOR_BYTES = 1 << 20
MAX_PACKETS = 4096
MAX_RSA_BITS = 8192
MIN_RSA_BITS = 2048
_CHUNK = 1 << 20


class PgpError(Exception):
    """Подпись или ключ не прошли проверку (текст — для журнала, не для показа)."""


@dataclass(frozen=True)
class RsaKey:
    """Ключ RSA, пригодный для проверки подписей."""

    fingerprint: bytes  # 20 байт, v4
    n: int
    e: int
    created: int  # время создания ключа, секунды эпохи

    @property
    def key_id(self) -> bytes:
        return self.fingerprint[-8:]

    @property
    def hex(self) -> str:
        return self.fingerprint.hex().upper()


# --- ASCII-броня -------------------------------------------------------------


def _crc24(data: bytes) -> int:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def dearmor(text: str | bytes, kind: str) -> bytes:
    """Снимает ASCII-броню блока `-----BEGIN PGP <kind>-----`.

    Строка контрольной суммы (`=XXXX`) необязательна, но если есть — сверяется.
    """
    if isinstance(text, bytes):
        if len(text) > MAX_ARMOR_BYTES:
            raise PgpError("слишком большой блок брони")
        try:
            text = text.decode("ascii")
        except UnicodeDecodeError as exc:
            raise PgpError("броня не ASCII") from exc
    if len(text) > MAX_ARMOR_BYTES:
        raise PgpError("слишком большой блок брони")
    lines = [line.strip() for line in text.replace("\r", "").split("\n")]
    begin = f"-----BEGIN PGP {kind}-----"
    end = f"-----END PGP {kind}-----"
    try:
        start = lines.index(begin)
        stop = lines.index(end, start + 1)
    except ValueError as exc:
        raise PgpError(f"нет блока PGP {kind}") from exc
    body = lines[start + 1:stop]
    # Заголовки брони («Comment: …») идут до первой пустой строки.
    if "" in body:
        first_blank = body.index("")
        if all(":" in line for line in body[:first_blank]):
            body = body[first_blank + 1:]
    checksum = None
    if body and body[-1].startswith("="):
        checksum = body.pop()
    payload = "".join(line for line in body if line)
    try:
        data = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise PgpError("неверный base64 в броне") from exc
    if checksum is not None:
        try:
            expected = base64.b64decode(checksum[1:], validate=True)
        except ValueError as exc:
            raise PgpError("неверная контрольная сумма брони") from exc
        if len(expected) != 3 or int.from_bytes(expected, "big") != _crc24(data):
            raise PgpError("контрольная сумма брони не совпала")
    return data


# --- Пакеты -------------------------------------------------------------------


def iter_packets(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Пакеты OpenPGP: (тег, тело). Частичные длины не поддерживаются (для
    ключей и подписей они запрещены стандартом)."""
    pos = 0
    count = 0
    size = len(data)
    while pos < size:
        count += 1
        if count > MAX_PACKETS:
            raise PgpError("слишком много пакетов")
        header = data[pos]
        pos += 1
        if not header & 0x80:
            raise PgpError("неверный заголовок пакета")
        if header & 0x40:  # новый формат
            tag = header & 0x3F
            if pos >= size:
                raise PgpError("обрезанный пакет")
            first = data[pos]
            pos += 1
            if first < 192:
                length = first
            elif first < 224:
                if pos >= size:
                    raise PgpError("обрезанный пакет")
                length = ((first - 192) << 8) + data[pos] + 192
                pos += 1
            elif first == 255:
                if pos + 4 > size:
                    raise PgpError("обрезанный пакет")
                length = struct.unpack(">I", data[pos:pos + 4])[0]
                pos += 4
            else:
                raise PgpError("частичные длины пакетов не поддерживаются")
        else:  # старый формат
            tag = (header >> 2) & 0x0F
            length_type = header & 0x03
            if length_type == 3:
                raise PgpError("неопределённая длина пакета не поддерживается")
            width = (1, 2, 4)[length_type]
            if pos + width > size:
                raise PgpError("обрезанный пакет")
            length = int.from_bytes(data[pos:pos + width], "big")
            pos += width
        if pos + length > size:
            raise PgpError("обрезанный пакет")
        yield tag, data[pos:pos + length]
        pos += length


def _mpi(body: bytes, pos: int) -> tuple[int, bytes, int]:
    """MPI: (значение, байты без ведущих нулей, новая позиция)."""
    if pos + 2 > len(body):
        raise PgpError("обрезанное MPI")
    bits = struct.unpack(">H", body[pos:pos + 2])[0]
    length = (bits + 7) // 8
    pos += 2
    if pos + length > len(body):
        raise PgpError("обрезанное MPI")
    raw = body[pos:pos + length]
    return int.from_bytes(raw, "big"), raw, pos + length


def _parse_key_body(body: bytes) -> RsaKey | None:
    """Пакет ключа v4 RSA → RsaKey; другие версии/алгоритмы → None."""
    if len(body) < 6 or body[0] != 4:
        return None
    created = struct.unpack(">I", body[1:5])[0]
    algo = body[5]
    if algo not in _RSA_ALGOS:
        return None
    n, _raw, pos = _mpi(body, 6)
    e, _raw, pos = _mpi(body, pos)
    if pos != len(body):
        raise PgpError("лишние данные в пакете ключа")
    bits = n.bit_length()
    if bits < MIN_RSA_BITS or bits > MAX_RSA_BITS or e < 3 or e % 2 == 0:
        raise PgpError("недопустимые параметры RSA")
    fingerprint = hashlib.sha1(_key_hash_prefix(body)).digest()
    return RsaKey(fingerprint=fingerprint, n=n, e=e, created=created)


def _key_hash_prefix(body: bytes) -> bytes:
    """Ключ в том виде, в каком он хэшируется (отпечаток v4, подписи привязки)."""
    if len(body) > 0xFFFF:
        raise PgpError("слишком большой пакет ключа")
    return b"\x99" + struct.pack(">H", len(body)) + body


@dataclass
class _Signature:
    sig_type: int
    pub_algo: int
    hash_algo: int
    hashed_part: bytes  # версия … конец hashed-подпакетов (то, что хэшируется)
    left16: bytes
    value: bytes  # s без ведущих нулей
    created: int | None
    expires_after: int | None  # подпакет 3: секунд после создания (0 — бессрочно)
    key_expires_after: int | None  # подпакет 9
    key_flags: int | None
    issuer_fpr: bytes | None
    issuer_id: bytes | None


def _subpackets(area: bytes) -> Iterator[tuple[int, bool, bytes]]:
    pos = 0
    while pos < len(area):
        first = area[pos]
        pos += 1
        if first < 192:
            length = first
        elif first < 255:
            if pos >= len(area):
                raise PgpError("обрезанный подпакет")
            length = ((first - 192) << 8) + area[pos] + 192
            pos += 1
        else:
            if pos + 4 > len(area):
                raise PgpError("обрезанный подпакет")
            length = struct.unpack(">I", area[pos:pos + 4])[0]
            pos += 4
        if length < 1 or pos + length > len(area):
            raise PgpError("неверная длина подпакета")
        kind = area[pos]
        yield kind & 0x7F, bool(kind & 0x80), area[pos + 1:pos + length]
        pos += length


def parse_signature(body: bytes) -> _Signature:
    """Тело пакета подписи v4 (тег 2)."""
    if len(body) < 6 or body[0] != 4:
        raise PgpError("поддерживаются только подписи v4")
    sig_type, pub_algo, hash_algo = body[1], body[2], body[3]
    hashed_len = struct.unpack(">H", body[4:6])[0]
    pos = 6 + hashed_len
    if pos + 2 > len(body):
        raise PgpError("обрезанная подпись")
    hashed_area = body[6:pos]
    hashed_part = body[:pos]
    unhashed_len = struct.unpack(">H", body[pos:pos + 2])[0]
    pos += 2
    unhashed_area = body[pos:pos + unhashed_len]
    if len(unhashed_area) != unhashed_len:
        raise PgpError("обрезанная подпись")
    pos += unhashed_len
    if pos + 2 > len(body):
        raise PgpError("обрезанная подпись")
    left16 = body[pos:pos + 2]
    pos += 2
    if pub_algo not in _RSA_ALGOS:
        raise PgpError("подпись не RSA")
    _s, value, pos = _mpi(body, pos)
    if pos != len(body):
        raise PgpError("лишние данные в подписи")

    sig = _Signature(
        sig_type=sig_type, pub_algo=pub_algo, hash_algo=hash_algo, hashed_part=hashed_part,
        left16=left16, value=value, created=None, expires_after=None,
        key_expires_after=None, key_flags=None, issuer_fpr=None, issuer_id=None,
    )
    for kind, critical, data in _subpackets(hashed_area):
        if critical and kind not in _KNOWN_SUBPACKETS:
            raise PgpError(f"неизвестный критичный подпакет {kind}")
        if kind == _SUB_CREATED and len(data) == 4:
            sig.created = struct.unpack(">I", data)[0]
        elif kind == _SUB_SIG_EXPIRES and len(data) == 4:
            sig.expires_after = struct.unpack(">I", data)[0]
        elif kind == _SUB_KEY_EXPIRES and len(data) == 4:
            sig.key_expires_after = struct.unpack(">I", data)[0]
        elif kind == _SUB_KEY_FLAGS and data:
            sig.key_flags = data[0]
        elif kind == _SUB_ISSUER_FPR and len(data) == 21 and data[0] == 4:
            sig.issuer_fpr = data[1:]
        elif kind == _SUB_ISSUER and len(data) == 8:
            sig.issuer_id = data
    # В нехэшированной части берём только «ID издателя» — это лишь подсказка,
    # какой ключ пробовать; подлинность всё равно решает математика RSA.
    for kind, _critical, data in _subpackets(unhashed_area):
        if kind == _SUB_ISSUER and len(data) == 8 and sig.issuer_id is None:
            sig.issuer_id = data
    if sig.created is None:
        raise PgpError("в подписи нет времени создания")
    return sig


# --- Проверка ----------------------------------------------------------------


class _HashForPkcs1:
    """Готовый дайджест в виде, который ждёт `pkcs1_15.verify` (oid + digest())."""

    def __init__(self, oid: str, digest: bytes) -> None:
        self.oid = oid
        self._digest = digest
        self.digest_size = len(digest)

    def digest(self) -> bytes:
        return self._digest


def _new_hash(sig: _Signature):
    entry = _HASHES.get(sig.hash_algo)
    if entry is None:
        raise PgpError(f"неподдерживаемый хэш {sig.hash_algo}")
    return hashlib.new(entry[0])


def _finish(sig: _Signature, hasher, key: RsaKey) -> None:
    """Дописывает hashed-часть и трейлер, проверяет RSA-подпись ключом `key`."""
    from Cryptodome.PublicKey import RSA
    from Cryptodome.Signature import pkcs1_15

    hasher.update(sig.hashed_part)
    hasher.update(b"\x04\xff" + struct.pack(">I", len(sig.hashed_part)))
    digest = hasher.digest()
    if digest[:2] != sig.left16:
        raise PgpError("подпись не соответствует данным")
    k = (key.n.bit_length() + 7) // 8
    if len(sig.value) > k:
        raise PgpError("подпись длиннее модуля ключа")
    signature = sig.value.rjust(k, b"\x00")
    try:
        pkcs1_15.new(RSA.construct((key.n, key.e))).verify(
            _HashForPkcs1(_HASHES[sig.hash_algo][1], digest), signature
        )
    except (ValueError, TypeError) as exc:
        raise PgpError("подпись RSA неверна") from exc


def _check_times(sig: _Signature, key: RsaKey, now: float) -> None:
    assert sig.created is not None
    if sig.created < key.created:
        raise PgpError("подпись старше ключа")
    if sig.created > now + 24 * 3600:
        raise PgpError("подпись из будущего")
    if sig.expires_after and sig.created + sig.expires_after <= now:
        raise PgpError("срок действия подписи истёк")


def load_keyring(
    armored_key: str | bytes, expected_fingerprint: str, *, now: float | None = None
) -> list[RsaKey]:
    """Разбирает открытый ключ и возвращает ключи, пригодные для подписи.

    Основной ключ обязан иметь отпечаток `expected_fingerprint` (40 hex-цифр,
    пробелы допустимы), иначе `PgpError`. Подключи — только с действительной
    привязкой основным ключом и флагом «подпись» (см. докстринг модуля).
    """
    now = time.time() if now is None else now
    expected = bytes.fromhex(expected_fingerprint.replace(" ", ""))
    data = dearmor(armored_key, "PUBLIC KEY BLOCK")
    packets = list(iter_packets(data))
    if not packets or packets[0][0] != 6:
        raise PgpError("блок не начинается с открытого ключа")
    primary_body = packets[0][1]
    primary = _parse_key_body(primary_body)
    if primary is None:
        raise PgpError("основной ключ не RSA v4")
    if primary.fingerprint != expected:
        raise PgpError("отпечаток ключа не совпал с ожидаемым")
    result = [primary]

    # Подключи: (тело, ключ, привязан?, отозван?)
    current: list | None = None
    subkeys: list[list] = []
    for tag, body in packets[1:]:
        if tag == 6:
            break  # второй ключ в блоке нас не интересует
        if tag == 14:
            current = [body, _parse_key_body(body), False, False]
            subkeys.append(current)
            continue
        if tag in (13, 17):  # user id / user attribute: подписи дальше — не к подключу
            current = None
            continue
        if tag != 2 or current is None or current[1] is None:
            continue
        try:
            sig = parse_signature(body)
        except PgpError:
            continue
        if sig.sig_type not in (_SIG_SUBKEY_BINDING, _SIG_SUBKEY_REVOCATION):
            continue
        if sig.issuer_fpr is not None and sig.issuer_fpr != primary.fingerprint:
            continue
        hasher = _new_hash(sig) if sig.hash_algo in _HASHES else None
        if hasher is None:
            continue
        hasher.update(_key_hash_prefix(primary_body))
        hasher.update(_key_hash_prefix(current[0]))
        try:
            _finish(sig, hasher, primary)
            _check_times(sig, primary, now)
        except PgpError:
            continue
        sub: RsaKey = current[1]
        if sig.sig_type == _SIG_SUBKEY_REVOCATION:
            current[3] = True
            continue
        if sig.key_flags is None or not sig.key_flags & _FLAG_SIGN:
            continue
        if sig.key_expires_after and sub.created + sig.key_expires_after <= now:
            continue
        current[2] = True
    for _body, sub, bound, revoked in subkeys:
        if sub is not None and bound and not revoked:
            result.append(sub)
    return result


def _read_signature(armored_sig: str | bytes) -> _Signature:
    data = dearmor(armored_sig, "SIGNATURE")
    packets = list(iter_packets(data))
    sigs = [body for tag, body in packets if tag == 2]
    if len(sigs) != 1 or len(packets) != 1:
        raise PgpError("ожидалась ровно одна подпись")
    sig = parse_signature(sigs[0])
    if sig.sig_type != _SIG_BINARY:
        raise PgpError("подпись не двоичного документа")
    return sig


def _pick_key(sig: _Signature, keys: Iterable[RsaKey]) -> RsaKey:
    keys = list(keys)
    for key in keys:
        if sig.issuer_fpr is not None:
            if key.fingerprint == sig.issuer_fpr:
                return key
        elif sig.issuer_id is not None and key.key_id == sig.issuer_id:
            return key
    raise PgpError("подпись сделана не доверенным ключом")


def verify_detached(
    feed: Callable[[Callable[[bytes], None]], None],
    armored_sig: str | bytes,
    keys: Iterable[RsaKey],
    *,
    now: float | None = None,
) -> RsaKey:
    """Проверяет отделённую подпись. `feed(update)` должна передать в `update`
    все байты документа по порядку. Возвращает ключ, которым подписано;
    при любой неудаче — `PgpError`."""
    now = time.time() if now is None else now
    sig = _read_signature(armored_sig)
    key = _pick_key(sig, keys)
    hasher = _new_hash(sig)
    feed(hasher.update)
    _finish(sig, hasher, key)
    _check_times(sig, key, now)
    return key


def verify_bytes(data: bytes, armored_sig: str | bytes, keys: Iterable[RsaKey], **kw) -> RsaKey:
    return verify_detached(lambda update: update(data), armored_sig, keys, **kw)


def verify_file(
    path: Path | str,
    armored_sig: str | bytes,
    keys: Iterable[RsaKey],
    *,
    check: Callable[[], None] | None = None,
    now: float | None = None,
) -> RsaKey:
    """Проверяет подпись файла. `check()` вызывается между кусками (отмена)."""

    def feed(update: Callable[[bytes], None]) -> None:
        with open(path, "rb") as fh:
            while True:
                if check is not None:
                    check()
                chunk = fh.read(_CHUNK)
                if not chunk:
                    return
                update(chunk)

    return verify_detached(feed, armored_sig, keys, now=now)
