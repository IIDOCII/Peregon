"""Точка входа собранной программы (PyInstaller, `Peregon.exe`).

`multiprocessing.freeze_support()` — самым первым делом: дочерний процесс
`spawn` (декодирование SVG/RAW, аудио/видео, PDF, документы) запускает этот
же exe с особыми аргументами, и здесь он должен сразу уйти в код
`multiprocessing`, не создавая окна и не импортируя PySide6.
"""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()

    from konverter import main

    main()
