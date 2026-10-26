"""Конвертеры аудио (категория AUDIO) и видео (категория VIDEO) на PyAV (FFmpeg).

Вход и выход (раздел 3 ТЗ):
* видео `mp4 mov mkv avi webm wmv flv 3gp mts` → MP4, MKV, WEBM, MOV, AVI,
  GIF, «только звук» MP3 и WAV;
* аудио `mp3 wav flac aac m4a ogg opus wma amr` → MP3, WAV, FLAC, M4A, OGG, OPUS.

Кодеки (выбраны по совместимости с плеерами):

=========  ==========================  =========================
Вариант    Видео                       Звук
=========  ==========================  =========================
MP4, MOV   H.264 (libx264, yuv420p)    AAC (встроенный aac)
MKV        H.264 (libx264, yuv420p)    AAC
WEBM       VP9 (libvpx-vp9)            Opus (libopus)
AVI        MPEG-4 Part 2 (mpeg4)       MP3 (libmp3lame)
GIF        GIF (палитра palettegen)    —
MP3        —                           MP3 (libmp3lame)
WAV        —                           PCM s16le (при > 4 ГБ — RF64)
FLAC       —                           FLAC (16 бит)
M4A        —                           AAC (контейнер ipod/M4A)
OGG        —                           Vorbis (встроенный энкодер FFmpeg)
OPUS       —                           Opus (libopus, контейнер Ogg)
=========  ==========================  =========================

OGG: `libvorbis` в сборке FFmpeg из колеса `av` нет. Есть встроенный
энкодер `vorbis` (помечен экспериментальным, включается `strict=-2`): он
даёт настоящий Ogg Vorbis, который играет любой плеер, понимающий .ogg;
качество у него хуже libvorbis, но для «OGG» это честнее, чем класть Opus
в файл .ogg (многие считают .ogg именно Vorbis). Встроенный энкодер
поддерживает не больше 2 каналов — звук сводится в стерео.

Принятые решения:
* «В тот же формат» не предлагается (как у изображений): у видео `fmt`
  совпадает с id варианта (mp4 → MP4 скрыт), у аудио так же.
* Копирование потоков без перекодирования (remux) — только при пресете
  «Высокое» (максимальное качество = без потерь): поток копируется, если его
  кодек подходит целевому контейнеру (`_COPY_CODECS`) и исходный контейнер
  даёт надёжные временные метки (`_COPY_SOURCES`; не AVI/FLV/WMV/WMA/AMR,
  у них метки часто неполные). Решение принимается по каждому потоку
  отдельно: например, MKV с H.264 + PCM → MP4 = видео копируется, звук
  перекодируется в AAC. «Среднее» и «Маленький размер» всегда
  перекодируют (пользователь выбрал размер). H.264/HEVC/AAC копируются
  только при наличии extradata (иначе MP4 получится неполным). Видео с
  поворотом (матрица отображения ≠ 0°) не копируется, а перекодируется с
  поворотом кадров — чтобы результат не зависел от того, учитывает ли
  плеер матрицу (в MKV её понимают не все).
  При копировании метки сдвигаются на общее начало (`t0`), пакеты с
  немонотонным DTS отбрасываются (отброшено больше `MAX_DROPPED_SHARE` —
  ошибка «Повреждены метки времени», а не молча обрезанный результат).
  Метаданные (теги, GPS) не копируются. Приватность при копировании: из
  H.264/HEVC удаляются все SEI (bitstream-фильтр `filter_units`, NAL 6 и
  39/40) — в user-data SEI бывают строка настроек энкодера, серийные номера,
  данные камеры; заодно теряются служебные SEI (HDR-метаданные HEVC в SEI,
  точки восстановления) — сознательная цена. Фильтр не чистит extradata
  (x265 кладёт SEI в hvcC) — её чистит `_strip_sei_extradata`; extradata,
  которую не удалось разобрать, — поток перекодируется. Пакет, который
  фильтр не смог разобрать, отбрасывается (больше `MAX_DROPPED_SHARE` —
  «Повреждены данные видео»). Vorbis не копируется
  (перекодируется): его заголовок комментариев с тегами лежит в extradata.
* Разрывы меток времени (форматы с флагом FFmpeg `AVFMT_TS_DISCONT`, плюс
  MTS и FLV по коду формата — например, склеенные клипы MTS): скачок DTS
  назад больше `TS_DISCONT_BACK` или вперёд больше `TS_DISCONT_JUMP`
  компенсируется общим для файла смещением (как в ffmpeg CLI), результат
  получает полную длительность.
* Звук при перекодировании в видео: метки кадров сверяются со счётчиком
  отсчётов; расхождение больше `AUDIO_SYNC_THRESHOLD` — дыра заполняется
  тишиной, перекрытие отбрасывается (как `aresample=async`), звук не
  уезжает от видео. В «только звук» и аудио → аудио метки не учитываются
  (синхронизировать не с чем).
* Пресеты (Ф7): см. `_X264_CRF`, `_VP9_CRF`, `_MPEG4_Q`, `_AUDIO_BITRATE`;
  у «Маленький размер» видео уменьшается до 720 строк по высоте (без
  увеличения меньших). При перекодировании видео не больше
  `MAX_OUTPUT_SIDE` (4096) px по длинной стороне: кадры 8K декодер + x264
  не укладывают в лимит памяти процесса (замер: вход 8000×5000 → 3,6 ГБ;
  с уменьшением до 4096 — ≈ 2 ГБ). Копирование (remux) разрешение не меняет. FLAC и WAV пресетом не управляются
  (`uses_preset=False`). GIF: ≤ 480 px по длинной стороне и 15 к/с
  («Высокое»), 480 px и 12 к/с («Среднее»), 320 px и 10 к/с («Маленький»).
* Видео → GIF: не длиннее `GIF_MAX_SECONDS` (60 с), иначе ошибка;
  палитра строится по всему ролику (два прохода: palettegen, затем
  paletteuse) — память не растёт с длиной ролика.
* Фрагмент для GIF (Ф20): `VideoConverter.convert(..., fragment=(начало,
  конец))` — секунды от начала видео. Проверка в родителе и ещё раз в
  ребёнке: конечные числа, 0 ≤ начало < конец, длина ≤ `GIF_MAX_SECONDS`.
  Декодируется только отрезок: seek к ближайшему ключевому кадру не позже
  начала (если seek промахнулся — первый кадр позже начала больше чем на
  `_SEEK_SLACK` с или seek не удался — файл открывается заново и читается
  с начала), кадры раньше начала отбрасываются (но учитываются в пределе
  «слишком сложный файл»), чтение останавливается на первом кадре не раньше
  конца. Метки результата начинаются с 0. Конец позже фактической длины —
  не ошибка (GIF до конца видео); начало за концом видео или ни одного
  кадра во фрагменте — понятная ошибка. Прогресс — по длине фрагмента.
  Без фрагмента всё как раньше (видео > 60 с → `gif_too_long`).
  Окно выбора фрагмента (`ui/gif_fragment.py`) получает длительность, кадры
  начала/конца (`fragment_frames`) и маленький GIF (`gif_preview`) тоже
  только через дочерний процесс (действия "frames" и "preview" задачи
  "media", счётчик мест `pool="preview"`).
* Видео → «только звук»: берётся первая звуковая дорожка; её нет —
  ошибка «В видео нет звуковой дорожки».
* Несколько дорожек: берётся первая видеодорожка (кроме обложек —
  `attached_pic`) и первая звуковая; остальные дорожки, субтитры, данные,
  главы и вложения отбрасываются.
* Метаданные (теги, GPS из MOV/MP4 телефонов, обложки) НЕ переносятся —
  ради приватности (как EXIF у изображений): в выходной контейнер не
  пишется ничего, кроме служебного тега муксера.
* Поворот (матрица отображения, rotation) при перекодировании
  применяется к самим кадрам (transpose/hflip/vflip, как autorotate в
  ffmpeg), в результате матрицы нет — видео с телефона не ложится боком
  ни в каком плеере. Неквадратные пиксели (SAR) приводятся к квадратным.
* Звук: в форматы с потерями — не больше 2 каналов (сведение), во FLAC/WAV —
  до 8; частота — исходная, если энкодер её поддерживает, иначе 48 кГц
  (Opus — всегда из его списка). FLAC/WAV пишутся в 16 бит.
* Прогресс: по времени демультиплексированных пакетов относительно
  длительности (контейнера или потоков); длительность неизвестна — по
  позиции в файле (байты); неизвестно и это — доля не сообщается
  (интерфейс стоит на 0 % до конца, но таймаут «нет продвижения» всё равно
  продлевается отметками времени/позиции). Доля монотонна, деления на 0 нет.
  GIF: первый проход (палитра) — доля 0–0,5, второй — 0,5–1; отметки
  продвижения растут через оба прохода (иначе второй проход «молчал» бы и
  сторож `STALL_TIMEOUT` счёл бы его зависанием).

Безопасность и устойчивость (входные файлы враждебны):
* вся работа с FFmpeg — в ДОЧЕРНЕМ процессе (`_isolated.run_task`): падение
  нативного кода, зависание или «съедание» памяти не роняют программу;
  отмена — kill процесса (≤ 2 с); лимит памяти процесса —
  `MEDIA_MEMORY_LIMIT` (Job Object); «нет продвижения» `STALL_TIMEOUT` с →
  «Обработка зависла»; общий предел `MAX_TIME`;
* демультиплексор выбирается строго по коду формата из detect
  (`_DEMUXERS`), FFmpeg не угадывает формат сам; разрешён только протокол
  `file` (никаких сетевых и вложенных ссылок), число потоков ограничено;
* до тяжёлой работы проверяются заголовки: разрешение ≤ `MAX_WIDTH` ×
  `MAX_HEIGHT` и ≤ `MAX_FRAME_PIXELS`, длительность ≤ `MAX_DURATION`,
  потоков ≤ `MAX_STREAMS`, частота и каналы звука; размер КАЖДОГО
  декодированного кадра проверяется ещё раз (смена разрешения посреди
  потока), выходное время ограничено `MAX_DURATION`;
* ошибки декодирования отдельных пакетов пропускаются (как в ffmpeg), но
  не больше `MAX_DECODE_ERRORS`; ни одного кадра — «файл повреждён»;
* число декодированных кадров видео ограничено:
  ≤ длительность × `MAX_FPS_FACTOR` + `MAX_FRAMES_BASE` (длительность — большее
  из заголовка и пройденного времени) и ≤ `MAX_PIXEL_FRAMES` в сумме
  «пиксели × кадры» — иначе «Слишком сложный файл» (крошечные skip-кадры
  дают огромную работу на байт входа);
* размер результата: до записи — оценка (PCM/FLAC — длительность × частота
  × каналы × 2 байта, сжатый звук — по битрейту, копирование — размер
  входа, перекодированное видео и GIF — без оценки) и проверка свободного
  места (оценка + `DISK_RESERVE`); во время записи — счётчик байт с
  пределом `MAX_OUTPUT_BYTES` и проверка свободного места каждые
  `DISK_CHECK_BYTES` (осталось < `MIN_FREE_BYTES` → ошибка);
* ребёнок пишет результат прямо в `dst` (временный файл ядра); при ошибке
  или отмене уборку делает ядро.
* Длинные пути (`\\\\?\\…`, > 260 символов): FFmpeg открывает их как
  обычный файл (протокол `file` на Windows переводит путь в UTF-16 и
  принимает префикс `\\\\?\\`) — проверено тестом.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from konverter.strings import tr

from . import _isolated
from .base import (
    CancelToken,
    Category,
    ConversionError,
    Preset,
    ProgressCallback,
    Target,
)

log = logging.getLogger(__name__)

# --- Лимиты (проверяются в дочернем процессе по заголовкам и кадрам) ----------

MAX_WIDTH = 8192
MAX_HEIGHT = 8192
MAX_FRAME_PIXELS = 40_000_000
MAX_DURATION = 24 * 3600.0  # с
MAX_STREAMS = 32
MAX_SAMPLE_RATE = 384_000
MAX_CHANNELS = 32
MAX_DECODE_ERRORS = 100
GIF_MAX_SECONDS = 60.0
# Допуск сравнения времён фрагмента (ошибки округления меток), с.
FRAGMENT_EPS = 1e-3
# Кадров видео не больше длительность × MAX_FPS_FACTOR + MAX_FRAMES_BASE.
MAX_FPS_FACTOR = 240
MAX_FRAMES_BASE = 1000
# Сумма «пиксели × кадры» декодированного видео: 4·10¹² ≈ 2 ч 4K при 60 к/с
# (такое x264 всё равно не перекодирует быстрее MAX_TIME).
MAX_PIXEL_FRAMES = 4 * 10**12
# Результат не больше, байт.
MAX_OUTPUT_BYTES = 50 * 2**30
# Запас свободного места сверх оценки размера результата, байт.
DISK_RESERVE = 2**30
# Во время записи: осталось меньше — остановиться с ошибкой, байт.
MIN_FREE_BYTES = 2**30
# Как часто (по записанным байтам) проверять свободное место.
DISK_CHECK_BYTES = 64 * 2**20

# --- Метки времени ------------------------------------------------------------

# Звук в видео: расхождение меток и счётчика отсчётов больше этого, с, →
# вставка тишины (дыра) или отбрасывание (перекрытие).
AUDIO_SYNC_THRESHOLD = 0.04
# Форматы с разрывами меток: скачок DTS назад больше TS_DISCONT_BACK с или
# вперёд больше TS_DISCONT_JUMP с — разрыв (пороги как у ffmpeg CLI).
TS_DISCONT_BACK = 0.1
TS_DISCONT_JUMP = 10.0
# AVFMT_TS_DISCONT в флагах демультиплексора FFmpeg.
_AVFMT_TS_DISCONT = 0x0200
# Коды форматов, которые считаются «с разрывами» независимо от флага.
_TS_DISCONT_FORMATS = frozenset({"mts", "flv"})
# Отброшено больше этой доли пакетов/кадров из-за меток, идущих назад, →
# «Повреждены метки времени».
MAX_DROPPED_SHARE = 0.05

# --- Изоляция -----------------------------------------------------------------

# Нет продвижения работы столько секунд → «Обработка зависла».
STALL_TIMEOUT = 60.0
# Общий предел времени одной задачи, с.
MAX_TIME = 6 * 3600.0
# Лимит выделенной памяти дочернего процесса (как у SVG/RAW).
MEDIA_MEMORY_LIMIT = 3 * 2**30
# Имя задачи в `_isolated.TASKS` (тесты подменяют на тестовые задачи).
_TASK = "media"

# --- Тексты ошибок ------------------------------------------------------------

MSG_DAMAGED = "Файл повреждён или не поддерживается"
MSG_NO_MEMORY = "Недостаточно памяти для обработки файла"

# Ключи ошибок из дочернего процесса → шаблоны (параметры — только числа).
_USER_MESSAGES: dict[str, str] = {
    "no_audio": "В видео нет звуковой дорожки",
    "no_audio_file": "В файле нет звуковой дорожки",
    "no_video": "В файле нет видеодорожки",
    "gif_too_long": "Слишком длинное видео для GIF: {0:.0f} с, максимум {1:.0f} с",
    "gif_too_long_unknown": "Слишком длинное видео для GIF: максимум {0:.0f} с",
    "too_long": "Слишком длинный файл: {0:.1f} ч, максимум {1:.0f} ч",
    "too_large": (
        "Слишком большое разрешение видео: {0:.0f}×{1:.0f}, "
        "максимум {2:.0f}×{3:.0f} и {4:.0f} Мп"
    ),
    "too_many_streams": "Слишком много дорожек в файле: {0:.0f}, максимум {1:.0f}",
    "bad_audio": "Неподдерживаемые параметры звука",
    "bad_timestamps": "Повреждены метки времени",
    "bad_video": "Повреждены данные видео",
    "too_complex": "Слишком сложный файл: слишком много кадров",
    "no_space": "Недостаточно места на диске: нужно ~{0:.1f} ГБ, свободно {1:.1f} ГБ",
    "disk_full": "Недостаточно места на диске: свободно меньше {0:.1f} ГБ",
    "output_too_large": "Слишком большой результат: больше {0:.0f} ГБ",
    "damaged": MSG_DAMAGED,
    "fragment_bad": tr("gif_fragment_bad"),
    "fragment_too_long": tr("gif_fragment_too_long"),
    "fragment_after_end": tr("gif_fragment_after_end"),
    "fragment_empty": tr("gif_fragment_empty"),
}

# --- Варианты -----------------------------------------------------------------

VIDEO_INPUTS = ("mp4", "mov", "mkv", "avi", "webm", "wmv", "flv", "3gp", "mts")
AUDIO_INPUTS = ("mp3", "wav", "flac", "aac", "m4a", "ogg", "opus", "wma", "amr")

TARGET_AUDIO_MP3 = "audio_mp3"
TARGET_AUDIO_WAV = "audio_wav"

# (id, расширение, uses_preset) в каноническом порядке.
_VIDEO_OUTPUTS = (
    ("mp4", "mp4", True),
    ("mkv", "mkv", True),
    ("webm", "webm", True),
    ("mov", "mov", True),
    ("avi", "avi", True),
    ("gif", "gif", True),
    (TARGET_AUDIO_MP3, "mp3", True),
    (TARGET_AUDIO_WAV, "wav", False),
)
_AUDIO_OUTPUTS = (
    ("mp3", "mp3", True),
    ("wav", "wav", False),
    ("flac", "flac", False),
    ("m4a", "m4a", True),
    ("ogg", "ogg", True),
    ("opus", "opus", True),
)
VIDEO_TARGET_ORDER: tuple[str, ...] = tuple(t for t, _e, _u in _VIDEO_OUTPUTS)
AUDIO_TARGET_ORDER: tuple[str, ...] = tuple(t for t, _e, _u in _AUDIO_OUTPUTS)

# Код формата из detect → демультиплексор FFmpeg (формат не угадывается).
_DEMUXERS: dict[str, str] = {
    "mp4": "mov", "mov": "mov", "3gp": "mov", "m4a": "mov",
    "mkv": "matroska", "webm": "matroska",
    "avi": "avi", "wmv": "asf", "wma": "asf", "flv": "flv", "mts": "mpegts",
    "mp3": "mp3", "wav": "wav", "flac": "flac", "aac": "aac",
    "ogg": "ogg", "opus": "ogg", "amr": "amr",
}

# Вариант → (муксер, видеокодек | None, звуковой кодек | None).
_PLANS: dict[str, tuple[str, str | None, str | None]] = {
    # видео
    "mp4": ("mp4", "libx264", "aac"),
    "mov": ("mov", "libx264", "aac"),
    "mkv": ("matroska", "libx264", "aac"),
    "webm": ("webm", "libvpx-vp9", "libopus"),
    "avi": ("avi", "mpeg4", "libmp3lame"),
    "gif": ("gif", "gif", None),
    TARGET_AUDIO_MP3: ("mp3", None, "libmp3lame"),
    TARGET_AUDIO_WAV: ("wav", None, "pcm_s16le"),
    # аудио
    "mp3": ("mp3", None, "libmp3lame"),
    "wav": ("wav", None, "pcm_s16le"),
    "flac": ("flac", None, "flac"),
    "m4a": ("ipod", None, "aac"),
    "ogg": ("ogg", None, "vorbis"),
    "opus": ("opus", None, "libopus"),
}

# Remux («Высокое»): какие исходные кодеки можно скопировать в вариант.
_COPY_CODECS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "mp4": (frozenset({"h264"}), frozenset({"aac"})),
    "mov": (frozenset({"h264"}), frozenset({"aac"})),
    # Vorbis не копируется никуда: теги лежат в его extradata (заголовок
    # комментариев) — перекодирование их не переносит.
    "mkv": (
        frozenset({"h264", "hevc", "vp9", "av1"}),
        frozenset({"aac", "mp3", "opus", "flac"}),
    ),
    "webm": (frozenset({"vp9", "vp8", "av1"}), frozenset({"opus"})),
    TARGET_AUDIO_MP3: (frozenset(), frozenset({"mp3"})),
    "mp3": (frozenset(), frozenset({"mp3"})),
    "m4a": (frozenset(), frozenset({"aac"})),
    "ogg": (frozenset(), frozenset()),
    "opus": (frozenset(), frozenset({"opus"})),
    "flac": (frozenset(), frozenset({"flac"})),
}
# Исходные контейнеры с надёжными метками времени (только из них копируем).
_COPY_SOURCES = frozenset({"mp4", "mov", "mkv", "webm", "3gp", "mts", "m4a", "mp3", "flac", "ogg", "opus"})
# Кодеки, которым для копирования нужна extradata.
_NEEDS_EXTRADATA = frozenset({"h264", "hevc", "aac"})
# При копировании: bitstream-фильтр, удаляющий SEI (user data и пр.).
_COPY_BSF = {
    "h264": "filter_units=remove_types=6",
    "hevc": "filter_units=remove_types=39|40",
}
# Типы NAL SEI, удаляемые и из extradata (то же, что в `_COPY_BSF`).
_SEI_TYPES = {"h264": frozenset({6}), "hevc": frozenset({39, 40})}
# Стандартные раскладки по числу каналов (если исходную нельзя сохранить).
_STD_LAYOUTS = {1: "mono", 2: "stereo", 3: "3.0", 4: "quad", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}

# --- Пресеты ------------------------------------------------------------------

_X264_CRF = {Preset.HIGH: 18, Preset.MEDIUM: 23, Preset.SMALL: 28}
_X264_PRESET = "fast"
_VP9_CRF = {Preset.HIGH: 24, Preset.MEDIUM: 32, Preset.SMALL: 40}
_MPEG4_Q = {Preset.HIGH: 2, Preset.MEDIUM: 5, Preset.SMALL: 10}
# Битрейт звука, бит/с, по кодеку и пресету.
_AUDIO_BITRATE: dict[str, dict[Preset, int]] = {
    "libmp3lame": {Preset.HIGH: 320_000, Preset.MEDIUM: 192_000, Preset.SMALL: 96_000},
    "aac": {Preset.HIGH: 256_000, Preset.MEDIUM: 160_000, Preset.SMALL: 96_000},
    "libopus": {Preset.HIGH: 192_000, Preset.MEDIUM: 128_000, Preset.SMALL: 64_000},
    "vorbis": {Preset.HIGH: 256_000, Preset.MEDIUM: 160_000, Preset.SMALL: 96_000},
}
# Звук внутри видео (MP4/MOV/MKV/WEBM/AVI) — скромнее.
_VIDEO_AUDIO_BITRATE: dict[str, dict[Preset, int]] = {
    "aac": {Preset.HIGH: 192_000, Preset.MEDIUM: 128_000, Preset.SMALL: 96_000},
    "libopus": {Preset.HIGH: 160_000, Preset.MEDIUM: 112_000, Preset.SMALL: 64_000},
    "libmp3lame": {Preset.HIGH: 256_000, Preset.MEDIUM: 192_000, Preset.SMALL: 128_000},
}
# Перекодированное видео — не больше этого по длинной стороне (4K DCI):
# кадры 8K (до MAX_FRAME_PIXELS) декодер + x264 не укладывают в лимит памяти
# процесса (замер: 40 Мп → 3,6 ГБ), а плееры их всё равно почти не тянут.
# При копировании потоков (remux) разрешение не меняется.
MAX_OUTPUT_SIDE = 4096
# Кадр больше этого (пикселей) — «огромный»: энкодер в экономном режиме.
BIG_FRAME_PIXELS = 16_000_000
# «Маленький размер»: высота видео не больше.
SMALL_MAX_HEIGHT = 720
# GIF: (длинная сторона, кадров в секунду, цветов в палитре).
_GIF_SETTINGS = {
    Preset.HIGH: (480, 15, 256),
    Preset.MEDIUM: (480, 12, 256),
    Preset.SMALL: (320, 10, 128),
}

# --- Окно «Фрагмент для GIF»: кадры и предпросмотр (Ф20) ---------------------

# Предпросмотр: (длинная сторона, к/с, цветов) — маленький и быстрый.
PREVIEW_GIF_SETTINGS = (240, 8, 64)
# Картинки первого/последнего кадра — не больше по длинной стороне, px.
THUMB_MAX_SIDE = 320
# Пределы дочернего процесса окна (кадры — быстро, предпросмотр — дольше).
PREVIEW_STALL_TIMEOUT = 30.0
FRAMES_MAX_TIME = 60.0
PREVIEW_MAX_TIME = 300.0
PREVIEW_MEMORY_LIMIT = 2 * 2**30
# Предпросмотр больше этого (байт) не показываем (защита от «бомбы»).
PREVIEW_MAX_BYTES = 64 * 2**20
# Предпросмотр делается за ОДИН проход: уменьшенные кадры держатся в памяти
# (не больше стольких; при 240 px и RGB24 — до ≈ 100 МБ), по ним строятся
# палитра и GIF. Лишние кадры отбрасываются по меткам ДО графа фильтров.
PREVIEW_MAX_FRAMES = 600
# Палитра предпросмотра — по стольким кадрам (равномерно), а не по всем:
# palettegen на 480 кадрах — ≈ 1,3 с из бюджета 5 с (замер 1080p, 60 с).
PREVIEW_PALETTE_FRAMES = 60
# Дизеринг предпросмотра: упорядоченный (bayer) вдвое быстрее sierra2_4a.
PREVIEW_DITHER = "bayer:bayer_scale=3"
# Потоков декодера в предпросмотре (не больше числа ЦП; огромные кадры — 2):
# 16 вместо 8 — декодирование 1080p на 16 ЦП ≈ 2,1 с вместо 2,8 с на 60 с.
PREVIEW_DECODER_THREADS = 16
# …но только для кадров не больше стольких пикселей (≈ 4 Мп): у кадровой
# многопоточности каждый поток держит свои кадры — на 5K с 16 потоками пик
# памяти ≈ 1,3 ГБ при лимите пула "preview" 2 ГБ. Крупнее — 8, как обычно.
PREVIEW_THREADS_MAX_PIXELS = 4_200_000
# Фрагмент «до конца видео» (конец ≥ длительности из заголовка): конец
# отсекает кадры с допуском — не больше двух длительностей кадра и не больше
# столько секунд (метки AVI с B-кадрами сдвинуты на кадр, а заголовок может
# занижать длительность).
OPEN_END_SLACK = 0.5
# Жёсткий предел длины GIF из фрагмента по меткам кадров: gif_max + столько, с.
GIF_LENGTH_SLACK = 0.5
# Допустимые настройки GIF от родителя (проверка в ребёнке).
_GIF_SIDE_RANGE = (16, 480)
_GIF_FPS_RANGE = (1, 15)
_GIF_COLORS_RANGE = (2, 256)
# Seek «промахнулся»: первый кадр позже цели больше чем на столько, с.
_SEEK_SLACK = 0.5
# Читать пакеты не дальше конца фрагмента + столько, с (кадры без меток,
# перестановка B-кадров): дальше чтение прекращается.
_FRAGMENT_TAIL = 10.0


def _target_label(target_id: str) -> str:
    return tr(f"target_{target_id}")


def _make_targets(fmt: str, inputs: tuple[str, ...], outputs) -> list[Target]:
    fmt = (fmt or "").lower()
    if fmt not in inputs:
        return []
    return [
        Target(id=tid, label=_target_label(tid), extension=ext, uses_preset=uses, is_long=True)
        for tid, ext, uses in outputs
        if tid != fmt
    ]


def _user_message(key: str, params: tuple) -> str:
    template = _USER_MESSAGES.get(key)
    if template is None:
        return MSG_DAMAGED
    try:
        return template.format(*params)
    except (IndexError, ValueError, TypeError):
        return MSG_DAMAGED


class FragmentError(ValueError):
    """Фрагмент задан неверно: `key` — ключ `_USER_MESSAGES`, `params` — числа."""

    def __init__(self, key: str, *params: float) -> None:
        super().__init__(key, *params)
        self.key = key
        self.params = params


def parse_fragment(value: Any, max_seconds: float = GIF_MAX_SECONDS) -> tuple[float, float] | None:
    """Проверяет фрагмент (начало, конец) в секундах; None — фрагмента нет.

    Конечные числа, 0 ≤ начало < конец, длина ≤ `max_seconds` (с допуском
    `FRAGMENT_EPS`). Иначе — `FragmentError`. Вызывается и в родителе, и в
    ребёнке (данным из pickle ребёнок не доверяет).
    """
    if value is None:
        return None
    try:
        start, end = value
    except (TypeError, ValueError):
        raise FragmentError("fragment_bad") from None
    values = []
    for item in (start, end):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise FragmentError("fragment_bad")
        try:
            item = float(item)
        except OverflowError:  # целое больше диапазона float (10**400)
            raise FragmentError("fragment_bad") from None
        if not math.isfinite(item):
            raise FragmentError("fragment_bad")
        values.append(item)
    start, end = values
    if start < 0 or end <= start + FRAGMENT_EPS:
        raise FragmentError("fragment_bad")
    if end - start > max_seconds + FRAGMENT_EPS:
        raise FragmentError("fragment_too_long", end - start, max_seconds)
    return start, end


def _limits() -> dict[str, Any]:
    """Пределы для ребёнка (передаются словарём: тесты их подменяют)."""
    return {
        "max_width": MAX_WIDTH,
        "max_height": MAX_HEIGHT,
        "max_pixels": MAX_FRAME_PIXELS,
        "max_duration": MAX_DURATION,
        "max_streams": MAX_STREAMS,
        "gif_max_seconds": GIF_MAX_SECONDS,
        "max_decode_errors": MAX_DECODE_ERRORS,
        "max_fps_factor": MAX_FPS_FACTOR,
        "max_frames_base": MAX_FRAMES_BASE,
        "max_pixel_frames": MAX_PIXEL_FRAMES,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "disk_reserve": DISK_RESERVE,
        "min_free_bytes": MIN_FREE_BYTES,
        "disk_check_bytes": DISK_CHECK_BYTES,
    }


def _run(
    category: Category,
    inputs: tuple[str, ...],
    sources: list[Path],
    dst: Path,
    target: Target,
    preset: Preset,
    progress: ProgressCallback,
    cancel: CancelToken,
    formats: list[str],
    fragment: tuple[float, float] | None = None,
) -> None:
    """Общая часть `convert`: проверки и запуск задачи в дочернем процессе."""
    cancel.raise_if_cancelled()
    if not sources:
        raise ConversionError("Нет файлов для конвертации")
    formats = [str(f or "").lower() for f in (formats or [])]
    if len(formats) != len(sources):
        raise ConversionError("Внутренняя ошибка: не переданы форматы исходных файлов")
    if len(sources) != 1:
        raise ConversionError("Этот вариант обрабатывает один файл за раз")
    fmt = formats[0]
    if fmt not in inputs:
        raise ConversionError(MSG_DAMAGED)
    allowed = {t for t, _e, _u in (_VIDEO_OUTPUTS if category == Category.VIDEO else _AUDIO_OUTPUTS)}
    if target.id not in allowed or target.id not in _PLANS:
        raise ConversionError("Внутренняя ошибка: неизвестный вариант конвертации")
    preset = Preset(preset)
    if fragment is not None and target.id != "gif":
        raise ConversionError("Внутренняя ошибка: фрагмент задаётся только для GIF")
    try:
        fragment = parse_fragment(fragment)
    except FragmentError as exc:
        raise ConversionError(_user_message(exc.key, exc.params)) from None
    args = {
        "src": str(sources[0]),
        "dst": str(dst),
        "fmt": fmt,
        "target": target.id,
        "preset": preset.value,
        "limits": _limits(),
    }
    if fragment is not None:
        args["fragment"] = list(fragment)
    try:
        _isolated.run_task(
            _TASK, args, cancel=cancel, progress=progress,
            stall_timeout=STALL_TIMEOUT, max_time=MAX_TIME, memory_limit=MEDIA_MEMORY_LIMIT,
        )
    except _isolated.TaskFailed as exc:
        raise ConversionError(_user_message(exc.key, exc.params)) from exc
    except _isolated.ChildFailed as exc:
        log.info("Дочерний процесс аудио/видео: %s", exc)
        raise ConversionError(MSG_NO_MEMORY if exc.code == "memory" else MSG_DAMAGED) from exc
    progress(1.0)


class VideoConverter:
    """Конвертер видео (см. докстринг модуля)."""

    category = Category.VIDEO
    target_order = VIDEO_TARGET_ORDER
    # Очередь тяжёлых задач core.jobs: одна на видео и аудио (общий счётчик
    # мест "long" в _isolated).
    queue = "media"

    def targets(self, fmt: str) -> list[Target]:
        return _make_targets(fmt, VIDEO_INPUTS, _VIDEO_OUTPUTS)

    def convert(
        self,
        sources: list[Path],
        dst: Path,
        target: Target,
        preset: Preset,
        progress: ProgressCallback,
        cancel: CancelToken,
        *,
        formats: list[str],
        fragment: tuple[float, float] | None = None,
    ) -> None:
        """`fragment` — (начало, конец) в секундах, только для варианта "gif" (Ф20)."""
        _run(Category.VIDEO, VIDEO_INPUTS, [Path(s) for s in sources], Path(dst), target,
             preset, progress, cancel, formats, fragment)


class AudioConverter:
    """Конвертер аудио (см. докстринг модуля)."""

    category = Category.AUDIO
    target_order = AUDIO_TARGET_ORDER
    # Очередь тяжёлых задач core.jobs: одна на видео и аудио (общий счётчик
    # мест "long" в _isolated).
    queue = "media"

    def targets(self, fmt: str) -> list[Target]:
        return _make_targets(fmt, AUDIO_INPUTS, _AUDIO_OUTPUTS)

    def convert(
        self,
        sources: list[Path],
        dst: Path,
        target: Target,
        preset: Preset,
        progress: ProgressCallback,
        cancel: CancelToken,
        *,
        formats: list[str],
    ) -> None:
        _run(Category.AUDIO, AUDIO_INPUTS, [Path(s) for s in sources], Path(dst), target,
             preset, progress, cancel, formats)


# --- Окно «Фрагмент для GIF»: кадры и предпросмотр (родительская сторона) ----


@dataclass(frozen=True)
class FrameImage:
    """Картинка кадра: RGB24 без выравнивания строк (`width * 3` байт на строку)."""

    width: int
    height: int
    data: bytes


@dataclass(frozen=True)
class FragmentInfo:
    """Ответ `fragment_frames`.

    `duration` — длительность видео, с (None — неизвестна); `start`, `end` —
    фрагмент, для которого взяты кадры (при `end=None` в запросе ребёнок
    выбирает его сам: всё видео, если оно ≤ `GIF_MAX_SECONDS`, иначе
    первые `GIF_MAX_SECONDS` с); `first` — первый кадр фрагмента, `last` —
    последний (None — кадра нет: например, начало за концом видео).
    """

    duration: float | None
    start: float
    end: float
    first: FrameImage | None
    last: FrameImage | None


def _check_video_source(fmt: str) -> str:
    fmt = str(fmt or "").lower()
    if fmt not in VIDEO_INPUTS:
        raise ConversionError(MSG_DAMAGED)
    return fmt


def _run_preview_task(args: dict[str, Any], cancel: CancelToken, max_time: float) -> Any:
    """Действие окна GIF в дочернем процессе; ошибки → ConversionError, отмена → Cancelled."""
    args["limits"] = _limits()
    try:
        return _isolated.run_task(
            _TASK, args, cancel=cancel, progress=None, stall_timeout=PREVIEW_STALL_TIMEOUT,
            max_time=max_time, memory_limit=PREVIEW_MEMORY_LIMIT, pool="preview",
        )
    except _isolated.TaskFailed as exc:
        raise ConversionError(_user_message(exc.key, exc.params)) from exc
    except _isolated.ChildFailed as exc:
        log.info("Дочерний процесс окна GIF: %s", exc)
        raise ConversionError(MSG_NO_MEMORY if exc.code == "memory" else MSG_DAMAGED) from exc


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _frame_image(raw: Any) -> FrameImage | None:
    """Кадр из ответа ребёнка — с проверкой размеров (ответу не доверяем)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConversionError(MSG_DAMAGED)
    width, height, data = raw.get("width"), raw.get("height"), raw.get("data")
    if (not isinstance(width, int) or not isinstance(height, int) or not isinstance(data, bytes)
            or not 0 < width <= THUMB_MAX_SIDE or not 0 < height <= THUMB_MAX_SIDE
            or len(data) != width * height * 3):
        raise ConversionError(MSG_DAMAGED)
    return FrameImage(width, height, data)


