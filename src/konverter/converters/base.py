"""Общий контракт между ядром, конвертерами и интерфейсом.

Здесь только типы и правила, без логики конвертации. Менять этот файл —
значит менять договорённость между частями программы, поэтому делать это
осознанно и сразу править всех потребителей.

Правила для любого конвертера:
- конвертер пишет результат ТОЛЬКО в переданный путь `dst` (временный файл
  или временную папку, которые подготовило ядро); исходник не меняет;
  переименование в конечное имя, конфликт имён и уборку делает ядро;
- периодически вызывает `cancel.raise_if_cancelled()` (между кадрами,
  страницами, файлами) и `progress(доля от 0.0 до 1.0)`;
- ошибки, понятные пользователю, сообщает исключением `ConversionError`
  с текстом на русском; прочие исключения ядро тоже перехватит и покажет
  как «внутреннюю ошибку», но конвертер должен стараться их не допускать;
- тип входного файла НЕ определяет заново (ни по расширению, ни по
  содержимому): ядро передаёт в `convert` коды форматов (`formats`),
  определённые `core.detect`, и декодер выбирается строго по ним;
- никакой сети, никаких внешних программ (кроме модуля LibreOffice на этапе 5
  и дочерних процессов самой программы — для изоляции опасных декодеров;
  скачивание модуля LibreOffice — отдельный пакет `konverter.libreoffice`).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence


class Category(str, Enum):
    """Категория формата. По ней файлы делятся на группы (Ф5)."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    PDF = "pdf"
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    BOOK = "book"
    ARCHIVE = "archive"


class Preset(str, Enum):
    """Пресет качества (Ф7)."""

    HIGH = "high"
    MEDIUM = "medium"
    SMALL = "small"


@dataclass(frozen=True)
class DetectedFormat:
    """Результат определения формата файла по содержимому (Ф3).

    `fmt` — короткий код формата в нижнем регистре: "jpg", "png", "heic",
    "raw", "svg", "mp4", "pdf" и т.п. (для RAW-камер — "raw", конкретная
    марка в `label`). `extension_mismatch` — расширение файла не соответствует
    содержимому.
    """

    fmt: str
    category: Category
    label: str
    extension_mismatch: bool = False


@dataclass(frozen=True)
class Target:
    """Вариант конвертации, который видит пользователь (Ф6).

    `id` — уникальный код варианта внутри конвертера ("png", "pdf_single",
    "pdf_each", "mp3_audio" …). `extension` — расширение результата без точки
    (пусто, если результат — папка, например «распаковать» или «страницы PDF»).
    `uses_preset` — влияет ли пресет качества на этот вариант.
    `many_to_one` — вариант собирает ВСЕ файлы группы в один результат
    (например, «картинки → один PDF», «объединить PDF»).
    `is_long` — задача может быть долгой: показывать прогресс и «Отмена» (Ф9).
    """

    id: str
    label: str
    extension: str
    uses_preset: bool = True
    many_to_one: bool = False
    is_long: bool = False


class Cancelled(Exception):
    """Задача отменена пользователем."""


class ConversionError(Exception):
    """Ошибка конвертации с понятным пользователю текстом на русском."""


@dataclass
class CancelToken:
    """Флаг отмены задачи; безопасен для использования из разных потоков."""

    _event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled()


# Колбэк прогресса: доля выполненного от 0.0 до 1.0.
ProgressCallback = Callable[[float], None]


class Converter(Protocol):
    """Конвертер одной категории форматов."""

    category: Category

    # Необязательно. Канонический порядок вариантов в списке интерфейса:
    # последовательность id вариантов (`Target.id`). `core.grouping.common_targets`
    # сортирует по нему варианты группы (неизвестные — следом, в прежнем
    # порядке), так что порядок не зависит от порядка файлов в группе.
    # None (или атрибута нет вовсе) — порядок по первому появлению в
    # `targets(fmt)`. Ядро читает его через `getattr(..., None)`, поэтому
    # конвертеру объявлять его не обязательно.
    target_order: Sequence[str] | None = None

    # Необязательно. Очередь тяжёлых задач (`core.jobs`): долгие варианты
    # (`Target.is_long`) этого конвертера выполняются отдельной группой
    # потоков с этим именем ("media" — видео и аудио, "pdf" — PDF), по одной
    # задаче одновременно на очередь; очереди разных имён друг друга не ждут.
    # None (или атрибута нет) — общий пул лёгких задач. Ядро читает его
    # через `getattr(..., None)`.
    queue: str | None = None

    # Необязательные методы (ядро и интерфейс читают их через getattr):
    # * `queue_for(target_id, formats) -> str | None` — очередь задачи с
    #   этим вариантом и форматами исходников (если очередь зависит от
    #   формата: например, DOCX → PDF через LibreOffice идёт в "office",
    #   а TXT → PDF — в "docs"); без метода — атрибут `queue`;
    # * `module_for(target_id, fmt) -> str | None` — имя скачиваемого модуля,
    #   нужного для варианта и формата ("libreoffice" — этап 5), или None.
    #   По нему интерфейс перед постановкой задач спрашивает разрешение на
    #   скачивание модуля (Ф15).

    def targets(self, fmt: str) -> list[Target]:
        """Допустимые варианты для входного формата `fmt` (пустой список — нельзя)."""
        ...

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
        """Выполняет конвертацию.

        Для обычного варианта `sources` содержит один файл, для
        `many_to_one` — все файлы группы в порядке, выбранном пользователем.
        `formats` — коды форматов (`DetectedFormat.fmt`) той же длины, что
        `sources`: `formats[i]` — формат файла `sources[i]`, определённый
        `core.detect` по содержимому. Конвертер выбирает декодер строго по
        этому коду; несоответствие длины или неизвестный код —
        `ConversionError`.
        `dst` — путь к временному файлу результата (или временной папке,
        если `target.extension` пустое; папка уже создана ядром).
        """
        ...


# Реестр конвертеров по категориям; заполняется в converters/__init__.py.
REGISTRY: dict[Category, Converter] = {}


def register(converter: Converter) -> None:
    """Регистрирует конвертер категории (повторная регистрация — замена)."""
    REGISTRY[converter.category] = converter
