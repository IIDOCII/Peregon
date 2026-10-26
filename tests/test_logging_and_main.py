"""Журнал работы и точка входа (ревью, п.7а, 7г, п.2 — freeze_support)."""

from __future__ import annotations

import logging

import pytest

import konverter
from konverter import logging_setup, paths


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    logging.captureWarnings(False)
    logging_setup._release_lock()  # блокировку «первой копии» не держим после теста


def test_log_file_with_rotation(tmp_path, monkeypatch, restore_root_logging):
    monkeypatch.setattr(paths, "app_root", lambda: tmp_path)
    log_path = logging_setup.setup_logging()
    assert log_path == tmp_path / "data" / "logs" / "konverter.log"
    logging.getLogger("konverter.test").info("проверка журнала")
    handler = next(h for h in logging.getLogger().handlers
                   if getattr(h, "_konverter_handler", False))
    handler.flush()
    text = log_path.read_text(encoding="utf-8")
    assert "проверка журнала" in text and "INFO" in text and "[MainThread]" in text
    assert handler.maxBytes == logging_setup.LOG_MAX_BYTES and handler.backupCount == 3

    # Повторный вызов не плодит обработчики.
    logging_setup.setup_logging()
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_konverter_handler", False)]
    assert len(ours) == 1


def test_log_falls_back_when_folder_unwritable(tmp_path, monkeypatch, restore_root_logging):
    monkeypatch.setattr(paths, "logs_dir", lambda: None)
    assert logging_setup.setup_logging() is None
    logging.getLogger("konverter.test").info("не падаем")


def test_log_falls_back_when_file_cannot_open(tmp_path, monkeypatch, restore_root_logging):
    monkeypatch.setattr(paths, "logs_dir", lambda: tmp_path)

    def deny(*args, **kwargs):
        raise PermissionError("нет доступа")

    monkeypatch.setattr(logging_setup, "RotatingFileHandler", deny)
    assert logging_setup.setup_logging() is None


def test_main_order(monkeypatch):
    calls: list[str] = []
    import multiprocessing

    import konverter.core.temp_journal as tj
    import konverter.ui.main_window as mw

    monkeypatch.setattr(multiprocessing, "freeze_support", lambda: calls.append("freeze"))
    monkeypatch.setattr(logging_setup, "setup_logging", lambda: calls.append("log"))
    monkeypatch.setattr(tj, "cleanup_orphans", lambda: calls.append("orphans") or 0)
    monkeypatch.setattr(mw, "run", lambda: calls.append("gui") or 0)
    with pytest.raises(SystemExit) as info:
        konverter.main()
    assert info.value.code == 0
    assert calls == ["freeze", "log", "orphans", "gui"]


# --- Ревью 2, п.5: две копии программы не делят один журнал с ротацией -----------


def _hold_log_lock(folder):
    """«Первая копия программы»: держит блокировку журнала с отдельного дескриптора."""
    import os

    from konverter.core import temp_journal

    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder / "konverter.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    assert temp_journal._try_lock(fd)
    return fd


def _release(fd):
    import os

    from konverter.core import temp_journal

    temp_journal._unlock(fd)
    os.close(fd)


def test_second_instance_logs_to_pid_file(tmp_path, monkeypatch, restore_root_logging):
    import os
    from logging.handlers import RotatingFileHandler

    monkeypatch.setattr(paths, "app_root", lambda: tmp_path)
    logs = tmp_path / "data" / "logs"
    fd = _hold_log_lock(logs)
    try:
        log_path = logging_setup.setup_logging()
        assert log_path == logs / f"konverter-{os.getpid()}.log"
        handler = next(h for h in logging.getLogger().handlers
                       if getattr(h, "_konverter_handler", False))
        assert not isinstance(handler, RotatingFileHandler)
        logging.getLogger("konverter.test").info("вторая копия")
        handler.flush()
        assert "вторая копия" in log_path.read_text(encoding="utf-8")
        assert not (logs / "konverter.log").exists()
    finally:
        _release(fd)
        logging_setup.shutdown_logging()


def test_first_instance_gets_main_log(tmp_path, monkeypatch, restore_root_logging):
    monkeypatch.setattr(paths, "app_root", lambda: tmp_path)
    logs = tmp_path / "data" / "logs"
    try:
        assert logging_setup.setup_logging() == logs / "konverter.log"
        # Пока первая копия жива, блокировку не взять.
        import os

        from konverter.core import temp_journal

        fd = os.open(logs / "konverter.lock", os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            assert not temp_journal._try_lock(fd)
        finally:
            os.close(fd)
    finally:
        logging_setup.shutdown_logging()


def test_pid_log_size_capped(tmp_path, monkeypatch, restore_root_logging):
    monkeypatch.setattr(paths, "app_root", lambda: tmp_path)
    monkeypatch.setattr(logging_setup, "LOG_MAX_BYTES", 2000)
    fd = _hold_log_lock(tmp_path / "data" / "logs")
    try:
        log_path = logging_setup.setup_logging()
        for i in range(200):
            logging.getLogger("konverter.test").info("строка %d", i)
        for h in logging.getLogger().handlers:
            h.flush()
        assert log_path.stat().st_size <= 2000 + 500
    finally:
        _release(fd)
        logging_setup.shutdown_logging()


def test_old_pid_logs_removed_by_exact_pattern(tmp_path, monkeypatch, restore_root_logging):
    import os
    import time

    monkeypatch.setattr(paths, "app_root", lambda: tmp_path)
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    old = time.time() - (logging_setup.EXTRA_LOG_MAX_AGE_DAYS + 1) * 86400
    names = {
        "konverter-123.log": False,  # старый журнал второй копии — удалить
        "konverter-456.log": True,   # свежий — оставить
        "konverter-123.log.txt": True,
        "konverter-abc.log": True,
        "konverter.log.1": True,
        "notes-1.log": True,
    }
    for name, keep in names.items():
        path = logs / name
        path.write_text("x", encoding="utf-8")
        if name != "konverter-456.log":
            os.utime(path, (old, old))
    try:
        logging_setup.setup_logging()
        for name, keep in names.items():
            assert (logs / name).exists() == keep, name
    finally:
        logging_setup.shutdown_logging()


def test_gui_script_declared():
    """Ревью 2, п.10: запуск без консоли — `konverter-gui` в [project.gui-scripts]."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["gui-scripts"]["konverter-gui"] == "konverter:main"
    assert data["project"]["scripts"]["konverter"] == "konverter:main"  # консольный — для отладки


def _pe_subsystem(path) -> int:
    import struct

    data = path.read_bytes()[:4096]
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[pe:pe + 4] == b"PE\0\0"
    return struct.unpack_from("<H", data, pe + 24 + 68)[0]


def test_gui_launcher_is_windows_subsystem():
    """Если окружение установлено (uv sync), konverter-gui.exe — без консоли (подсистема GUI = 2)."""
    import os
    import sys
    from pathlib import Path

    exe = Path(sys.prefix) / "Scripts" / "konverter-gui.exe"
    if os.name != "nt" or not exe.exists():
        pytest.skip("лаунчер не установлен в этом окружении")
    assert _pe_subsystem(exe) == 2
    assert _pe_subsystem(Path(sys.prefix) / "Scripts" / "konverter.exe") == 3
