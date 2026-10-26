"""Запуск LibreOffice без окна для одной конвертации (Ф17).

* Папка работы `data/temp/lo-run-<pid>-<случайное>/`: `in/input.<расш>` —
  копия входа под ASCII-именем с расширением по коду формата (не по имени
  пользователя: ни кириллицы, ни пробелов, ни подсунутого расширения),
  `out/` — результат, `profile/` — профиль LibreOffice.
* Профиль НОВЫЙ на каждую задачу: враждебный документ не может оставить в
  профиле ничего (настройки, автотекст, «доверенные места», кэш), что
  повлияет на следующие конвертации; параллельные задачи профиль не делят
  (и IPC-канал LibreOffice строится от пути профиля, так что открытый у
  пользователя LibreOffice не перехватит команду). Чтобы не платить за
  первичную инициализацию профиля (~9 с против ~3 с на 26.2.6), профиль
  КОПИРУЕТСЯ из шаблона `template` (его создаёт проверочная конвертация при
  установке на нашем собственном тексте; конвертации шаблон не меняют), а
  `registrymodifications.xcu` после копирования всё равно перезаписывается.
* В профиль заранее кладётся `user/registrymodifications.xcu` с
  безопасными настройками (`XCU_SETTINGS`): макросы выключены, активное
  содержимое (OLE/DDE) выключено, Python и OLE-автоматизация выключены,
  ссылки из недоверенных мест заблокированы, «экзотические» форматы не
  загружаются, внешние ссылки Calc/Writer не обновляются, без отчётов о сбоях
  и мастера первого запуска, а сетевой прокси — заведомо нерабочий
  (127.0.0.1:9), чтобы документ не мог тянуть ресурсы из сети.
* Процесс — в объекте задания (`jobobject.run`): KILL_ON_JOB_CLOSE, лимит
  памяти `MEMORY_LIMIT`, запуск приостановленным; отмена (опрос каждые
  0,25 с) и таймаут `TIMEOUT` убивают всё дерево; после любой развязки
  процессов задачи не остаётся.
* Длинные пути: в профиле LibreOffice есть глубокие папки
  (`user/extensions/bundled/registry/…PackageRegistryBackend/luXXXX.tmp/…`,
  ~150 символов от корня профиля), поэтому при длинном пути к программе
  полный путь превышает MAX_PATH (260). Копирование и удаление таких деревьев
  идут через расширенный путь `paths.long_path` (префикс `\\\\?\\`).
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
from xml.sax.saxutils import escape, quoteattr

from konverter import paths

from . import jobobject

log = logging.getLogger(__name__)

TIMEOUT = 600.0
MEMORY_LIMIT = 3 * 2**30
RUN_PREFIX = "lo-run-"

# Расширение входной копии по коду формата (core.detect).
INPUT_EXTENSIONS = {
    "docx": "docx", "doc": "doc", "odt": "odt", "rtf": "rtf",
    "xlsx": "xlsx", "xls": "xls", "ods": "ods",
    "pptx": "pptx", "ppt": "ppt", "odp": "odp",
    "txt": "txt",  # только для проверочного запуска после установки
}
FAMILY = {
    "docx": "writer", "doc": "writer", "odt": "writer", "rtf": "writer", "txt": "writer",
    "xlsx": "calc", "xls": "calc", "ods": "calc",
    "pptx": "impress", "ppt": "impress", "odp": "impress",
}
# Фильтр экспорта: (семейство, вид результата) → аргумент --convert-to.
FILTERS = {
    ("writer", "pdf"): "pdf:writer_pdf_Export",
    ("calc", "pdf"): "pdf:calc_pdf_Export",
    ("impress", "pdf"): "pdf:impress_pdf_Export",
    ("writer", "docx"): "docx:MS Word 2007 XML",
    ("calc", "xlsx"): "xlsx:Calc MS Excel 2007 XML",
}

# (путь узла, имя свойства, тип, значение) — см. докстринг модуля.
# Имена и типы сверены со схемами установленного LibreOffice 26.2.6
# (share/registry/main.xcd, component-schema), кроме элемента набора Jobs.
XCU_SETTINGS: tuple[tuple[str, str, str, str], ...] = (
    ("/org.openoffice.Office.Common/Security/Scripting", "MacroSecurityLevel", "int", "3"),
    ("/org.openoffice.Office.Common/Security/Scripting", "DisableMacrosExecution", "boolean", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "DisableActiveContent", "boolean", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "DisablePythonRuntime", "boolean", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "DisableOLEAutomation", "boolean", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "BlockUntrustedRefererLinks", "boolean", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "CheckDocumentEvents", "boolean", "true"),
    ("/org.openoffice.Office.Common/Security", "LoadExoticFileFormats", "int", "0"),
    ("/org.openoffice.Office.Common/Misc", "CrashReport", "boolean", "false"),
    ("/org.openoffice.Office.Common/Misc", "ShowTipOfTheDay", "boolean", "false"),
    ("/org.openoffice.Office.Common/Misc", "FirstRun", "boolean", "false"),
    ("/org.openoffice.Setup/Office", "ooSetupInstCompleted", "boolean", "true"),
    # Обновление внешних ссылок при загрузке — шкалы у Calc и Writer РАЗНЫЕ:
    # Calc (ScLkUpdMode): 0 всегда, 1 никогда, 2 по запросу (в схеме по умолчанию 2);
    # Writer (SwLinkUpdateMode, как и UNO LinkUpdateModes): 0 никогда, 1 по запросу,
    # 2 автоматически (в схеме по умолчанию 1). Подпись в схеме Writer
    # «Always/On request/Never» — порядок кнопок в диалоге, а не значения.
    # Значения перечислений — по исходникам LibreOffice (по памяти, исходник в
    # этой работе не открывался); на практике ревьюер проверил, что связанные
    # разделы при конвертации не подтягиваются ни при 0, ни при 1, ни при 2.
    ("/org.openoffice.Office.Calc/Content/Update", "Link", "int", "1"),
    ("/org.openoffice.Office.Writer/Content/Update", "Link", "int", "0"),
    # Сеть: ручной прокси (0 — нет, 1 — системный, 2 — ручной; в установке
    # по умолчанию 1) на закрытый локальный порт, без исключений.
    ("/org.openoffice.Inet/Settings", "ooInetProxyType", "int", "2"),
    ("/org.openoffice.Inet/Settings", "ooInetHTTPProxyName", "string", "127.0.0.1"),
    ("/org.openoffice.Inet/Settings", "ooInetHTTPProxyPort", "int", "9"),
    ("/org.openoffice.Inet/Settings", "ooInetHTTPSProxyName", "string", "127.0.0.1"),
    ("/org.openoffice.Inet/Settings", "ooInetHTTPSProxyPort", "int", "9"),
    ("/org.openoffice.Inet/Settings", "ooInetFTPProxyName", "string", "127.0.0.1"),
    ("/org.openoffice.Inet/Settings", "ooInetFTPProxyPort", "int", "9"),
    ("/org.openoffice.Inet/Settings", "ooInetNoProxy", "string", ""),
    # Проверка обновлений не запускается (модуль обновления и так не распаковывается).
    ("/org.openoffice.Office.Jobs/Jobs/org.openoffice.Office.Jobs:Job['UpdateCheck']/Arguments",
     "AutoCheckEnabled", "boolean", "false"),
)


class SofficeError(Exception):
    """LibreOffice не смог выполнить конвертацию (текст — для журнала)."""


class SofficeTimeout(SofficeError):
    pass


def registry_xcu(settings=XCU_SETTINGS) -> str:
    """Текст `registrymodifications.xcu` с настройками `settings`."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
    ]
    for path, name, kind, value in settings:
        lines.append(
            f"<item oor:path={quoteattr(path)}><prop oor:name={quoteattr(name)} oor:op=\"fuse\">"
            f"<value>{escape(value)}</value></prop></item>"
        )
    lines.append("</oor:items>")
    return "\n".join(lines) + "\n"


