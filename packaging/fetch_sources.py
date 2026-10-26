"""Скачивает исходники сторонних компонентов под GPL/LGPL/MPL (Corresponding Source).

    uv run python -I packaging/fetch_sources.py

Список — `packaging/sources.json`. Файлы кладутся в `dist/sources/<filename>`,
подписи — в `dist/sources/signatures/`, ключи — в `dist/sources/keys/`.
Уже скачанные файлы повторно не скачиваются (проверяется только их sha256).

Проверка каждого файла:
- sha256 — со значением из sources.json (uv.lock / официальная сумма / дайджест
  GitHub / pkg.py сборщика); при несовпадении файл помечается как НЕ ПРОШЕДШИЙ;
- подпись GPG — если для файла указаны signature_url и signature_key_url и в системе
  есть gpg. Ключ импортируется во временный каталог build/sources-gnupg
  (основной ключник пользователя не трогается). Если gpg нет — «подпись не проверена»;
- если официальной суммы нет (архивы, которые GitHub генерирует на лету) — фактический
  sha256 записывается, а содержимое архива сверяется с деревом коммита (поле git_tree:
  git-хеш каждого файла через GitHub API).

Скачанное — недоверенные данные: скрипт ничего из них не запускает и не распаковывает.
Отчёт — `dist/sources/fetch_report.json`. Код выхода 0 — все файлы скачаны и ни одна
проверка не провалилась; 1 — есть ошибки.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES_JSON = ROOT / "packaging" / "sources.json"
OUT = ROOT / "dist" / "sources"
SIG_DIR = OUT / "signatures"
KEY_DIR = OUT / "keys"
GNUPG_HOME = ROOT / "build" / "sources-gnupg"
REPORT = OUT / "fetch_report.json"

# Скачивать разрешено только с официальных сайтов проектов / PyPI / GitHub / MSYS2.
ALLOWED_HOSTS = {
    "ffmpeg.org", "github.com", "codeload.github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com", "raw.githubusercontent.com",
    "code.videolan.org", "repo.msys2.org", "download.qt.io",
    "files.pythonhosted.org", "www.libraw.org",
}
USER_AGENT = "Peregon-fetch-sources/1.0 (+https://github.com)"
CHUNK = 1 << 20


def _host_ok(url: str) -> bool:
    p = urllib.parse.urlparse(url)
    return p.scheme == "https" and p.hostname in ALLOWED_HOSTS


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    """Скачивает url в dest (через .part). Исходный URL обязан быть из белого списка.

    download.qt.io перенаправляет на зеркала — их проверка идёт по sha256 с download.qt.io.
    """
    if not _host_ok(url):
        raise ValueError(f"URL не из списка официальных источников: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f, CHUNK)
    tmp.replace(dest)


def find_gpg() -> str | None:
    for cand in (shutil.which("gpg"), r"C:\Program Files\Git\usr\bin\gpg.exe"):
        if cand and Path(cand).exists():
            return cand
    return None


def _posix(path: Path) -> str:
    # gpg из Git for Windows (MSYS) искажает абсолютные пути с кириллицей (папка
    # «Конвертер»), поэтому передаём пути относительно корня проекта (только ASCII)
    # и запускаем gpg с cwd=ROOT.
    return path.relative_to(ROOT).as_posix()


def gpg_verify(gpg: str, key: Path, sig: Path, data: Path) -> tuple[bool, str]:
    """Импортирует ключ во временный ключник и проверяет отделённую подпись.

    Возвращает (успех, описание). Успех — только при VALIDSIG в машинном выводе gpg.
    """
    GNUPG_HOME.mkdir(parents=True, exist_ok=True)
    base = [gpg, "--batch", "--no-tty", "--homedir", _posix(GNUPG_HOME)]
    imp = subprocess.run(base + ["--import", _posix(key)], capture_output=True, text=True, cwd=ROOT,
                         encoding="utf-8", errors="replace")
    if imp.returncode not in (0, 2):  # 2 — часть ключей не импортирована (например, отозванные)
        return False, f"не удалось импортировать ключ: {imp.stderr.strip()[:300]}"
    res = subprocess.run(base + ["--status-fd", "1", "--verify", _posix(sig), _posix(data)],
                         capture_output=True, text=True, cwd=ROOT, encoding="utf-8", errors="replace")
    fprs = [line.split()[2] for line in res.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG")]
    if res.returncode == 0 and fprs:
        return True, f"подпись верна, ключ {fprs[0]} (ключ взят с {key.name}; доверие к ключу — по официальному источнику)"
    tail = (res.stdout + res.stderr).strip().splitlines()[-3:]
    return False, "подпись НЕ подтверждена: " + " | ".join(tail)[:400]


def git_tree_check(archive: Path, spec: str) -> tuple[bool, str]:
    """Сверяет архив, сгенерированный GitHub, с деревом коммита: git-хеш (blob SHA-1) каждого файла.

    spec — «owner/repo@commit». Архив читается в памяти через tarfile, на диск ничего
    не распаковывается и не выполняется. Дерево берётся из GitHub API (без токена).
    """
    import tarfile

    repo, commit = spec.split("@", 1)
    url = f"https://api.github.com/repos/{repo}/git/trees/{commit}?recursive=1"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    if data.get("truncated"):
        return False, "дерево git слишком большое (API вернул truncated) — не сверено"
    # submodule-записи (type=commit) в архив GitHub не попадают; ссылки (mode 120000) — сверяем как blob
    tree = {t["path"]: t["sha"] for t in data["tree"] if t["type"] == "blob"}
    got: dict[str, str] = {}
    with tarfile.open(archive) as tar:
        for m in tar.getmembers():
            if not (m.isfile() or m.issym()):
                continue
            rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
            body = m.linkname.encode() if m.issym() else tar.extractfile(m).read()
            got[rel] = hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()
    missing = sorted(set(tree) - set(got))
    extra = sorted(set(got) - set(tree))
    differ = sorted(p for p in tree.keys() & got.keys() if tree[p] != got[p])
    if missing or extra or differ:
        return False, (f"НЕ совпадает с {spec}: нет {len(missing)}, лишних {len(extra)}, "
                       f"различаются {len(differ)} ({(missing + extra + differ)[:3]})")
    return True, f"содержимое совпадает с деревом git {spec} ({len(tree)} файлов)"


def main() -> int:
    entries = json.loads(SOURCES_JSON.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    gpg = find_gpg()
    report = []
    failed = False
    for e in entries:
        name = e["filename"]
        dest = OUT / name
        item = {"name": e["name"], "version": e["version"], "filename": name, "url": e["url"]}
        try:
            if dest.exists():
                item["downloaded"] = "уже был"
            else:
                print(f"скачиваю {name} …", flush=True)
                download(e["url"], dest)
                item["downloaded"] = "скачан"
        except Exception as exc:  # noqa: BLE001 — фиксируем любую сетевую ошибку в отчёте
            item["downloaded"] = f"ОШИБКА: {exc}"
            item["ok"] = False
            failed = True
            report.append(item)
            print(f"  ошибка: {exc}", flush=True)
            continue

        actual = sha256_of(dest)
        item["size_bytes"] = dest.stat().st_size
        item["sha256"] = actual
        expected = e.get("sha256")
        ok = True
        if expected:
            if actual == expected:
                item["sha256_check"] = f"совпадает ({e.get('sha256_source')})"
            else:
                item["sha256_check"] = f"НЕ СОВПАДАЕТ с {expected} ({e.get('sha256_source')})"
                # Допускается только для записей с явным пояснением в sources.json и только
                # если содержимое совпало с деревом git (см. ниже).
                ok = bool(e.get("sha256_mismatch_ok") and e.get("git_tree"))
        else:
            item["sha256_check"] = f"официальной суммы нет — записан фактический sha256 ({e.get('sha256_source')})"

        if e.get("git_tree"):
            try:
                good, text = git_tree_check(dest, e["git_tree"])
            except Exception as exc:  # noqa: BLE001
                good, text = False, f"сверка с деревом git не выполнена: {exc}"
            item["git_tree_check"] = text
            ok = ok and good

        sig_url, key_url = e.get("signature_url"), e.get("signature_key_url")
        if sig_url and key_url:
            try:
                sig = SIG_DIR / sig_url.rsplit("/", 1)[-1]
                key = KEY_DIR / key_url.rsplit("/", 1)[-1]
                if not sig.exists():
                    download(sig_url, sig)
                if not key.exists():
                    download(key_url, key)
                if gpg is None:
                    item["signature_check"] = "подпись не проверена: gpg не найден"
                else:
                    good, text = gpg_verify(gpg, key, sig, dest)
                    item["signature_check"] = text
                    ok = ok and good
            except Exception as exc:  # noqa: BLE001
                item["signature_check"] = f"подпись не проверена: {exc}"
                ok = False
        else:
            item["signature_check"] = "подписи нет"

        item["ok"] = ok
        failed = failed or not ok
        report.append(item)
        print(f"  {name}: {item['size_bytes']} байт; {item['sha256_check']}; {item['signature_check']}"
              + (f"; {item['git_tree_check']}" if "git_tree_check" in item else ""), flush=True)

    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(i.get("size_bytes", 0) for i in report)
    print(f"Итого: {len(report)} файлов, {total} байт; отчёт — {REPORT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
