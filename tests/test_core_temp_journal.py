"""Журнал временных файлов и уборка осиротевших `.part` (ревью, п.7г)."""

from __future__ import annotations

import uuid
from pathlib import Path

from konverter.core import naming, temp_journal
from konverter.core.temp_journal import TempJournal


def _our(ext: str = "") -> str:
    """Имя точно нашего формата (см. naming.is_temp_name)."""
    name = f"{naming.TEMP_PREFIX}{uuid.uuid4().hex}{naming.TEMP_MARK}"
    return f"{name}.{ext}" if ext else name


def _journal_text(journal: TempJournal) -> str:
    return journal.path.read_text(encoding="utf-8") if journal.path else ""


def test_temp_file_journaled_and_forgotten(tmp_path, isolated_temp_journal):
    journal = isolated_temp_journal
    temp = naming.make_temp_file(tmp_path, "png")
    assert f"+{temp}" in _journal_text(journal)
    naming.finalize(temp, tmp_path, "итог", "png")
    assert _journal_text(journal) == ""  # активных нет — журнал усечён

    temp_dir = naming.make_temp_dir(tmp_path)
    assert f"+{temp_dir}" in _journal_text(journal)
    assert naming.cleanup(temp_dir)
    assert _journal_text(journal) == ""


def test_cleanup_keep_journal_entry(tmp_path, isolated_temp_journal):
    temp = naming.make_temp_file(tmp_path, "png")
    assert naming.cleanup(temp, forget=False)
    assert f"+{temp}" in _journal_text(isolated_temp_journal)


def test_cleanup_orphans_of_dead_process(tmp_path):
    journals = tmp_path / "journals"
    journals.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    orphan = work / _our("png")
    orphan.write_bytes(b"x")
    orphan_dir = work / _our()
    # Внутри — только объекты нашего формата (с чужими папка не удаляется).
    (orphan_dir / _our()).mkdir(parents=True)
    finished = work / _our("png")
    finished.write_bytes(b"y")  # вычеркнут в журнале — не трогать
    foreign = work / "чужой файл.txt"
    foreign.write_bytes(b"z")
    # Журнал «упавшего» процесса: никто не держит его блокировку.
    dead = journals / "active-999999-deadbeef.txt"
    dead.write_text(
        f"+{orphan}\n+{orphan_dir}\n+{finished}\n-{finished}\n+{foreign}\n", encoding="utf-8"
    )

    journal = TempJournal(lambda: journals)
    try:
        assert journal.cleanup_orphans() == 2
    finally:
        journal.close()
    assert not orphan.exists() and not orphan_dir.exists()
    assert finished.exists()
    assert foreign.exists()  # не наше имя — не удаляется
    assert not dead.exists()


def test_live_journal_of_other_instance_untouched(tmp_path):
    journals = tmp_path / "journals"
    journals.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    other = TempJournal(lambda: journals)  # «вторая копия программы»
    mine = TempJournal(lambda: journals)
    try:
        temp = work / _our("png")
        temp.write_bytes(b"x")
        other.add(naming.os_path(temp))
        assert mine.cleanup_orphans() == 0
        assert temp.exists()
        assert other.path.exists()
    finally:
        other.close()
        mine.close()
    assert temp.exists()


def test_journal_disabled_when_data_unavailable(tmp_path):
    journal = TempJournal(lambda: None)
    journal.add(tmp_path / "x")
    journal.remove(tmp_path / "x")
    assert journal.path is None
    assert journal.cleanup_orphans() == 0
    journal.close()


def test_module_cleanup_orphans_never_raises(monkeypatch):
    class Broken(TempJournal):
        def cleanup_orphans(self) -> int:
            raise RuntimeError("сбой")

    monkeypatch.setattr(temp_journal, "JOURNAL", Broken(lambda: None))
    assert temp_journal.cleanup_orphans() == 0


def test_closed_empty_journal_removed(tmp_path):
    journal = TempJournal(lambda: tmp_path)
    name = _our()
    journal.add(tmp_path / name)
    path: Path = journal.path
    journal.remove(tmp_path / name)
    journal.close()
    assert not path.exists()