def write_profile(profile: Path) -> None:
    user = profile / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "registrymodifications.xcu").write_text(registry_xcu(), encoding="utf-8")


# Что не копируется из профиля в шаблон и из шаблона в папку работы
# (имена в КОРНЕ профиля): LibreOffice пересоздаёт их сам при запуске.
# * `cache` — кэш скомпилированных ядер OpenCL/формул (`*.bin`,
#   `opencl_profile.xml`); при отсутствии строится заново.
# * `crash` — `dump.ini` с параметрами отчёта о сбое; пишется заново при
#   каждом запуске (отчёты о сбоях у нас выключены).
# * `.lock` — блокировка профиля работавшего процесса (после его завершения
#   обычно уже удалена; чужая блокировка новой копии не нужна).
# НЕ пропускаются папки `luXXXX.tmp` в `user/extensions/…/registry/…`: несмотря
# на имя, это данные зарегистрированных встроенных расширений (словари,
# справка) — на них ссылаются `backenddb.xml` и `configmgr.ini`; без них
# профиль несогласован. Длинные пути в них решаются расширенным путём.
PROFILE_SKIP_ROOT = frozenset({"cache", "crash", ".lock"})


def profile_ignore(root: Path | str) -> Callable[[str, list[str]], set[str]]:
    """Фильтр `ignore` для копирования профиля: `PROFILE_SKIP_ROOT` только в корне."""
    root_key = os.path.normcase(paths.long_path(root))

    def ignore(directory: str, names: list[str]) -> set[str]:
        if os.path.normcase(paths.long_path(directory)) != root_key:
            return set()
        return {name for name in names if name.lower() in PROFILE_SKIP_ROOT}

    return ignore


