"""Все строки интерфейса в одном месте.

Строки сгруппированы по языкам; сейчас есть только русский. Чтобы
добавить язык, достаточно добавить словарь с теми же ключами в `STRINGS`.
Код интерфейса берёт строки только через `tr()`.
"""

from __future__ import annotations

DEFAULT_LANGUAGE = "ru"

# Объём скачивания модуля LibreOffice, МБ (около): один источник и для
# пояснения в вопросе, и для подписи кнопки «Скачать (N МБ)».
OFFICE_DOWNLOAD_MB = 375

STRINGS: dict[str, dict[str, str]] = {
    "ru": {
        "app_title": "Peregon",
        "drop_hint": "Перетащите файлы сюда",
        "data_not_writable": (
            "Папка программы недоступна для записи. "
            "Настройки не будут сохраняться."
        ),
        # --- Сообщения ядра (core/) ---
        "err_file_not_found": "Файл не найден",
        "err_file_no_access": "Нет доступа к файлу",
        "err_not_a_file": "Это не обычный файл",
        "err_file_read": "Не удалось прочитать файл: {error}",
        "err_folder_read": "Не удалось прочитать папку: {error}",
        "err_symlink_folder": "Ссылка на папку не раскрывается",
        "unsupported_format": "Формат не поддерживается или не распознан",
        "unsupported_empty": "Пустой файл",
        "too_many_files": (
            "Слишком много файлов за один раз. "
            "Взяты первые {limit}, остальные пропущены."
        ),
        "err_internal": "Внутренняя ошибка: {error}",
        "err_conversion_failed": "Не удалось выполнить конвертацию",
        "err_write_result": "Не удалось записать результат: {error}",
        "err_no_free_name": "Не удалось подобрать свободное имя для результата",
        # {count} — «1 файл», «2 файла», «5 файлов» (ui/common.files_count);
        # окончание глагола согласуется с числом (ui/common.skipped_same_format_text).
        "skipped_same_format_one": "{count} уже в формате {format} — пропущен",
        "skipped_same_format_many": "{count} уже в формате {format} — пропущены",
        # --- Варианты конвертации изображений (converters/images.py) ---
        "target_jpg": "JPG",
        "target_png": "PNG",
        "target_webp": "WEBP",
        "target_avif": "AVIF",
        "target_gif": "GIF",
        "target_bmp": "BMP",
        "target_tiff": "TIFF",
        "target_ico": "ICO (значок)",
        "target_pdf_each": "PDF (отдельный файл на каждое изображение)",
        "target_pdf_single": "PDF (все изображения в один файл)",
        "target_print_ppi": "Для печати (PPI)…",
        # --- Варианты конвертации видео и аудио (converters/media.py) ---
        # «target_gif» общий с изображениями (выше).
        "target_mp4": "MP4",
        "target_mkv": "MKV",
        "target_webm": "WEBM",
        "target_mov": "MOV",
        "target_avi": "AVI",
        "target_audio_mp3": "Только звук (MP3)",
        "target_audio_wav": "Только звук (WAV)",
        "target_mp3": "MP3",
        "target_wav": "WAV",
        "target_flac": "FLAC",
        "target_m4a": "M4A (AAC)",
        "target_ogg": "OGG (Vorbis)",
        "target_opus": "OPUS",
        # --- Варианты конвертации PDF (converters/pdf.py) ---
        "target_pdf_pages_png": "Страницы в PNG",
        "target_pdf_pages_jpg": "Страницы в JPG",
        "target_pdf_txt": "Текст (TXT)",
        "target_pdf_merge": "Объединить в один PDF",
        "target_pdf_split": "Разделить на страницы",
        # --- Варианты конвертации книг (converters/books.py) ---
        "target_book_epub": "EPUB",
        "target_book_fb2": "FB2",
        "target_book_pdf": "PDF",
        "target_book_txt": "Текст (TXT)",
        # --- Варианты конвертации документов (converters/documents.py) ---
        "target_doc_docx": "Word (DOCX)",
        "target_doc_pdf": "PDF",
        "target_doc_html": "Веб-страница (HTML)",
        "target_doc_md": "Markdown (MD)",
        "target_doc_txt": "Текст (TXT)",
        # --- Варианты конвертации таблиц (converters/tables.py) ---
        "target_table_xlsx": "Excel (XLSX)",
        "target_table_csv": "CSV (первый лист)",
        "target_table_csv_sheets": "CSV (каждый лист — отдельный файл, в папку)",
        "target_table_pdf": "PDF",
        # --- Варианты конвертации презентаций (converters/presentations.py) ---
        "target_slides_pdf": "PDF",
        "target_slides_slides_png": "Слайды в PNG",
        # --- Модуль LibreOffice (libreoffice/, ui/main_window.py) ---
        "office_ask_title": "Нужен модуль LibreOffice",
        # Вопрос о скачивании: короткий вопрос (setText), объём и источник
        # (setInformativeText), лицензия и товарный знак (setDetailedText).
        "office_ask_text": (
            "Для конвертации офисных документов (DOC, DOCX, ODT, RTF, XLS, ODS, "
            "PPT, PPTX, ODP) нужен модуль LibreOffice. Скачать его сейчас?"
        ),
        "office_ask_info": (
            f"Объём: около {OFFICE_DOWNLOAD_MB} МБ скачивание, около 1,5 ГБ на диске.\n"
            "Источник: официальный сервер The Document Foundation "
            "(download.documentfoundation.org); подлинность проверяется по "
            "цифровым подписям.\n"
            "Модуль кладётся в папку программы (data\\modules), в систему "
            "ничего не устанавливается; удалить — удалить эту папку.\n\n"
            "Если отказаться, офисные документы не будут конвертироваться, "
            "остальное продолжит работать."
        ),
        "office_ask_details": (
            "Это необязательный компонент, лицензия MPL-2.0. "
            "«LibreOffice» — товарный знак The Document Foundation; программа "
            "с этой организацией не связана."
        ),
        "office_ask_download": f"&Скачать ({OFFICE_DOWNLOAD_MB} МБ)",
        "office_ask_not_now": "&Не сейчас",
        "office_download_name": "Модуль LibreOffice",
        "office_download_target": "скачивание и распаковка",
        "office_refused": (
            "Модуль LibreOffice не скачан — офисные документы не конвертируются"
        ),
        "office_outdated_text": (
            "Установленный модуль LibreOffice {version} устарел (нужна версия не ниже "
            "{minimum}: в старых версиях есть известные уязвимости).\n\n"
        ),
        "office_outdated": (
            "Модуль LibreOffice {version} устарел (нужна версия не ниже {minimum}) — "
            "офисные документы не конвертируются; скачайте модуль заново"
        ),
        "office_not_writable": (
            "Модуль LibreOffice недоступен: папка программы недоступна для записи "
            "(например, программа лежит в Program Files). Перенесите папку программы "
            "в доступное для записи место."
        ),
        "office_err_busy": "Модуль LibreOffice уже устанавливается",
        "office_err_busy_other": (
            "Модуль LibreOffice сейчас устанавливает другая запущенная копия программы"
        ),
        "office_err_no_space": (
            "Недостаточно места на диске для модуля LibreOffice: нужно ~{need:.1f} ГБ, "
            "свободно {free:.1f} ГБ"
        ),
        "office_err_disk_full": "Недостаточно места на диске для скачивания LibreOffice",
        "office_err_write": "Не удалось записать файл LibreOffice: {error}",
        "office_err_no_network": (
            "Не удалось связаться с сервером LibreOffice (нет сети или сервер недоступен)"
        ),
        "office_err_bad_data": "Сервер LibreOffice вернул неожиданные данные",
        "office_err_too_old_on_server": (
            "На сервере нет подходящей версии LibreOffice (слишком старая)"
        ),
        "office_err_too_big": "Файл LibreOffice на сервере больше допустимого размера",
        "office_err_no_mirror": (
            "Не удалось скачать LibreOffice: ни одно зеркало не ответило по HTTPS"
        ),
        "office_err_hash": (
            "Скачанный файл LibreOffice повреждён или подменён (не совпала контрольная "
            "сумма) — файл удалён"
        ),
        "office_err_pgp": (
            "Скачанный файл LibreOffice не прошёл проверку подписи (OpenPGP) — файл удалён"
        ),
        "office_err_authenticode": (
            "Скачанный файл LibreOffice не прошёл проверку цифровой подписи Windows — "
            "файл удалён"
        ),
        "office_err_chain": (
            "Windows не смогла проверить цепочку сертификатов цифровой подписи LibreOffice "
            "(нет доверенного корневого сертификата или он не загрузился). Файл не "
            "подменён по проверке OpenPGP, но установка остановлена — файл удалён. "
            "Проверьте подключение к интернету и обновления корневых сертификатов "
            "Windows, затем попробуйте снова."
        ),
        "office_err_product": (
            "Скачанный пакет — не тот LibreOffice, который ожидался (версия или название "
            "внутри пакета не совпали) — файл удалён"
        ),
        "office_err_no_soffice": "В пакете LibreOffice нет program\\soffice.exe",
        "office_err_trial": (
            "Распакованный LibreOffice не запустился (проверочная конвертация не удалась)"
        ),
        "office_err_trial_output": "Распакованный LibreOffice выдал неверный результат",
        "office_err_unpack": "Не удалось распаковать LibreOffice: {error}",
        "office_err_profile_copy": (
            "Не удалось сохранить настройки LibreOffice в папку программы. "
            "Возможно, путь к папке программы слишком длинный: переместите папку "
            "Peregon ближе к корню диска (например, в C:\\Peregon) и повторите "
            "установку. Подробности — в журнале программы."
        ),
        # --- Интерфейс (ui/) ---
        "choose_files": "&Выбрать файлы…",
        # Зона броска в свёрнутом виде (в окне уже есть группы или задачи).
        "add_files": "+ &Добавить файлы",
        "add_files_tip": "Выбрать файлы (Ctrl+O). Файлы можно перетащить в любое место окна",
        "clipboard_no_files": "В буфере обмена нет файлов",
        "choose_files_title": "Выберите файлы для конвертации",
        "detecting": "Определяю форматы…",
        "category_image": "Изображения",
        "category_video": "Видео",
        "category_audio": "Аудио",
        "category_pdf": "PDF",
        "category_document": "Документы",
        "category_spreadsheet": "Таблицы",
        "category_presentation": "Презентации",
        "category_book": "Книги",
        "category_archive": "Архивы",
        "files_one": "{n} файл",
        "files_few": "{n} файла",
        "files_many": "{n} файлов",
        "group_title": "{category} — {count}",
        "mismatch": "{name} — на самом деле {label}",
        "and_more": "…и ещё {n}",
        "target_label": "Во что:",
        "preset_label": "Качество:",
        "preset_high": "Высокое",
        "preset_medium": "Среднее",
        "preset_small": "Маленький размер",
        "convert": "&Конвертировать",
        "convert_all": "Конвертировать &всё",
        "convert_all_tip": "Конвертировать все группы по очереди",
        "remove_group": "Убрать группу (Del)",
        "group_not_supported": "Эти файлы пока не поддерживаются",
        "unsupported_title": "Пропущены файлы ({n}):",
        "unsupported_item": "{name} — {reason}",
        "close_notice": "Закрыть сообщение",
        "jobs_title": "Задачи",
        "clear_finished": "&Очистить завершённые",
        "status_queued": "В очереди",
        "status_running": "Выполняется",
        "status_done": "Готово",
        "status_error": "Ошибка",
        "status_cancelled": "Отменено",
        "cancel": "Отмена",
        "show_in_folder": "Показать в папке",
        "job_name_many": "{name} и ещё {n}",
        "job_target": "→ {target}",
        "choose_output_dir": "Папка для результатов",
        "output_dir_needed": (
            "В папку исходных файлов группы «{group}» нельзя записать результат — "
            "выберите другую папку."
        ),
        "output_dir_cancelled": (
            "Конвертация группы «{group}» не начата: папка для результата не выбрана."
        ),
        "output_dir_not_writable": "В выбранную папку нельзя записать результат: {folder}",
        "no_converter": "Для этой группы нет подходящего конвертера",
        "submit_failed": "Не удалось поставить задачу: {error}",
        "grouping_failed": "Не удалось разобрать файлы: {error}",
        "result_missing": "Файл результата не найден: {path}",
        "reveal_failed": "Не удалось открыть Проводник: {error}",
        "confirm_close_title": "Закрыть программу?",
        "confirm_close_text": (
            "Ещё не завершено задач: {n}. Отменить их и закрыть программу?"
        ),
        "confirm_close_yes": "&Отменить задачи и закрыть",
        "confirm_close_no": "&Продолжить работу",
        # --- Завершение всех задач (уведомление Windows) ---
        "batch_title": "Конвертация завершена",
        "batch_done": "готово: {n}",
        "batch_error": "с ошибками: {n}",
        "batch_cancelled": "отменено: {n}",
        # Подписи кнопок диалогов: стандартные кнопки Qt без QTranslator
        # остаются английскими (Yes/No), поэтому задаём свои.
        "button_yes": "Да",
        "button_no": "Нет",
        "button_ok": "ОК",
        "button_cancel": "Отмена",
        # Кнопка подробностей в QMessageBox (строка самого Qt, ui/common.py).
        "button_show_details": "Подробнее…",
        "button_hide_details": "Скрыть подробности",
        # --- Фрагмент для GIF (Ф20): ошибки ядра (converters/media.py) ---
        "gif_fragment_bad": "Фрагмент для GIF задан неверно: конец должен быть позже начала",
        "gif_fragment_too_long": "Фрагмент для GIF слишком длинный: {0:.1f} с, максимум {1:.0f} с",
        "gif_fragment_after_end": "Начало фрагмента ({0:.1f} с) — за концом видео ({1:.1f} с)",
        "gif_fragment_empty": "Во фрагменте нет ни одного кадра",
        # --- Окно «Фрагмент для GIF» (ui/gif_fragment.py) ---
        "gif_dialog_title": "Фрагмент для GIF",
        "gif_dialog_title_n": "Фрагмент для GIF — видео {index} из {total}",
        "gif_dialog_file": "{name}",
        "gif_start": "Начало:",
        "gif_end": "Конец:",
        "gif_time_hint": "Время — мм:сс или чч:мм:сс, можно с десятыми (например, 1:10.5)",
        "gif_duration": "Длина видео: {duration}. Фрагмент — не длиннее {max} с.",
        "gif_duration_unknown": (
            "Длина видео неизвестна — введите время вручную. "
            "Фрагмент — не длиннее {max} с."
        ),
        "gif_length": "Длина фрагмента: {length}",
        "gif_reading": "Чтение видео…",
        "gif_loading": "Загрузка…",
        "gif_no_frame": "Нет кадра",
        # Фрагмент неверен — кадров для него нет.
        "gif_frame_unavailable": "—",
        "gif_first_frame": "Первый кадр",
        "gif_last_frame": "Последний кадр",
        "gif_read_failed": "Не удалось прочитать видео: {error}",
        "gif_frames_failed": "Не удалось показать кадры: {error}",
        "gif_err_time_format": "Время «{text}» не распознано: нужно мм:сс или чч:мм:сс",
        "gif_err_end_before_start": "Конец фрагмента должен быть позже начала",
        "gif_err_too_long": "Фрагмент длиннее {max} с — сократите его",
        "gif_err_start_after_end": "Начало фрагмента — за концом видео",
        "gif_preview": "&Предпросмотр",
        "gif_preview_hint": "Нажмите «Предпросмотр», чтобы увидеть GIF",
        "gif_preview_loading": "Готовлю предпросмотр…",
        "gif_preview_failed": "Не удалось сделать предпросмотр: {error}",
        "gif_make": "Сделать &GIF",
        "gif_preview_toggle_tip": "Щёлкните, чтобы остановить или продолжить предпросмотр",
        "gif_apply_all": "Применить ко всем: первые {n} с",
        "gif_apply_all_tip": (
            "Сделать GIF из первых N секунд этого и всех остальных видео группы "
            "(видео короче — целиком), без новых окон"
        ),
        "gif_skipped": "GIF не сделан (отменено в окне фрагмента): {count}",
        # --- Подготовка к печати (converters/images.py, ui/print_ppi.py) ---
        "print_err_ppi": "PPI — целое число от {min} до {max}",
        "print_err_cm": "Размер в сантиметрах — число больше нуля",
        "print_err_side": "Длинная сторона результата больше {max} px — уменьшите размер или PPI",
        "print_err_pixels": "Результат слишком большой: {pixels} Мп, максимум {max} Мп",
        "print_err_no_params": "Не заданы PPI и размер печати",
        "print_dialog_title": "Подготовить к печати",
        "print_dialog_title_n": "Подготовить к печати — картинка {index} из {total}",
        "print_ppi": "PPI:",
        "print_ppi_tip": "Плотность пикселей: 300 — обычная печать, 150 — плакаты, 72–96 — экран",
        "print_cm": "Длинная сторона, см:",
        "print_cm_tip": "Размер печати по длинной стороне; вторая сторона — по пропорциям картинки",
        "print_as_is": "Как сейчас",
        "print_as_is_tip": "Подставить текущий физический размер картинки (без PPI в файле — по 96)",
        "print_current": "Сейчас: {width}×{height} px, {ppi} PPI",
        "print_current_no_ppi": "Сейчас: {width}×{height} px, PPI в файле нет",
        "print_unknown_size": "Размер картинки неизвестен — он будет определён при подготовке",
        "print_result": "Получится: {width}×{height} px; вторая сторона — {other} см",
        "print_upscale": "Картинка увеличится в {factor} раз — качество от этого не вырастет",
        "print_make": "&Сделать",
        "print_apply_all": "Применить ко всем: эти PPI и размер",
        "print_apply_all_tip": (
            "Подготовить этот и все остальные файлы группы с теми же PPI и длинной "
            "стороной в сантиметрах, без новых окон"
        ),
        "print_skipped": "Не подготовлено к печати (отменено в окне): {count}",
    },
}

# Текущий язык интерфейса.
_current_language = DEFAULT_LANGUAGE


def set_language(language: str) -> None:
    """Переключает язык интерфейса. Неизвестный язык — ошибка ValueError."""
    global _current_language
    if language not in STRINGS:
        raise ValueError(f"Неизвестный язык интерфейса: {language!r}")
    _current_language = language


def tr(key: str) -> str:
    """Возвращает строку интерфейса по ключу на текущем языке.

    Если в текущем языке ключа нет, берётся строка языка по умолчанию;
    если нет и там — возвращается сам ключ (чтобы пропуск был заметен,
    но программа не падала).
    """
    table = STRINGS.get(_current_language, {})
    if key in table:
        return table[key]
    return STRINGS[DEFAULT_LANGUAGE].get(key, key)
