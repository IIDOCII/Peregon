"""Тесты настроек (core/settings.py)."""

from __future__ import annotations

import json

from konverter import paths
from konverter.converters.base import Preset
from konverter.core import settings as settings_mod
from konverter.core.settings import Settings, load_settings, save_settings


def test_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    assert save_settings(Settings(window_pos=(10, -20), last_preset=Preset.SMALL), path)
    loaded = load_settings(path)
    assert loaded.window_pos == (10, -20)
    assert loaded.last_preset == Preset.SMALL
    # Временных файлов не осталось.
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_missing_file_gives_defaults(tmp_path):
    assert load_settings(tmp_path / "нет.json") == Settings()


def test_broken_json_gives_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{не json", encoding="utf-8")
    assert load_settings(path) == Settings()
    path.write_bytes(b"\xff\xfe\x00garbage")
    assert load_settings(path) == Settings()


def test_wrong_types_per_field(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"window_pos": ["a", 1], "last_preset": "high"}), encoding="utf-8"
    )
    loaded = load_settings(path)
    assert loaded.window_pos is None
    assert loaded.last_preset == Preset.HIGH

    path.write_text(json.dumps({"window_pos": [True, 10**9], "last_preset": 5}))
    assert load_settings(path) == Settings()

    path.write_text(json.dumps([1, 2, 3]))
    assert load_settings(path) == Settings()


def test_too_big_file_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(" " * (settings_mod.MAX_SETTINGS_SIZE + 10) + "{}")
    assert load_settings(path) == Settings()


def test_no_permission_does_not_crash(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise PermissionError(13, "Отказано в доступе (смоделировано)")

    path = tmp_path / "settings.json"
    path.write_text("{}")
    monkeypatch.setattr("builtins.open", deny)
    assert load_settings(path) == Settings()
    monkeypatch.undo()

    monkeypatch.setattr(settings_mod.tempfile, "mkstemp", deny)
    assert save_settings(Settings(), path) is False
    assert path.read_text() == "{}"


def test_default_path_inside_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "app_root", lambda: tmp_path)
    assert save_settings(Settings(window_pos=(1, 2)))
    assert (tmp_path / "data" / "settings" / "settings.json").is_file()
    assert load_settings().window_pos == (1, 2)


def test_settings_dir_unavailable(monkeypatch):
    monkeypatch.setattr(paths, "settings_dir", lambda: None)
    assert load_settings() == Settings()
    assert save_settings(Settings()) is False
