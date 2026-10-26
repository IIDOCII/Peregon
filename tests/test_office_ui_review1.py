"""Интерфейс модуля LibreOffice: исправления ревью 1 (порядок вопросов, устаревший модуль)."""

from __future__ import annotations

import json

from PySide6.QtWidgets import QFileDialog

from konverter.core import jobs as core_jobs
from konverter.libreoffice import module
from konverter.strings import tr
from konverter.ui import main_window as main_window_module

from test_office_ui import (  # noqa: F401 — фикстуры
    Asker,
    _office_group,
    _wait_finished,
    isolated_data,
    window,
)


def test_folder_choice_cancelled_no_download_question(qtbot, window, tmp_path, monkeypatch):
    """Вопрос о скачивании — после выбора папки; отмена выбора → ни вопроса, ни скачивания."""
    asker = Asker(True)
    monkeypatch.setattr(main_window_module, "ask_choice", asker)
    monkeypatch.setattr(core_jobs, "default_output_dir", lambda source: None)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")
    jobs = window.convert_group(_office_group(window, tmp_path, "a.doc"))
    assert jobs == []
    assert asker.calls == []
    assert window.office_download_job() is None


def test_outdated_module_asks_again(qtbot, window, tmp_path, monkeypatch, isolated_data):
    root = isolated_data / "data" / "modules" / module.MODULE_DIR_NAME
    (root / "program").mkdir(parents=True)
    (root / "program" / "soffice.exe").write_bytes(b"x")
    (root / module.MARKER_NAME).write_text(json.dumps({"version": "7.6.4"}), encoding="utf-8")
    texts: list[str] = []

    def asker(parent, title, text, accept_text, reject_text, **kwargs) -> bool:
        texts.append(text)
        return False

    monkeypatch.setattr(main_window_module, "ask_choice", asker)
    jobs = window.convert_group(_office_group(window, tmp_path, "a.doc"))
    assert len(texts) == 1 and "7.6.4" in texts[0] and tr("office_ask_text") in texts[0]
    _wait_finished(qtbot, window, jobs)
    message = window.job_manager.get(jobs[0].id).error_message
    assert "7.6.4" in message and "устарел" in message