def fragment_frames(
    src: Path, fmt: str, start: float, end: float | None, cancel: CancelToken,
) -> FragmentInfo:
    """Длительность видео и картинки первого/последнего кадра фрагмента.

    Всё чтение файла — в дочернем процессе (`pool="preview"`); отмена
    (`cancel`) убивает его — `Cancelled`. Ошибки — `ConversionError` с
    понятным текстом. `end=None` — фрагмент по умолчанию (см. `FragmentInfo`).
    """
    fmt = _check_video_source(fmt)
    start = _finite_or_none(start)
    if start is None:
        raise ConversionError(_user_message("fragment_bad", ()))
    if end is not None:
        end = _finite_or_none(end)
        if end is None or end <= start:
            raise ConversionError(_user_message("fragment_bad", ()))
    result = _run_preview_task(
        {"action": "frames", "src": str(src), "dst": "", "fmt": fmt, "target": "gif",
         "preset": Preset.MEDIUM.value, "start": start, "end": end},
        cancel, FRAMES_MAX_TIME,
    )
    if not isinstance(result, dict):
        raise ConversionError(MSG_DAMAGED)
    got_start = _finite_or_none(result.get("start"))
    got_end = _finite_or_none(result.get("end"))
    if got_start is None or got_end is None:
        raise ConversionError(MSG_DAMAGED)
    return FragmentInfo(
        duration=_finite_or_none(result.get("duration")),
        start=got_start,
        end=got_end,
        first=_frame_image(result.get("first")),
        last=_frame_image(result.get("last")),
    )


