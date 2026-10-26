"""Модуль LibreOffice в папке программы: состояние, установка, уборка (Ф15–Ф19).

Где что лежит (всё внутри `data/`):
* `data/modules/libreoffice/` — распакованный LibreOffice (готовый модуль);
  признак готовности — файл `konverter-module.json` (версия, SHA-256 msi)
  и `program/soffice.exe`;
* `data/temp/lo-download/` — скачивание (`*.msi.part` + `*.part.json` для
  докачки); после распаковки msi удаляется;
* `data/temp/lo-unpack-<pid>-<…>/` — временная распаковка; в конце
  атомарно переименовывается в `data/modules/libreoffice/`;
* `data/temp/lo-run-<pid>-<…>/` — папки отдельных конвертаций (`soffice`).

Установка (`install`): выбор версии и метаданные → загрузка (размер,
SHA-256) → подпись OpenPGP закреплённым ключом TDF → Authenticode с
субъектом «The Document Foundation» → только после этого чтение msi →
версия и название ВНУТРИ msi (`ProductVersion` = выбранной версии и не ниже
`download.MIN_VERSION`, `ProductName` — сам LibreOffice): подпись подтверждает
лишь, что пакет выпустила TDF, но не какой он версии, а версию сообщает сервер —
без этой сверки взломанный сервер мог бы подсунуть старый подписанный msi с
известными уязвимостями → распаковка CAB → раскладка (без обхода путей, с лимитами) → библиотеки
Visual C++ x64 из msi в `program\\`, если их там нет → проверочная
конвертация → переименование. Любой провал проверки подлинности — msi
удаляется.

Две запущенные копии программы: установка защищена межпроцессным замком
(`lo-install.lock` в `data/temp`, блокировка байта `msvcrt.locking`;
снимается системой и при аварийном завершении), а готовый модуль с
действительным маркером не заменяется никогда.

Устаревший модуль (версия в маркере ниже `download.MIN_VERSION`, например,
после обновления программы) считается отсутствующим: `availability()` →
"outdated", интерфейс предлагает скачать заново тем же вопросом (Ф15). Ни реестра, ни %APPDATA%, ни %TEMP% системы, ни прав
администратора.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from konverter import paths
from konverter.strings import tr

from . import authenticode, download, msi, openpgp, soffice
from ._tdf_key import TDF_FINGERPRINT, TDF_PUBLIC_KEY

log = logging.getLogger(__name__)

MODULE_DIR_NAME = "libreoffice"
MARKER_NAME = "konverter-module.json"
DOWNLOAD_DIR_NAME = "lo-download"
UNPACK_PREFIX = "lo-unpack-"
TRASH_PREFIX = "lo-old-"
LOCK_NAME = "lo-install.lock"
# Названия, которые НЕ являются основным пакетом LibreOffice.
_NOT_MAIN_PRODUCT = ("help pack", "language pack", "sdk")
# Объём для оценки свободного места: msi + CAB + распаковка (байт).
REQUIRED_FREE_BYTES = int(2.5 * 2**30)

# Библиотеки Visual C++ (x64), которые берутся из msi в program\, если их нет.
_VC_RUNTIME = re.compile(
    r"^(vcruntime140(_1|_threads)?|msvcp140(_1|_2|_atomic_wait|_codecvt_ids)?|concrt140|vccorlib140)\.dll$",
    re.I,
)

_install_lock = threading.Lock()

# Очередь core.jobs, в которой идут скачивание и офисные конвертации.
QUEUE = "office"


class InstallError(Exception):
    """Установка не удалась; `str()` — понятный пользователю текст."""


class InstallCancelled(Exception):
    """Установка отменена пользователем."""


# --- Состояние ----------------------------------------------------------------


def module_path() -> Path:
    return paths.data_dir_path() / paths.MODULES_DIR_NAME / MODULE_DIR_NAME


def soffice_path() -> Path:
    return module_path() / "program" / "soffice.exe"


PROFILE_TEMPLATE_NAME = "konverter-profile"


def profile_template() -> Path | None:
    """Шаблон профиля LibreOffice (см. `soffice` — профиль копируется на задачу)."""
    path = module_path() / PROFILE_TEMPLATE_NAME
    return path if path.is_dir() else None


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """«26.2.6» / «26.2.6.3» → (26, 2, 6); иначе None."""
    match = re.match(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:\.\d{1,6})?$", (text or "").strip())
    return tuple(int(x) for x in match.groups()) if match else None  # type: ignore[return-value]


def _marker_version(root: Path) -> str | None:
    """Версия из маркера; "" — маркер есть, но непонятный; None — маркера нет."""
    try:
        data = json.loads((root / MARKER_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return ""
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else ""


def _version_ok(version: str | None) -> bool:
    parsed = parse_version(version)
    return parsed is not None and parsed >= download.MIN_VERSION


def marker_valid(root: Path) -> bool:
    """Готовый модуль в папке `root`: маркер с допустимой версией и soffice.exe."""
    return _version_ok(_marker_version(root)) and (root / "program" / "soffice.exe").is_file()


def is_installed() -> bool:
    return marker_valid(module_path())


def installed_version() -> str | None:
    """Версия из маркера распакованного модуля (в т.ч. устаревшего) или None."""
    root = module_path()
    if not (root / "program" / "soffice.exe").is_file():
        return None
    return _marker_version(root)


def is_outdated() -> bool:
    """Модуль распакован, но версия в маркере ниже допустимой (или непонятна)."""
    version = installed_version()
    return version is not None and not _version_ok(version)


def _min_text() -> str:
    return ".".join(map(str, download.MIN_VERSION))


def availability() -> str:
    """"installed" — готов; "missing" — можно скачать; "outdated" — есть, но
    устарел (скачать заново); "unavailable" — `data/` недоступна для записи
    (Ф19: модуль недоступен, вопрос не задаётся)."""
    if is_installed():
        return "installed"
    if not paths.is_data_writable():
        return "unavailable"
    return "outdated" if is_outdated() else "missing"


def temp_root() -> Path:
    folder = paths.temp_dir()
    if folder is None:
        raise InstallError(MSG_NOT_WRITABLE)
    return folder


MSG_NOT_WRITABLE = tr("office_not_writable")
MSG_NOT_INSTALLED = tr("office_refused")


def outdated_message() -> str:
    return tr("office_outdated").format(version=installed_version() or "?", minimum=_min_text())


def ask_prefix() -> str:
    """Начало вопроса о скачивании для устаревшего модуля (иначе пусто)."""
    if not is_outdated():
        return ""
    return tr("office_outdated_text").format(
        version=installed_version() or "?", minimum=_min_text()
    )


def missing_message() -> str:
    if not paths.is_data_writable():
        return MSG_NOT_WRITABLE
    return outdated_message() if is_outdated() else MSG_NOT_INSTALLED


# --- Уборка -------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        return True  # не знаем — не трогаем


_OWN_DIR = re.compile(rf"^(?:{UNPACK_PREFIX}|{soffice.RUN_PREFIX}|{TRASH_PREFIX})(\d+)-[0-9a-f]{{6,32}}$")


def cleanup_stale() -> int:
    """Удаляет брошенные временные папки модуля (после сбоя или закрытия).

    Трогаются только папки нашего формата в `data/temp`, чей процесс уже
    не работает. Недокачанный msi (`lo-download`) остаётся для докачки:
    при следующей загрузке он будет докачан или удалён, если версия сменилась;
    если к нему не возвращались `STALE_DOWNLOAD_DAYS` дней — удаляется здесь.
    """
    folder = paths.data_dir_path() / paths.TEMP_DIR_NAME
    removed = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    for entry in entries:
        match = _OWN_DIR.match(entry.name)
        if not match or not entry.is_dir(follow_symlinks=False):
            continue
        if _pid_alive(int(match.group(1))):
            continue
        soffice.remove_tree(Path(entry.path))
        removed += 1
    removed += _cleanup_old_downloads(folder / DOWNLOAD_DIR_NAME)
    if removed:
        log.info("Убрано брошенных папок/файлов модуля LibreOffice: %d", removed)
    return removed


STALE_DOWNLOAD_DAYS = 7
_DOWNLOAD_FILE = re.compile(r"^LibreOffice_[0-9.]+_Win_x86-64\.msi(\.part(\.json)?)?$")


def _cleanup_old_downloads(folder: Path) -> int:
    import time

    removed = 0
    limit = time.time() - STALE_DOWNLOAD_DAYS * 86400
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    for entry in entries:
        try:
            if (
                _DOWNLOAD_FILE.match(entry.name)
                and entry.is_file(follow_symlinks=False)
                and entry.stat(follow_symlinks=False).st_mtime < limit
            ):
                os.remove(entry.path)
                removed += 1
        except OSError as exc:
            log.warning("Не удалось удалить %s: %s", entry.path, exc)
    return removed


# --- Установка ----------------------------------------------------------------

ProgressFn = Callable[[float], None]


class _Phases:
    """Пересчёт прогресса этапов в общую долю 0…1."""

    def __init__(self, progress: ProgressFn | None) -> None:
        self._progress = progress
        self._start = 0.0
        self._span = 0.0

    def begin(self, start: float, end: float) -> ProgressFn:
        self._start, self._span = start, end - start
        self(0.0)
        return self

    def __call__(self, fraction: float) -> None:
        if self._progress is not None:
            self._progress(self._start + self._span * min(1.0, max(0.0, fraction)))


def _free_bytes(folder: Path) -> int | None:
    import shutil

    try:
        return shutil.disk_usage(folder).free
    except OSError:
        return None


def verify_authenticity(
    path: Path, signature: str, cancelled: Callable[[], bool] = lambda: False
) -> None:
    """OpenPGP (зашитый ключ TDF) и Authenticode (субъект TDF); иначе InstallError.

    Отмена проверяется между кусками файла (OpenPGP) и во время WinVerifyTrust
    (он идёт в отдельном потоке) → `InstallCancelled`.
    """
    try:
        keys = openpgp.load_keyring(TDF_PUBLIC_KEY, TDF_FINGERPRINT)
        signer = openpgp.verify_file(path, signature, keys, check=lambda: _check(cancelled))
        log.info("Подпись OpenPGP верна: %s", signer.hex)
    except openpgp.PgpError as exc:
        log.warning("Подпись OpenPGP не прошла: %s", exc)
        raise InstallError(tr("office_err_pgp")) from exc
    try:
        name = _authenticode_cancellable(path, cancelled)
        log.info("Authenticode верна: %s", name)
    except authenticode.ChainError as exc:
        log.warning("Authenticode: не построена цепочка сертификатов: %s", exc)
        raise InstallError(tr("office_err_chain")) from exc
    except authenticode.AuthenticodeError as exc:
        log.warning("Authenticode не прошла: %s", exc)
        raise InstallError(tr("office_err_authenticode")) from exc


def _authenticode_cancellable(path: Path, cancelled: Callable[[], bool]) -> str:
    """`authenticode.verify` в отдельном потоке с опросом отмены.

    WinVerifyTrust нельзя прервать; при отмене поток дорабатывает сам и затем
    удаляет файл (пока WinVerifyTrust читает файл, удалить его нельзя).
    """
    result: dict = {}
    done = threading.Event()
    abandoned = threading.Event()

    def work() -> None:
        try:
            result["name"] = authenticode.verify(path)
        except BaseException as exc:  # noqa: BLE001 — передаётся вызывающему
            result["error"] = exc
        finally:
            done.set()
            if abandoned.is_set():
                _remove_file(path)

    threading.Thread(target=work, name="konverter-authenticode", daemon=True).start()
    while not done.wait(0.1):
        if cancelled():
            abandoned.set()
            raise InstallCancelled()
    if "error" in result:
        raise result["error"]
    return result["name"]


def check_product(plan: msi.Plan, release: download.Release, minimum: tuple[int, int, int]) -> None:
    """Версия и название внутри msi совпадают с выбранным выпуском; иначе InstallError."""
    name = plan.product_name or ""
    version = plan.product_version or ""
    parsed = parse_version(version)
    # «LibreOffice» с необязательным номером версии: отсекает LibreOfficeDev, Help Pack, SDK и т.п.
    name_ok = re.fullmatch(r"LibreOffice(?: \d+(?:\.\d+){0,3})?", name) is not None and not any(
        word in name.lower() for word in _NOT_MAIN_PRODUCT
    )
    version_ok = (
        parsed is not None
        and parsed == tuple(release.version)
        and parsed >= tuple(minimum)
        and (version == release.version_text or version.startswith(release.version_text + "."))
    )
    if not (name_ok and version_ok):
        log.warning(
            "Пакет не тот: ProductName=%r ProductVersion=%r, ожидалась %s",
            name, version, release.version_text,
        )
        raise InstallError(tr("office_err_product"))


@contextmanager
def _process_lock(temp: Path):
    """Межпроцессный замок установки (см. докстринг модуля)."""
    if os.name != "nt":
        yield
        return
    import msvcrt

    fd = os.open(temp / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise InstallError(tr("office_err_busy_other")) from exc
        try:
            yield
        finally:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        os.close(fd)


def install(
    progress: ProgressFn | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    *,
    policy: download.Policy = download.Policy(),
) -> str:
    """Скачивает, проверяет и распаковывает LibreOffice. Возвращает версию.

    Ошибки — `InstallError` (текст для пользователя), отмена — `InstallCancelled`
    (недокачанное и распакованное удалены).
    """
    if not _install_lock.acquire(blocking=False):
        raise InstallError(tr("office_err_busy"))
    try:
        if is_installed():
            return installed_version() or ""
        if not paths.is_data_writable():
            raise InstallError(MSG_NOT_WRITABLE)
        with _process_lock(temp_root()):
            if is_installed():  # могла поставить другая копия программы
                return installed_version() or ""
            return _install(progress, cancelled, policy)
    finally:
        _install_lock.release()


def _check(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise InstallCancelled()


def _install(progress, cancelled, policy) -> str:
    phases = _Phases(progress)
    root = temp_root()
    free = _free_bytes(root)
    if free is not None and free < REQUIRED_FREE_BYTES:
        raise InstallError(tr("office_err_no_space").format(
            need=REQUIRED_FREE_BYTES / 2**30, free=free / 2**30))
    phases.begin(0.0, 0.01)
    try:
        release = download.resolve_release(policy, cancelled=cancelled)
    except download.DownloadCancelled as exc:
        raise InstallCancelled() from exc
    except download.DownloadError as exc:
        log.warning("Метаданные LibreOffice: %s", exc)
        raise InstallError(exc.user) from exc
    _check(cancelled)
    log.info("LibreOffice: выбрана версия %s (%s)", release.version_text, release.sha256)

    download_dir = root / DOWNLOAD_DIR_NAME
    try:
        msi_path = download.download_release(
            release, download_dir, policy=policy, progress=phases.begin(0.01, 0.70),
            cancelled=cancelled,
        )
    except download.DownloadCancelled as exc:
        raise InstallCancelled() from exc
    except download.DownloadError as exc:
        log.warning("Загрузка LibreOffice: %s", exc)
        raise InstallError(exc.user) from exc

    try:
        phases.begin(0.70, 0.72)
        verify_authenticity(msi_path, release.signature, cancelled)
        _check(cancelled)
        version = _unpack_and_check(msi_path, release, root, phases, cancelled, policy)
    except (InstallError, InstallCancelled):
        _remove_file(msi_path)
        raise
    except msi.Cancelled as exc:
        _remove_file(msi_path)
        raise InstallCancelled() from exc
    except Exception as exc:
        _remove_file(msi_path)
        log.exception("Распаковка LibreOffice не удалась")
        raise InstallError(tr("office_err_unpack").format(error=exc)) from exc
    _remove_file(msi_path)
    try:
        download_dir.rmdir()  # только если пуста
    except OSError:
        pass
    return version


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Не удалось удалить %s: %s", path, exc)


def unpack_msi(
    msi_path: Path,
    work: Path,
    *,
    progress: ProgressFn | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    expect: Callable[[msi.Plan], None] | None = None,
) -> tuple[Path, msi.Plan, list[str]]:
    """Распаковывает проверенный msi в `work/root`; (папка, план, скопированные VC++ DLL).

    `expect(plan)` вызывается сразу после чтения таблиц, ДО распаковки (сверка
    версии и названия пакета).
    """

    def check() -> None:
        if cancelled():
            raise msi.Cancelled()

    phases = _Phases(progress)
    plan = msi.read_plan(msi_path)
    if expect is not None:
        expect(plan)
    if not plan.cabinets:
        raise msi.MsiError("в msi нет встроенных CAB")
    cab_dir, flat_dir, install_dir = work / "cab", work / "flat", work / "root"
    cab_dir.mkdir()
    flat_dir.mkdir()
    install_dir.mkdir()
    phases.begin(0.0, 0.1)
    cabs = msi.extract_cabinets(msi_path, plan, cab_dir, check)
    expected = len(plan.files)
    for index, cab in enumerate(cabs):
        start = 0.1 + 0.8 * index / len(cabs)
        msi.expand_cabinet(
            cab, flat_dir, expected_files=expected, check=check,
            progress=phases.begin(start, start + 0.8 / len(cabs)),
        )
        _remove_file(cab)
    phases.begin(0.9, 1.0)
    copied = msi.place_files(
        plan, flat_dir, install_dir, check=check,
        system_whitelist=lambda name: bool(_VC_RUNTIME.match(name)),
    )
    soffice.remove_tree(flat_dir)
    soffice.remove_tree(cab_dir)
    phases(1.0)
    return install_dir, plan, copied


def trial_conversion(
    soffice_exe: Path, temp: Path, poll: Callable[[], None], keep_profile: Path | None = None
) -> None:
    """Пробная конвертация маленького текста в PDF; иначе InstallError.

    `keep_profile` — куда сохранить получившийся (уже инициализированный)
    профиль как шаблон для будущих конвертаций.
    """
    with soffice.work_dir(temp) as work:
        source = work / "probe.txt"
        source.write_text("Konverter: проверка LibreOffice\n", encoding="utf-8")
        try:
            produced = soffice.convert(soffice_exe, work, source, "txt", "pdf", poll=poll, timeout=300)
        except soffice.SofficeError as exc:
            log.warning("Проверочная конвертация LibreOffice: %s", exc)
            raise InstallError(tr("office_err_trial")) from exc
        with open(produced, "rb") as fh:
            if not fh.read(5).startswith(b"%PDF-"):
                raise InstallError(tr("office_err_trial_output"))
        if keep_profile is not None:
            import shutil

            shutil.copytree(work / "profile", keep_profile, symlinks=False)


def _unpack_and_check(
    msi_path: Path, release: download.Release, root: Path, phases, cancelled,
    policy: download.Policy,
) -> str:
    work = root / f"{UNPACK_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:12]}"
    work.mkdir()
    try:
        install_dir, plan, copied = unpack_msi(
            msi_path, work, progress=phases.begin(0.72, 0.95), cancelled=cancelled,
            expect=lambda plan: check_product(plan, release, policy.min_version),
        )
        log.info(
            "LibreOffice распакован: %d файлов, VC++ из msi: %s", len(plan.files), copied or "не нужны"
        )
        exe = install_dir / "program" / "soffice.exe"
        if not exe.is_file():
            raise InstallError(tr("office_err_no_soffice"))

        def poll() -> None:
            if cancelled():
                raise InstallCancelled()

        phases.begin(0.95, 1.0)
        trial_conversion(exe, root, poll, keep_profile=install_dir / PROFILE_TEMPLATE_NAME)
        marker = {
            "version": release.version_text,
            "product_version": plan.product_version,
            "sha256": release.sha256,
            "installed": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "vc_runtime_from_msi": copied,
        }
        (install_dir / MARKER_NAME).write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
        _activate(install_dir, root)
        phases(1.0)
        return release.version_text
    finally:
        soffice.remove_tree(work)


def _activate(install_dir: Path, temp: Path) -> None:
    """Атомарно переносит готовую папку в `data/modules/libreoffice`."""
    modules = paths.modules_dir()
    if modules is None:
        raise InstallError(MSG_NOT_WRITABLE)
    target = modules / MODULE_DIR_NAME
    if target.exists():
        if marker_valid(target):
            # Готовый модуль (например, поставила другая копия программы) не трогаем.
            log.info("Модуль LibreOffice уже на месте — свою распаковку отбрасываю")
            soffice.remove_tree(install_dir)
            return
        # Неполная, устаревшая или без маркера — убираем через корзину в temp.
        trash = temp / f"{TRASH_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:12]}"
        try:
            os.replace(target, trash)
        except OSError as exc:
            raise InstallError(tr("office_err_unpack").format(error=exc.strerror or exc)) from exc
        soffice.remove_tree(trash)
    os.replace(install_dir, target)


def install_job(progress: ProgressFn, cancel) -> None:
    """Служебная задача очереди (`JobManager.submit_runner`): установка модуля.

    `cancel` — `CancelToken`. Отмена → `Cancelled`, ошибка → `ConversionError`.
    """
    from konverter.converters.base import Cancelled, ConversionError

    try:
        version = install(progress, lambda: cancel.cancelled)
    except InstallCancelled as exc:
        raise Cancelled() from exc
    except InstallError as exc:
        raise ConversionError(str(exc)) from exc
    log.info("Модуль LibreOffice %s готов", version)
