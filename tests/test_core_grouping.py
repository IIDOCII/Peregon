"""Тесты группировки файлов (core/grouping.py)."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest
from PIL import Image

from konverter.converters.base import Category, Target
from konverter.core.grouping import Group, build_groups, common_targets, split_for_target


def _png(path: Path) -> Path:
    Image.new("RGB", (4, 4)).save(path, "PNG")
    return path


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return path


def test_three_png_two_wav_two_groups(tmp_path):
    files = [
        _png(tmp_path / "1.png"),
        _wav(tmp_path / "a.wav"),
        _png(tmp_path / "2.png"),
        _png(tmp_path / "3.png"),
        _wav(tmp_path / "b.wav"),
    ]
    result = build_groups(files)
    assert [g.category for g in result.groups] == [Category.IMAGE, Category.AUDIO]
    assert result.groups[0].paths == [files[0], files[2], files[3]]
    assert result.groups[1].paths == [files[1], files[4]]
    assert result.unsupported == []
    assert not result.limit_exceeded


def test_folder_expanded_without_recursion(tmp_path):
    folder = tmp_path / "папка"
    folder.mkdir()
    _png(folder / "b.png")
    _png(folder / "A.png")
    (folder / ".hidden.png").write_bytes((folder / "b.png").read_bytes())
    sub = folder / "вложенная"
    sub.mkdir()
    _png(sub / "deep.png")

    result = build_groups([folder])
    assert len(result.groups) == 1
    names = [p.name for p in result.groups[0].paths]
    assert names == ["A.png", "b.png"]  # по имени без учёта регистра, без скрытых и вложенных


def test_duplicates_removed(tmp_path):
    png = _png(tmp_path / "x.png")
    result = build_groups([png, png, tmp_path / "." / "x.png", tmp_path])
    assert len(result.groups[0].files) == 1


def test_unsupported_listed(tmp_path):
    png = _png(tmp_path / "ok.png")
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    exe = tmp_path / "prog.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 58 + b"\x80\x00\x00\x00" + b"\x00" * 200)
    missing = tmp_path / "нет.png"

    result = build_groups([png, empty, exe, missing])
    assert [g.category for g in result.groups] == [Category.IMAGE]
    unsupported = dict(result.unsupported)
    assert set(unsupported) == {empty, exe, missing}
    assert unsupported[empty] == "Пустой файл"
    assert unsupported[missing] == "Файл не найден"


def test_file_limit(tmp_path):
    files = [_png(tmp_path / f"{i}.png") for i in range(5)]
    result = build_groups(files, max_files=3)
    assert result.limit_exceeded
    assert result.message and "3" in result.message
    assert len(result.groups[0].files) == 3


class _FakeConverter:
    category = Category.IMAGE

    def targets(self, fmt):
        common = [Target("pdf", "PDF", "pdf"), Target("png", "PNG", "png")]
        if fmt == "png":
            return [Target("jpg", "JPEG", "jpg"), *common]
        return [Target("png_x", "X", "png"), *common, Target("jpg", "JPEG", "jpg")]

    def convert(self, *args, **kwargs):  # pragma: no cover — не нужен
        raise NotImplementedError


def test_common_targets_intersection(tmp_path):
    png = _png(tmp_path / "a.png")
    jpg = tmp_path / "b.jpg"
    Image.new("RGB", (4, 4)).save(jpg, "JPEG")
    group = build_groups([png, jpg]).groups[0]

    registry = {Category.IMAGE: _FakeConverter()}
    ids = [t.id for t in common_targets(group, registry)]
    assert ids == ["jpg", "pdf", "png"]  # порядок — как у первого формата


def test_common_targets_no_converter():
    assert common_targets(Group(Category.BOOK, []), {}) == []


# --- П.5 ревью: смешанная группа и «уже в этом формате» -------------------------


def _jpg(path: Path) -> Path:
    Image.new("RGB", (4, 4)).save(path, "JPEG")
    return path


def test_mixed_jpg_png_offers_both(tmp_path):
    from konverter.converters.images import ImageConverter

    group = build_groups([_jpg(tmp_path / "a.jpg"), _png(tmp_path / "b.png")]).groups[0]
    ids = [t.id for t in common_targets(group, {Category.IMAGE: ImageConverter()})]
    assert "jpg" in ids and "png" in ids
    assert {"webp", "pdf_each", "pdf_single"} <= set(ids)


_IMAGE_ORDER = [
    "jpg", "png", "webp", "avif", "gif", "bmp", "tiff", "ico", "pdf_each", "pdf_single", "print_ppi",
]


@pytest.mark.parametrize("first", ["jpg", "png"])
def test_mixed_group_canonical_order(tmp_path, first):
    """Ревью: порядок вариантов канонический (как задаёт конвертер), не зависит
    от порядка файлов; «JPG» не оказывается последним."""
    from konverter.converters.images import ImageConverter

    jpg, png = _jpg(tmp_path / "a.jpg"), _png(tmp_path / "b.png")
    items = [jpg, png] if first == "jpg" else [png, jpg]
    group = build_groups(items).groups[0]
    ids = [t.id for t in common_targets(group, {Category.IMAGE: ImageConverter()})]
    assert ids == _IMAGE_ORDER


def test_homogeneous_group_order(tmp_path):
    from konverter.converters.images import ImageConverter

    group = build_groups([_png(tmp_path / "b.png")]).groups[0]
    ids = [t.id for t in common_targets(group, {Category.IMAGE: ImageConverter()})]
    assert ids == [i for i in _IMAGE_ORDER if i != "png"]


def test_target_order_attribute_sorts_unknown_last(tmp_path):
    """Варианты, которых нет в `target_order`, идут после известных в прежнем порядке."""

    class Conv(_FakeConverter):
        target_order = ("png", "pdf")

    png = _png(tmp_path / "a.png")
    group = build_groups([png, _jpg(tmp_path / "b.jpg")]).groups[0]
    ids = [t.id for t in common_targets(group, {Category.IMAGE: Conv()})]
    assert ids == ["png", "pdf", "jpg"]


def test_homogeneous_group_hides_same_format(tmp_path):
    from konverter.converters.images import ImageConverter

    group = build_groups([_jpg(tmp_path / "a.jpg"), _jpg(tmp_path / "b.jpg")]).groups[0]
    ids = [t.id for t in common_targets(group, {Category.IMAGE: ImageConverter()})]
    assert "jpg" not in ids and "png" in ids


def test_split_for_target(tmp_path):
    from konverter.converters.images import ImageConverter

    jpg = _jpg(tmp_path / "a.jpg")
    png = _png(tmp_path / "b.png")
    group = build_groups([jpg, png]).groups[0]
    targets = {t.id: t for t in common_targets(group, {Category.IMAGE: ImageConverter()})}

    split = split_for_target(group, targets["jpg"])
    assert [p for p, _ in split.convert] == [png]
    assert [p for p, _ in split.skipped] == [jpg]
    assert split.convert_paths == [png] and split.convert_formats == ["png"]

    split = split_for_target(group, targets["png"])
    assert [p for p, _ in split.convert] == [jpg] and [p for p, _ in split.skipped] == [png]

    # «Много → один» берёт все файлы, пропусков нет.
    split = split_for_target(group, targets["pdf_single"])
    assert split.convert_paths == [jpg, png] and split.skipped == []
    assert split.convert_formats == ["jpg", "png"]


def test_mixed_target_is_long_if_any_format_long(tmp_path):
    class Conv:
        category = Category.IMAGE

        def targets(self, fmt):
            return [Target("png", "PNG", "png", is_long=(fmt == "jpg"))] if fmt != "png" else []

    group = build_groups([_jpg(tmp_path / "a.jpg"), _png(tmp_path / "b.png")]).groups[0]
    (target,) = common_targets(group, {Category.IMAGE: Conv()})
    assert target.id == "png" and target.is_long


# --- Ревью 2, п.6: конвертер явно предлагает вариант для своего же формата -------


class _CompressConverter:
    """Фиктивный конвертер: «MP4 → MP4 (сжать)» и «MP4 → WEBM»; для WEBM — только «MP4»."""

    category = Category.VIDEO

    def targets(self, fmt):
        if fmt == "mp4":
            return [Target("mp4", "MP4 (сжать)", "mp4"), Target("webm", "WEBM", "webm")]
        if fmt == "webm":
            return [Target("mp4", "MP4", "mp4")]
        return []

    def convert(self, *args, **kwargs):  # pragma: no cover — не нужен
        raise NotImplementedError


def _video_group(*items: tuple[str, str]) -> Group:
    from konverter.converters.base import DetectedFormat

    return Group(
        Category.VIDEO,
        [(Path(name), DetectedFormat(fmt, Category.VIDEO, fmt.upper())) for name, fmt in items],
    )


def test_same_format_target_offered_when_converter_lists_it():
    group = _video_group(("a.mp4", "mp4"), ("b.mp4", "mp4"))
    registry = {Category.VIDEO: _CompressConverter()}
    ids = [t.id for t in common_targets(group, registry)]
    assert ids == ["mp4", "webm"]


def test_split_converts_when_converter_lists_same_format():
    group = _video_group(("a.mp4", "mp4"), ("b.webm", "webm"))
    registry = {Category.VIDEO: _CompressConverter()}
    target = next(t for t in common_targets(group, registry) if t.id == "mp4")
    split = split_for_target(group, target, registry)
    assert split.convert_paths == [Path("a.mp4"), Path("b.webm")]
    assert split.skipped == []


def test_split_skips_same_format_when_not_offered(tmp_path):
    """Без явного варианта (PNG → PNG не предлагается) файл по-прежнему пропускается."""
    from konverter.converters.images import ImageConverter

    group = build_groups([_jpg(tmp_path / "a.jpg"), _png(tmp_path / "b.png")]).groups[0]
    registry = {Category.IMAGE: ImageConverter()}
    target = next(t for t in common_targets(group, registry) if t.id == "png")
    split = split_for_target(group, target, registry)
    assert [p.name for p, _ in split.skipped] == ["b.png"]


def test_target_order_declared_in_protocol():
    """Ревью 2, п.9: `target_order` объявлен в протоколе Converter (необязательный)."""
    import typing

    from konverter.converters.base import Converter

    hints = typing.get_type_hints(Converter)
    assert "target_order" in hints
    assert Converter.target_order is None  # по умолчанию порядка нет
    # Конвертер без атрибута по-прежнему работает (getattr с умолчанием).
    assert common_targets(_video_group(("a.mp4", "mp4")), {Category.VIDEO: _CompressConverter()})