def gif_preview(src: Path, fmt: str, start: float, end: float, dst: Path, cancel: CancelToken) -> bytes:
    """Маленький GIF фрагмента (`PREVIEW_GIF_SETTINGS`) — содержимое файла.

    Ребёнок пишет его в `dst` (временный файл вызывающего; удаляет его тоже
    вызывающий — и при ошибке, и при отмене). Отмена — `Cancelled`, ошибки —
    `ConversionError`; GIF больше `PREVIEW_MAX_BYTES` — ошибка.
    """
    fmt = _check_video_source(fmt)
    try:
        fragment = parse_fragment((start, end))
    except FragmentError as exc:
        raise ConversionError(_user_message(exc.key, exc.params)) from None
    _run_preview_task(
        {"action": "preview", "src": str(src), "dst": str(dst), "fmt": fmt, "target": "gif",
         "preset": Preset.MEDIUM.value, "fragment": list(fragment),
         "gif_settings": list(PREVIEW_GIF_SETTINGS)},
        cancel, PREVIEW_MAX_TIME,
    )
    try:
        size = os.path.getsize(dst)
        if size > PREVIEW_MAX_BYTES:
            raise ConversionError(MSG_DAMAGED)
        with open(dst, "rb") as fh:
            return fh.read(PREVIEW_MAX_BYTES + 1)
    except OSError as exc:
        raise ConversionError(MSG_DAMAGED) from exc


# =============================================================================
# Дочерний процесс: вся работа с FFmpeg ниже выполняется ТОЛЬКО в ребёнке.
# =============================================================================

TaskError = _isolated.TaskError

# Позиция в файле и время — «отметки» продвижения для родителя.
_US = 1_000_000


def child_task(args: dict[str, Any], report) -> dict[str, Any]:
    """Точка входа задачи "media" в дочернем процессе."""
    import av  # noqa: F401 — импорт только в ребёнке

    # Журнал FFmpeg полностью отключён (обратный вызов-заглушка на C, это
    # умолчание PyAV). НЕЛЬЗЯ ставить числовой уровень (например PANIC):
    # тогда PyAV ставит Python-обратный вызов, которому нужен GIL из потоков
    # декодера FFmpeg, — это взаимная блокировка (воспроизведено: зависание
    # в декодировании после настройки logging в процессе).
    av.logging.set_level(None)
    action = args.get("action", "convert")
    if action == "frames":
        return _MediaJob(args, report).run_frames(args.get("start"), args.get("end"))
    if action == "preview":
        args = dict(args, target="gif", single_pass=True)
        return _MediaJob(args, report).run()
    if action != "convert":
        raise ValueError("unknown media action")
    return _MediaJob(args, report).run()


