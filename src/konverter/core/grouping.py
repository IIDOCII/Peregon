"""Разбор брошенных файлов на группы по категориям (Ф2, Ф4, Ф5).

- Папки раскрываются на файлы ВЕРХНЕГО уровня (без рекурсии); скрытые и
  системные файлы пропускаются; ссылки (symlink, junction) внутри папок
  пропускаются целиком — они не разыменовываются.
- Если брошена сама ссылка: ссылка на файл обрабатывается как файл, ссылка
  на папку не раскрывается (попадает в список неподдерживаемых).
- Повторы путей убираются.
- Число файлов за один бросок ограничено (`MAX_FILES`); при превышении берутся
  первые `MAX_FILES` и заполняется `GroupingResult.message`.
- Порядок групп — по первому появлению категории; порядок файлов внутри
  группы — порядок броска (внутри папки — по имени без учёта регистра).

Функция `build_groups` читает начало каждого файла и может занять заметное
время на тысячах файлов — интерфейсу лучше вызывать её в фоновом потоке.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping

from konverter.converters.base import REGISTRY, Category, Converter, DetectedFormat, Target
from konverter.core.detect import UnreadableFileError, detect
from konverter.strings import tr

log = logging.getLogger(__name__)

# Предельное число файлов за один бросок.
MAX_FILES = 5000

# Атрибуты файлов Windows.
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4


@dataclass
class Group:
    """Группа файлов одной категории."""

    category: Category
    files: list[tuple[Path, DetectedFormat]] = field(default_factory=list)

    @property
    def formats(self) -> list[str]:
        """Различные коды форматов группы в порядке появления."""
        seen: dict[str, None] = {}
        for _, fmt in self.files:
            seen.setdefault(fmt.fmt, None)
        return list(seen)

    @property
    def paths(self) -> list[Path]:
        return [path for path, _ in self.files]


@dataclass
class GroupingResult:
    """Результат разбора: группы, неподдерживаемые файлы и предупреждение."""

    groups: list[Group] = field(default_factory=list)
    unsupported: list[tuple[Path, str]] = field(default_factory=list)
    limit_exceeded: bool = False
    message: str | None = None  # текст для пользователя (например, о лимите)


def _is_hidden_or_system(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM))


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return False


def _list_folder(folder: Path) -> tuple[list[Path], str | None]:
    """Файлы верхнего уровня папки (отсортированы по имени) или текст ошибки."""
    files: list[tuple[str, Path]] = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if entry.is_symlink() or entry.is_junction():
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if _is_hidden_or_system(entry):
                    continue
                files.append((entry.name.casefold(), Path(entry.path)))
    except OSError as exc:
        return [], tr("err_folder_read").format(error=exc.strerror or exc)
    files.sort(key=lambda item: item[0])
    return [path for _, path in files], None


def _dedup_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def build_groups(
    paths: list[Path],
    *,
    max_files: int = MAX_FILES,
    detect_func: Callable[[Path], DetectedFormat | None] = detect,
) -> GroupingResult:
    """Раскрывает папки, определяет форматы и делит файлы на группы."""
    result = GroupingResult()
    seen: set[str] = set()
    candidates: list[Path] = []

    def add(path: Path) -> bool:
        """Добавляет файл-кандидат; False — лимит исчерпан."""
        key = _dedup_key(path)
        if key in seen:
            return True
        if len(candidates) >= max_files:
            result.limit_exceeded = True
            return False
        seen.add(key)
        candidates.append(path)
        return True

    for raw in paths:
        path = Path(raw)
        try:
            st = os.stat(path)
        except OSError:
            st = None
        if st is not None and stat.S_ISDIR(st.st_mode):
            if _is_link(path):
                if _dedup_key(path) not in seen:
                    seen.add(_dedup_key(path))
                    result.unsupported.append((path, tr("err_symlink_folder")))
                continue
            files, error = _list_folder(path)
            if error is not None:
                result.unsupported.append((path, error))
                continue
            if not all(add(file) for file in files):
                break
        else:
            # Обычный файл или ошибка доступа — разберётся detect.
            if not add(path):
                break

    if result.limit_exceeded:
        result.message = tr("too_many_files").format(limit=max_files)

    by_category: dict[Category, Group] = {}
    for path in candidates:
        try:
            fmt = detect_func(path)
        except UnreadableFileError as exc:
            result.unsupported.append((path, exc.reason))
            continue
        except Exception as exc:  # noqa: BLE001 — один файл не должен ломать весь бросок
            log.exception("Сбой при определении формата %s", path)
            result.unsupported.append((path, tr("err_internal").format(error=exc)))
            continue
        if fmt is None:
            reason = tr("unsupported_format")
            try:
                if path.stat().st_size == 0:
                    reason = tr("unsupported_empty")
            except OSError:
                pass
            result.unsupported.append((path, reason))
            continue
        group = by_category.get(fmt.category)
        if group is None:
            group = Group(fmt.category)
            by_category[fmt.category] = group
            result.groups.append(group)
        group.files.append((path, fmt))
    return result


def common_targets(
    group: Group, registry: Mapping[Category, Converter] | None = None
) -> list[Target]:
    """Варианты конвертации, допустимые для группы.

    Вариант допустим, если для КАЖДОГО формата группы он либо есть в
    `converter.targets(fmt)`, либо файл уже в этом формате (`target.id ==
    fmt`; такие файлы потом пропускаются, см. `split_for_target`), и хотя
    бы один файл реально нужно конвертировать. Поэтому смешанная группа
    JPG+PNG видит и «JPG», и «PNG», а однородная группа JPG — не видит «JPG».
    Вариант «много → один» должен быть в `targets(fmt)` у всех форматов
    (его id не совпадает с кодом формата).

    Порядок — канонический, если конвертер его задаёт атрибутом
    `target_order` (последовательность id вариантов): варианты сортируются
    по нему, а отсутствующие в нём идут следом в прежнем порядке. Так у
    смешанной группы JPG+PNG «JPG» стоит на своём месте, а не в конце, и
    порядок не зависит от порядка файлов. Без `target_order` — по первому
    появлению в списках форматов группы (в порядке форматов).
    Объект варианта берётся из первого списка, где вариант встретился,
    `is_long` — истина, если долгим его считает хотя бы один формат.
    Нет конвертера категории или он упал — пустой список.

    Необязательный атрибут конвертера `min_files` (словарь id варианта →
    наименьшее число файлов в группе) скрывает варианты, бессмысленные для
    маленькой группы: например, «Объединить в один PDF» для одного файла.
    Читается через `getattr(..., None)`, объявлять его не обязательно.
    """
    if registry is None:
        registry = REGISTRY
    converter = registry.get(group.category)
    if converter is None or not group.files:
        return []
    formats = group.formats
    try:
        lists = {fmt: converter.targets(fmt) for fmt in formats}
    except Exception:  # noqa: BLE001
        log.exception("Конвертер %s упал в targets()", group.category)
        return []
    ids_by_format = {fmt: {t.id for t in targets} for fmt, targets in lists.items()}

    candidates: dict[str, Target] = {}
    for fmt in formats:
        for target in lists[fmt]:
            candidates.setdefault(target.id, target)

    result: list[Target] = []
    for target_id, target in candidates.items():
        if not all(
            target_id in ids_by_format[fmt] or (target_id == fmt and not target.many_to_one)
            for fmt in formats
        ):
            continue
        # Хотя бы один файл реально конвертируется: вариант есть в его
        # targets(fmt) (в том числе «свой» формат, если конвертер его явно
        # предлагает, например «MP4 → MP4, сжать»). Файлы формата `target_id`,
        # для которых вариант НЕ предложен, будут пропущены.
        if not any(target_id in ids_by_format[fmt] for fmt in formats):
            continue  # все файлы уже в этом формате
        is_long = any(
            t.is_long for fmt in formats for t in lists[fmt] if t.id == target_id
        )
        if is_long != target.is_long:
            target = replace(target, is_long=is_long)
        result.append(target)

    min_files = getattr(converter, "min_files", None) or {}
    if min_files:
        count = len(group.files)
        result = [t for t in result if count >= int(min_files.get(t.id, 0))]

    order = getattr(converter, "target_order", None)
    if order:
        rank = {target_id: index for index, target_id in enumerate(order)}
        # sorted устойчив: неизвестные варианты сохраняют взаимный порядок.
        result.sort(key=lambda t: rank.get(t.id, len(rank)))
    return result


@dataclass
class TargetSplit:
    """Деление группы для выбранного варианта.

    `convert` — файлы, которые нужно конвертировать (с форматами из detect),
    `skipped` — файлы, уже находящиеся в целевом формате (их пропускают и
    сообщают пользователю).
    """

    convert: list[tuple[Path, DetectedFormat]] = field(default_factory=list)
    skipped: list[tuple[Path, DetectedFormat]] = field(default_factory=list)

    @property
    def convert_paths(self) -> list[Path]:
        return [path for path, _ in self.convert]

    @property
    def convert_formats(self) -> list[str]:
        """Коды форматов для `JobManager.submit(formats=...)`."""
        return [fmt.fmt for _, fmt in self.convert]


def split_for_target(
    group: Group, target: Target, registry: Mapping[Category, Converter] | None = None
) -> TargetSplit:
    """Делит группу на «конвертировать» и «уже в этом формате — пропустить».

    Файл пропускается, только если его формат совпадает с вариантом
    (`fmt == target.id`) И конвертер категории НЕ предлагает этот вариант
    для этого формата (`target.id not in targets(fmt)`). Если предлагает
    (например, будущее «MP4 → MP4, сжать»), файл конвертируется.
    Для `many_to_one` (например, «все в один PDF») пропусков нет — берутся
    все файлы в исходном порядке.
    """
    if registry is None:
        registry = REGISTRY
    converter = registry.get(group.category)
    offered: dict[str, bool] = {}

    def offers_same(fmt_code: str) -> bool:
        if fmt_code not in offered:
            try:
                ids = {t.id for t in converter.targets(fmt_code)} if converter else set()
            except Exception:  # noqa: BLE001
                log.exception("Конвертер %s упал в targets()", group.category)
                ids = set()
            offered[fmt_code] = target.id in ids
        return offered[fmt_code]

    split = TargetSplit()
    for path, fmt in group.files:
        if not target.many_to_one and fmt.fmt == target.id and not offers_same(fmt.fmt):
            split.skipped.append((path, fmt))
        else:
            split.convert.append((path, fmt))
    return split