def copy_tree(src: Path, dst: Path, *, skip_profile_caches: bool = False) -> None:
    """Копирует дерево (ссылки не раскрываются) через расширенные пути.

    Ошибки — как у `shutil.copytree` (`shutil.Error` со списком файлов,
    OSError); частичную копию убирает вызывающий.
    """
    ignore = profile_ignore(src) if skip_profile_caches else None
    shutil.copytree(paths.long_path(src), paths.long_path(dst), symlinks=False, ignore=ignore)


def error_summary(exc: BaseException) -> str:
    """Короткое описание ошибки копирования (без списка всех файлов из shutil.Error)."""
    if isinstance(exc, shutil.Error) and exc.args and isinstance(exc.args[0], list):
        errors = exc.args[0]
        first = errors[0][2] if errors and len(errors[0]) == 3 else "?"
        return f"не скопировано объектов: {len(errors)}, первая ошибка: {first}"
    return str(exc)


def remove_tree(path: Path) -> None:
    """Удаляет СВОЮ временную папку (ссылки не раскрываются); ошибки — в журнал.

    Через расширенный путь: в профиле LibreOffice бывают пути > 260 символов.
    """

    def on_error(func, target, exc) -> None:
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            log.warning("Не удалось удалить %s: %s", target, exc)

    try:
        shutil.rmtree(paths.long_path(path), onexc=on_error)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Не удалось удалить %s: %s", path, exc)


@contextmanager
def work_dir(temp_root: Path) -> Iterator[Path]:
    """Новая папка работы внутри `temp_root`; удаляется при выходе."""
    path = temp_root / f"{RUN_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        remove_tree(path)


def build_args(soffice: Path, profile: Path, convert_to: str, out_dir: Path, source: Path) -> list[str]:
    return [
        str(soffice),
        f"-env:UserInstallation={profile.absolute().as_uri()}",
        "--headless", "--invisible", "--norestore", "--nologo", "--nodefault",
        "--nolockcheck", "--nofirststartwizard",
        "--convert-to", convert_to,
        "--outdir", str(out_dir),
        str(source),
    ]


def convert(
    soffice: Path,
    work: Path,
    source: Path,
    fmt: str,
    kind: str,
    *,
    poll: Callable[[], None] | None = None,
    timeout: float = TIMEOUT,
    memory_limit: int = MEMORY_LIMIT,
    template: Path | None = None,
) -> Path:
    """Конвертирует `source` (формат `fmt`) в `kind` ("pdf"/"docx"/"xlsx").

    Возвращает путь к результату внутри `work/out`. `poll()` — проверка
    отмены (исключение из неё убивает LibreOffice и пробрасывается).
    """
    family = FAMILY.get(fmt)
    ext = INPUT_EXTENSIONS.get(fmt)
    convert_to = FILTERS.get((family, kind)) if family else None
    if ext is None or convert_to is None:
        raise SofficeError(f"неподдерживаемая пара {fmt} → {kind}")
    work = Path(os.path.abspath(work))  # LibreOffice запускается с cwd=work
    in_dir, out_dir, profile = work / "in", work / "out", work / "profile"
    in_dir.mkdir()
    out_dir.mkdir()
    if template is not None and template.is_dir():
        try:
            copy_tree(template, profile, skip_profile_caches=True)
        except (shutil.Error, OSError) as exc:
            # Не беда: LibreOffice создаст профиль сам (только медленнее).
            log.warning(
                "Шаблон профиля LibreOffice не скопирован (%s): %s — запуск с новым профилем",
                template, error_summary(exc),
            )
            log.debug("Подробности копирования шаблона: %r", exc)
            remove_tree(profile)
    write_profile(profile)
    copy = in_dir / f"input.{ext}"
    shutil.copyfile(source, copy)
    args = build_args(soffice, profile, convert_to, out_dir, copy)
    # Переменные, которыми можно перенастроить LibreOffice/UNO/встроенный
    # Python извне, — не передаются.
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("URE_", "UNO_", "SAL_", "PYTHON", "OOO_", "LIBO_"))
    }
    # Временные файлы LibreOffice — в папку работы, а не в %TEMP% системы.
    tmp = work / "tmp"
    tmp.mkdir()
    env["TMP"] = env["TEMP"] = str(tmp)
    result = jobobject.run(
        args, timeout=timeout, poll=poll, cwd=work, env=env, memory_limit=memory_limit,
    )
    if result.timed_out:
        raise SofficeTimeout("LibreOffice не уложился во время")
    produced = out_dir / f"input.{kind}"
    if result.returncode != 0 or not produced.is_file() or produced.stat().st_size == 0:
        raise SofficeError(f"код {result.returncode}, результат {'есть' if produced.exists() else 'нет'}")
    return produced
