"""Небольшие настройки программы в `data/settings/settings.json`.

Хранится: позиция окна, последний выбранный пресет качества и что делать,
когда все задачи завершились (уведомление, звук, открыть папку результата).
Битый JSON, неверные типы, слишком большой файл, отсутствие прав — всё это
даёт значения по умолчанию (для каждого поля отдельно) и не роняет
программу. Запись атомарная: временный файл в той же папке + `os.replace`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from konverter import paths
from konverter.converters.base import Preset

log = logging.getLogger(__name__)

SETTINGS_FILE_NAME = "settings.json"
# Настройки маленькие; больший файл считаем испорченным и не читаем.
MAX_SETTINGS_SIZE = 64 * 1024
# Разумный предел координат окна (защита от мусора в файле).
MAX_COORDINATE = 100_000


# Флажки «по завершении всех задач» (bool; значения по умолчанию — в Settings).
_BOOL_FIELDS = ("notify_on_finish", "sound_on_finish", "open_folder_on_finish")


@dataclass
class Settings:
    """Настройки. `window_pos` — (x, y) левого верхнего угла или None.

    `notify_on_finish`, `sound_on_finish`, `open_folder_on_finish` — когда
    завершились все задачи: уведомление Windows, системный звук (оба по
    умолчанию включены), Проводник с последним результатом (по умолчанию
    выключен — решение владельца). Окна настроек нет — только этот файл.
    """

    window_pos: tuple[int, int] | None = None
    last_preset: Preset = Preset.MEDIUM
    notify_on_finish: bool = True
    sound_on_finish: bool = True
    open_folder_on_finish: bool = False

    def to_dict(self) -> dict:
        return {
            "window_pos": list(self.window_pos) if self.window_pos else None,
            "last_preset": self.last_preset.value,
            **{name: getattr(self, name) for name in _BOOL_FIELDS},
        }

    @classmethod
    def from_dict(cls, data: object) -> "Settings":
        """Строит настройки из разобранного JSON, проверяя каждое поле."""
        result = cls()
        if not isinstance(data, dict):
            return result
        pos = data.get("window_pos")
        if (
            isinstance(pos, list)
            and len(pos) == 2
            and all(type(v) is int and abs(v) <= MAX_COORDINATE for v in pos)
        ):
            result.window_pos = (pos[0], pos[1])
        preset = data.get("last_preset")
        if isinstance(preset, str):
            try:
                result.last_preset = Preset(preset)
            except ValueError:
                pass
        for name in _BOOL_FIELDS:
            value = data.get(name)
            if isinstance(value, bool):
                setattr(result, name, value)
        return result


def settings_path() -> Path | None:
    """Путь к файлу настроек или None, если папку настроек создать нельзя."""
    folder = paths.settings_dir()
    return folder / SETTINGS_FILE_NAME if folder is not None else None


def load_settings(path: Path | None = None) -> Settings:
    """Читает настройки; при любой проблеме — значения по умолчанию."""
    if path is None:
        path = settings_path()
        if path is None:
            return Settings()
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_SETTINGS_SIZE + 1)
    except FileNotFoundError:
        return Settings()
    except OSError as exc:
        log.warning("Не удалось прочитать настройки %s: %s", path, exc)
        return Settings()
    if len(raw) > MAX_SETTINGS_SIZE:
        log.warning("Файл настроек %s слишком большой, игнорирую", path)
        return Settings()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        log.warning("Файл настроек %s испорчен: %s", path, exc)
        return Settings()
    return Settings.from_dict(data)


def save_settings(settings: Settings, path: Path | None = None) -> bool:
    """Сохраняет настройки атомарно. True — успешно, False — не удалось."""
    if path is None:
        path = settings_path()
        if path is None:
            return False
    path = Path(path)
    tmp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".settings-", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings.to_dict(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        return True
    except OSError as exc:
        log.warning("Не удалось сохранить настройки %s: %s", path, exc)
        return False
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
