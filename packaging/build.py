"""Сборка портативной версии одной командой: `uv run python packaging/build.py`.

Результат:
- `dist/Peregon/` — папка программы (Peregon.exe + _internal + LICENSE,
  THIRD_PARTY_LICENSES.md, licenses/);
- `dist/Peregon-<версия>-win64.zip` — она же в архиве (внутри — папка Peregon).

Версия — из pyproject.toml. Пишет только в build/ и dist/ проекта (обе
в .gitignore); прежняя dist/Peregon заменяется PyInstaller'ом (--noconfirm).
Ключ `--no-zip` — без архива.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "konverter.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "Peregon"  # видимое имя; совпадает с APP_NAME в konverter.spec
# Лицензия программы и тексты лицензий сторонних компонентов — рядом с Peregon.exe
# (GPL/LGPL требуют передавать их вместе с программой).
LEGAL_FILES = ("LICENSE", "THIRD_PARTY_LICENSES.md")
LEGAL_DIRS = ("licenses",)


def version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def zip_name(ver: str) -> str:
    return f"{APP_NAME}-{ver}-win64.zip"


def folder_size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def copy_legal(app_dir: Path) -> None:
    """Кладёт LICENSE, THIRD_PARTY_LICENSES.md и licenses/ рядом с exe."""
    for name in LEGAL_FILES:
        shutil.copyfile(ROOT / name, app_dir / name)
    for name in LEGAL_DIRS:
        # PyInstaller (--noconfirm) пересоздаёт app_dir, поэтому папки здесь ещё нет;
        # dirs_exist_ok — на случай повторного вызова без пересборки.
        shutil.copytree(ROOT / name, app_dir / name, dirs_exist_ok=True)


def make_zip(app_dir: Path, target: Path) -> Path:
    """Архив папки программы; внутри — папка `Peregon/`. Без data/."""
    tmp = target.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in sorted(app_dir.rglob("*")):
            rel = path.relative_to(app_dir)
            if rel.parts and rel.parts[0] == "data":
                continue  # рабочие данные (если exe уже запускали) — не в архив
            if path.is_file():
                z.write(path, Path(APP_NAME) / rel)
    tmp.replace(target)
    return target


def build(with_zip: bool = True) -> int:
    ver = version()
    print(f"Сборка {APP_NAME} {ver}")
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", str(DIST), "--workpath", str(BUILD), str(SPEC),
    ]
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("PyInstaller завершился с ошибкой", file=sys.stderr)
        return result.returncode
    app_dir = DIST / APP_NAME
    copy_legal(app_dir)
    print(f"Папка: {app_dir} — {folder_size(app_dir) / 2**20:.1f} МБ")
    if with_zip:
        archive = make_zip(app_dir, DIST / zip_name(ver))
        print(f"Архив: {archive} — {archive.stat().st_size / 2**20:.1f} МБ")
    return 0


if __name__ == "__main__":
    sys.exit(build(with_zip="--no-zip" not in sys.argv[1:]))
