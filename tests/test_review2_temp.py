"""Ревью 2, п.2–3: подделанный журнал временных файлов и гонка создания журнала."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from konverter.core import naming, temp_journal
from konverter.core.temp_journal import TempJournal


def _our_name(ext: str = "") -> str:
    name = f"{naming.TEMP_PREFIX}{uuid.uuid4().hex}{naming.TEMP_MARK}"
    return f"{name}.{ext}" if ext else name


def _run_cleanup(journals: Path, text: str) -> int:
    (journals / "active-999999-deadbeef.txt").write_text(text, encoding="utf-8")
    journal = TempJournal(lambda: journals)
    try:
        return journal.cleanup_orphans()
    finally:
        journal.close()


@pytest.fixture
def dirs(tmp_path):
    journals = tmp_path / "journals"
    work = tmp_path / "work"
    journals.mkdir()
    work.mkdir()
    return journals, work


# --- п.2: точный формат имени ---------------------------------------------------


def test_temp_name_pattern_matches_generated(tmp_path):
    """Шаблон совпадает с тем, что реально создают make_temp_file/dir."""
    for ext in ("png", "jpg", "pdf", "webp", ""):
        temp = naming.make_temp_file(tmp_path, ext) if ext else naming.make_temp_dir(tmp_path)
        assert naming.is_temp_name(temp.name), temp.name
        assert naming.cleanup(temp)


@pytest.mark.parametrize(
    "name",
    [
        ".~konverter-notes.party.txt",
        ".~konverter-evil.part",
        ".~konverter-dead.part.png",
        f".~konverter-{'A' * 32}.part",  # заглавные — не наш формат
        f".~konverter-{'a' * 31}.part",
        f".~konverter-{'a' * 32}.part.png.exe",
        f".~konverter-{'a' * 32}.part.",
        f".~konverter-{'a' * 32}.part.{'x' * 17}",
        f".~konverter-{'a' * 32}.partx",
        f"x.~konverter-{'a' * 32}.part",
    ],
)
def test_foreign_names_rejected(name):
    assert not naming.is_temp_name(name)


def test_compound_extension_temp_name_still_ours(tmp_path):
    """Составное расширение (tar.gz) не делает временный объект «чужим»."""
    temp = naming.make_temp_file(tmp_path, "tar.gz")
    assert naming.is_temp_name(temp.name)
    assert naming.cleanup(temp)


def test_forged_journal_lookalike_name_not_deleted(dirs):
    journals, work = dirs
    lookalike = work / ".~konverter-notes.party.txt"
    lookalike.write_text("заметки", encoding="utf-8")
    assert _run_cleanup(journals, f"+{lookalike}\n") == 0
    assert lookalike.exists()


def test_forged_journal_relative_path_not_deleted(dirs, monkeypatch):
    journals, work = dirs
    name = _our_name("png")
    victim = work / name
    victim.write_bytes(b"x")
    monkeypatch.chdir(work)
    assert _run_cleanup(journals, f"+{name}\n+.\\{name}\n") == 0
    assert victim.exists()


def test_forged_journal_dir_with_foreign_content_not_deleted(dirs):
    journals, work = dirs
    evil = work / ".~konverter-evil.part"
    (evil / "важное").mkdir(parents=True)
    (evil / "важное" / "док.txt").write_text("x", encoding="utf-8")
    ours_name = work / _our_name()
    (ours_name / "sub").mkdir(parents=True)
    (ours_name / "фото.jpg").write_bytes(b"x")
    assert _run_cleanup(journals, f"+{evil}\n+{ours_name}\n") == 0
    assert (evil / "важное" / "док.txt").exists()
    assert (ours_name / "фото.jpg").exists()


def test_orphan_dir_removed_if_empty_or_only_ours(dirs):
    journals, work = dirs
    empty = work / _our_name()
    empty.mkdir()
    nested = work / _our_name()
    nested.mkdir()
    (nested / _our_name("png")).write_bytes(b"x")
    assert _run_cleanup(journals, f"+{empty}\n+{nested}\n") == 2
    assert not empty.exists() and not nested.exists()


@pytest.mark.skipif(os.name != "nt", reason="точка соединения — Windows")
def test_orphan_dir_link_not_followed(dirs, tmp_path):
    """Ссылка (junction) внутри «нашей» папки не раскрывается: цель не трогается."""
    import subprocess

    journals, work = dirs
    target = tmp_path / "цель"
    target.mkdir()
    precious = target / _our_name("png")  # даже «наши» имена по ту сторону ссылки
    precious.write_bytes(b"x")
    orphan = work / _our_name()
    orphan.mkdir()
    link = orphan / _our_name()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True, capture_output=True,
    )
    _run_cleanup(journals, f"+{orphan}\n")
    assert precious.exists()
    # Сама ссылка (и пустая папка) удаляется, цель остаётся.
    assert not orphan.exists()


def test_cleanup_own_temp_dir_with_content(tmp_path):
    """naming.cleanup для папки, созданной make_temp_dir этого процесса, удаляет содержимое."""
    temp_dir = naming.make_temp_dir(tmp_path)
    (temp_dir / "распаковано" / "глубже").mkdir(parents=True)
    (temp_dir / "распаковано" / "файл.txt").write_text("x", encoding="utf-8")
    assert naming.cleanup(temp_dir)
    assert not temp_dir.exists()


def test_cleanup_foreign_dir_with_our_name_keeps_content(tmp_path):
    """Папка с нашим именем, но не из make_temp_dir этого процесса, — без рекурсии."""
    alien = tmp_path / _our_name()
    (alien / "чужое").mkdir(parents=True)
    assert naming.cleanup(alien) is False
    assert (alien / "чужое").exists()


# --- п.3: гонка создания журнала и блокировки -------------------------------------


def _locked_by_someone(path: Path) -> bool:
    fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        if temp_journal._try_lock(fd):
            temp_journal._unlock(fd)
            return False
        return True
    finally:
        os.close(fd)


def test_journal_retries_when_lock_lost(tmp_path, monkeypatch):
    """Первая попытка блокировки не удалась (её «перехватила» уборка другой копии):
    журнал создаётся заново и в итоге заблокирован; брошенный файл убран."""
    real = temp_journal._try_lock
    calls = {"n": 0}

    def flaky(fd):
        calls["n"] += 1
        return False if calls["n"] == 1 else real(fd)

    monkeypatch.setattr(temp_journal, "_try_lock", flaky)
    journal = TempJournal(lambda: tmp_path)
    try:
        journal.add(tmp_path / _our_name("png"))
        assert journal.path is not None
        monkeypatch.setattr(temp_journal, "_try_lock", real)
        assert _locked_by_someone(journal.path)
        journals = [p.name for p in tmp_path.iterdir()]
        assert journals == [journal.path.name]  # первый, незаблокированный, удалён
    finally:
        journal.close()


def test_journal_disabled_when_lock_never_taken(tmp_path, monkeypatch):
    monkeypatch.setattr(temp_journal, "_try_lock", lambda fd: False)
    journal = TempJournal(lambda: tmp_path)
    try:
        journal.add(tmp_path / _our_name("png"))
        assert journal.path is None  # без блокировки журнал не ведём
        assert list(tmp_path.iterdir()) == []
    finally:
        journal.close()
