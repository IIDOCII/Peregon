"""Позволяет запускать программу командой `python -m konverter`.

Модуль импортируется заново в дочерних процессах `multiprocessing`
(контекст `spawn`, имя `__mp_main__`), поэтому на верхнем уровне — только
лёгкие импорты без PySide6, а запуск — под `if __name__ == "__main__"`.
"""

import multiprocessing

from konverter import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
