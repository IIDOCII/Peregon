"""Пути программы.

Всё, что программа пишет (настройки, логи, временные файлы, скачанные
модули), хранится внутри папки программы, в подпапке `data/`.

- В собранной PyInstaller-версии (`sys.frozen`) корень — папка рядом с exe.
- При разработке корень — папка проекта (та, где лежит pyproject.toml).

Требование Ф19: если папка программы недоступна для записи (например,
лежит в Program Files), программа не падает, а сообщает об этом и работает
без сохранения настроек. Поэтому функции здесь не бросают исключений
файловой системы наружу: подпапки возвращаются как `Path | None`, а для
проверки есть `is_writable()`.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Имя подпапки с рабочими данными программы.
DATA_DIR_NAME = "data"

# Имена подпапок внутри data/.
SETTINGS_DIR_NAME = "settings"
LOGS_DIR_NAME = "logs"
TEMP_DIR_NAME = "temp"
MODULES_DIR_NAME = "modules"

# Файл-маркер корня проекта при разработке.
_PROJECT_MARKER = "pyproject.toml"


def app_root() -> Path:
    """Возвращает корневую папку программы.

    В собранной версии — папку с exe. При разработке — ближайшую вверх
    от этого файла папку, содержащую pyproject.toml. Если такой нет
    (например, пакет установлен куда-то ещё), — текущую рабочую папку.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / _PROJECT_MARKER).is_file():
            return candidate

    log.warning("Не найден %s выше %s, использую текущую папку", _PROJECT_MARKER, here)
    return Path.cwd()


def data_dir_path() -> Path:
    """Путь к папке data/ (без создания)."""
    return app_root() / DATA_DIR_NAME


def _ensure_dir(path: Path) -> Path | None:
    """Создаёт папку при необходимости. При ошибке возвращает None и пишет в лог."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Не удалось создать папку %s: %s", path, exc)
        return None
    return path


def data_dir() -> Path | None:
    """Папка data/; создаётся по требованию. None — если создать нельзя."""
    return _ensure_dir(data_dir_path())


def _data_subdir(name: str) -> Path | None:
    """Подпапка внутри data/; создаётся по требованию. None — если создать нельзя."""
    return _ensure_dir(data_dir_path() / name)


def settings_dir() -> Path | None:
    """Папка для настроек."""
    return _data_subdir(SETTINGS_DIR_NAME)


def logs_dir() -> Path | None:
    """Папка для логов."""
    return _data_subdir(LOGS_DIR_NAME)


def temp_dir() -> Path | None:
    """Папка для временных файлов."""
    return _data_subdir(TEMP_DIR_NAME)


def modules_dir() -> Path | None:
    """Папка для скачанных модулей."""
    return _data_subdir(MODULES_DIR_NAME)


def is_writable(folder: Path) -> bool:
    """Проверяет, можно ли писать в папку.

    Проверка делается на деле: создаётся и сразу удаляется пробный файл.
    Одних атрибутов/прав недостаточно — на Windows запись в Program Files
    может запрещаться UAC или перенаправляться (виртуализация), поэтому
    надёжнее попробовать. Если папки нет, она создаётся.
    Никаких исключений наружу не выбрасывает.
    """
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".write_probe_", dir=folder)
    except OSError as exc:
        log.info("Папка %s недоступна для записи: %s", folder, exc)
        return False

    try:
        os.close(fd)
        os.remove(probe)
    except OSError as exc:
        # Запись удалась, а убрать пробный файл не вышло — считаем папку
        # доступной, но отмечаем в логе.
        log.warning("Не удалось удалить пробный файл %s: %s", probe, exc)
    return True


def is_data_writable() -> bool:
    """Можно ли сохранять рабочие данные программы (папка data/). См. Ф19."""
    return is_writable(data_dir_path())
