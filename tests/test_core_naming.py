"""Тесты имён результатов и временных файлов (core/naming.py)."""

from __future__ import annotations

import os
from pathlib import Path

from konverter.core import naming


def test_unique_path_increments(tmp_path):
    first = naming.unique_path(tmp_path, "фото", "png")
    assert first == tmp_path / "фото.png"
    first.write_bytes(b"1")
    second = naming.unique_path(tmp_path, "фото", "png")
    assert second == tmp_path / "фото (1).png"
    second.write_bytes(b"2")
    assert naming.unique_path(tmp_path, "фото", ".png") == tmp_path / "фото (2).png"


def test_unique_path_for_folder(tmp_path):
    (tmp_path / "архив").mkdir()
    assert naming.unique_path(tmp_path, "архив", "") == tmp_path / "архив (1)"


def test_finalize_never_overwrites(tmp_path):
    existing = tmp_path / "фото.png"
    existing.write_bytes(b"ORIGINAL")
    (tmp_path / "фото (1).png").write_bytes(b"ORIGINAL-1")

    temp = naming.make_temp_file(tmp_path, "png")
    temp.write_bytes(b"NEW")
    result = naming.finalize(temp, tmp_path, "фото", "png")

    assert result == tmp_path / "фото (2).png"
    assert result.read_bytes() == b"NEW"
    assert existing.read_bytes() == b"ORIGINAL"
    assert (tmp_path / "фото (1).png").read_bytes() == b"ORIGINAL-1"
    assert not temp.exists()


def test_finalize_folder(tmp_path):
    (tmp_path / "out").mkdir()
    temp = naming.make_temp_dir(tmp_path)
    (temp / "page1.png").write_bytes(b"x")
    result = naming.finalize(temp, tmp_path, "out", "")
    assert result == tmp_path / "out (1)"
    assert (result / "page1.png").read_bytes() == b"x"


def test_finalize_race_name_taken_after_check(tmp_path, monkeypatch):
    """Имя заняли между проверкой и переносом — берётся следующее, чужое цело."""
    temp = naming.make_temp_file(tmp_path, "txt")
    temp.write_bytes(b"NEW")
    real_move = naming._move_no_replace
    calls = []

    def racing_move(src, dst, is_dir):
        if not calls:
            Path(dst).write_bytes(b"FOREIGN")  # кто-то успел создать файл
        calls.append(dst)
        return real_move(src, dst, is_dir)

    monkeypatch.setattr(naming, "_move_no_replace", racing_move)
    result = naming.finalize(temp, tmp_path, "a", "txt")
    assert (tmp_path / "a.txt").read_bytes() == b"FOREIGN"
    assert result == tmp_path / "a (1).txt"
    assert result.read_bytes() == b"NEW"


def test_temp_names_are_hidden_and_exclusive(tmp_path):
    temp = naming.make_temp_file(tmp_path, "pdf")
    assert temp.name.startswith(naming.TEMP_PREFIX)
    assert temp.name.endswith(".part.pdf")
    other = naming.make_temp_file(tmp_path, "pdf")
    assert other != temp
    folder = naming.make_temp_dir(tmp_path)
    assert folder.is_dir() and folder.name.endswith(".part")


def test_temp_file_writable_by_open(tmp_path):
    """Конвертеры пишут через open(..., "wb") — временный файл не должен мешать."""
    temp = naming.make_temp_file(tmp_path, "png")
    with open(temp, "wb") as f:
        f.write(b"data")
    assert temp.read_bytes() == b"data"


def test_cleanup_only_our_files(tmp_path):
    foreign = tmp_path / "важный.png"
    foreign.write_bytes(b"keep")
    foreign_dir = tmp_path / "папка"
    foreign_dir.mkdir()
    assert naming.cleanup(foreign) is False
    assert naming.cleanup(foreign_dir) is False
    assert foreign.exists() and foreign_dir.exists()

    temp = naming.make_temp_file(tmp_path, "png")
    temp_dir = naming.make_temp_dir(tmp_path)
    (temp_dir / "inner.txt").write_text("x")
    assert naming.cleanup(temp) is True
    assert naming.cleanup(temp_dir) is True
    assert naming.cleanup(temp) is True  # повторно — не падает
    assert sorted(p.name for p in tmp_path.iterdir()) == ["важный.png", "папка"]


def test_sanitize_stem():
    assert naming.sanitize_stem('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"
    assert naming.sanitize_stem("CON") == "CON_"
    assert naming.sanitize_stem("имя. ") == "имя"
    assert naming.sanitize_stem("") == "result"
    assert len(naming.sanitize_stem("я" * 400)) <= naming.MAX_COMPONENT_LENGTH - 24


def test_source_and_merged_stem():
    assert naming.source_stem(Path("копия.tar.gz")) == "копия"
    assert naming.source_stem(Path("фото.JPG")) == "фото"
    assert naming.merged_stem([Path("b.png"), Path("a.png")]) == "b"


def test_long_path(tmp_path):
    """Путь длиннее 260 символов: временный файл и перенос работают."""
    deep = tmp_path
    while len(str(deep)) < 280:
        deep = deep / ("д" * 40)
    os.makedirs(naming.os_path(deep), exist_ok=True)
    temp = naming.make_temp_file(deep, "txt")
    with open(temp, "wb") as f:
        f.write(b"long")
    result = naming.finalize(temp, deep, "результат", "txt")
    assert len(str(result)) > 260
    assert not str(result).startswith("\\\\?\\")
    with open(naming.os_path(result), "rb") as f:
        assert f.read() == b"long"


def test_can_write(tmp_path):
    assert naming.can_write(tmp_path) is True
