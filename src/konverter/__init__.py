"""Конвертер — портативный локальный конвертер файлов для Windows."""


def main() -> None:
    """Точка входа: журнал, уборка после аварийного выхода, графический интерфейс.

    GUI импортируется внутри функции, чтобы импорт пакета `konverter`
    (например, в тестах, утилитах и в дочерних процессах декодирования,
    запускаемых через `multiprocessing` с контекстом `spawn`) не тянул за
    собой PySide6.
    """
    import multiprocessing

    # Для собранного exe (PyInstaller): дочерний процесс `spawn` запускает
    # тот же exe, и здесь он должен сразу уйти в код `multiprocessing`.
    multiprocessing.freeze_support()

    import logging

    from konverter.logging_setup import setup_logging

    setup_logging()
    log = logging.getLogger("konverter")

    # Скрытая самопроверка собранной программы без окна (`--selftest <папка>`,
    # см. konverter/selftest.py). Разбор — до GUI: обычный запуск ничего не теряет.
    import sys

    from konverter import selftest

    try:
        selftest_folder = selftest.parse_selftest_args(sys.argv)
    except selftest.SelftestUsageError as exc:
        log.warning("%s", exc)
        raise SystemExit(selftest.EXIT_USAGE) from None
    if selftest_folder is not None:
        log.info("Самопроверка в %s", selftest_folder)
        raise SystemExit(selftest.main(selftest_folder))

    log.info("Запуск программы")

    from konverter.core.temp_journal import cleanup_orphans

    cleanup_orphans()

    # Брошенные временные папки модуля LibreOffice (распаковка, конвертации)
    # после сбоя — в фоне: их удаление может занять секунды.
    import threading

    from konverter.libreoffice.module import cleanup_stale

    threading.Thread(target=cleanup_stale, name="konverter-lo-cleanup", daemon=True).start()

    from konverter.ui.main_window import run

    code = run()
    log.info("Выход из программы, код %s", code)
    raise SystemExit(code)
