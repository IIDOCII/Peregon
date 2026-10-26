"""Дымовая проверка собранного exe без участия человека (этап 6, П9).

    uv run python packaging/smoke_test.py <папка программы> <рабочая папка> [RAW-файл ...]

`<папка программы>` — папка с Peregon.exe (например, копия dist/Peregon);
`<рабочая папка>` — пустая или несуществующая папка для самопроверки и отчёта.

Шаги:
1. снимок «до»: %APPDATA%, %LOCALAPPDATA% (верхний уровень и подпапки с
   именами Konverter/Peregon/Qt/PySide/magika/onnx/python), %TEMP% (верхний уровень),
   реестр HKCU\\Software (ключи и значения, где встречается konverter/qt/
   pyside/onnx/magika, а также ключи автозагрузки Run/RunOnce);
2. `Peregon.exe --selftest <рабочая>/selftest` (RAW-файлы копируются в
   selftest/extra) — код выхода и отчёт report.json;
3. обычный запуск с окном: процесс должен прожить ≥ 5 с, затем закрывается
   штатно (taskkill без /F — окну приходит WM_CLOSE); если не закрылся за
   20 с — taskkill /F только по PID этого процесса;
4. журнал data/logs/konverter.log рядом с exe — без ERROR/Traceback;
5. снимок «после» и разница — в `<рабочая>/smoke.json`.

Ничего не удаляет; пишет только в рабочую папку (и программа — в свою data/).
Код выхода 0 — всё прошло, 1 — есть замечания.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

INTERESTING = re.compile(r"konverter|peregon|xclassik|qt|pyside|shiboken|onnx|magika|python|pyinstaller|_mei", re.I)
REG_INTERESTING = re.compile(r"konverter|peregon|xclassik|peregon|xclassik|qtproject|trolltech|pyside|onnx|magika|pyinstaller", re.I)
AUTORUN_KEYS = (
    r"Software\Microsoft\Windows\CurrentVersion\Run",
    r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
)
GUI_ALIVE_SECONDS = 6.0
GUI_CLOSE_TIMEOUT = 20.0


def _listing(folder: Path, deep_filter: re.Pattern | None) -> dict[str, float]:
    """Имена верхнего уровня (+ рекурсивно — подпапки с «интересными» именами)."""
    result: dict[str, float] = {}
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return result
    for entry in entries:
        try:
            result[entry.name] = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            result[entry.name] = 0.0
        if deep_filter is not None and entry.is_dir(follow_symlinks=False) and deep_filter.search(entry.name):
            for root, dirs, files in os.walk(entry.path):
                for name in dirs + files:
                    path = os.path.join(root, name)
                    try:
                        result[os.path.relpath(path, folder)] = os.stat(path).st_mtime
                    except OSError:
                        pass
    return result


def _walk_registry(root_key, path: str, out: set[str], depth: int = 0) -> None:
    """Все ключи/значения под HKCU\\path, в имени (пути) которых есть REG_INTERESTING."""
    try:
        key = winreg.OpenKey(root_key, path)
    except OSError:
        return
    with key:
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            text = f"{name}={value!r}"[:300]
            if REG_INTERESTING.search(path) or REG_INTERESTING.search(text):
                out.add(f"{path} :: {text}")
            i += 1
        i = 0
        while depth < 12:
            try:
                sub = winreg.EnumKey(key, i)
            except OSError:
                break
            sub_path = f"{path}\\{sub}"
            if REG_INTERESTING.search(sub):
                out.add(sub_path)
            _walk_registry(root_key, sub_path, out, depth + 1)
            i += 1


def snapshot() -> dict:
    env = os.environ
    reg: set[str] = set()
    started = time.monotonic()
    _walk_registry(winreg.HKEY_CURRENT_USER, "Software", reg)
    autorun: set[str] = set()
    for key_path in AUTORUN_KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    autorun.add(f"{key_path} :: {name}={value!r}")
                    i += 1
        except OSError:
            pass
    return {
        "appdata": _listing(Path(env["APPDATA"]), INTERESTING),
        "localappdata": _listing(Path(env["LOCALAPPDATA"]), INTERESTING),
        "temp": _listing(Path(env["TEMP"]), None),
        "registry": sorted(reg),
        "autorun": sorted(autorun),
        "registry_seconds": round(time.monotonic() - started, 1),
    }


def diff(before: dict, after: dict) -> dict:
    result = {}
    for key in ("appdata", "localappdata", "temp"):
        b, a = before[key], after[key]
        result[key] = {
            "new": sorted(set(a) - set(b)),
            "changed": sorted(n for n in set(a) & set(b) if INTERESTING.search(n) and a[n] != b[n]),
        }
    for key in ("registry", "autorun"):
        result[key] = {
            "new": sorted(set(after[key]) - set(before[key])),
            "gone": sorted(set(before[key]) - set(after[key])),
        }
    return result


def run_gui(exe: Path) -> dict:
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
    time.sleep(GUI_ALIVE_SECONDS)
    alive = proc.poll() is None
    info: dict = {"pid": proc.pid, "alive_after_s": GUI_ALIVE_SECONDS if alive else None}
    if not alive:
        info["exit_code_early"] = proc.returncode
        return info
    subprocess.run(["taskkill", "/PID", str(proc.pid)], capture_output=True)
    try:
        proc.wait(GUI_CLOSE_TIMEOUT)
        info["closed"] = "graceful"
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True)
        proc.wait(10)
        info["closed"] = "forced"
    info["exit_code"] = proc.returncode
    return info


def check_log(app_dir: Path) -> dict:
    log = app_dir / "data" / "logs" / "konverter.log"
    if not log.is_file():
        return {"ok": False, "error": "нет журнала"}
    text = log.read_text(encoding="utf-8", errors="replace")
    bad = [line for line in text.splitlines() if " ERROR " in line or " CRITICAL " in line or "Traceback" in line]
    return {
        "ok": not bad and "Выход из программы" in text,
        "path": str(log),
        "bad_lines": bad[:20],
        "has_exit_line": "Выход из программы" in text,
    }


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    app_dir, work = Path(argv[1]).resolve(), Path(argv[2]).resolve()
    raws = [Path(p) for p in argv[3:]]
    exe = app_dir / "Peregon.exe"
    work.mkdir(parents=True, exist_ok=True)
    report: dict = {"exe": str(exe)}

    before = snapshot()
    report["registry_scan_seconds"] = before["registry_seconds"]

    folder = work / "selftest"
    (folder / "extra").mkdir(parents=True, exist_ok=False)
    for raw in raws:
        shutil.copy2(raw, folder / "extra" / raw.name)
    started = time.monotonic()
    proc = subprocess.run([str(exe), "--selftest", str(folder)], timeout=900)
    report["selftest"] = {
        "exit_code": proc.returncode,
        "seconds": round(time.monotonic() - started, 1),
    }
    report_path = folder / "report.json"
    if report_path.is_file():
        st = json.loads(report_path.read_text(encoding="utf-8"))
        report["selftest"]["cases"] = {
            c["source"]: c.get("status") + (f" ({c['error']})" if c.get("error") else "")
            for c in st["cases"]
        }
        report["selftest"]["checks"] = {k: st[k].get("ok") for k in ("child", "paths", "qt")}

    # Неверный вызов самопроверки — код 2, папка не трогается.
    bad = subprocess.run([str(exe), "--selftest", str(folder)], timeout=120)
    report["selftest_nonempty_folder_exit"] = bad.returncode

    report["gui"] = run_gui(exe)
    report["log"] = check_log(app_dir)

    after = snapshot()
    report["p9_diff"] = diff(before, after)

    ok = (
        report["selftest"]["exit_code"] == 0
        and report["selftest_nonempty_folder_exit"] == 2
        and report["gui"].get("alive_after_s")
        and report["gui"].get("closed") == "graceful"
        and report["log"]["ok"]
    )
    report["ok"] = bool(ok)
    (work / "smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