def _open_input(src: str, fmt: str, max_streams: int):
    import av

    return av.open(
        src,
        mode="r",
        format=_DEMUXERS[fmt],
        container_options={
            "protocol_whitelist": "file",
            "max_streams": str(max_streams + 1),
        },
    )


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def _rotation_filters(rotation: float) -> list[tuple[str, str | None]]:
    """Фильтры, применяющие поворот матрицы отображения (как autorotate в ffmpeg).

    `rotation` — `VideoFrame.rotation` (угол против часовой стрелки);
    нужный поворот картинки theta = -rotation.
    """
    theta = (-round(rotation)) % 360
    if abs(theta - 90) < 1:
        return [("transpose", "clock")]
    if abs(theta - 180) < 1:
        return [("hflip", None), ("vflip", None)]
    if abs(theta - 270) < 1:
        return [("transpose", "cclock")]
    return []


class _MediaJob:
    """Одна конвертация в дочернем процессе."""

    def __init__(self, args: dict[str, Any], report) -> None:
        self.src = str(args["src"])
        self.dst = str(args["dst"])
        self.fmt = str(args["fmt"])
        self.target = str(args["target"])
        self.preset = Preset(args["preset"])
        limits = dict(args.get("limits") or {})
        self.max_width = int(limits.get("max_width", MAX_WIDTH))
        self.max_height = int(limits.get("max_height", MAX_HEIGHT))
        self.max_pixels = int(limits.get("max_pixels", MAX_FRAME_PIXELS))
        self.max_duration = float(limits.get("max_duration", MAX_DURATION))
        self.max_streams = int(limits.get("max_streams", MAX_STREAMS))
        self.gif_max = float(limits.get("gif_max_seconds", GIF_MAX_SECONDS))
        self.max_decode_errors = int(limits.get("max_decode_errors", MAX_DECODE_ERRORS))
        self.max_fps_factor = float(limits.get("max_fps_factor", MAX_FPS_FACTOR))
        self.max_frames_base = float(limits.get("max_frames_base", MAX_FRAMES_BASE))
        self.max_pixel_frames = float(limits.get("max_pixel_frames", MAX_PIXEL_FRAMES))
        self.max_output_bytes = int(limits.get("max_output_bytes", MAX_OUTPUT_BYTES))
        self.disk_reserve = int(limits.get("disk_reserve", DISK_RESERVE))
        self.min_free_bytes = int(limits.get("min_free_bytes", MIN_FREE_BYTES))
        self.disk_check_bytes = max(1, int(limits.get("disk_check_bytes", DISK_CHECK_BYTES)))
        self.report = report
        self.muxer, self.vcodec, self.acodec = _PLANS[self.target]
        # Фрагмент для GIF (Ф20): (начало, конец) в секундах от начала видео.
        try:
            self.fragment = parse_fragment(args.get("fragment"), self.gif_max)
        except FragmentError as exc:
            raise TaskError(exc.key, *exc.params) from None
        # Фрагмент до конца видео: конец не отсекает кадры (метки бывают
        # сдвинуты на кадр — например, H.264 с B-кадрами в AVI).
        self.open_end = False
        # Предпросмотр: один проход (см. `_run_gif_single`).
        self.single_pass = bool(args.get("single_pass"))
        if self.fragment is not None and self.target != "gif":
            raise TaskError("fragment_bad")
        self.gif_settings = _parse_gif_settings(args.get("gif_settings"), self.preset)
        # Нижняя граница «длительности» для предела числа кадров: при чтении
        # фрагмента декодируется всё от ключевого кадра до его конца.
        self.count_floor = 0.0
        try:
            self.file_size = os.path.getsize(self.src)
        except OSError:
            self.file_size = 0
        self.decode_errors = 0
        # Прогресс: доля [span_from, span_to] для текущего прохода.
        self.span = (0.0, 1.0)
        self.total: float | None = None
        self.best_time_us = 0
        self.best_pos = 0
        # Отметки продвижения монотонны по всей задаче (а не по проходу):
        # к времени/позиции текущего прохода прибавляются итоги прошлых
        # проходов — иначе `_Reporter` во втором проходе GIF молчит.
        self.mark_base_time = 0
        self.mark_base_pos = 0
        # Декодированные кадры видео (предел «слишком сложный файл»).
        self.frames = 0
        self.pixel_frames = 0
        # Записанные байты результата и когда последний раз проверяли место.
        self.written = 0
        self.checked_at = 0

    # -- проходы, кадры, диск --

    def _next_pass(self, span: tuple[float, float]) -> None:
        """Начало следующего прохода по файлу (GIF): доля — в `span`, отметки растут дальше."""
        self.mark_base_time += self.best_time_us + 1
        self.mark_base_pos += self.best_pos + 1
        self.best_time_us = 0
        self.best_pos = 0
        self.span = span
        self.frames = 0
        self.pixel_frames = 0

    def _count_frame(self, width: int, height: int) -> None:
        """Учёт декодированного кадра видео: кадров не больше длительность ×
        `max_fps_factor` + `max_frames_base`, «пиксели × кадры» — не больше
        `max_pixel_frames`."""
        self.frames += 1
        self.pixel_frames += max(0, width) * max(0, height)
        seconds = max(self.total or 0.0, self.best_time_us / _US, self.count_floor)
        if (self.frames > seconds * self.max_fps_factor + self.max_frames_base
                or self.pixel_frames > self.max_pixel_frames):
            raise TaskError("too_complex")

    def _free_space(self) -> int | None:
        try:
            folder = os.path.dirname(os.path.abspath(self.dst)) or "."
            return int(shutil.disk_usage(folder).free)
        except (OSError, ValueError):
            log.debug("Не удалось узнать свободное место", exc_info=True)
            return None

    def _check_space(self, estimate: int) -> None:
        """До записи: не больше ли оценка предела размера и свободно ли место
        под результат (оценка + запас)."""
        if estimate > self.max_output_bytes:
            raise TaskError("output_too_large", self.max_output_bytes / 2**30)
        need = max(0, int(estimate)) + self.disk_reserve
        free = self._free_space()
        if free is not None and free < need:
            raise TaskError("no_space", need / 2**30, free / 2**30)

    def _account(self, size: int) -> None:
        """Учёт записанных байт: предел размера и периодическая проверка места."""
        self.written += max(0, int(size))
        if self.written > self.max_output_bytes:
            raise TaskError("output_too_large", self.max_output_bytes / 2**30)
        if self.written - self.checked_at >= self.disk_check_bytes:
            self.checked_at = self.written
            free = self._free_space()
            if free is not None and free < self.min_free_bytes:
                raise TaskError("disk_full", self.min_free_bytes / 2**30)

    def _ts_fixer(self, container) -> "_TsFixer":
        try:
            flags = int(container.format.flags)
        except Exception:  # noqa: BLE001
            flags = 0
        return _TsFixer(bool(flags & _AVFMT_TS_DISCONT) or self.fmt in _TS_DISCONT_FORMATS)

    # -- общие проверки --

    def _check_frame_size(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise TaskError("damaged")
        if width > self.max_width or height > self.max_height or width * height > self.max_pixels:
            raise TaskError(
                "too_large", width, height, self.max_width, self.max_height,
                self.max_pixels / 1_000_000,
            )

    def _inspect(self, container):
        """Проверки заголовков; возвращает (видеопоток | None, звуковой | None, длительность | None)."""
        streams = list(container.streams)
        if len(streams) > self.max_streams:
            raise TaskError("too_many_streams", len(streams), self.max_streams)
        video = None
        audio = None
        for stream in streams:
            if stream.type == "video" and video is None and stream.codec_context is not None:
                try:
                    attached = bool(int(stream.disposition) & 0x400)  # AV_DISPOSITION_ATTACHED_PIC
                except Exception:  # noqa: BLE001
                    attached = False
                if not attached:
                    video = stream
            elif stream.type == "audio" and audio is None and stream.codec_context is not None:
                audio = stream
        if video is not None:
            cc = video.codec_context
            if cc.width or cc.height:
                self._check_frame_size(int(cc.width), int(cc.height))
        if audio is not None:
            cc = audio.codec_context
            rate = int(cc.sample_rate or 0)
            channels = int(cc.channels or 0) if hasattr(cc, "channels") else 0
            if rate < 0 or rate > MAX_SAMPLE_RATE or channels > MAX_CHANNELS:
                raise TaskError("bad_audio")
        duration = None
        if container.duration is not None and container.duration > 0:
            duration = container.duration / _US
        else:
            for stream in (video, audio):
                if stream is not None and stream.duration and stream.time_base:
                    value = float(stream.duration * stream.time_base)
                    if value > 0:
                        duration = max(duration or 0.0, value)
        if duration is not None and not math.isfinite(duration):
            duration = None
        if duration is not None and duration > self.max_duration:
            raise TaskError("too_long", duration / 3600, self.max_duration / 3600)
        return video, audio, duration

    @staticmethod
    def _start_time(streams) -> float:
        """Общее начало выбранных потоков, с (на него сдвигаются метки)."""
        starts = []
        for stream in streams:
            if stream is not None and stream.start_time is not None and stream.time_base:
                starts.append(float(stream.start_time * stream.time_base))
        starts = [s for s in starts if math.isfinite(s)]
        return min(starts) if starts else 0.0

    # -- прогресс --

    def _progress_packet(self, packet, t0: float) -> None:
        time_s = None
        ts = packet.pts if packet.pts is not None else packet.dts
        if ts is not None and packet.time_base:
            time_s = float(ts * packet.time_base) - t0
        pos = packet.pos if packet.pos is not None and packet.pos >= 0 else None
        self._advance(time_s, pos)

    def _advance(self, time_s: float | None, pos: int | None = None) -> None:
        if time_s is not None and math.isfinite(time_s) and time_s > 0:
            if time_s > self.max_duration:
                raise TaskError("too_long", time_s / 3600, self.max_duration / 3600)
            self.best_time_us = max(self.best_time_us, int(time_s * _US))
        if pos is not None:
            self.best_pos = max(self.best_pos, int(pos))
        fraction = None
        if self.total:
            fraction = min(0.99, self.best_time_us / _US / self.total)
        elif self.file_size > 0 and self.best_pos > 0:
            fraction = min(0.99, self.best_pos / self.file_size)
        if fraction is not None:
            low, high = self.span
            fraction = low + (high - low) * fraction
        self.report(fraction, self.mark_base_time + self.best_time_us,
                    self.mark_base_pos + self.best_pos)

    @staticmethod
    def _setup_decoder(stream, max_threads: int = 8) -> None:
        """Потоки декодера видео: у кадровой многопоточности каждый поток держит
        свои кадры, поэтому для огромных кадров — только 2 потока (память).
        `max_threads` — предел потоков для обычных кадров (предпросмотр — больше)."""
        if stream is None or stream.type != "video":
            return
        cc = stream.codec_context
        try:
            pixels = int(cc.width or 0) * int(cc.height or 0)
            cc.thread_type = "AUTO"
            if pixels > PREVIEW_THREADS_MAX_PIXELS:
                max_threads = min(max_threads, 8)
            cc.thread_count = (2 if pixels > BIG_FRAME_PIXELS
                               else max(1, min(max_threads, os.cpu_count() or 1)))
        except Exception:  # noqa: BLE001 — не критично
            log.debug("Не удалось настроить потоки декодера", exc_info=True)

    def _decode(self, packet):
        """Кадры пакета; ошибки отдельных пакетов пропускаются (до предела)."""
        import av

        try:
            return packet.decode()
        except av.error.FFmpegError as exc:
            if isinstance(exc, MemoryError):
                raise
            self.decode_errors += 1
            if self.decode_errors > self.max_decode_errors:
                raise TaskError("damaged") from exc
            return []

    # -- основной путь --

    def run(self) -> dict[str, Any]:
        if self.target == "gif":
            return self._run_gif()
        with _open_input(self.src, self.fmt, self.max_streams) as inp:
            video, audio, duration = self._inspect(inp)
            self.total = duration
            want_video = self.vcodec is not None
            if not want_video:
                video = None
                if audio is None:
                    raise TaskError("no_audio" if self.target in (TARGET_AUDIO_MP3, TARGET_AUDIO_WAV)
                                    else "no_audio_file")
            elif video is None:
                raise TaskError("no_video")
            self.report(0.0, 0, 0)
            return self._transcode(inp, video, audio)

    def _first_frame(self, video) -> "_FrameInfo":
        """Первый кадр видео (размер, формат, поворот) — отдельным открытием файла.

        Все выходные потоки нужно создать ДО записи первого пакета (иначе
        муксер получит поток после заголовка), а размер результата зависит
        от поворота и размера кадра, которые известны только после
        декодирования. Поэтому файл открывается ещё раз и декодируется до
        первого кадра (не больше `_PROBE_PACKETS` пакетов).
        """
        with _open_input(self.src, self.fmt, self.max_streams) as probe:
            stream = next((s for s in probe.streams if s.index == video.index), None)
            if stream is None:
                raise TaskError("damaged")
            self._setup_decoder(stream)
            for count, packet in enumerate(probe.demux(stream)):
                if count >= _PROBE_PACKETS:
                    break
                for frame in self._decode(packet):
                    self._check_frame_size(int(frame.width), int(frame.height))
                    return _FrameInfo.of(frame)
            try:
                for frame in stream.codec_context.decode(None):
                    self._check_frame_size(int(frame.width), int(frame.height))
                    return _FrameInfo.of(frame)
            except TaskError:
                raise
            except Exception:  # noqa: BLE001
                pass
        raise TaskError("damaged")

    def _can_copy(self, stream, video: bool) -> bool:
        if self.preset != Preset.HIGH or self.fmt not in _COPY_SOURCES:
            return False
        allowed = _COPY_CODECS.get(self.target)
        if allowed is None:
            return False
        name = stream.codec_context.name
        if name not in allowed[0 if video else 1]:
            return False
        if name in _NEEDS_EXTRADATA and not stream.codec_context.extradata:
            return False
        # extradata, из которой нельзя надёжно убрать SEI, — не копируем.
        if name in _SEI_TYPES and _strip_sei_extradata(name, bytes(stream.codec_context.extradata)) is None:
            return False
        return True

    def _transcode(self, inp, video, audio) -> dict[str, Any]:
        import av

        t0 = self._start_time([video, audio])
        self._setup_decoder(video)
        container_options = {"rf64": "auto"} if self.muxer == "wav" else {}
        first = self._first_frame(video) if video is not None else None
        # Повёрнутое видео не копируется: поворот применяется к кадрам.
        copy_video = (video is not None and self._can_copy(video, True)
                      and not _rotation_filters(first.rotation))
        copy_audio = audio is not None and self._can_copy(audio, False)
        audio_only = video is None
        # Место на диске — до создания файла результата.
        self._check_space(self._estimate(
            None if audio is None or copy_audio else audio, audio_only, copy_video or copy_audio,
        ))
        with av.open(self.dst, mode="w", format=self.muxer, container_options=container_options) as raw_out:
            out = _Output(self, raw_out)
            vout = aout = None
            if video is not None:
                vout = (_CopyStream(out, video, t0) if copy_video
                        else _VideoEncoder(self, out, video, t0, first))
            if audio is not None:
                aout = (_CopyStream(out, audio, t0) if copy_audio
                        else _AudioEncoder(self, out, audio, t0, audio_only))
            selected = [s for s in (video, audio) if s is not None]
            handlers = {}
            if video is not None:
                handlers[video.index] = vout
            if audio is not None:
                handlers[audio.index] = aout
            fixer = self._ts_fixer(inp)
            for packet in inp.demux(*selected):
                handler = handlers.get(packet.stream.index)
                if handler is None:
                    continue
                if packet.dts is None and packet.pts is None and packet.size == 0:
                    continue  # пакет-заглушка конца потока
                fixer.fix(packet)
                self._progress_packet(packet, t0)
                handler.packet(packet)
            for handler in handlers.values():
                handler.flush()
            produced = {name: h.produced for name, h in (("video", vout), ("audio", aout)) if h}
        if not any(produced.values()):
            raise TaskError("damaged")
        if vout is not None and not vout.produced:
            raise TaskError("damaged")
        return {
            "copied_video": isinstance(vout, _CopyStream),
            "copied_audio": isinstance(aout, _CopyStream),
        }

    def _estimate(self, encode_audio, audio_only: bool, copied: bool) -> int:
        """Оценка размера результата сверху, байт.

        PCM и FLAC — длительность × частота × каналы × 2 байта (FLAC не больше
        PCM); сжатый звук — по битрейту (+10 %); копирование — размер входа;
        перекодированное видео — без оценки (0, его размер заранее не узнать:
        за ним следит счётчик байт во время записи).
        """
        duration = self.total
        total = self.file_size if copied else 0
        if encode_audio is None or not duration or duration <= 0:
            return total
        rate, channels, _layout, _fmt = _audio_plan(self.acodec, encode_audio.codec_context)
        if self.acodec in ("pcm_s16le", "flac"):
            total += int(duration * rate * channels * 2)
        else:
            table = _AUDIO_BITRATE if audio_only else _VIDEO_AUDIO_BITRATE
            bitrate = table.get(self.acodec, {}).get(self.preset, 320_000)
            total += int(duration * bitrate / 8 * 1.1)
        return total

    # -- GIF --

    def _gif_prepare(self) -> tuple[int, float]:
        """Проверки заголовков и длины для GIF; → (индекс видеопотока, t0)."""
        with _open_input(self.src, self.fmt, self.max_streams) as inp:
            video, _audio, duration = self._inspect(inp)
            if video is None:
                raise TaskError("no_video")
            t0 = self._start_time([video])
            index = video.index
        if self.fragment is None:
            if duration is not None and duration > self.gif_max + 0.5:
                raise TaskError("gif_too_long", math.ceil(duration), self.gif_max)
            self.total = duration
            return index, t0
        start, end = self.fragment
        if duration is not None:
            if start >= duration - FRAGMENT_EPS:
                raise TaskError("fragment_after_end", start, duration)
            # Конец позже фактической длины — не ошибка: GIF до конца видео.
            end = min(end, duration)
        self.total = max(end - start, FRAGMENT_EPS)
        self.open_end = duration is not None and self.fragment[1] >= duration - FRAGMENT_EPS
        self.count_floor = self.fragment[1]
        return index, t0

    def _skip_frame(self, frame) -> None:
        """Кадр, который не идёт в результат, — тоже в пределах «слишком сложный файл»."""
        self._check_frame_size(int(frame.width), int(frame.height))
        self._count_frame(int(frame.width), int(frame.height))

    def _frames(self, index: int, t0: float, seek_to: float, stop_at: float | None, *,
                gif_check: bool = False):
        """Кадры видеопотока: (поток, кадр, время от начала видео | None).

        С ключевого кадра не позже `seek_to` (0 — с начала файла); чтение
        пакетов прекращается, когда их время больше `stop_at` +
        `_FRAGMENT_TAIL` (None — до конца файла). Если seek не удался или
        «промахнулся» (первый кадр позже `seek_to` + `_SEEK_SLACK`), файл
        открывается заново и читается с начала — до первого кадра ничего не
        отдано, так что повтор незаметен вызывающему. `gif_check` — проверка
        длины «GIF без фрагмента» по меткам пакетов (как раньше).
        """
        import av

        seek = seek_to > FRAGMENT_EPS
        while True:
            missed = False
            with _open_input(self.src, self.fmt, self.max_streams) as inp:
                video = next((s for s in inp.streams if s.index == index), None)
                if video is None or not video.time_base:
                    raise TaskError("damaged")
                self._setup_decoder(video, PREVIEW_DECODER_THREADS if self.single_pass else 8)
                if seek:
                    try:
                        inp.seek(int(math.floor((t0 + seek_to) / video.time_base)),
                                 stream=video, backward=True, any_frame=False)
                    except (av.error.FFmpegError, OverflowError, ValueError):
                        log.debug("seek не удался — читаю с начала", exc_info=True)
                        seek = False
                        continue
                fixer = self._ts_fixer(inp)
                # Прогресс — от начала фрагмента (раньше — время ≤ 0, не считается).
                origin = t0 + (seek_to if seek_to > 0 else 0.0)
                first = True
                for packet in inp.demux(video):
                    fixer.fix(packet)
                    self._progress_packet(packet, origin)
                    if gif_check:
                        self._gif_time_check(packet, t0)
                    if stop_at is not None:
                        packet_time = _packet_seconds(packet, t0)
                        if packet_time is not None and packet_time > stop_at + _FRAGMENT_TAIL:
                            return
                    for frame in self._decode(packet):
                        seconds = _frame_seconds(frame, t0)
                        if first and seek and seconds is not None and seconds > seek_to + _SEEK_SLACK:
                            missed = True
                            break
                        first = False
                        yield video, frame, seconds
                    if missed:
                        break
            if not missed:
                return
            log.debug("seek промахнулся — читаю с начала")
            seek = False

    def _gif_frames(self, index: int, t0: float):
        """Кадры для GIF: (поток, кадр). Без фрагмента — всё видео, с фрагментом —
        только [начало, конец): раньше начала — отбрасываются, на первом кадре
        не раньше конца чтение прекращается."""
        if self.fragment is None:
            frames = self._frames(index, t0, 0.0, None, gif_check=True)
            try:
                for stream, frame, _seconds in frames:
                    yield stream, frame
            finally:
                frames.close()
            return
        start, end = self.fragment
        started = False
        first: float | None = None
        slack: float | None = None
        frames = self._frames(index, t0, start, end)
        try:
            for stream, frame, seconds in frames:
                if slack is None:
                    slack = 0.0
                    if self.open_end:
                        slack = OPEN_END_SLACK
                        try:
                            rate = float(stream.average_rate or 0)
                            if rate > 0:
                                slack = min(OPEN_END_SLACK, 2.0 / rate)
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass
                if seconds is None:
                    if not started:
                        self._skip_frame(frame)  # без метки: место не определить
                        continue
                elif seconds < start - FRAGMENT_EPS:
                    self._skip_frame(frame)
                    continue
                elif seconds >= end + slack - FRAGMENT_EPS:
                    return
                if seconds is not None:
                    if first is None:
                        first = seconds
                    elif seconds - first > self.gif_max + GIF_LENGTH_SLACK:
                        return  # жёсткая защита: GIF не длиннее предела
                started = True
                yield stream, frame
        finally:
            frames.close()

    def _run_gif_single(self) -> dict[str, Any]:
        """Предпросмотр за один проход по файлу.

        Кадры фрагмента прореживаются по меткам до нужных к/с (берётся первый
        кадр каждого интервала 1/к/с — как фильтр fps) ДО графа фильтров,
        уменьшаются и держатся в памяти (не больше `PREVIEW_MAX_FRAMES`), по
        ним строится палитра, затем GIF. После первого взятого кадра, если
        частота видео хотя бы вдвое выше нужной, декодер пропускает
        неопорные кадры (`skip_frame=NONREF`) — первый кадр остаётся точным.
        """
        import av

        max_side, fps, colors = self.gif_settings
        index, t0 = self._gif_prepare()
        self.report(0.0, 0, 0)
        start = self.fragment[0] if self.fragment is not None else 0.0
        empty = "fragment_empty" if self.fragment is not None else "damaged"
        small: list = []
        pipe: _VideoPipe | None = None
        out_size: tuple[int, int] | None = None
        last_slot = -1
        nonref_checked = False
        frames = self._gif_frames(index, t0)
        try:
            for stream, frame in frames:
                seconds = _frame_seconds(frame, t0)
                if seconds is None:
                    slot = last_slot + 1
                else:
                    slot = int(math.floor(max(0.0, seconds - start) * fps + 1e-6))
                if slot <= last_slot:
                    self._skip_frame(frame)  # лишний для этих к/с — в граф не идёт
                    continue
                self._skip_frame(frame)
                last_slot = slot
                if not nonref_checked:
                    nonref_checked = True
                    try:
                        rate = float(stream.average_rate or 0)
                        if rate >= 2 * fps:
                            stream.codec_context.skip_frame = "NONREF"
                    except Exception:  # noqa: BLE001 — только ускорение
                        log.debug("skip_frame недоступен", exc_info=True)
                if pipe is None:
                    width, height, rotation = _VideoGeometry.compute(
                        frame, stream, max_height=None, max_side=max_side,
                    )
                    out_size = (width, height)
                    pipe = _VideoPipe(_StreamInfo(stream), out_size, rotation, "rgb24")
                # Метка — настоящее время от начала фрагмента (в сотых, как у GIF).
                frame.pts = slot * 100 // fps if seconds is None else int(round(max(0.0, seconds - start) * 100))
                frame.time_base = Fraction(1, 100)
                small.extend(pipe.push(frame))
                if len(small) >= PREVIEW_MAX_FRAMES:
                    break  # метки без смысла (кадры без pts) — предпросмотр короче
        finally:
            frames.close()
        if pipe is not None:
            small.extend(pipe.flush())
        small = small[:PREVIEW_MAX_FRAMES]
        if not small or out_size is None:
            raise TaskError(empty)
        step = max(1, math.ceil(len(small) / PREVIEW_PALETTE_FRAMES))
        palette = _make_palette(small[::step], colors)
        self._check_space(0)
        with av.open(self.dst, mode="w", format="gif") as raw_out:
            out = _Output(self, raw_out)
            stream = out.add_stream("gif", rate=fps)
            stream.width, stream.height = out_size
            stream.pix_fmt = "pal8"
            stream.codec_context.time_base = Fraction(1, 100)
            encoder = _FrameWriter(out, stream, Fraction(1, 100), 0.0, rebase=True)
            info = _StreamInfo.of(Fraction(1, 100))
            use = _VideoPipe(info, out_size, [], "rgb24", palette=palette, dither=PREVIEW_DITHER)
            small.reverse()
            while small:
                for result in use.push(small.pop()):  # память освобождается по ходу
                    encoder.write(result)
            for result in use.flush():
                encoder.write(result)
            encoder.flush()
            if not encoder.produced:
                raise TaskError(empty)
        return {"copied_video": False, "copied_audio": False}

    def _run_gif(self) -> dict[str, Any]:
        import av

        if self.single_pass:
            return self._run_gif_single()
        max_side, fps, colors = self.gif_settings
        index, t0 = self._gif_prepare()
        self.report(0.0, 0, 0)
        empty = "fragment_empty" if self.fragment is not None else "damaged"
        # Проход 1: палитра по всему ролику или фрагменту (доля 0–0,5).
        self.span = (0.0, 0.5)
        palette_pipe: _GifPipe | None = None
        frames = self._gif_frames(index, t0)
        try:
            for stream, frame in frames:
                if palette_pipe is None:
                    palette_pipe = _GifPipe(self, stream, max_side, fps, colors, palette=None)
                palette_pipe.push(frame)
        finally:
            frames.close()
        if palette_pipe is None:
            raise TaskError(empty)
        palette = palette_pipe.finish_palette()
        # Проход 2: кадры с этой палитрой (доля 0,5–1; отметки продолжают расти).
        self._next_pass((0.5, 1.0))
        # Размер GIF заранее не оценить (≤ 60 с маленьких кадров) — только запас.
        self._check_space(0)
        with av.open(self.dst, mode="w", format="gif") as raw_out:
            out = _Output(self, raw_out)
            stream = out.add_stream("gif", rate=fps)
            stream.width, stream.height = palette_pipe.out_size
            stream.pix_fmt = "pal8"
            stream.codec_context.time_base = Fraction(1, 100)
            # С фрагментом метки результата начинаются ровно с 0.
            encoder = _FrameWriter(out, stream, Fraction(1, 100), t0,
                                   rebase=self.fragment is not None)
            pipe: _GifPipe | None = None
            frames = self._gif_frames(index, t0)
            try:
                for video, frame in frames:
                    if pipe is None:
                        pipe = _GifPipe(self, video, max_side, fps, colors, palette=palette,
                                        out_size=palette_pipe.out_size)
                    for result in pipe.push(frame):
                        encoder.write(result)
            finally:
                frames.close()
            if pipe is not None:
                for result in pipe.flush():
                    encoder.write(result)
            encoder.flush()
            if not encoder.produced:
                raise TaskError(empty)
        return {"copied_video": False, "copied_audio": False}

    def _gif_time_check(self, packet, t0: float) -> None:
        ts = packet.pts if packet.pts is not None else packet.dts
        if ts is not None and packet.time_base:
            if float(ts * packet.time_base) - t0 > self.gif_max + 0.5:
                raise TaskError("gif_too_long_unknown", self.gif_max)

    # -- окно «Фрагмент для GIF»: длительность и кадры (Ф20) --

    def run_frames(self, start: Any, end: Any) -> dict[str, Any]:
        """Длительность и картинки первого/последнего кадра фрагмента [start, end).

        `end=None` — фрагмент по умолчанию: всё видео, если оно не длиннее
        `gif_max`, иначе первые `gif_max` с (длительность неизвестна — тоже
        `gif_max`). Кадра нет (начало за концом видео) — None вместо картинки.
        """
        start = _finite_or_none(start)
        if start is None:
            raise TaskError("fragment_bad")
        if end is not None:
            end = _finite_or_none(end)
            if end is None or end <= start:
                raise TaskError("fragment_bad")
        with _open_input(self.src, self.fmt, self.max_streams) as inp:
            video, _audio, duration = self._inspect(inp)
            if video is None:
                raise TaskError("no_video")
            t0 = self._start_time([video])
            index = video.index
        self.report(None, 0, 0)
        if end is None:
            end = min(duration, self.gif_max) if duration else self.gif_max
            if end <= start:
                end = start + self.gif_max
        first = self._thumb_at(index, t0, start, end, last=False)
        last = self._thumb_at(index, t0, start, end, last=True) if first is not None else None
        return {"duration": duration, "start": start, "end": end, "first": first, "last": last}

    def _thumb_at(self, index: int, t0: float, start: float, end: float, *, last: bool):
        """Первый (`last=False`) или последний кадр в [start, end) — картинкой."""
        self.count_floor = max(self.count_floor, end)
        seek_to = max(0.0, end - 2 * FRAGMENT_EPS) if last else start
        found = None
        info = None
        frames = self._frames(index, t0, seek_to, end)
        try:
            for stream, frame, seconds in frames:
                self._skip_frame(frame)
                if info is None:
                    info = _StreamInfo(stream)
                if seconds is None:
                    continue
                if seconds >= end - FRAGMENT_EPS:
                    break
                if seconds < start - FRAGMENT_EPS:
                    continue
                found = frame
                if not last:
                    break
        finally:
            frames.close()
        if found is None or info is None:
            return None
        return _thumbnail(found, info)


def _packet_seconds(packet, t0: float) -> float | None:
    ts = packet.pts if packet.pts is not None else packet.dts
    if ts is None or not packet.time_base:
        return None
    value = float(ts * packet.time_base) - t0
    return value if math.isfinite(value) else None


def _frame_seconds(frame, t0: float) -> float | None:
    if frame.pts is None or not frame.time_base:
        return None
    value = float(frame.pts * frame.time_base) - t0
    return value if math.isfinite(value) else None


def _parse_gif_settings(value: Any, preset: Preset) -> tuple[int, int, int]:
    """Настройки GIF (сторона, к/с, цветов): по пресету или от родителя (с проверкой)."""
    if value is None:
        return _GIF_SETTINGS[preset]
    try:
        side, fps, colors = (int(v) for v in value)
    except (TypeError, ValueError):
        raise TaskError("damaged") from None
    for item, (low, high) in zip((side, fps, colors), (_GIF_SIDE_RANGE, _GIF_FPS_RANGE, _GIF_COLORS_RANGE)):
        if not low <= item <= high:
            raise TaskError("damaged")
    return side, fps, colors


class _StreamInfo:
    """То, что нужно от потока для картинки кадра, — копией (поток живёт, пока открыт файл)."""

    def __init__(self, stream) -> None:
        self.sample_aspect_ratio = stream.sample_aspect_ratio
        self.time_base = stream.time_base

    @classmethod
    def of(cls, time_base: Fraction) -> "_StreamInfo":
        """Для уже уменьшенных кадров: квадратные пиксели, заданная база времени."""
        info = cls.__new__(cls)
        info.sample_aspect_ratio = None
        info.time_base = time_base
        return info


def _make_palette(frames: list, colors: int):
    """Палитра (palettegen) по кадрам в памяти."""
    import av

    graph = av.filter.Graph()
    first = frames[0]
    src = graph.add_buffer(width=first.width, height=first.height, format=first.format,
                           time_base=first.time_base)
    gen = graph.add("palettegen", f"max_colors={colors}:stats_mode=full")
    sink = graph.add("buffersink")
    src.link_to(gen)
    gen.link_to(sink)
    graph.configure()
    for frame in frames:
        src.push(frame)
        _pull_all(graph)
    src.push(None)
    result = _pull_all(graph)
    if not result:
        raise TaskError("damaged")
    return result[-1]


def _thumbnail(frame, info: _StreamInfo) -> dict[str, Any]:
    """Кадр → RGB24 не больше `THUMB_MAX_SIDE` по длинной стороне (поворот и SAR учтены)."""
    width, height, rotation = _VideoGeometry.compute(
        frame, info, max_height=None, max_side=THUMB_MAX_SIDE,
    )
    pipe = _VideoPipe(info, (width, height), rotation, "rgb24")
    frames = pipe.push(frame) + pipe.flush()
    if not frames:
        raise TaskError("damaged")
    image = frames[0].to_image()
    if image.mode != "RGB":
        image = image.convert("RGB")
    return {"width": int(image.width), "height": int(image.height), "data": image.tobytes()}


# --- Потоки результата --------------------------------------------------------


class _TsFixer:
    """Сшивка разрывов меток времени (форматы с AVFMT_TS_DISCONT: MPEG-TS, FLV).

    Как в ffmpeg CLI: для каждого потока помнится ожидаемый DTS следующего
    пакета (DTS + длительность); если пришедший DTS меньше ожидаемого больше
    чем на `TS_DISCONT_BACK` с или больше него на `TS_DISCONT_JUMP` с —
    это разрыв (например, склеенные клипы MTS начинают метки заново), и
    смещение меняется так, чтобы пакет встал на ожидаемое место.

    Смещение общее для файла (звук и видео сдвигаются одинаково, иначе
    рассинхрон), но каждая дорожка переходит на новое смещение, только
    когда сама увидит разрыв: демультиплексор MPEG-TS отдаёт последний
    пакет дорожки первого клипа уже ПОСЛЕ первых пакетов второго (PES
    завершается началом следующего), и этот хвост сдвигать нельзя. Если
    смещение, найденное другой дорожкой, ставит пакет на ожидаемое место —
    берётся оно; нет — считается новое. Для прочих форматов ничего не делает.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.offset = 0.0  # с, последнее найденное смещение файла
        self.stream_offset: dict[int, float] = {}
        self.expected: dict[int, float] = {}

    @staticmethod
    def _is_break(delta: float) -> bool:
        return delta < -TS_DISCONT_BACK or delta > TS_DISCONT_JUMP

    def fix(self, packet) -> None:
        if not self.enabled:
            return
        tb = packet.time_base
        if not tb:
            return
        index = packet.stream.index
        offset = self.stream_offset.setdefault(index, self.offset)
        if packet.dts is not None:
            raw = float(packet.dts * tb)
            expected = self.expected.get(index)
            if expected is not None and math.isfinite(raw) and self._is_break(raw + offset - expected):
                if offset != self.offset and not self._is_break(raw + self.offset - expected):
                    offset = self.offset  # разрыв уже найден другой дорожкой
                else:
                    offset -= raw + offset - expected
                    self.offset = offset
                self.stream_offset[index] = offset
            duration = float(packet.duration * tb) if packet.duration else 0.0
            if math.isfinite(raw):
                self.expected[index] = raw + offset + duration
        shift = round(offset / tb)
        if shift:
            if packet.pts is not None:
                packet.pts += shift
            if packet.dts is not None:
                packet.dts += shift


class _Output:
    """Выходной контейнер: считает записанные байты (предел размера, место на диске)."""

    def __init__(self, job: "_MediaJob", container) -> None:
        self._job = job
        self._container = container

    def mux(self, packet) -> None:
        self._job._account(packet.size)
        self._container.mux(packet)

    def __getattr__(self, name: str):
        return getattr(self._container, name)


def _check_dropped(dropped: int, total: int) -> None:
    """Отброшено больше `MAX_DROPPED_SHARE` пакетов/кадров (метки назад) — ошибка."""
    if dropped and dropped > MAX_DROPPED_SHARE * (total + dropped):
        raise TaskError("bad_timestamps")


def _nal_type(name: str, nal: bytes) -> int:
    """Тип NAL по заголовку (H.264 — 5 бит, HEVC — 6 бит после запрещённого бита)."""
    return nal[0] & 0x1F if name == "h264" else (nal[0] >> 1) & 0x3F


def _nal_ok(name: str, nal: bytes) -> bool:
    """NAL не короче заголовка и запрещённый бит равен 0."""
    return len(nal) >= (1 if name == "h264" else 2) and not nal[0] & 0x80


def _strip_sei_annexb(name: str, data: bytes) -> bytes | None:
    """Annex B (стартовые коды 00 00 01 / 00 00 00 01): убрать NAL SEI."""
    removed = _SEI_TYPES[name]
    # Позиции стартовых кодов 00 00 01 (у четырёхбайтного ведущий 0 — хвост
    # предыдущего NAL, он отрезается вместе с trailing_zero_8bits).
    starts = []
    pos = data.find(b"\x00\x00\x01")
    if pos not in (0, 1) or data[:pos] != b"\x00" * pos:
        return None  # перед первым стартовым кодом — посторонние байты
    while pos != -1:
        starts.append(pos)
        pos = data.find(b"\x00\x00\x01", pos + 3)
    kept = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(data)
        nal = data[start + 3:end].rstrip(b"\x00")
        if not nal:
            continue
        if not _nal_ok(name, nal):
            return None
        if _nal_type(name, nal) not in removed:
            kept.append(nal)
    if not kept:
        return None
    return b"".join(b"\x00\x00\x00\x01" + nal for nal in kept)


def _read_units(name: str, data: bytes, pos: int, count: int) -> tuple[list[bytes], int] | None:
    """`count` NAL с 16-битной длиной перед каждым (как в avcC/hvcC)."""
    units = []
    for _ in range(count):
        if pos + 2 > len(data):
            return None
        size = int.from_bytes(data[pos:pos + 2], "big")
        nal = data[pos + 2:pos + 2 + size]
        if len(nal) != size or not _nal_ok(name, nal):
            return None
        units.append(nal)
        pos += 2 + size
    return units, pos


def _unit_list(units: list[bytes]) -> bytes:
    return b"".join(len(nal).to_bytes(2, "big") + nal for nal in units)


def _strip_sei_hvcc(data: bytes) -> bytes | None:
    """hvcC (ISO/IEC 14496-15, 8.3.3): заголовок 22 байта, numOfArrays,
    массивы (тип NAL, numNalus, NAL с 16-битной длиной); массивы SEI убираются."""
    removed = _SEI_TYPES["hevc"]
    if len(data) < 23:
        return None
    out = bytearray()
    arrays = 0
    pos = 23
    for _ in range(data[22]):
        if pos + 3 > len(data):
            return None
        head = data[pos]
        parsed = _read_units("hevc", data, pos + 3, int.from_bytes(data[pos + 1:pos + 3], "big"))
        if parsed is None:
            return None
        units, pos = parsed
        if head & 0x3F in removed:
            continue
        # Тип в заголовке массива и у самих NAL может расходиться — проверяем оба.
        units = [nal for nal in units if _nal_type("hevc", nal) not in removed]
        if units:
            out += bytes([head]) + len(units).to_bytes(2, "big") + _unit_list(units)
            arrays += 1
    if pos != len(data):
        return None  # посторонний хвост
    return data[:22] + bytes([arrays]) + bytes(out)


def _strip_sei_avcc(data: bytes) -> bytes | None:
    """avcC (ISO/IEC 14496-15, 5.3.3): SPS и PPS (+ расширения SPS у High);
    NAL другого типа (в т. ч. SEI) убираются, неожиданная структура — None."""
    if len(data) < 7:
        return None
    parsed = _read_units("h264", data, 6, data[5] & 0x1F)
    if parsed is None:
        return None
    sps, pos = parsed
    if pos >= len(data):
        return None
    parsed = _read_units("h264", data, pos + 1, data[pos])
    if parsed is None:
        return None
    pps, pos = parsed
    tail = b""
    if pos != len(data):
        # Расширение для High-профилей: 3 байта, число SPS Ext, сами SPS Ext.
        if pos + 4 > len(data):
            return None
        parsed = _read_units("h264", data, pos + 4, data[pos + 3])
        if parsed is None or parsed[1] != len(data):
            return None
        ext = [nal for nal in parsed[0] if _nal_type("h264", nal) == 13]
        tail = data[pos:pos + 3] + bytes([len(ext)]) + _unit_list(ext)
    sps = [nal for nal in sps if _nal_type("h264", nal) == 7]
    pps = [nal for nal in pps if _nal_type("h264", nal) == 8]
    if not sps or not pps:
        return None
    return (data[:5] + bytes([(data[5] & 0xE0) | len(sps)]) + _unit_list(sps)
            + bytes([len(pps)]) + _unit_list(pps) + tail)


def _strip_sei_extradata(name: str, data: bytes) -> bytes | None:
    """extradata H.264/HEVC без NAL SEI (`_SEI_TYPES`).

    Понимает оба представления: avcC/hvcC (из MP4/MKV) и Annex B (из MTS и
    после bitstream-фильтра). Не удалось разобрать — None (поток тогда не
    копируется).
    """
    if data.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")):
        return _strip_sei_annexb(name, data)
    if not data or data[0] != 1:
        return None
    return _strip_sei_avcc(data) if name == "h264" else _strip_sei_hvcc(data)


class _CopyStream:
    """Копирование пакетов без перекодирования (remux).

    Из H.264/HEVC при этом удаляются SEI: из пакетов — bitstream-фильтром
    (`_COPY_BSF`), из extradata — `_strip_sei_extradata` (фильтр её не
    чистит, только переводит в Annex B, а муксер MKV/MP4 собрал бы из неё
    hvcC вместе с SEI). Решение «копировать» принимает `_can_copy`, уже
    проверив, что исходную extradata удаётся разобрать. Пакет, который
    фильтр не смог разобрать, отбрасывается целиком (не идёт в результат
    нефильтрованным — в нём может быть SEI); таких больше
    `MAX_DROPPED_SHARE` — «Повреждены данные видео».
    """

    def __init__(self, out, stream, t0: float) -> None:
        self.out = out
        # Копируются только параметры кодека (не теги потока).
        self.ostream = out.add_stream_from_template(stream)
        self.time_base = stream.time_base
        self.offset = round(t0 / stream.time_base) if stream.time_base else 0
        self.last_dts: int | None = None
        self.produced = 0
        self.dropped = 0
        self.broken = 0  # пакеты, которые фильтр не смог разобрать
        self.bsf = None
        name = stream.codec_context.name
        description = _COPY_BSF.get(name)
        if description is not None:
            from av.bitstream import BitStreamFilterContext
            from av.error import InvalidDataError

            self._invalid_data = InvalidDataError
            self.bsf = BitStreamFilterContext(description, stream, self.ostream)
            # Фильтр копирует параметры входа целиком, вместе с тегом кодека
            # исходного контейнера (у MPEG-TS — 0x1B): муксер MP4 такой
            # тег отвергает. Сбрасываем — муксер выберет свой.
            self.ostream.codec_context.codec_tag = "\x00\x00\x00\x00"
            # extradata после фильтра (Annex B) — без SEI.
            extradata = self.ostream.codec_context.extradata
            if extradata:
                cleaned = _strip_sei_extradata(name, bytes(extradata))
                if cleaned is None:
                    # Исходную `_can_copy` разобрал, значит, сюда не попадаем;
                    # на всякий случай — ошибка, а не результат с SEI.
                    raise TaskError("damaged")
                self.ostream.codec_context.extradata = cleaned

    def packet(self, packet) -> None:
        if packet.size == 0:
            return
        if packet.pts is not None:
            packet.pts -= self.offset
        if packet.dts is not None:
            packet.dts -= self.offset
            if self.last_dts is not None and packet.dts <= self.last_dts:
                self.dropped += 1
                return  # немонотонная метка — пакет отбрасывается
            self.last_dts = packet.dts
        elif packet.pts is None:
            return
        if self.bsf is None:
            self._mux(packet)
            return
        try:
            filtered_packets = self.bsf.filter(packet)
        except self._invalid_data:
            self.broken += 1
            return  # неразбираемый пакет отбрасывается (в нём может быть SEI)
        for filtered in filtered_packets:
            self._mux(filtered)

    def _mux(self, packet) -> None:
        packet.stream = self.ostream
        packet.time_base = self.time_base
        self.out.mux(packet)
        self.produced += 1

    def flush(self) -> None:
        if self.bsf is not None:
            for filtered in self.bsf.filter(None):
                self._mux(filtered)
        if self.broken and self.broken > MAX_DROPPED_SHARE * (self.produced + self.broken):
            raise TaskError("bad_video")
        _check_dropped(self.dropped, self.produced)


class _FrameWriter:
    """Кодирование кадров с пересчётом меток в базу энкодера (монотонно).

    Кадр с той же меткой, что у предыдущего, отбрасывается молча (так
    прореживается VFR при грубой базе времени, например у MPEG-4 в AVI);
    кадры с меткой НАЗАД считаются: их больше `MAX_DROPPED_SHARE` — ошибка
    «Повреждены метки времени» вместо молча обрезанного результата.
    """

    def __init__(self, out, stream, time_base: Fraction, t0: float, *, rebase: bool = False) -> None:
        self.out = out
        self.stream = stream
        self.time_base = time_base
        self.t0 = t0
        # rebase: первый записанный кадр получает метку 0 (GIF из фрагмента).
        self.rebase = rebase
        self.last_pts: int | None = None
        self.produced = 0
        self.written = 0
        self.dropped = 0

    def write(self, frame) -> None:
        if frame.pts is not None and frame.time_base:
            if self.rebase and self.last_pts is None:
                self.t0 = float(frame.pts * frame.time_base)
            seconds = float(frame.pts * frame.time_base) - self.t0
            pts = round(seconds / self.time_base)
        else:
            pts = (self.last_pts + 1) if self.last_pts is not None else 0
        if self.last_pts is not None and pts <= self.last_pts:
            if pts < self.last_pts:
                self.dropped += 1
            return  # совпала или пошла назад — кадр отбрасывается
        if pts < 0 and self.last_pts is None:
            pts = 0
        self.last_pts = pts
        frame.pts = pts
        frame.time_base = self.time_base
        self.written += 1
        for packet in self.stream.encode(frame):
            self.out.mux(packet)
            self.produced += 1

    def flush(self) -> None:
        for packet in self.stream.encode(None):
            self.out.mux(packet)
            self.produced += 1
        _check_dropped(self.dropped, self.written)


def _pull_all(graph) -> list:
    import av

    frames = []
    while True:
        try:
            frames.append(graph.vpull())
        except (av.error.BlockingIOError, BlockingIOError):
            break
        except (av.error.EOFError, EOFError):
            break
    return frames


# Сколько пакетов видео смотреть в поисках первого кадра.
_PROBE_PACKETS = 2000


class _FrameInfo:
    """Сведения о первом кадре (без пикселей)."""

    def __init__(self, width: int, height: int, rotation: float) -> None:
        self.width = width
        self.height = height
        self.rotation = rotation

    @classmethod
    def of(cls, frame) -> "_FrameInfo":
        try:
            rotation = float(frame.rotation or 0)
        except Exception:  # noqa: BLE001
            rotation = 0.0
        return cls(int(frame.width), int(frame.height), rotation)


class _VideoGeometry:
    """Размер результата по первому кадру: поворот, SAR, «Маленький», GIF."""

    @staticmethod
    def compute(frame, stream, *, max_height: int | None, max_side: int | None) -> tuple[int, int, list]:
        width, height = int(frame.width), int(frame.height)
        sar = stream.sample_aspect_ratio
        try:
            if sar and float(sar) > 0 and abs(float(sar) - 1.0) > 1e-3:
                width = int(round(width * float(sar)))
        except (TypeError, ZeroDivisionError, ValueError):
            pass
        rotation = 0.0
        try:
            rotation = float(frame.rotation or 0)
        except Exception:  # noqa: BLE001
            rotation = 0.0
        filters = _rotation_filters(rotation)
        if any(name == "transpose" for name, _ in filters):
            width, height = height, width
        scale = 1.0
        if max_height is not None and height > max_height:
            scale = min(scale, max_height / height)
        if max_side is not None and max(width, height) > max_side:
            scale = min(scale, max_side / max(width, height))
        out_w = _even(width * scale)
        out_h = _even(height * scale)
        return out_w, out_h, filters


class _VideoPipe:
    """Граф фильтров для видео: поворот → масштаб → формат пикселей.

    Если посреди потока поменялись размеры или формат кадра, граф
    пересобирается (размер результата не меняется).
    """

    def __init__(self, stream, out_size: tuple[int, int], rotation: list, pix_fmt: str,
                 extra_before: list | None = None, palette=None,
                 dither: str = "sierra2_4a") -> None:
        self.stream = stream
        self.dither = dither
        self.out_size = out_size
        self.rotation = rotation
        self.pix_fmt = pix_fmt
        self.extra_before = extra_before or []
        self.palette = palette
        self.key = None
        self.graph = None

    def _build(self, frame) -> None:
        import av

        graph = av.filter.Graph()
        src = graph.add_buffer(
            width=frame.width, height=frame.height, format=frame.format,
            time_base=frame.time_base or self.stream.time_base,
        )
        nodes = [src]
        for name, arg in self.extra_before + self.rotation:
            nodes.append(graph.add(name, arg) if arg is not None else graph.add(name))
        w, h = self.out_size
        nodes.append(graph.add("scale", f"{w}:{h}:flags=bicubic"))
        nodes.append(graph.add("setsar", "1"))
        if self.palette is None:
            nodes.append(graph.add("format", self.pix_fmt))
        for left, right in zip(nodes, nodes[1:]):
            left.link_to(right)
        last = nodes[-1]
        if self.palette is not None:
            pal_src = graph.add_buffer(
                width=self.palette.width, height=self.palette.height,
                format=self.palette.format, time_base=Fraction(1, 100),
            )
            use = graph.add("paletteuse", f"dither={self.dither}")
            last.link_to(use, 0, 0)
            pal_src.link_to(use, 0, 1)
            last = use
            self.palette_src = pal_src
        sink = graph.add("buffersink")
        last.link_to(sink)
        graph.configure()
        if self.palette is not None:
            palette = self.palette
            palette.pts = 0
            self.palette_src.push(palette)
            self.palette_src.push(None)
        self.graph = graph
        self.src = src
        self.key = (frame.width, frame.height, frame.format.name)

    def push(self, frame) -> list:
        frames: list = []
        key = (frame.width, frame.height, frame.format.name)
        if self.graph is not None and key != self.key:
            frames.extend(self.flush())
        if self.graph is None:
            self._build(frame)
        self.src.push(frame)
        frames.extend(_pull_all(self.graph))
        return frames

    def flush(self) -> list:
        if self.graph is None:
            return []
        self.src.push(None)
        frames = _pull_all(self.graph)
        self.graph = None
        self.key = None
        return frames


class _VideoEncoder:
    """Перекодирование видео (H.264, VP9, MPEG-4)."""

    def __init__(self, job: _MediaJob, out, stream, t0: float, first: _FrameInfo) -> None:
        self.job = job
        self.out = out
        self.stream = stream
        self.t0 = t0
        self.codec = job.vcodec
        self.preset = job.preset
        self.ostream = None
        self.pipe: _VideoPipe | None = None
        self.writer: _FrameWriter | None = None
        self.produced = 0
        # Частота кадров для энкодера (оценка; метки берутся из кадров).
        rate = stream.average_rate or stream.guessed_rate or stream.base_rate
        try:
            fps = float(rate) if rate else 25.0
        except (TypeError, ZeroDivisionError):
            fps = 25.0
        if not math.isfinite(fps) or fps <= 0 or fps > 240:
            fps = 25.0
        self.fps = fps
        self._open(first)

    def _open(self, frame) -> None:
        max_height = SMALL_MAX_HEIGHT if self.preset == Preset.SMALL else None
        out_w, out_h, rotation = _VideoGeometry.compute(
            frame, self.stream, max_height=max_height, max_side=MAX_OUTPUT_SIDE,
        )
        # Память энкодера растёт с (потоки + кадры упреждения) × размер кадра:
        # для огромных кадров (> BIG_FRAME_PIXELS) берём меньше потоков и
        # короче упреждение, чтобы уложиться в лимит памяти процесса.
        big = out_w * out_h > BIG_FRAME_PIXELS
        threads = "2" if big else str(max(1, min(8, os.cpu_count() or 1)))
        if self.codec == "libx264":
            options = {
                "crf": str(_X264_CRF[self.preset]), "preset": _X264_PRESET,
                "threads": threads,
                "x264-params": "rc-lookahead=" + ("8" if big else "20"),
            }
            time_base = Fraction(1, 90000)
            rate = Fraction(self.fps).limit_denominator(1001)
        elif self.codec == "libvpx-vp9":
            options = {
                "crf": str(_VP9_CRF[self.preset]), "b": "0", "deadline": "good",
                "cpu-used": "5", "row-mt": "1", "threads": threads,
                "lag-in-frames": "4" if big else "16",
            }
            time_base = Fraction(1, 90000)
            rate = Fraction(self.fps).limit_denominator(1001)
        else:  # mpeg4 в AVI: база времени = 1/частота (ограничение MPEG-4, AVI).
            fps_int = max(1, min(120, round(self.fps)))
            options = {
                "flags": "+qscale", "global_quality": str(_MPEG4_Q[self.preset] * 118),
                "threads": threads,
            }
            time_base = Fraction(1, fps_int)
            rate = Fraction(fps_int)
        ostream = self.out.add_stream(self.codec, rate=rate, options=options)
        ostream.width, ostream.height = out_w, out_h
        ostream.pix_fmt = "yuv420p"
        ostream.codec_context.time_base = time_base
        if self.codec == "mpeg4":
            ostream.codec_context.codec_tag = "XVID"
        self.ostream = ostream
        self.pipe = _VideoPipe(self.stream, (out_w, out_h), rotation, "yuv420p")
        self.writer = _FrameWriter(self.out, ostream, time_base, self.t0)

    def packet(self, packet) -> None:
        for frame in self.job._decode(packet):
            self._frame(frame)

    def _frame(self, frame) -> None:
        self.job._check_frame_size(int(frame.width), int(frame.height))
        self.job._count_frame(int(frame.width), int(frame.height))
        for result in self.pipe.push(frame):
            self.writer.write(result)
            self.job._advance(self._time(result))
        self.produced = self.writer.produced

    def _time(self, frame) -> float | None:
        return float(self.writer.last_pts * self.writer.time_base) if self.writer.last_pts else None

    def flush(self) -> None:
        # Кадры, задержанные декодером.
        try:
            for frame in self.stream.codec_context.decode(None):
                self._frame(frame)
        except Exception:  # noqa: BLE001 — сбой при сбросе декодера не критичен
            log.debug("Сброс видеодекодера", exc_info=True)
        if self.writer is None:
            return
        for result in self.pipe.flush():
            self.writer.write(result)
        self.writer.flush()
        self.produced = self.writer.produced


def _audio_plan(codec_name: str, in_cc) -> tuple[int, int, str, str]:
    """Параметры звука результата: (частота, каналы, раскладка, формат отсчётов).

    Каналы: в форматы с потерями — не больше 2 (Vorbis — всегда 2), во
    FLAC/WAV — до 8. Раскладка задаётся СТРОКОЙ (PyAV 19 не принимает число
    каналов в `AudioLayout`): для WAV при сохранении числа каналов (> 2) —
    исходная (`layout.name`, если она упорядочена — не «6 channels»),
    иначе стандартная по числу каналов (`_STD_LAYOUTS`; FLAC принимает
    только их — порядок каналов FLAC фиксирован).
    """
    import av

    codec = av.Codec(codec_name, "w")
    in_rate = int(in_cc.sample_rate or 0) or 48000
    rates = codec.audio_rates
    if codec_name == "libopus":
        rate = in_rate if rates and in_rate in rates else 48000
    elif rates:
        rate = in_rate if in_rate in rates else (48000 if 48000 in rates else rates[0])
    else:
        rate = min(in_rate, 192000)
    in_channels = int(getattr(in_cc, "channels", 0) or 0) or 2
    lossless = codec_name in ("flac", "pcm_s16le")
    channels = min(in_channels, 8 if lossless else 2)
    if codec_name == "vorbis":
        channels = 2  # встроенный энкодер Vorbis умеет только стерео
    layout = _STD_LAYOUTS[max(1, channels)]
    if codec_name == "pcm_s16le" and channels > 2 and channels == in_channels:
        try:
            name = str(in_cc.layout.name)
            if "channels" not in name and av.AudioLayout(name).nb_channels == channels:
                layout = name
        except Exception:  # noqa: BLE001 — неизвестная раскладка: стандартная
            log.debug("Исходная раскладка звука не подходит", exc_info=True)
    sample_format = {
        "pcm_s16le": "s16", "flac": "s16", "libopus": "flt", "libmp3lame": "fltp",
        "aac": "fltp", "vorbis": "fltp",
    }[codec_name]
    return rate, channels, layout, sample_format


class _AudioEncoder:
    """Перекодирование звука: ресемплер → FIFO по размеру кадра энкодера → энкодер.

    Время результата считается по отсчётам. В видео (не `audio_only`) метки
    входных кадров сверяются с этим счётом: расхождение больше
    `AUDIO_SYNC_THRESHOLD` — дыра заполняется тишиной, перекрытие
    отбрасывается (как `aresample=async` в ffmpeg), иначе звук после дыры
    уехал бы от видео.
    """

    def __init__(self, job: _MediaJob, out, stream, t0: float, audio_only: bool) -> None:
        import av

        self.job = job
        self.out = out
        self.stream = stream
        self.t0 = t0
        self.audio_only = audio_only
        codec_name = job.acodec
        self.codec_name = codec_name
        rate, _channels, layout, sample_format = _audio_plan(codec_name, stream.codec_context)
        self.layout = layout
        self.sample_format = sample_format
        options: dict[str, str] = {}
        if codec_name == "vorbis":
            options["strict"] = "experimental"
        ostream = out.add_stream(codec_name, rate=rate, options=options)
        cc = ostream.codec_context
        cc.layout = layout
        cc.format = sample_format
        cc.time_base = Fraction(1, rate)
        table = _AUDIO_BITRATE if audio_only else _VIDEO_AUDIO_BITRATE
        if codec_name in table:
            cc.bit_rate = table[codec_name][job.preset]
        self.ostream = ostream
        self.rate = rate
        self.resampler = av.AudioResampler(format=sample_format, layout=layout, rate=rate)
        self.fifo = av.AudioFifo()
        self.next_pts: int | None = None
        self.frame_size = 0
        self.produced = 0
        self.small_last = codec_name in ("aac", "libmp3lame", "libopus", "flac", "pcm_s16le")
        # Синхронизация с метками (только в видео): позиция следующего
        # входного отсчёта на шкале результата (в отсчётах результата) и
        # сколько отсчётов результата ещё отбросить (перекрытие).
        self.sync = not audio_only
        self.threshold = AUDIO_SYNC_THRESHOLD * rate
        self.position = 0.0
        self.drop = 0

    def packet(self, packet) -> None:
        for frame in self.job._decode(packet):
            self._frame(frame)

    def _frame(self, frame) -> None:
        in_rate = int(frame.sample_rate or 0) or self.rate
        length = frame.samples * self.rate / in_rate  # в отсчётах результата
        stamp = None
        if frame.pts is not None and frame.time_base:
            value = (float(frame.pts * frame.time_base) - self.t0) * self.rate
            if math.isfinite(value):
                stamp = value
        if self.next_pts is None:
            start = 0
            if not self.audio_only and stamp is not None and stamp > 0:
                start = round(stamp)
            self.next_pts = start
            self.position = float(start)
            if self.sync and stamp is not None and stamp < 0:
                self.drop = round(-stamp)  # звук начался раньше общего начала
        elif self.sync and stamp is not None:
            diff = stamp - self.position
            if diff > self.threshold:
                self._silence(round(diff))  # дыра в звуке
                self.position = stamp
            elif diff < -self.threshold:
                self.drop += round(-diff)  # перекрытие: лишнее отбрасывается
                self.position = stamp
        self.position += length
        frame.pts = None
        for resampled in self.resampler.resample(frame):
            resampled.pts = None
            self._write(resampled)
        self._drain(final=False)

    def _write(self, frame) -> None:
        """В FIFO — с учётом отсчётов, которые надо отбросить (перекрытие)."""
        import av

        if self.drop > 0:
            if self.drop >= frame.samples:
                self.drop -= frame.samples
                return
            tail = av.AudioFifo()
            tail.write(frame)
            tail.read(self.drop)
            self.drop = 0
            frame = tail.read(tail.samples)
            if frame is None:
                return
        self.fifo.write(frame)

    def _silence(self, samples: int) -> None:
        """Тишина `samples` отсчётов (дыра в звуке) — кусками по секунде."""
        end = (self.position + samples) / self.rate
        if end > self.job.max_duration:
            raise TaskError("too_long", end / 3600, self.job.max_duration / 3600)
        while samples > 0:
            chunk = min(samples, self.rate)
            self.fifo.write(self._silence_frame(chunk))
            samples -= chunk
            self._drain(final=False)

    def _encode(self, frame) -> None:
        for packet in self.ostream.encode(frame):
            self.out.mux(packet)
            self.produced += 1

    def _drain(self, final: bool) -> None:
        if not self.frame_size:
            self.frame_size = int(self.ostream.codec_context.frame_size or 0) or 1024
        while self.fifo.samples >= self.frame_size:
            self._emit(self.fifo.read(self.frame_size))
        if final and self.fifo.samples:
            rest = self.fifo.samples
            if not self.small_last:
                self._pad(self.frame_size - rest)
                self._emit(self.fifo.read(self.frame_size))
            else:
                self._emit(self.fifo.read(rest))

    def _pad(self, samples: int) -> None:
        """Дописывает в FIFO тишину (для энкодеров без «короткого последнего кадра»)."""
        self.fifo.write(self._silence_frame(samples))

    def _silence_frame(self, samples: int):
        import av

        silence = av.AudioFrame(format=self.sample_format, layout=self.layout, samples=samples)
        for plane in silence.planes:
            plane.update(bytes(plane.buffer_size))
        silence.sample_rate = self.rate
        return silence

    def _emit(self, frame) -> None:
        frame.pts = self.next_pts
        frame.time_base = Fraction(1, self.rate)
        frame.sample_rate = self.rate
        self.next_pts += frame.samples
        self._encode(frame)
        self.job._advance(self.next_pts / self.rate)

    def flush(self) -> None:
        try:
            for frame in self.stream.codec_context.decode(None):
                self._frame(frame)
        except Exception:  # noqa: BLE001
            log.debug("Сброс аудиодекодера", exc_info=True)
        if self.next_pts is None:
            return
        for resampled in self.resampler.resample(None):
            resampled.pts = None
            self._write(resampled)
        self._drain(final=True)
        self._encode(None)


class _GifPipe:
    """Видео → кадры GIF: частота → поворот → масштаб → палитра."""

    def __init__(self, job: _MediaJob, stream, max_side: int, fps: int, colors: int, *,
                 palette, out_size: tuple[int, int] | None = None) -> None:
        self.job = job
        self.stream = stream
        self.stream_index = stream.index
        self.max_side = max_side
        self.fps = fps
        self.colors = colors
        self.palette = palette
        self.out_size = out_size
        self.pipe: _VideoPipe | None = None
        self.palette_graph = None
        # Сколько кадров вышло из фильтра fps и последний входной кадр: у видео
        # из одного кадра fps ничего не выдаёт (нет следующей метки) — тогда
        # этот кадр идёт в GIF без fps.
        self.produced = 0
        self.last_input = None

    def _ensure(self, frame) -> None:
        if self.pipe is not None:
            return
        self.stream_info = _StreamInfo(self.stream)
        out_w, out_h, rotation = _VideoGeometry.compute(
            frame, self.stream, max_height=None, max_side=self.max_side,
        )
        if self.out_size is None:
            self.out_size = (out_w, out_h)
        self.pipe = _VideoPipe(
            self.stream, self.out_size, rotation, "rgb24",
            extra_before=[("fps", str(self.fps))], palette=self.palette,
        )

    def push(self, frame) -> list:
        self.job._check_frame_size(int(frame.width), int(frame.height))
        self.job._count_frame(int(frame.width), int(frame.height))
        self._ensure(frame)
        self.last_input = frame
        frames = self.pipe.push(frame)
        self.produced += len(frames)
        if self.palette is None:
            self._collect(frames)
            return []
        return frames

    def flush(self) -> list:
        if self.pipe is None:
            return []
        frames = self.pipe.flush()
        self.produced += len(frames)
        if not self.produced and self.last_input is not None:
            single = _VideoPipe(self.stream_info, self.out_size, self.pipe.rotation, "rgb24",
                                palette=self.palette)
            frames = single.push(self.last_input) + single.flush()
            self.produced += len(frames)
        return frames

    def _collect(self, frames: list) -> None:
        """Проход 1: кадры (уже уменьшенные) — в palettegen."""
        import av

        for frame in frames:
            if self.palette_graph is None:
                graph = av.filter.Graph()
                src = graph.add_buffer(width=frame.width, height=frame.height,
                                       format=frame.format, time_base=frame.time_base)
                gen = graph.add("palettegen", f"max_colors={self.colors}:stats_mode=full")
                sink = graph.add("buffersink")
                src.link_to(gen)
                gen.link_to(sink)
                graph.configure()
                self.palette_graph = graph
                self.palette_src = src
            self.palette_src.push(frame)
            _pull_all(self.palette_graph)

    def finish_palette(self):
        self._collect(self.flush())
        if self.palette_graph is None:
            raise TaskError("damaged")
        self.palette_src.push(None)
        frames = _pull_all(self.palette_graph)
        if not frames:
            raise TaskError("damaged")
        return frames[-1]
