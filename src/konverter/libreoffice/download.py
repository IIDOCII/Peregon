"""Загрузка установщика LibreOffice с сервера The Document Foundation (Ф16).

Схема (утверждена пользователем 24.10.2026):
1. Список выпусков — `https://download.documentfoundation.org/libreoffice/stable/`
   (HTTPS, без редиректов). Ветки — по major.minor; из двух самых новых веток
   младшая — **still** (по умолчанию, `BRANCH`), старшая — **fresh**. Берётся
   новейшая версия ветки; ниже `MIN_VERSION` — отказ.
2. Метаданные `<msi>.sha256`, `<msi>.meta4`, `<msi>.asc` — ТОЛЬКО с того же
   сервера TDF по HTTPS, редирект на них — отказ. Из `.meta4` берутся размер
   и список зеркал; SHA-256 из `.sha256` и из `.meta4` должны совпасть.
3. Сам msi: сначала адрес на сервере TDF (он отвечает 302 на зеркало), затем
   https-зеркала из `.meta4` по приоритету. Редирект разрешён только на
   `https://` (бывают редиректы на http-зеркала — такая попытка отвергается,
   и берётся следующее зеркало). Размер ≤ `MAX_MSI_BYTES` и ровно равен
   объявленному в `.meta4`.
4. Докачка: недокачанный файл `<имя>.part` и рядом `<имя>.part.json` (имя
   файла, SHA-256 и размер из метаданных). Если при следующей попытке они
   совпадают с текущими метаданными — запрос с `Range`, иначе файл удаляется и
   качается заново. Отмена удаляет недокачанное.
5. После загрузки с каждого зеркала — размер и SHA-256 (из `.sha256`):
   неверный хэш → файл удаляется и пробуется следующее зеркало (не больше
   `MAX_HASH_FAILURES` раз); подпись OpenPGP, Authenticode и версию внутри msi
   проверяет `module.install` (до распаковки).
Сбои зеркала любого рода (HTTP, TLS, обрыв, `http.client.HTTPException` —
BadStatusLine, IncompleteRead …) → следующее зеркало; ошибки записи на диск
(нет места) → понятная ошибка. Отмена проверяется между запросами и кусками.

Только стандартная библиотека (urllib): проверка сертификата TLS — по
умолчанию (системные корни через ssl.create_default_context), системный
прокси — как у urllib по умолчанию. Ничего не пишется вне переданной папки.
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from konverter.strings import tr

log = logging.getLogger(__name__)

TDF_ORIGIN = "https://download.documentfoundation.org"
STABLE_URL = TDF_ORIGIN + "/libreoffice/stable/"
# "still" (по умолчанию) или "fresh".
BRANCH = "still"
MIN_VERSION = (26, 2, 6)
MAX_MSI_BYTES = 600 * 2**20
MAX_METADATA_BYTES = 2 * 2**20
TIMEOUT = 30.0
# Таймаут запросов метаданных (маленькие файлы), с.
METADATA_TIMEOUT = 15.0
# Сколько зеркал с неверным SHA-256 терпеть, прежде чем сдаться.
MAX_HASH_FAILURES = 2
CHUNK = 256 * 1024
# Сколько зеркал из .meta4 пробовать (кроме сервера TDF).
MAX_MIRRORS = 6
USER_AGENT = "Peregon (LibreOffice module downloader)"


class DownloadError(Exception):
    """Ошибка загрузки; `str()` — текст для журнала, `user` — для показа."""

    def __init__(self, message: str, user: str | None = None) -> None:
        super().__init__(message)
        self.user = user or tr("office_err_bad_data")


class DownloadCancelled(Exception):
    """Загрузка отменена пользователем."""


@dataclass(frozen=True)
class Policy:
    """Откуда и как качать (в тестах подменяется на локальный сервер)."""

    stable_url: str = STABLE_URL
    metadata_origin: str = TDF_ORIGIN
    mirror_schemes: tuple[str, ...] = ("https",)
    branch: str = BRANCH
    min_version: tuple[int, int, int] = MIN_VERSION
    max_bytes: int = MAX_MSI_BYTES
    timeout: float = TIMEOUT
    metadata_timeout: float = METADATA_TIMEOUT
    max_mirrors: int = MAX_MIRRORS


@dataclass
class Release:
    """Выбранный выпуск и его метаданные."""

    version: tuple[int, int, int]
    msi_url: str
    file_name: str
    size: int = 0
    sha256: str = ""
    signature: str = ""
    mirrors: list[str] = field(default_factory=list)

    @property
    def version_text(self) -> str:
        return ".".join(map(str, self.version))


# --- HTTP ---------------------------------------------------------------------


class _RedirectRefused(urllib.error.URLError):
    pass


class _RedirectPolicy(urllib.request.HTTPRedirectHandler):
    """Редиректы: для метаданных — запрещены; для msi — только на разрешённые схемы."""

    def __init__(self, allowed_schemes: tuple[str, ...] | None) -> None:
        super().__init__()
        self.allowed = allowed_schemes  # None — никаких редиректов

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        scheme = urllib.parse.urlsplit(newurl).scheme.lower()
        if self.allowed is None:
            raise _RedirectRefused(f"редирект запрещён: {code} → {scheme}://…")
        if scheme not in self.allowed:
            raise _RedirectRefused(f"редирект на недопустимую схему {scheme}://")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(allowed_redirects: tuple[str, ...] | None) -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()  # проверка сертификата и имени хоста
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _RedirectPolicy(allowed_redirects)
    )


# Фабрика «открывателей» (тесты могут подменить).
make_opener: Callable[[tuple[str, ...] | None], urllib.request.OpenerDirector] = _opener


def _request(url: str, headers: dict[str, str] | None = None) -> urllib.request.Request:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return request


def _check_metadata_url(url: str, policy: Policy) -> None:
    if not url.startswith(policy.metadata_origin.rstrip("/") + "/"):
        raise DownloadError(f"метаданные не с сервера TDF: {url}")


def fetch_text(url: str, policy: Policy, limit: int = MAX_METADATA_BYTES) -> str:
    """Небольшой текст с сервера TDF без редиректов."""
    _check_metadata_url(url, policy)
    try:
        with make_opener(None).open(_request(url), timeout=policy.metadata_timeout) as response:
            if response.status != 200:
                raise DownloadError(f"{url}: HTTP {response.status}")
            data = response.read(limit + 1)
    except _RedirectRefused as exc:
        raise DownloadError(f"{url}: {exc.reason}") from exc
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise DownloadError(
            f"{url}: {getattr(exc, 'reason', exc)!r}", tr("office_err_no_network")
        ) from exc
    if len(data) > limit:
        raise DownloadError(f"{url}: слишком большой ответ")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DownloadError(f"{url}: не UTF-8") from exc


def _bad_data() -> str:
    return tr("office_err_bad_data")


# --- Выбор версии -------------------------------------------------------------

_VERSION_HREF = re.compile(r'href="(\d{1,3})\.(\d{1,3})\.(\d{1,3})/"')


def parse_versions(listing: str) -> list[tuple[int, int, int]]:
    return sorted({tuple(int(x) for x in m.groups()) for m in _VERSION_HREF.finditer(listing)})


def choose_version(
    versions: list[tuple[int, int, int]], branch: str, minimum: tuple[int, int, int]
) -> tuple[int, int, int]:
    """Новейшая версия ветки still/fresh (см. докстринг модуля)."""
    branches = sorted({v[:2] for v in versions})
    if not branches:
        raise DownloadError("в списке выпусков нет версий", _bad_data())
    if branch == "fresh" or len(branches) == 1:
        chosen = branches[-1]
    elif branch == "still":
        chosen = branches[-2]
    else:
        raise ValueError(f"неизвестная ветка {branch}")
    version = max(v for v in versions if v[:2] == chosen)
    if version < minimum:
        raise DownloadError(
            f"версия {version} ниже минимальной {minimum}",
            tr("office_err_too_old_on_server"),
        )
    return version


def msi_name(version: tuple[int, int, int], suffix: str = "") -> str:
    text = ".".join(map(str, version))
    return f"LibreOffice_{text}_Win_x86-64{suffix}.msi"


def release_url(policy: Policy, version: tuple[int, int, int], suffix: str = "") -> str:
    text = ".".join(map(str, version))
    return f"{policy.stable_url.rstrip('/')}/{text}/win/x86_64/{msi_name(version, suffix)}"


# --- Метаданные ---------------------------------------------------------------

_SHA_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$")


def parse_sha256(text: str, file_name: str) -> str:
    for line in text.splitlines():
        match = _SHA_LINE.match(line.strip())
        if match and match.group(2) == file_name:
            return match.group(1).lower()
    raise DownloadError("в .sha256 нет хэша файла", _bad_data())


def parse_meta4(text: str, file_name: str, schemes: tuple[str, ...]) -> tuple[int, str, list[str]]:
    """(размер, sha-256, зеркала с разрешённой схемой по приоритету) из Metalink 4."""
    from defusedxml import ElementTree as SafeET

    try:
        root = SafeET.fromstring(text.encode("utf-8"), forbid_dtd=True)
    except Exception as exc:  # noqa: BLE001 — любой сбой разбора = плохие данные
        raise DownloadError(f"неверный .meta4: {type(exc).__name__}", _bad_data()) from exc
    ns = "{urn:ietf:params:xml:ns:metalink}"
    for node in root.iter(ns + "file"):
        if node.get("name") != file_name:
            continue
        size_node = node.find(ns + "size")
        try:
            size = int((size_node.text or "").strip()) if size_node is not None else 0
        except ValueError:
            size = 0
        sha = ""
        for hash_node in node.findall(ns + "hash"):
            if hash_node.get("type") == "sha-256":
                sha = (hash_node.text or "").strip().lower()
        mirrors: list[tuple[int, str]] = []
        for url_node in node.findall(ns + "url"):
            url = (url_node.text or "").strip()
            if urllib.parse.urlsplit(url).scheme.lower() not in schemes:
                continue
            try:
                priority = int(url_node.get("priority") or 999999)
            except ValueError:
                priority = 999999
            mirrors.append((priority, url))
        mirrors.sort(key=lambda item: item[0])
        if size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise DownloadError("в .meta4 нет размера или хэша", _bad_data())
        return size, sha, [url for _p, url in mirrors]
    raise DownloadError("в .meta4 нет описания файла", _bad_data())


def resolve_release(
    policy: Policy = Policy(), suffix: str = "", cancelled: Callable[[], bool] = lambda: False
) -> Release:
    """Выбирает версию и загружает её метаданные с сервера TDF.

    Отмена (`cancelled()`) проверяется перед каждым запросом → `DownloadCancelled`.
    """
    _check_cancel(cancelled)
    listing = fetch_text(policy.stable_url, policy)
    version = choose_version(parse_versions(listing), policy.branch, policy.min_version)
    url = release_url(policy, version, suffix)
    name = msi_name(version, suffix)
    _check_cancel(cancelled)
    sha = parse_sha256(fetch_text(url + ".sha256", policy), name)
    _check_cancel(cancelled)
    size, meta_sha, mirrors = parse_meta4(
        fetch_text(url + ".meta4", policy), name, policy.mirror_schemes
    )
    if meta_sha != sha:
        raise DownloadError("SHA-256 в .sha256 и .meta4 не совпали", _bad_data())
    if size > policy.max_bytes:
        raise DownloadError(
            f"объявленный размер {size} больше предела {policy.max_bytes}",
            tr("office_err_too_big"),
        )
    _check_cancel(cancelled)
    signature = fetch_text(url + ".asc", policy, limit=64 * 1024)
    return Release(
        version=version, msi_url=url, file_name=name, size=size, sha256=sha,
        signature=signature, mirrors=mirrors[: policy.max_mirrors],
    )


# --- Загрузка файла -----------------------------------------------------------


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Не удалось удалить %s: %s", path, exc)


def part_paths(folder: Path, release: Release) -> tuple[Path, Path]:
    part = folder / (release.file_name + ".part")
    return part, folder / (release.file_name + ".part.json")


def _resume_offset(part: Path, info: Path, release: Release) -> int:
    """Сколько байт уже скачано и годится для докачки (0 — начать заново)."""
    try:
        meta = json.loads(info.read_text(encoding="utf-8"))
        size = part.stat().st_size
    except (OSError, ValueError):
        return 0
    if (
        isinstance(meta, dict)
        and meta.get("sha256") == release.sha256
        and meta.get("size") == release.size
        and meta.get("file") == release.file_name
        and 0 < size < release.size
    ):
        return size
    return 0


def _complete_file_matches(final: Path, release: Release, cancelled: Callable[[], bool]) -> bool:
    try:
        if final.stat().st_size != release.size:
            _remove(final)
            return False
    except OSError:
        return False
    if sha256_file(final, lambda: _check_cancel(cancelled)) == release.sha256:
        return True
    _remove(final)
    return False


def sha256_file(path: Path, check: Callable[[], None] | None = None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if check is not None:
                check()
            chunk = fh.read(1 << 20)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def download_release(
    release: Release,
    folder: Path,
    *,
    policy: Policy = Policy(),
    progress: Callable[[float], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> Path:
    """Качает msi в `folder` (докачка, зеркала, лимиты), сверяет размер и SHA-256.

    Возвращает путь к готовому файлу `<folder>/<имя>.msi`. Отмена →
    `DownloadCancelled` (недокачанное удалено), ошибка → `DownloadError`
    (неверный файл удалён; .part с верным началом остаётся для докачки только
    при сетевых сбоях).
    """
    if release.size <= 0 or release.size > policy.max_bytes:
        raise DownloadError("недопустимый размер файла")
    part, info = part_paths(folder, release)
    final = folder / release.file_name
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if _complete_file_matches(final, release, cancelled):
            # Уже скачан прошлой попыткой (например, сбой при распаковке).
            if progress is not None:
                progress(1.0)
            return final
        offset = _resume_offset(part, info, release)
        if offset == 0:
            _remove(part)
        info.write_text(
            json.dumps({"file": release.file_name, "sha256": release.sha256, "size": release.size}),
            encoding="utf-8",
        )
        _download_loop(release, part, policy, progress, cancelled)
        _remove(final)
        os.replace(part, final)
    except DownloadCancelled:
        _remove(part)
        _remove(info)
        raise
    except DownloadError:
        if not part.exists():
            _remove(info)
        raise
    except OSError as exc:
        # Сбой записи/чтения на нашем диске (в т.ч. нет места).
        log.warning("Загрузка LibreOffice: ошибка диска: %s", exc)
        _remove(part)
        _remove(info)
        raise DownloadError(f"ошибка диска: {exc}", _disk_message(exc)) from exc
    _remove(info)
    return final


def _disk_message(exc: OSError) -> str:
    if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112):
        return tr("office_err_disk_full")
    return tr("office_err_write").format(error=exc.strerror or exc)


def _download_loop(release, part, policy, progress, cancelled) -> None:
    """Перебор адресов: TDF (редирект на зеркало), затем зеркала из .meta4."""
    candidates = [release.msi_url] + [u for u in release.mirrors if u != release.msi_url]
    errors: list[str] = []
    hash_failures = 0
    tried: set[str] = set()  # адреса, откуда реально пришёл файл (после редиректа)
    for url in candidates:
        _check_cancel(cancelled)
        if url in tried:
            continue  # сервер TDF уже перенаправлял на это зеркало
        try:
            served = _fetch_into(url, part, release, policy, progress, cancelled)
            tried.add(served)
        except _MirrorFailed as exc:
            errors.append(f"{url}: {exc}")
            log.info("Загрузка LibreOffice: зеркало не подошло: %s: %s", url, exc)
            if exc.discard:
                _remove(part)
            continue
        if sha256_file(part, lambda: _check_cancel(cancelled)) == release.sha256:
            return
        # Длина верная, содержимое — нет: испорченное/подменённое зеркало.
        _remove(part)
        hash_failures += 1
        errors.append(f"{url}: неверный SHA-256")
        log.warning("Загрузка LibreOffice: неверный SHA-256 с %s", url)
        if hash_failures >= MAX_HASH_FAILURES:
            break
    if hash_failures:
        raise DownloadError("SHA-256 скачанного файла не совпал: " + "; ".join(errors[-3:]),
                            tr("office_err_hash"))
    raise DownloadError("ни одно зеркало не подошло: " + "; ".join(errors[-3:]),
                        tr("office_err_no_mirror"))


class _LocalIOError(Exception):
    """Ошибка записи в наш файл (не вина зеркала) — оборачивает OSError."""

    def __init__(self, exc: OSError) -> None:
        super().__init__(str(exc))
        self.exc = exc


def _open_part(path: Path, mode: str):
    """Открывает .part для записи (тесты подменяют, чтобы смоделировать «нет места»)."""
    return open(path, mode)


class _MirrorFailed(Exception):
    def __init__(self, message: str, discard: bool = False) -> None:
        super().__init__(message)
        self.discard = discard


def _check_cancel(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise DownloadCancelled()


def _fetch_into(
    url: str,
    part: Path,
    release: Release,
    policy: Policy,
    progress: Callable[[float], None] | None,
    cancelled: Callable[[], bool],
) -> str:
    """Качает с одного адреса в `part`; возвращает адрес, откуда пришёл ответ."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    is_tdf = url.startswith(policy.metadata_origin.rstrip("/") + "/")
    if scheme not in policy.mirror_schemes and not is_tdf:
        raise _MirrorFailed("недопустимая схема")
    try:
        offset = part.stat().st_size
    except OSError:
        offset = 0
    if offset >= release.size:
        raise _MirrorFailed("недокачанный файл больше ожидаемого", discard=True)
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    try:
        response = make_opener(policy.mirror_schemes).open(
            _request(url, headers), timeout=policy.timeout
        )
    except _RedirectRefused as exc:
        raise _MirrorFailed(str(exc.reason)) from exc
    except urllib.error.HTTPError as exc:
        if exc.code == 416:
            raise _MirrorFailed("HTTP 416", discard=True) from exc
        raise _MirrorFailed(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise _MirrorFailed(repr(getattr(exc, "reason", exc))) from exc
    try:
        with response:
            served = response.geturl()
            _receive(response, part, release, policy, progress, cancelled, offset)
        return served
    except _LocalIOError as exc:
        raise exc.exc from None  # наш диск: не зеркало виновато → наверх как OSError
    except (OSError, http.client.HTTPException) as exc:
        raise _MirrorFailed(f"обрыв: {exc!r}") from exc


def _receive(response, part, release, policy, progress, cancelled, offset) -> None:
    final_scheme = urllib.parse.urlsplit(response.geturl()).scheme.lower()
    if final_scheme not in policy.mirror_schemes:
        raise _MirrorFailed("ответ пришёл не по разрешённой схеме")
    status = response.status
    if offset and status == 206:
        content_range = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range.strip())
        if not match or int(match.group(1)) != offset or int(match.group(3)) != release.size:
            raise _MirrorFailed("неверный Content-Range", discard=True)
        mode = "ab"
    elif status == 200:
        offset = 0
        mode = "wb"
    else:
        raise _MirrorFailed(f"HTTP {status}")
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            expected = offset + int(length)
        except ValueError as exc:
            raise _MirrorFailed("неверный Content-Length") from exc
        if expected != release.size:
            raise _MirrorFailed("неверный размер ответа", discard=mode == "wb")
    done = offset
    try:
        out = _open_part(part, mode)
    except OSError as exc:
        raise _LocalIOError(exc) from exc
    with out:
        while True:
            _check_cancel(cancelled)
            chunk = response.read(CHUNK)  # OSError/HTTPException → обрыв (выше)
            if not chunk:
                break
            done += len(chunk)
            if done > release.size or done > policy.max_bytes:
                raise _MirrorFailed("файл больше объявленного размера", discard=True)
            try:
                out.write(chunk)
            except OSError as exc:
                raise _LocalIOError(exc) from exc
            if progress is not None:
                progress(done / release.size)
    if done != release.size:
        raise _MirrorFailed(f"получено {done} из {release.size} байт")
