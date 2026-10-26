"""Упаковывает скачанные исходники в `dist/Peregon-<версия>-sources.zip`.

    uv run python -I packaging/pack_sources.py

Перед этим — `packaging/fetch_sources.py` (он скачивает файлы в `dist/sources/`
и пишет `dist/sources/fetch_report.json`). Скрипт отказывается упаковывать, если
какой-то файл не скачан или не прошёл проверку.

Внутри архива (папка `Peregon-<версия>-sources/`):
- все архивы исходников без изменений (они уже сжаты, поэтому кладутся без сжатия);
- `signatures/`, `keys/` — подписи и ключи, которыми проверялись файлы;
- `SOURCES.md` — таблица: компонент, версия, откуда, sha256, как проверено;
- `sources.json`, `fetch_report.json` — машиночитаемые данные.

Скачанные файлы — недоверенные данные: здесь они только копируются в zip.
"""

from __future__ import annotations

import json
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dist" / "sources"
SOURCES_JSON = ROOT / "packaging" / "sources.json"
REPORT = SRC / "fetch_report.json"


def version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def how_checked(item: dict) -> str:
    parts = [item["sha256_check"]]
    if item.get("signature_check") and item["signature_check"] != "подписи нет":
        parts.append("GPG: " + item["signature_check"])
    if item.get("git_tree_check"):
        parts.append("git: " + item["git_tree_check"])
    return "; ".join(parts)


def make_markdown(ver: str, entries: list[dict], report: dict[str, dict]) -> str:
    total = sum(report[e["filename"]]["size_bytes"] for e in entries)
    lines = [
        f"# Исходный код сторонних компонентов Peregon {ver}",
        "",
        "Этот архив — «соответствующий исходный код» (Corresponding Source, GPLv3 §6; LGPL) "
        "компонентов под GPL, LGPL и MPL, входящих в сборку `Peregon-" + ver + "-win64.zip`, "
        "а также исходники и сценарии сборки пакетов-обёрток, которые собирали эти библиотеки. "
        "Исходный код самого Peregon — в репозитории (тег версии / архив «Source code» выпуска).",
        "",
        "Все файлы скачаны без изменений с официальных адресов, указанных в таблице "
        "(скрипт `packaging/fetch_sources.py`), и проверены так, как сказано в последнем столбце. "
        "Подписи GPG проверялись ключами из папки `keys/` (взяты с официальных сайтов: "
        "`ffmpeg-devel.asc` — ffmpeg.org, `msys2.gpg` — репозиторий msys2/MSYS2-keyring); "
        "доверие к самим ключам основано только на источнике, из которого они взяты.",
        "",
        f"Файлов: {len(entries)}, общий размер: {total} байт ({total / 1_000_000:.1f} МБ).",
        "",
        "| Компонент | Версия | Лицензия | Файл | Откуда | sha256 | Как проверено |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in entries:
        r = report[e["filename"]]
        lines.append("| " + " | ".join(_cell(x) for x in (
            e["name"], e["version"], e["license"], e["filename"], e["url"],
            "`" + r["sha256"] + "`", how_checked(r),
        )) + " |")
    lines += ["", "## Откуда известны версии и примечания", ""]
    for e in entries:
        lines.append(f"- **{e['name']}** {e['version']}. Версия: {e['version_source']}. {e['note']}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ver = version()
    entries = json.loads(SOURCES_JSON.read_text(encoding="utf-8"))
    report = {i["filename"]: i for i in json.loads(REPORT.read_text(encoding="utf-8"))}
    bad = [e["filename"] for e in entries
           if e["filename"] not in report or not report[e["filename"]].get("ok")
           or not (SRC / e["filename"]).is_file()]
    if bad:
        print("Не скачаны или не прошли проверку:", ", ".join(bad), file=sys.stderr)
        return 1

    prefix = f"Peregon-{ver}-sources"
    target = ROOT / "dist" / f"{prefix}.zip"
    tmp = target.with_suffix(".zip.part")
    md = make_markdown(ver, entries, report)
    with zipfile.ZipFile(tmp, "w") as z:
        z.writestr(f"{prefix}/SOURCES.md", md.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        z.write(SOURCES_JSON, f"{prefix}/sources.json", compress_type=zipfile.ZIP_DEFLATED)
        z.write(REPORT, f"{prefix}/fetch_report.json", compress_type=zipfile.ZIP_DEFLATED)
        for e in entries:
            # архивы исходников уже сжаты — кладём как есть
            z.write(SRC / e["filename"], f"{prefix}/{e['filename']}", compress_type=zipfile.ZIP_STORED)
        for sub in ("signatures", "keys"):
            folder = SRC / sub
            if folder.is_dir():
                for p in sorted(folder.iterdir()):
                    if p.is_file():
                        z.write(p, f"{prefix}/{sub}/{p.name}", compress_type=zipfile.ZIP_STORED)
    tmp.replace(target)
    size = target.stat().st_size
    print(f"Готово: {target} ({size} байт, {size / 1_000_000:.1f} МБ)")
    if size >= 2 * 1024 ** 3:
        print("ВНИМАНИЕ: архив больше 2 ГБ — лимит GitHub на файл выпуска", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
