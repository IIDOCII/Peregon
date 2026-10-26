"""Общие фикстуры тестов."""

from __future__ import annotations

import pytest

from konverter.core import temp_journal


@pytest.fixture(autouse=True)
def isolated_temp_journal(tmp_path_factory, monkeypatch):
    """Журнал временных файлов — в отдельной временной папке, не в реальной data/.

    Папка берётся из `tmp_path_factory`, а не из `tmp_path`, чтобы не
    появляться в `tmp_path` (тесты проверяют его содержимое).
    """
    folder = tmp_path_factory.mktemp("journal")
    journal = temp_journal.TempJournal(lambda: folder)
    monkeypatch.setattr(temp_journal, "JOURNAL", journal)
    yield journal
    journal.close()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "libreoffice: интеграционный тест с настоящим модулем LibreOffice"
    )
    config.addinivalue_line(
        "markers", "stress: быстрый стресс-тест (до ~20 с) на гонки потоков"
    )


@pytest.fixture(scope="session")
def qapp(qapp):
    """Приложение pytest-qt с тем же базовым шрифтом, что у программы (10,5 pt),
    и, как в программе, со сборкой циклического мусора только в главном
    потоке (konverter/ui/gc_guard.py): иначе сборщик, запущенный в фоновом
    потоке, удалял бы объекты Qt в чужом потоке (аварии access violation)."""
    from konverter.ui import gc_guard
    from konverter.ui.common import apply_base_font

    gc_guard.install(qapp)
    apply_base_font(qapp)
    return qapp


@pytest.fixture(autouse=True)
def _collect_cycles_in_main_thread():
    """Автоматическая сборка выключена (после создания приложения Qt), а
    тесты без цикла событий таймер не запускают — после каждого теста
    сборка по счётчикам, в главном потоке."""
    yield
    import sys

    gc_guard = sys.modules.get("konverter.ui.gc_guard")
    if gc_guard is not None and gc_guard.is_installed():
        gc_guard.collect_if_needed()


class FinishActions:
    """Записывает действия «по завершении всех задач» вместо настоящих."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str, bool]] = []
        self.sounds: list[bool] = []
        self.revealed: list = []
        self.on_click = None

    def notify(self, title, text, *, error=False, on_click=None) -> bool:
        self.notifications.append((title, text, error))
        self.on_click = on_click
        return True

    def sound(self, ok) -> None:
        self.sounds.append(ok)

    def reveal(self, path) -> None:
        self.revealed.append(path)


@pytest.fixture(autouse=True)
def finish_actions(monkeypatch):
    """В тестах уведомление, звук и Проводник по завершении задач подменены:
    ничего не открывается и не звучит; вызовы доступны тестам через эту фикстуру."""
    from konverter.ui import notify

    actions = FinishActions()
    monkeypatch.setattr(notify, "show_notification", actions.notify)
    monkeypatch.setattr(notify, "play_sound", actions.sound)
    monkeypatch.setattr(notify, "reveal_result", actions.reveal)
    return actions
