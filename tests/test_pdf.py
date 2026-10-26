"""PDF (этап 3): converters/pdf.py, счётчик мест "pdf" в _isolated, min_files в grouping.

PDF генерируются здесь же: fpdf2 (текст с кириллицей шрифтом Arial из
Windows, картинки) и pikepdf (шифрование, JavaScript, вложения, огромный
MediaBox, циклы объектов).

Большая часть проверок вариантов идёт через `convert` с подменённым
`_isolated.run_task`, который вызывает `pdf.child_task` ПРЯМО в процессе
тестов (фикстура `inproc`) — тот же код, что в ребёнке, но без запуска
процесса. Путь через настоящий дочерний процесс проверяют отдельные тесты:
PNG, TXT, «злые» файлы, падение и зависание ребёнка, отмена, длинные пути,
сквозной JobManager.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from contextlib import nullcontext
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from fpdf import FPDF
from pikepdf import Array, Dictionary, Name, String
from PIL import Image

from konverter.converters import _isolated, pdf
from konverter.converters.base import (
    REGISTRY,
    CancelToken,
    Cancelled,
    Category,
    ConversionError,
    DetectedFormat,
    Preset,
)
from konverter.core import budget, naming
from konverter.core.grouping import Group, common_targets
from konverter.core.jobs import JobManager, JobStatus
from konverter.strings import tr

FONT = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"
pytestmark = pytest.mark.skipif(not FONT.exists(), reason="нужен шрифт Arial с кириллицей")

CONV = pdf.PdfConverter()
# A4 в пунктах (fpdf2 по умолчанию).
A4 = (595.28, 841.89)


# --- Генерация PDF ----------------------------------------------------------------


def make_pdf(path: Path, texts: list[str], *, size: str = "A4") -> Path:
    """PDF с текстом: по одной строке на страницу."""
    doc = FPDF(format=size)
    doc.add_font("Arial", fname=str(FONT))
    for text in texts:
        doc.add_page()
        doc.set_font("Arial", size=24)
        doc.cell(text=text)
    doc.output(str(path))
    return path


def make_scan(path: Path, pages: int = 2) -> Path:
    """«Скан»: только картинки, без текстового слоя."""
    doc = FPDF()
    for index in range(pages):
        doc.add_page()
        doc.image(Image.new("RGB", (200, 280), (40 * index, 120, 200)), x=10, y=10, w=150)
    doc.output(str(path))
    return path


def page_texts(path: Path) -> list[str]:
    doc = pdfium.PdfDocument(str(path))
    try:
        result = []
        for index in range(len(doc)):
            page = doc[index]
            textpage = page.get_textpage()
            result.append(textpage.get_text_range().strip())
            textpage.close()
            page.close()
        return result
    finally:
        doc.close()


def target(tid: str):
    return next(t for t in CONV.targets("pdf") if t.id == tid)


def run(sources, dst: Path, tid: str, preset=Preset.MEDIUM, progress=None, cancel=None) -> Path:
    """`convert` с подготовкой dst, как это делает ядро (файл или папка)."""
    t = target(tid)
    sources = [Path(s) for s in (sources if isinstance(sources, (list, tuple)) else [sources])]
    if t.extension:
        Path(naming.os_path(dst)).touch()
    else:
        os.makedirs(naming.os_path(dst), exist_ok=True)
    CONV.convert(
        sources, dst, t, preset, progress or (lambda _p: None), cancel or CancelToken(),
        formats=["pdf"] * len(sources),
    )
    return dst


def no_children() -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not multiprocessing.active_children():
            return True
        time.sleep(0.05)
    return False


class _Reporter:
    def __init__(self) -> None:
        self.values: list[float] = []

    def __call__(self, fraction, *marks) -> None:
        if fraction is not None:
            self.values.append(fraction)

    def final(self) -> None:
        pass


@pytest.fixture
def inproc(monkeypatch):
    """`run_task` → `pdf.child_task` в процессе тестов (ошибки — как у родителя)."""
    calls: list[dict] = []

    def fake_run_task(task, args, *, cancel, progress=None, stall_timeout, max_time,
                      memory_limit=None, pool="long", hold=None):
        assert task == "pdf" and pool == "pdf"
        calls.append(args)
        with (hold() if hold is not None else nullcontext()):
            try:
                result = pdf.child_task(args, _Reporter())
            except _isolated.TaskError as exc:
                raise _isolated.TaskFailed(exc.key, exc.params) from exc
        return result

    monkeypatch.setattr(_isolated, "run_task", fake_run_task)
    return calls


@pytest.fixture
def three(tmp_path) -> Path:
    return make_pdf(tmp_path / "три стр.pdf", ["Привет, страница один", "Вторая страница", "Третья — ёж"])


# --- Варианты --------------------------------------------------------------------


def test_targets_order_labels_and_flags():
    targets = CONV.targets("pdf")
    assert [t.id for t in targets] == list(pdf.TARGET_ORDER) == [
        "pdf_pages_png", "pdf_pages_jpg", "pdf_txt", "pdf_merge", "pdf_split",
    ]
    assert [t.label for t in targets] == [
        "Страницы в PNG", "Страницы в JPG", "Текст (TXT)", "Объединить в один PDF",
        "Разделить на страницы",
    ]
    assert all(t.label == tr(f"target_{t.id}") for t in targets)
    assert all(t.is_long for t in targets)
    by_id = {t.id: t for t in targets}
    assert by_id["pdf_pages_png"].extension == by_id["pdf_pages_jpg"].extension == ""
    assert by_id["pdf_split"].extension == ""
    assert by_id["pdf_txt"].extension == "txt" and not by_id["pdf_txt"].uses_preset
    assert by_id["pdf_merge"].extension == "pdf" and by_id["pdf_merge"].many_to_one
    assert by_id["pdf_pages_png"].uses_preset and by_id["pdf_pages_jpg"].uses_preset
    assert CONV.targets("png") == [] and CONV.targets("") == []
    assert REGISTRY[Category.PDF].__class__ is pdf.PdfConverter


def _group(count: int) -> Group:
    fmt = DetectedFormat("pdf", Category.PDF, "PDF")
    return Group(Category.PDF, [(Path(f"f{i}.pdf"), fmt) for i in range(count)])


def test_merge_only_for_two_or_more_files():
    one = [t.id for t in common_targets(_group(1))]
    two = [t.id for t in common_targets(_group(2))]
    assert "pdf_merge" not in one
    assert one == ["pdf_pages_png", "pdf_pages_jpg", "pdf_txt", "pdf_split"]
    assert two == list(pdf.TARGET_ORDER)


def test_min_files_is_optional_for_other_converters():
    """Без атрибута `min_files` (как у картинок) ничего не скрывается."""
    images = REGISTRY[Category.IMAGE]
    assert getattr(images, "min_files", None) is None
    fmt = DetectedFormat("jpg", Category.IMAGE, "JPEG")
    single = common_targets(Group(Category.IMAGE, [(Path("a.jpg"), fmt)]))
    assert "pdf_single" in [t.id for t in single]


# --- П15 -----------------------------------------------------------------------


def test_p15_pages_png_real_child(three, tmp_path):
    out = run(three, tmp_path / "png", "pdf_pages_png", Preset.SMALL)
    names = sorted(os.listdir(out))
    assert names == ["стр-001.png", "стр-002.png", "стр-003.png"]
    for name in names:
        with Image.open(out / name) as image:
            assert image.format == "PNG" and image.mode == "RGB"
            assert abs(image.width - A4[0] * 96 / 72) <= 2
            assert abs(image.height - A4[1] * 96 / 72) <= 2
    assert no_children()


def test_p15_pages_jpg_sizes_by_dpi(three, tmp_path, inproc):
    for preset, dpi in ((Preset.HIGH, 300), (Preset.MEDIUM, 150)):
        out = run(three, tmp_path / f"jpg-{dpi}", "pdf_pages_jpg", preset)
        names = sorted(os.listdir(out))
        assert names == ["стр-001.jpg", "стр-002.jpg", "стр-003.jpg"]
        with Image.open(out / names[0]) as image:
            assert image.format == "JPEG"
            assert abs(image.width - A4[0] * dpi / 72) <= 2
            assert abs(image.height - A4[1] * dpi / 72) <= 2


def test_presets_png_size_order(three, tmp_path, inproc):
    sizes = {}
    for preset in (Preset.SMALL, Preset.MEDIUM, Preset.HIGH):
        out = run(three, tmp_path / preset.value, "pdf_pages_png", preset)
        sizes[preset] = (out / "стр-001.png").stat().st_size
    assert sizes[Preset.SMALL] < sizes[Preset.MEDIUM] < sizes[Preset.HIGH]


def test_page_number_width(tmp_path, inproc):
    src = make_pdf(tmp_path / "много.pdf", [f"с{i}" for i in range(12)])
    out = run(src, tmp_path / "out", "pdf_split")
    names = sorted(os.listdir(out))
    assert names[0] == "стр-001.pdf" and names[-1] == "стр-012.pdf" and len(names) == 12


def test_p15_text_real_child(three, tmp_path):
    out = run(three, tmp_path / "текст.txt", "pdf_txt")
    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert [line for line in text.splitlines() if line.startswith("--- ")] == [
        "--- Страница 1 ---", "--- Страница 2 ---", "--- Страница 3 ---",
    ]
    assert "Привет, страница один" in text and "Вторая страница" in text and "Третья — ёж" in text
    assert text.index("Привет") < text.index("Вторая") < text.index("Третья")


def test_p15_merge_order(tmp_path, inproc):
    a = make_pdf(tmp_path / "а.pdf", ["А1"])
    b = make_pdf(tmp_path / "б.pdf", ["Б1", "Б2"])
    c = make_pdf(tmp_path / "в.pdf", ["В1", "В2", "В3"])
    out = run([a, b, c], tmp_path / "итог.pdf", "pdf_merge")
    assert page_texts(out) == ["А1", "Б1", "Б2", "В1", "В2", "В3"]
    out2 = run([c, a], tmp_path / "итог2.pdf", "pdf_merge")
    assert page_texts(out2) == ["В1", "В2", "В3", "А1"]


def test_p15_split(three, tmp_path, inproc):
    out = run(three, tmp_path / "части", "pdf_split")
    names = sorted(os.listdir(out))
    assert names == ["стр-001.pdf", "стр-002.pdf", "стр-003.pdf"]
    assert [page_texts(out / n) for n in names] == [
        ["Привет, страница один"], ["Вторая страница"], ["Третья — ёж"],
    ]


def test_text_of_scan_is_error(tmp_path, inproc):
    src = make_scan(tmp_path / "скан.pdf")
    with pytest.raises(ConversionError) as info:
        run(src, tmp_path / "скан.txt", "pdf_txt")
    assert str(info.value) == (
        "В PDF нет текстового слоя (похоже на скан); распознавание текста не поддерживается"
    )
    # Рендер скана — работает.
    out = run(src, tmp_path / "кадры", "pdf_pages_png", Preset.SMALL)
    assert len(os.listdir(out)) == 2


# --- Шифрование ------------------------------------------------------------------


def _encrypt(src: Path, dst: Path, user: str) -> Path:
    with pikepdf.open(src) as doc:
        doc.save(dst, encryption=pikepdf.Encryption(
            owner="владелец-1", user=user,
            allow=pikepdf.Permissions(extract=False, modify_other=False, print_highres=False),
        ))
    return dst


@pytest.mark.parametrize("tid", ["pdf_pages_png", "pdf_txt", "pdf_split", "pdf_merge"])
def test_password_to_open_is_error(three, tmp_path, inproc, tid):
    src = _encrypt(three, tmp_path / "закрыт.pdf", user="secret")
    sources = [src, src] if tid == "pdf_merge" else [src]
    t = target(tid)
    with pytest.raises(ConversionError) as info:
        run(sources, tmp_path / ("out.pdf" if t.extension == "pdf" else
                                 "out.txt" if t.extension else "out"), tid)
    assert "PDF защищён паролем" in str(info.value)
    assert "не поддерживается" in str(info.value)


def test_owner_password_only_works(three, tmp_path, inproc):
    src = _encrypt(three, tmp_path / "права.pdf", user="")
    assert len(os.listdir(run(src, tmp_path / "png", "pdf_pages_png", Preset.SMALL))) == 3
    assert "Вторая страница" in run(src, tmp_path / "т.txt", "pdf_txt").read_text("utf-8")
    parts = run(src, tmp_path / "части", "pdf_split")
    assert page_texts(parts / "стр-002.pdf") == ["Вторая страница"]
    merged = run([src, three], tmp_path / "вместе.pdf", "pdf_merge")
    assert len(page_texts(merged)) == 6
    with pikepdf.open(merged) as doc:
        assert not doc.is_encrypted


def test_password_real_child(three, tmp_path):
    src = _encrypt(three, tmp_path / "закрыт.pdf", user="secret")
    with pytest.raises(ConversionError, match="PDF защищён паролем"):
        run(src, tmp_path / "out", "pdf_pages_png", Preset.SMALL)


# --- Активное содержимое и метаданные ---------------------------------------------

MARKERS = (b"JS-MARKER", b"SECRET-ATTACHMENT-42", b"AUTHOR-MARKER", b"PRODUCER-MARKER",
           b"XMP-MARKER", b"PAGE-XMP-MARKER", b"FIELD-VALUE-MARKER", b"calc.exe")


def make_active(tmp_path: Path) -> Path:
    base = make_pdf(tmp_path / "основа.pdf", ["Активная один", "Активная два"])
    dst = tmp_path / "активный.pdf"
    with pikepdf.open(base) as doc:
        js = doc.make_indirect(Dictionary(S=Name.JavaScript, JS=String("app.alert('JS-MARKER')")))
        ef = pikepdf.Stream(doc, b"SECRET-ATTACHMENT-42")
        ef.Type = Name.EmbeddedFile
        filespec = doc.make_indirect(Dictionary(
            Type=Name.Filespec, F=String("secret.txt"), EF=Dictionary(F=ef),
        ))
        doc.Root.OpenAction = js
        doc.Root.Names = Dictionary(
            JavaScript=Dictionary(Names=Array([String("init"), js])),
            EmbeddedFiles=Dictionary(Names=Array([String("secret.txt"), filespec])),
        )
        doc.Root.AA = Dictionary(WC=js)
        doc.docinfo["/Author"] = "AUTHOR-MARKER"
        doc.docinfo["/Producer"] = "PRODUCER-MARKER"
        with doc.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["dc:title"] = "XMP-MARKER"
        page = doc.pages[0].obj
        page.AA = Dictionary(O=js)
        page.Metadata = pikepdf.Stream(doc, b"<x:xmpmeta>PAGE-XMP-MARKER</x:xmpmeta>")
        field = doc.make_indirect(Dictionary(
            FT=Name.Tx, T=String("secretfield"), V=String("FIELD-VALUE-MARKER"),
        ))
        widget = doc.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Widget, Rect=[100, 100, 200, 120],
            Parent=field, AA=Dictionary(K=js), A=js,
        ))
        field.Kids = Array([widget])
        doc.Root.AcroForm = Dictionary(Fields=Array([field]))
        page.Annots = Array([
            doc.make_indirect(Dictionary(
                Type=Name.Annot, Subtype=Name.Link, Rect=[0, 0, 50, 50],
                A=Dictionary(S=Name.JavaScript, JS=String("JS-MARKER-2")),
            )),
            doc.make_indirect(Dictionary(
                Type=Name.Annot, Subtype=Name.Link, Rect=[60, 0, 110, 50],
                A=Dictionary(S=Name.URI, URI=String("https://example.org/"), Next=js),
            )),
            doc.make_indirect(Dictionary(
                Type=Name.Annot, Subtype=Name.Link, Rect=[120, 0, 170, 50],
                A=Dictionary(S=Name.Launch, F=String("calc.exe")),
            )),
            doc.make_indirect(Dictionary(
                Type=Name.Annot, Subtype=Name.FileAttachment, Rect=[180, 0, 200, 20], FS=filespec,
            )),
            widget,
        ])
        doc.save(dst)
    return dst


_FORBIDDEN_KEYS = {"/AA", "/JS", "/OpenAction", "/Names", "/EmbeddedFiles", "/Metadata",
                   "/AcroForm", "/EF", "/FS", "/JavaScript", "/Outlines", "/PieceInfo", "/Next"}


def _all_objects(doc: pikepdf.Pdf):
    """Все словари/потоки файла, включая вложенные прямые объекты."""
    seen: set = set()
    stack = list(doc.objects) + [doc.trailer]
    while stack:
        obj = stack.pop()
        if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream, pikepdf.Array)):
            if obj.is_indirect:
                if obj.objgen in seen:
                    continue
                seen.add(obj.objgen)
            if isinstance(obj, pikepdf.Array):
                stack.extend(obj)
                continue
            yield obj
            for key in obj.keys():
                if key == "/Parent":
                    continue
                stack.append(obj.get(key))


def assert_clean(path: Path) -> None:
    raw = path.read_bytes()
    for marker in MARKERS:
        assert marker not in raw, marker
    with pikepdf.open(path) as doc:
        assert "/Info" not in doc.trailer
        assert set(doc.Root.keys()) <= {"/Type", "/Pages"}
        uris = 0
        for obj in _all_objects(doc):
            assert not (_FORBIDDEN_KEYS & set(obj.keys())), set(obj.keys())
            if "/S" in obj:
                assert str(obj.S) in {"/GoTo", "/URI", "/Named"}, obj.S
                uris += str(obj.S) == "/URI"
            if obj.get("/Subtype") == Name.Widget:
                assert "/Parent" not in obj
            assert obj.get("/Subtype") != Name.FileAttachment
            if isinstance(obj, pikepdf.Stream):
                data = obj.read_bytes()
                for marker in MARKERS:
                    assert marker not in data, marker
        assert uris == 1  # ссылка URI (содержимое документа) осталась


def test_merge_removes_active_content_and_metadata(tmp_path, inproc):
    src = make_active(tmp_path)
    plain = make_pdf(tmp_path / "простой.pdf", ["Простой"])
    out = run([src, plain], tmp_path / "итог.pdf", "pdf_merge")
    assert page_texts(out) == ["Активная один", "Активная два", "Простой"]
    assert_clean(out)


def test_split_removes_active_content_and_metadata(tmp_path, inproc):
    src = make_active(tmp_path)
    out = run(src, tmp_path / "части", "pdf_split")
    first = out / "стр-001.pdf"
    assert page_texts(first) == ["Активная один"]
    assert_clean(first)
    with pikepdf.open(out / "стр-002.pdf") as doc:
        assert "/Info" not in doc.trailer and set(doc.Root.keys()) <= {"/Type", "/Pages"}


def test_split_does_not_carry_other_pages(tmp_path, inproc):
    """Ссылка на страницу 3 со страницы 1: в файл страницы 1 страница 3 не попадает."""
    base = make_pdf(tmp_path / "база.pdf", ["Первая", "Вторая", "ТРЕТЬЯ-СЕКРЕТ"])
    src = tmp_path / "ссылки.pdf"
    with pikepdf.open(base) as doc:
        link = doc.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Link, Rect=[0, 0, 10, 10],
            Dest=Array([doc.pages[2].obj, Name.Fit]),
        ))
        doc.pages[0].obj.Annots = Array([link])
        doc.save(src)
    out = run(src, tmp_path / "части", "pdf_split")
    with pikepdf.open(out / "стр-001.pdf") as doc:
        pages = [o for o in doc.objects
                 if isinstance(o, pikepdf.Dictionary) and o.get("/Type") == Name.Page]
        assert len(pages) == 1
    # Внутри объединения ссылка на страницу сохраняется и ведёт в дерево страниц.
    merged = run([src, base], tmp_path / "м.pdf", "pdf_merge")
    with pikepdf.open(merged) as doc:
        dest = doc.pages[0].obj.Annots[0].Dest[0]
        assert dest.objgen == doc.pages[2].obj.objgen


def test_render_does_not_init_forms(tmp_path, inproc, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("init_forms вызывать нельзя")

    monkeypatch.setattr(pdfium.PdfDocument, "init_forms", forbidden)
    src = make_active(tmp_path)
    out = run(src, tmp_path / "кадры", "pdf_pages_png", Preset.SMALL)
    assert len(os.listdir(out)) == 2
    assert "Активная один" in run(src, tmp_path / "т.txt", "pdf_txt").read_text("utf-8")
    # В сборке pdfium нет движка JavaScript (V8) и XFA.
    flags = {str(f).upper() for f in pdfium.version.PDFIUM_INFO.flags}
    assert "V8" not in flags and "XFA" not in flags


# --- Лимиты ---------------------------------------------------------------------


def _big_mediabox(tmp_path: Path, side: float) -> Path:
    base = make_pdf(tmp_path / "база.pdf", ["Огромная"])
    dst = tmp_path / "огромная.pdf"
    with pikepdf.open(base) as doc:
        doc.pages[0].obj.MediaBox = Array([0, 0, side, side])
        doc.save(dst)
    return dst


def test_huge_mediabox_is_downscaled(tmp_path, inproc, monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PAGE_PIXELS", 2_000_000)
    src = _big_mediabox(tmp_path, 200_000)
    out = run(src, tmp_path / "кадры", "pdf_pages_png", Preset.HIGH)
    with Image.open(out / "стр-001.png") as image:
        assert image.width * image.height <= 2_000_000
        assert image.width > 1000  # уменьшили до предела, а не до точки


def test_page_side_limit(tmp_path, inproc, monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PAGE_SIDE", 500)
    base = make_pdf(tmp_path / "лента.pdf", ["Лента"])
    src = tmp_path / "лента2.pdf"
    with pikepdf.open(base) as doc:
        doc.pages[0].obj.MediaBox = Array([0, 0, 14000, 100])
        doc.save(src)
    out = run(src, tmp_path / "кадры", "pdf_pages_jpg", Preset.HIGH)
    with Image.open(out / "стр-001.jpg") as image:
        assert max(image.size) <= 500


def test_total_pixels_limit(three, tmp_path, inproc, monkeypatch):
    monkeypatch.setattr(pdf, "MAX_TOTAL_PIXELS", 1_500_000)  # ~2 страницы при 96 dpi
    with pytest.raises(ConversionError, match="Слишком большой объём рендера"):
        run(three, tmp_path / "кадры", "pdf_pages_png", Preset.SMALL)


@pytest.mark.parametrize("tid", ["pdf_pages_png", "pdf_txt", "pdf_split", "pdf_merge"])
def test_too_many_pages(tmp_path, inproc, monkeypatch, tid):
    monkeypatch.setattr(pdf, "MAX_PAGES", 5)
    src = make_pdf(tmp_path / "шесть.pdf", [str(i) for i in range(6)])
    sources = [src] if tid != "pdf_merge" else [make_pdf(tmp_path / "три.pdf", ["1", "2", "3"])] * 2
    t = target(tid)
    dst = tmp_path / ("out." + t.extension if t.extension else "out")
    with pytest.raises(ConversionError) as info:
        run(sources, dst, tid)
    assert str(info.value) == "Слишком много страниц: 6, максимум 5"


def test_input_size_and_file_count_limits(three, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "MAX_INPUT_BYTES", 100)
    with pytest.raises(ConversionError, match="Слишком большой файл"):
        run(three, tmp_path / "out", "pdf_split")
    monkeypatch.setattr(pdf, "MAX_INPUT_BYTES", 2**30)
    monkeypatch.setattr(pdf, "MAX_MERGE_FILES", 2)
    with pytest.raises(ConversionError, match="Слишком много файлов для объединения"):
        run([three] * 3, tmp_path / "m.pdf", "pdf_merge")
    monkeypatch.setattr(pdf, "MAX_MERGE_BYTES", three.stat().st_size + 1)
    with pytest.raises(ConversionError, match="Слишком большие файлы для объединения"):
        run([three] * 2, tmp_path / "m2.pdf", "pdf_merge")


def test_disk_space_checks(three, tmp_path, inproc, monkeypatch):
    class Usage:
        def __init__(self, free):
            self.free = free

    monkeypatch.setattr(pdf.shutil, "disk_usage", lambda _p: Usage(100 * 2**20))
    with pytest.raises(ConversionError, match="Недостаточно места на диске: нужно"):
        run(three, tmp_path / "out", "pdf_pages_png", Preset.SMALL)
    # Место есть, но кончается во время записи.
    monkeypatch.setattr(pdf, "DISK_RESERVE", 0)
    monkeypatch.setattr(pdf, "DISK_CHECK_BYTES", 1)
    with pytest.raises(ConversionError, match="свободно меньше"):
        run(three, tmp_path / "out2", "pdf_split")


def test_output_size_limit(three, tmp_path, inproc, monkeypatch):
    monkeypatch.setattr(pdf, "MAX_OUTPUT_BYTES", 5_000)
    with pytest.raises(ConversionError, match="Слишком большой результат"):
        run([three, three], tmp_path / "m.pdf", "pdf_merge")
    with pytest.raises(ConversionError, match="Слишком большой результат"):
        run(three, tmp_path / "кадры", "pdf_pages_png", Preset.HIGH)


def test_render_holds_image_budget(three, tmp_path, monkeypatch):
    """Рендер резервирует пиксели в общем бюджете на время работы ребёнка."""
    test_budget = budget.PixelBudget(400_000_000)
    monkeypatch.setattr(budget, "IMAGE_BUDGET", test_budget)
    seen: list[int] = []
    run(three, tmp_path / "кадры", "pdf_pages_png", Preset.SMALL,
        progress=lambda _p: seen.append(test_budget.used))
    assert seen and all(value == pdf.MAX_PAGE_PIXELS for value in seen[:-1])
    assert test_budget.used == 0
    seen.clear()
    run(three, tmp_path / "т.txt", "pdf_txt", progress=lambda _p: seen.append(test_budget.used))
    assert set(seen) == {0}


def test_formats_and_target_checks(three, tmp_path):
    t = target("pdf_split")
    os.makedirs(tmp_path / "o")
    with pytest.raises(ConversionError, match="Внутренняя ошибка"):
        CONV.convert([three], tmp_path / "o", t, Preset.MEDIUM, lambda _p: None, CancelToken(),
                     formats=[])
    with pytest.raises(ConversionError, match="Файл повреждён"):
        CONV.convert([three], tmp_path / "o", t, Preset.MEDIUM, lambda _p: None, CancelToken(),
                     formats=["png"])
    with pytest.raises(ConversionError, match="один файл"):
        CONV.convert([three, three], tmp_path / "o", t, Preset.MEDIUM, lambda _p: None,
                     CancelToken(), formats=["pdf", "pdf"])


# --- «Злые» файлы через настоящий дочерний процесс -----------------------------------


def _evil_files(tmp_path: Path, good: Path) -> dict[str, Path]:
    data = good.read_bytes()
    files = {
        "empty": b"",
        "garbage": os.urandom(5000),
        "header_only": b"%PDF-1.7\n" + os.urandom(3000),
        "truncated": data[: len(data) // 2],
    }
    result = {}
    for name, content in files.items():
        path = tmp_path / f"{name}.pdf"
        path.write_bytes(content)
        result[name] = path
    return result


@pytest.mark.parametrize("tid", ["pdf_pages_png", "pdf_split"])
def test_evil_files_real_child(three, tmp_path, tid):
    for name, path in _evil_files(tmp_path, three).items():
        t0 = time.monotonic()
        try:
            run(path, tmp_path / f"out-{tid}-{name}", tid, Preset.SMALL)
        except ConversionError as exc:
            assert str(exc), name
            if name in ("empty", "garbage"):
                assert str(exc) == pdf.MSG_DAMAGED
        else:
            assert name == "truncated", name  # обрезанный файл могут «починить»
        assert time.monotonic() - t0 < 60
    assert no_children()


def _cyclic_pdf(tmp_path: Path) -> Path:
    """Цикл объектов в ресурсах, глубокая вложенность и цикл в дереве страниц."""
    base = make_pdf(tmp_path / "база.pdf", ["Цикл"])
    dst = tmp_path / "цикл.pdf"
    with pikepdf.open(base) as doc:
        loop = doc.make_indirect(Dictionary(Type=Name.Loop))
        loop.Self = loop
        loop.Other = doc.make_indirect(Dictionary(Back=loop))
        deep = Array([1])
        for _ in range(200):
            deep = Array([deep])
        page = doc.pages[0].obj
        page.Loop = loop
        page.Deep = deep
        page.Resources.Loop = loop
        doc.save(dst)
    return dst


def test_cycles_and_nesting(tmp_path):
    src = _cyclic_pdf(tmp_path)
    for tid in ("pdf_split", "pdf_pages_png"):
        t0 = time.monotonic()
        out = run(src, tmp_path / f"out-{tid}", tid, Preset.SMALL)
        assert len(os.listdir(out)) == 1
        assert time.monotonic() - t0 < 60
    # Цикл в дереве страниц (Pages ссылается сам на себя) — без зависания.
    tree = tmp_path / "дерево.pdf"
    tree.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[2 0 R 3 0 R]/Count 2>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 100 100]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    for tid in ("pdf_split", "pdf_pages_png"):
        t0 = time.monotonic()
        try:
            run(tree, tmp_path / f"tree-{tid}", tid, Preset.SMALL)
        except ConversionError:
            pass
        assert time.monotonic() - t0 < 60
    assert no_children()


def test_child_crash_is_conversion_error(three, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "_TASK", "test_abort")
    with pytest.raises(ConversionError) as info:
        run(three, tmp_path / "out", "pdf_pages_png")
    assert str(info.value) == pdf.MSG_DAMAGED
    assert no_children()


def test_child_hang_is_stall(three, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "_TASK", "test_hang")
    monkeypatch.setattr(pdf, "STALL_TIMEOUT", 1.5)
    t0 = time.monotonic()
    with pytest.raises(ConversionError, match=_isolated.MSG_STALLED):
        run(three, tmp_path / "out", "pdf_split")
    assert time.monotonic() - t0 < 20
    assert no_children()


def test_cancel_render_via_job_manager(tmp_path):
    """Отмена рендера многостраничного PDF: Cancelled < 2 с, временная папка убрана."""
    folder = tmp_path / "work"
    folder.mkdir()
    src = make_pdf(folder / "толстый.pdf", [f"Страница {i}" for i in range(80)])
    started = threading.Event()

    def on_update(job):
        if job.progress > 0:
            started.set()

    manager = JobManager(max_workers=1, on_update=on_update)
    try:
        job = manager.submit([src], CONV, target("pdf_pages_png"), Preset.HIGH, folder,
                             formats=["pdf"])
        assert started.wait(60), "рендер не начался"
        assert job.status == JobStatus.RUNNING
        t0 = time.monotonic()
        manager.cancel(job.id)
        while not job.is_finished and time.monotonic() - t0 < 10:
            time.sleep(0.01)
        assert job.status == JobStatus.CANCELLED
        assert time.monotonic() - t0 < 2.0
    finally:
        manager.shutdown(timeout=10)
    assert no_children()
    assert sorted(p.name for p in folder.iterdir()) == ["толстый.pdf"]


def test_cancel_direct(tmp_path):
    src = make_pdf(tmp_path / "толстый.pdf", [f"Страница {i}" for i in range(80)])
    token = CancelToken()
    errors: list[BaseException] = []
    progressed = threading.Event()

    def work():
        try:
            run(src, tmp_path / "out", "pdf_pages_png", Preset.HIGH,
                progress=lambda _p: progressed.set(), cancel=token)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    assert progressed.wait(60)
    t0 = time.monotonic()
    token.cancel()
    thread.join(10)
    assert time.monotonic() - t0 < 2.0
    assert errors and isinstance(errors[0], Cancelled)
    assert no_children()


# --- Счётчик мест "pdf" и hold в _isolated ----------------------------------------------


def test_pdf_pool_independent_of_long_pool(monkeypatch):
    monkeypatch.setattr(_isolated, "LONG_MAX_CHILDREN", 1)
    token = CancelToken()
    blocker = threading.Thread(
        target=lambda: pytest.raises(
            Cancelled, _isolated.run_task, "test_hang", {}, cancel=token,
            stall_timeout=60, max_time=60,
        ),
    )
    blocker.start()
    try:
        deadline = time.monotonic() + 30
        while _isolated._slots_used["long"] < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        held: list[int] = []

        class Hold:
            def __enter__(self):
                held.append(_isolated._slots_used["pdf"])

            def __exit__(self, *exc):
                held.append(-1)

        result = _isolated.run_task(
            "test_progress", {"steps": 3}, cancel=CancelToken(), stall_timeout=30,
            max_time=60, pool="pdf", hold=Hold,
        )
        assert result == {"steps": 3}
        assert held == [1, -1]  # hold берётся ПОСЛЕ места и отпускается
        assert _isolated._slots_used["pdf"] == 0
    finally:
        token.cancel()
        blocker.join(10)
    with pytest.raises(ValueError):
        _isolated.run_task("test_progress", {}, cancel=CancelToken(), stall_timeout=1,
                           max_time=1, pool="short")
    assert no_children()


# --- Имена, пути, сквозной JobManager ----------------------------------------------------


def test_unicode_and_long_path(tmp_path):
    base = naming.os_path(tmp_path / ("д" * 120) / ("е" * 120))
    os.makedirs(base)
    src = os.path.join(base, "отчёт с пробелами — №1.pdf")
    make_pdf(Path(src), ["Длинный путь", "Вторая"])
    out = naming.make_temp_dir(Path(base))
    assert len(str(out)) > 260 and str(out).startswith("\\\\?\\")
    CONV.convert([Path(src)], out, target("pdf_pages_png"), Preset.SMALL, lambda _p: None,
                 CancelToken(), formats=["pdf"])
    assert sorted(os.listdir(out)) == ["стр-001.png", "стр-002.png"]
    txt = os.path.join(base, ".~текст.part.txt")
    open(txt, "wb").close()
    CONV.convert([Path(src)], Path(txt), target("pdf_txt"), Preset.SMALL, lambda _p: None,
                 CancelToken(), formats=["pdf"])
    with open(txt, encoding="utf-8") as fh:
        assert "Длинный путь" in fh.read()
    naming.cleanup(out)


def _wait_jobs(jobs, timeout=90.0) -> None:
    deadline = time.monotonic() + timeout
    while not all(j.is_finished for j in jobs) and time.monotonic() < deadline:
        time.sleep(0.05)


def test_job_manager_png_folder_and_conflict(tmp_path):
    folder = tmp_path / "документы"
    folder.mkdir()
    src = make_pdf(folder / "мой отчёт.pdf", ["Один", "Два"])
    busy = folder / "мой отчёт"
    busy.mkdir()
    (busy / "чужой.txt").write_text("не трогать", encoding="utf-8")
    manager = JobManager(max_workers=2)
    try:
        jobs = [manager.submit([src], CONV, target("pdf_pages_png"), Preset.SMALL, folder,
                               formats=["pdf"])]
        _wait_jobs(jobs)
        jobs.append(manager.submit([src], CONV, target("pdf_split"), Preset.SMALL, folder,
                                   formats=["pdf"]))
        _wait_jobs(jobs)
    finally:
        manager.shutdown(timeout=10)
    assert [j.status for j in jobs] == [JobStatus.DONE] * 2, [j.error_message for j in jobs]
    assert jobs[0].result_path == folder / "мой отчёт (1)"
    assert jobs[1].result_path == folder / "мой отчёт (2)"
    assert jobs[0].result_path.is_dir()
    assert sorted(os.listdir(jobs[0].result_path)) == ["стр-001.png", "стр-002.png"]
    assert sorted(os.listdir(jobs[1].result_path)) == ["стр-001.pdf", "стр-002.pdf"]
    assert sorted(os.listdir(busy)) == ["чужой.txt"]
    assert sorted(p.name for p in folder.iterdir()) == [
        "мой отчёт", "мой отчёт (1)", "мой отчёт (2)", "мой отчёт.pdf",
    ]
    assert no_children()


def test_job_manager_merge_two(tmp_path):
    folder = tmp_path / "папка"
    folder.mkdir()
    first = make_pdf(folder / "первый.pdf", ["П1"])
    second = make_pdf(folder / "второй.pdf", ["В1", "В2"])
    manager = JobManager(max_workers=1)
    try:
        job = manager.submit([first, second], CONV, target("pdf_merge"), Preset.MEDIUM, folder,
                             formats=["pdf", "pdf"])
        _wait_jobs([job])
    finally:
        manager.shutdown(timeout=10)
    assert job.status == JobStatus.DONE, job.error_message
    # Стем — первого файла; имя «первый.pdf» занято исходником.
    assert job.result_path == folder / "первый (1).pdf"
    assert page_texts(job.result_path) == ["П1", "В1", "В2"]
    assert sorted(p.name for p in folder.iterdir()) == ["второй.pdf", "первый (1).pdf", "первый.pdf"]
    assert no_children()


# --- Ревью этапа 3: вложения, URI, слои, метки аннотаций, сторож при открытии --------


def _stream_data(doc: pikepdf.Pdf) -> bytes:
    """Распакованное содержимое всех потоков файла (для поиска маркеров)."""
    chunks = []
    for obj in doc.objects:
        if isinstance(obj, pikepdf.Stream):
            try:
                chunks.append(obj.read_bytes())
            except pikepdf.PdfError:
                chunks.append(obj.read_raw_bytes())
    return b"".join(chunks)


def _attachment(doc: pikepdf.Pdf, payload: bytes, name: str):
    """Спецификация файла с вложенным потоком `payload` и именем `name`."""
    ef = pikepdf.Stream(doc, payload)
    ef.Type = Name.EmbeddedFile
    return doc.make_indirect(Dictionary(
        Type=Name.Filespec, F=String(name), UF=String(name), EF=Dictionary(F=ef),
    ))


LEAK_MARKERS = (b"PAYLOAD-ONE", b"PAYLOAD-TWO", b"PAYLOAD-THREE", b"PAYLOAD-FOUR",
                b"PAYLOAD-FIVE", b"leak1.txt", b"leak2.txt", b"leak3.txt", b"leak4.txt",
                b"leak5.txt", b"BOGUS-MARKER", b"BOGUS2-MARKER")


def make_leaky(tmp_path: Path) -> Path:
    """Вложения, достижимые не через /Annots: /Parent у Popup, /IRT, свой ключ страницы."""
    base = make_pdf(tmp_path / "основа.pdf", ["Утечка один", "Утечка два"])
    dst = tmp_path / "утечка.pdf"
    with pikepdf.open(base) as doc:
        def annot(subtype, **extra):
            return doc.make_indirect(Dictionary(
                Type=Name.Annot, Subtype=subtype, Rect=[10, 10, 30, 30], **extra,
            ))

        fa1 = annot(Name.FileAttachment, FS=_attachment(doc, b"PAYLOAD-ONE", "leak1.txt"))
        fa2 = annot(Name.FileAttachment, FS=_attachment(doc, b"PAYLOAD-TWO", "leak2.txt"))
        fa3 = annot(Name.FileAttachment, FS=_attachment(doc, b"PAYLOAD-THREE", "leak3.txt"))
        bad_popup = annot(Name.Popup, Parent=fa2)
        reply = annot(Name.Text, IRT=fa3, Contents=String("Ответ"))
        good_text = annot(Name.Text, Contents=String("Комментарий"))
        good_popup = annot(Name.Popup, Parent=good_text)
        good_text.Popup = good_popup
        bogus = annot(Name("/Bogus"), Contents=String("BOGUS-MARKER"))
        # Аннотация без /Type с неизвестным подтипом — родитель всплывающего окна.
        bogus2 = doc.make_indirect(Dictionary(
            Subtype=Name("/Bogus"), Rect=[0, 0, 5, 5], Contents=String("BOGUS2-MARKER"),
        ))
        bogus_popup = annot(Name.Popup, Parent=bogus2)
        square = annot(Name.Square)
        # Вложение по /AF у квадрата.
        square.AF = Array([_attachment(doc, b"PAYLOAD-FIVE", "leak5.txt")])
        page = doc.pages[0].obj
        page.Annots = Array([fa1, bad_popup, reply, good_text, good_popup, bogus, bogus_popup,
                             square])
        page.MyKey = _attachment(doc, b"PAYLOAD-FOUR", "leak4.txt")
        page.Resources.MyRes = Dictionary(Inner=fa2)
        doc.save(dst)
    return dst


def assert_no_leak(path: Path) -> None:
    raw = path.read_bytes()
    with pikepdf.open(path) as doc:
        data = raw + _stream_data(doc)
        for marker in LEAK_MARKERS:
            assert marker not in data, marker
        for obj in _all_objects(doc):
            keys = set(obj.keys())
            assert not ({"/EF", "/RF", "/FS", "/AF"} & keys), keys
            assert obj.get("/Type") not in (Name.Filespec, Name.EmbeddedFile)
            assert obj.get("/Subtype") != Name.FileAttachment
        for obj in doc.objects:
            if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                assert obj.get("/Type") not in (Name.Filespec, Name.EmbeddedFile)


def _subtypes(path: Path) -> list[str]:
    with pikepdf.open(path) as doc:
        return [str(a.get("/Subtype")) for a in doc.pages[0].obj.get("/Annots", [])]


def test_review3_attachments_not_leaked_merge(tmp_path, inproc):
    src = make_leaky(tmp_path)
    plain = make_pdf(tmp_path / "простой.pdf", ["Простой"])
    out = run([src, plain], tmp_path / "итог.pdf", "pdf_merge")
    assert page_texts(out) == ["Утечка один", "Утечка два", "Простой"]
    assert_no_leak(out)
    # Безопасные аннотации остались, пара «комментарий + всплывающее окно» — тоже.
    assert _subtypes(out) == ["/Text", "/Text", "/Popup", "/Square"]
    with pikepdf.open(out) as doc:
        reply, text, popup = list(doc.pages[0].obj.Annots)[:3]
        assert "/IRT" not in reply
        assert text.Popup.objgen == popup.objgen and popup.Parent.objgen == text.objgen
        assert "/MyKey" not in doc.pages[0].obj


def test_review3_attachments_not_leaked_split(tmp_path, inproc):
    src = make_leaky(tmp_path)
    out = run(src, tmp_path / "части", "pdf_split")
    assert_no_leak(out / "стр-001.pdf")
    assert_no_leak(out / "стр-002.pdf")
    assert _subtypes(out / "стр-001.pdf") == ["/Text", "/Text", "/Popup", "/Square"]


_URIS = [
    ("https://ok.example/a", True),
    ("HTTP://ok2.example/", True),
    ("  MailTo:someone@example.org", True),
    ("file:///C:/Windows/system32/calc.exe", False),
    ("\\\\evilhost\\share\\x.exe", False),
    ("//evilhost/share", False),
    ("javascript:alert(1)", False),
    ("JaVaScRiPt:alert(2)", False),
    ("java\tscript:alert(3)", False),
    (" \x01javascript:alert(4)", False),
    ("data:text/html,evil", False),
    ("relative/page.html", False),
    ("", False),
    ("smb://evilhost/x", False),
    ("ms-settings:", False),
]


def make_uris(tmp_path: Path) -> Path:
    base = make_pdf(tmp_path / "основа.pdf", ["Ссылки"])
    dst = tmp_path / "ссылки.pdf"
    with pikepdf.open(base) as doc:
        annots = []
        for index, (uri, _ok) in enumerate(_URIS):
            annots.append(doc.make_indirect(Dictionary(
                Type=Name.Annot, Subtype=Name.Link, Rect=[index * 20, 0, index * 20 + 15, 15],
                A=Dictionary(S=Name.URI, URI=String(uri)),
            )))
        # Не строка вместо URI и /PA.
        annots.append(doc.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Link, Rect=[0, 20, 15, 35],
            A=Dictionary(S=Name.URI, URI=Name("/javascript")),
            PA=Dictionary(S=Name.URI, URI=String("https://pa.example/")),
        )))
        doc.pages[0].obj.Annots = Array(annots)
        doc.save(dst)
    return dst


def _uris(path: Path) -> list[str]:
    with pikepdf.open(path) as doc:
        found = []
        for obj in _all_objects(doc):
            assert "/PA" not in obj
            if obj.get("/S") == Name.URI:
                found.append(str(obj.URI))
        return sorted(found)


@pytest.mark.parametrize("tid", ["pdf_merge", "pdf_split"])
def test_review3_only_web_and_mail_uris(tmp_path, inproc, tid):
    src = make_uris(tmp_path)
    if tid == "pdf_merge":
        out = run([src, src], tmp_path / "итог.pdf", tid)
    else:
        out = run(src, tmp_path / "части", tid) / "стр-001.pdf"
    expected = sorted(uri for uri, ok in _URIS if ok)
    if tid == "pdf_merge":
        expected = sorted(expected * 2)
    assert _uris(out) == expected
    raw = out.read_bytes().lower()
    for marker in (b"calc.exe", b"evilhost", b"javascript", b"data:text", b"relative/",
                   b"ms-settings", b"pa.example"):
        assert marker not in raw, marker


def make_layers(tmp_path: Path, name: str, extra_on: bool = False) -> Path:
    """Страница: чёрный квадрат в ВЫКЛЮЧЕННОМ слое (и, по желанию, серый во включённом)."""
    base = make_pdf(tmp_path / f"{name}-основа.pdf", [name])
    dst = tmp_path / f"{name}.pdf"
    with pikepdf.open(base) as doc:
        hidden = doc.make_indirect(Dictionary(
            Type=Name.OCG, Name=String("Скрытый"),
            Usage=Dictionary(Creator=Dictionary(Creator=String("USER-MARKER"))),
        ))
        shown = doc.make_indirect(Dictionary(Type=Name.OCG, Name=String("Видимый")))
        page = doc.pages[0]
        page.obj.Resources.Properties = Dictionary(oc1=hidden, oc2=shown)
        content = b"/OC /oc1 BDC 0 0 0 rg 100 100 300 300 re f EMC\n"
        if extra_on:
            content += b"/OC /oc2 BDC 0.5 0.5 0.5 rg 100 500 100 100 re f EMC\n"
        page.contents_add(pikepdf.Stream(doc, content))
        js = doc.make_indirect(Dictionary(S=Name.JavaScript, JS=String("JS-MARKER")))
        doc.Root.OCProperties = Dictionary(
            OCGs=Array([hidden, shown]),
            D=Dictionary(
                Name=String("CONFIG-NAME-MARKER"), BaseState=Name.ON,
                OFF=Array([hidden]), ON=Array([shown]),
                Order=Array([String("Группа"), Array([hidden, shown])]),
                RBGroups=Array([Array([hidden, shown])]),
                AS=Array([Dictionary(Event=Name.View, Category=Array([Name.View]),
                                     OCGs=Array([hidden]))]),
            ),
            Configs=Array([Dictionary(OFF=Array([]), AS=Array([js]))]),
        )
        doc.save(dst)
    return dst


def _render_first(path: Path, index: int = 0):
    doc = pdfium.PdfDocument(str(path))
    try:
        page = doc[index]
        image = page.render(scale=1, may_draw_forms=False).to_pil().convert("L")
        page.close()
        return image
    finally:
        doc.close()


def _hidden_area_white(path: Path, index: int = 0) -> bool:
    image = _render_first(path, index)
    # Квадрат 100..400 по обеим осям (ось y pdfium — сверху вниз).
    box = (150, round(A4[1]) - 350, 350, round(A4[1]) - 150)
    return image.crop(box).getextrema()[0] > 200


def _shown_area_gray(path: Path, index: int = 0) -> bool:
    image = _render_first(path, index)
    box = (120, round(A4[1]) - 580, 180, round(A4[1]) - 520)
    lo, hi = image.crop(box).getextrema()
    return 100 < lo and hi < 160


def _assert_safe_oc(path: Path) -> None:
    raw = path.read_bytes()
    for marker in (b"JS-MARKER", b"USER-MARKER", b"CONFIG-NAME-MARKER"):
        assert marker not in raw, marker
    with pikepdf.open(path) as doc:
        assert set(doc.Root.keys()) <= {"/Type", "/Pages", "/OCProperties"}
        oc = doc.Root.OCProperties
        assert set(oc.keys()) <= {"/OCGs", "/D"}
        assert set(oc.D.keys()) <= {"/BaseState", "/ON", "/OFF", "/Order", "/RBGroups"}
        for ocg in oc.OCGs:
            assert ocg.Type == Name.OCG and set(ocg.keys()) <= {"/Type", "/Name", "/Intent"}


def test_review3_hidden_layers_stay_hidden(tmp_path, inproc):
    first = make_layers(tmp_path, "Первый", extra_on=True)
    second = make_layers(tmp_path, "Второй")
    assert _hidden_area_white(first) and _shown_area_gray(first)  # исходник
    merged = run([first, second], tmp_path / "итог.pdf", "pdf_merge")
    _assert_safe_oc(merged)
    assert _hidden_area_white(merged) and _shown_area_gray(merged)
    assert _hidden_area_white(merged, 1)  # страница второго исходника
    with pikepdf.open(merged) as doc:
        assert len(doc.Root.OCProperties.OCGs) == 4
        assert len(doc.Root.OCProperties.D.OFF) == 2
    parts = run(first, tmp_path / "части", "pdf_split")
    _assert_safe_oc(parts / "стр-001.pdf")
    assert _hidden_area_white(parts / "стр-001.pdf") and _shown_area_gray(parts / "стр-001.pdf")


def test_review3_base_state_off(tmp_path, inproc):
    """/BaseState /OFF: всё, что не в /ON, выключено — и после объединения тоже."""
    src = make_layers(tmp_path, "База", extra_on=True)
    with pikepdf.open(src, allow_overwriting_input=True) as doc:
        d = doc.Root.OCProperties.D
        d.BaseState = Name.OFF
        del d["/OFF"]
        d.ON = Array([doc.Root.OCProperties.OCGs[1]])
        doc.save(src)
    assert _hidden_area_white(src) and _shown_area_gray(src)
    plain = make_pdf(tmp_path / "простой.pdf", ["Простой"])
    merged = run([plain, src], tmp_path / "итог.pdf", "pdf_merge")
    assert _hidden_area_white(merged, 1) and _shown_area_gray(merged, 1)


def test_review3_markup_author_removed(tmp_path, inproc):
    base = make_pdf(tmp_path / "основа.pdf", ["Комментарии"])
    src = tmp_path / "комм.pdf"
    with pikepdf.open(base) as doc:
        note = doc.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Text, Rect=[0, 0, 20, 20],
            T=String("AUTHOR-NAME"), M=String("D:20240101"), NM=String("UNIQUE-ID-MARKER"),
            CreationDate=String("D:20230101"), Contents=String("COMMENT-TEXT"),
        ))
        widget = doc.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Widget, Rect=[30, 0, 60, 20], FT=Name.Tx,
            T=String("fieldname"),
        ))
        doc.pages[0].obj.Annots = Array([note, widget])
        doc.save(src)
    out = run([src, src], tmp_path / "итог.pdf", "pdf_merge")
    raw = out.read_bytes()
    for marker in (b"AUTHOR-NAME", b"UNIQUE-ID-MARKER", b"D:2024", b"D:2023"):
        assert marker not in raw, marker
    with pikepdf.open(out) as doc:
        note, widget = list(doc.pages[0].obj.Annots)
        assert str(note.Contents) == "COMMENT-TEXT"
        assert str(widget.T) == "fieldname"


def test_review3_merge_open_reports_progress(tmp_path, monkeypatch):
    """Открытие сотен файлов не должно выглядеть для сторожа STALL как зависание.

    Сторож здесь — свой, с коротким «STALL»: задача идёт в процессе тестов,
    открытие каждого файла замедлено; промежуток между отметками продвижения
    должен быть меньше STALL.
    """
    files = [make_pdf(tmp_path / f"ф{i}.pdf", [f"Стр {i}"]) for i in range(6)]
    original = pdf._PdfJob._open_pike
    delay = 0.25
    stall = 0.6  # 6 × 0,25 с открытия без отметок > 0,6 с

    def slow_open(self, stack, path):
        time.sleep(delay)
        return original(self, stack, path)

    monkeypatch.setattr(pdf._PdfJob, "_open_pike", slow_open)
    marks: list[float] = []
    last = [None]

    def report(fraction, *values):
        if values and (last[0] is None or values[0] > last[0]):
            last[0] = values[0]
            marks.append(time.monotonic())

    dst = tmp_path / "итог.pdf"
    dst.touch()
    args = {"op": "merge", "sources": [str(f) for f in files], "dst": str(dst),
            "estimate": 0, "limits": {}}
    started = time.monotonic()
    pdf.child_task(args, report)
    gaps = [b - a for a, b in zip([started] + marks, marks)]
    assert gaps and max(gaps) < stall, gaps


# --- Прицельное ревью этапа 3: mailto, цикл в /Order, действия под своими ключами, длина URI


_MAILTO = [
    ("mailto:a@b?attach=file:///C:/Windows/win.ini", "mailto:a@b"),
    ("mailto:?attachment=C:\\x", "mailto:"),
    ("mailto:c@d?subject=Hi&ATTACH=x&body=Text%20here", "mailto:c@d?subject=Hi&body=Text%20here"),
    ("mailto:e@f?%61ttach=y&cc=g@h", "mailto:e@f?cc=g@h"),
    ("mailto:e2@f?%2561ttach=y", "mailto:e2@f"),
    ("mailto:i@j?body=a%26attach%3Dz", "mailto:i@j"),  # значение прячет новый параметр
    ("mailto:m@n?To=o@p&Bcc=q@r&Subject=S", "mailto:m@n?To=o@p&Bcc=q@r&Subject=S"),
    ("MAILTO:u@v", "MAILTO:u@v"),  # без параметров — без изменений
    ("mailto:k%3Fattach=z@l", None),  # «?» в адресе после декодирования — действие удаляется
]


def _uri_pdf(tmp_path: Path, uris: list[str]) -> Path:
    base = make_pdf(tmp_path / "основа.pdf", ["Ссылки"])
    dst = tmp_path / "ссылки.pdf"
    with pikepdf.open(base) as doc:
        doc.pages[0].obj.Annots = Array([
            doc.make_indirect(Dictionary(
                Type=Name.Annot, Subtype=Name.Link, Rect=[i * 20, 0, i * 20 + 15, 15],
                A=Dictionary(S=Name.URI, URI=String(uri)),
            ))
            for i, uri in enumerate(uris)
        ])
        doc.save(dst)
    return dst


@pytest.mark.parametrize("tid", ["pdf_merge", "pdf_split"])
def test_review3b_mailto_params_whitelist(tmp_path, inproc, tid):
    src = _uri_pdf(tmp_path, [uri for uri, _ in _MAILTO])
    if tid == "pdf_merge":
        out = run([src, src], tmp_path / "итог.pdf", tid)
    else:
        out = run(src, tmp_path / "части", tid) / "стр-001.pdf"
    expected = sorted(new for _, new in _MAILTO if new is not None)
    if tid == "pdf_merge":
        expected = sorted(expected * 2)
    assert _uris(out) == expected
    raw = out.read_bytes().lower()
    for marker in (b"attach", b"win.ini", b"%61ttach", b"%2561"):
        assert marker not in raw, marker


def test_review3b_uri_length_limit(tmp_path, inproc):
    long_uri = "https://long.example/" + "a" * (pdf._MAX_URI_LENGTH + 1)
    ok_uri = "https://ok.example/" + "b" * 8000
    src = _uri_pdf(tmp_path, [long_uri, ok_uri])
    out = run([src, src], tmp_path / "итог.pdf", "pdf_merge")
    assert _uris(out) == [ok_uri, ok_uri]
    assert b"long.example" not in out.read_bytes()


def test_review3b_order_cycle(tmp_path, inproc):
    """Косвенный массив /Order, дважды ссылающийся на себя, — без ветвления 2^32."""
    src = make_layers(tmp_path, "Цикл", extra_on=True)
    with pikepdf.open(src, allow_overwriting_input=True) as doc:
        hidden, shown = list(doc.Root.OCProperties.OCGs)
        loop = doc.make_indirect(Array([hidden, shown]))
        loop.append(loop)
        loop.append(Array([String("Ветка"), loop]))
        doc.Root.OCProperties.D.Order = loop
        doc.save(src)
    t0 = time.monotonic()
    merged = run([src, src], tmp_path / "итог.pdf", "pdf_merge")
    parts = run(src, tmp_path / "части", "pdf_split")
    assert time.monotonic() - t0 < 10
    for path in (merged, parts / "стр-001.pdf"):
        _assert_safe_oc(path)
        assert _hidden_area_white(path) and _shown_area_gray(path)
    with pikepdf.open(parts / "стр-001.pdf") as doc:
        order = doc.Root.OCProperties.D.Order
        names = [str(item.Name) for item in order]
        assert names == ["Скрытый", "Видимый"]


def test_review3b_actions_under_custom_keys(tmp_path, inproc):
    base = make_pdf(tmp_path / "основа.pdf", ["Свои ключи"])
    src = tmp_path / "свои.pdf"
    with pikepdf.open(base) as doc:
        page = doc.pages[0].obj
        js_stream = pikepdf.Stream(doc, b"app.alert('STREAM-JS-MARKER')")
        page.MyA = Dictionary(S=Name.JavaScript, JS=String("MYA-JS-MARKER"))
        page.MyArr = Array([
            Dictionary(S=Name.Launch, F=String("calc.exe")),
            doc.make_indirect(Dictionary(S=Name.JavaScript, JS=js_stream)),
            Dictionary(S=Name.URI, URI=String("https://arr.example/")),
            Dictionary(S=Name("/SubmitForm"), F=String("https://submit.example/")),
        ])
        page.Names = Dictionary(JavaScript=Dictionary(
            Names=Array([String("x"), Dictionary(S=Name.JavaScript, JS=String("NAMES-JS-MARKER"))]),
        ))
        page.JS = String("PAGE-JS-MARKER")
        page.Group = Dictionary(Type=Name.Group, S=Name.Transparency, CS=Name.DeviceRGB)
        page.MyLabel = Dictionary(S=Name.D, St=1)
        page.Annots = Array([doc.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Link, Rect=[0, 0, 50, 50],
            MyA=Dictionary(S=Name("/ImportData"), F=String("IMPORT-MARKER")),
            Names=Dictionary(JavaScript=Dictionary(Names=Array([String("y")]))),
        ))])
        page.Resources.MyRes = Dictionary(Deep=Array([
            Dictionary(S=Name("/Rendition"), JS=String("RENDITION-JS-MARKER")),
        ]))
        doc.save(src)
    before = _render_first(src).tobytes()
    merged = run([src, src], tmp_path / "итог.pdf", "pdf_merge")
    parts = run(src, tmp_path / "части", "pdf_split")
    for path in (merged, parts / "стр-001.pdf"):
        raw = path.read_bytes()
        with pikepdf.open(path) as doc:
            data = raw + _stream_data(doc)
            for marker in (b"JavaScript", b"MYA-JS", b"STREAM-JS", b"NAMES-JS", b"PAGE-JS",
                           b"calc.exe", b"submit.example", b"IMPORT-MARKER", b"RENDITION"):
                assert marker not in data, marker
            for obj in _all_objects(doc):
                assert not ({"/JS", "/JavaScript"} & set(obj.keys()))
                if "/S" in obj:
                    assert str(obj.S) in {"/GoTo", "/URI", "/Named", "/Transparency", "/D"}, obj.S
            page = doc.pages[0].obj
            assert "/Names" not in page and "/Names" not in page.Annots[0]
            assert page.Group.S == Name.Transparency
            assert page.MyLabel.S == Name.D
            assert [str(a.URI) for a in page.MyArr if isinstance(a, Dictionary)] == [
                "https://arr.example/"]
        assert _render_first(path).tobytes() == before


# --- Ревью этапа 3: повторная работа в обходе очистки, «;» в mailto ----------------------


def _many_refs_pdf(tmp_path: Path) -> Path:
    """Массив из 20 000 ссылок на одно `mailto:`-действие с 800 параметрами (~128 КБ)."""
    src = tmp_path / "ссылки-много.pdf"
    uri = "mailto:a@b?" + "&".join(["subject=x"] * 800)
    assert len(uri) <= pdf._MAX_URI_LENGTH  # иначе URI отбрасывается без разбора
    with pikepdf.new() as doc:
        doc.add_blank_page()
        action = doc.make_indirect(Dictionary(S=Name.URI, URI=String(uri)))
        doc.pages[0].obj.MyArr = Array([action] * 20000)
        doc.save(src)
    return src


@pytest.mark.parametrize("tid", ["pdf_merge", "pdf_split"])
def test_review3c_shared_action_checked_once(tmp_path, inproc, tid):
    src = _many_refs_pdf(tmp_path)
    started = time.monotonic()
    if tid == "pdf_merge":
        out = run([src, src], tmp_path / "итог.pdf", tid)
    else:
        out = run(src, tmp_path / "части", tid) / "стр-001.pdf"
    assert time.monotonic() - started < 10
    with pikepdf.open(out) as doc:
        arr = doc.pages[0].obj.MyArr
        assert len(arr) == 20000 and str(arr[0].URI).startswith("mailto:a@b?subject=x")


@pytest.mark.parametrize("op", ["merge", "split"])
def test_review3c_long_array_reports_progress(tmp_path, op):
    """Обход длинного массива не должен выглядеть для сторожа STALL как зависание.

    Задача идёт в процессе тестов; проверка каждого элемента замедлена
    (`_clean_uri` — как у тяжёлого, но разного для каждой ссылки URI);
    промежутки между отметками продвижения должны быть меньше «STALL».
    """
    src = tmp_path / "массив.pdf"
    with pikepdf.new() as doc:
        doc.add_blank_page()
        doc.pages[0].obj.MyArr = Array([
            doc.make_indirect(Dictionary(S=Name.URI, URI=String(f"https://e.example/{i}")))
            for i in range(3000)
        ])
        doc.save(src)
    original = pdf._clean_uri

    def slow_clean(value):
        time.sleep(0.0005)
        return original(value)

    stall = 0.6  # 3000 × 0,5 мс ≈ 1,5 с без отметок > 0,6 с
    marks: list[float] = []
    last = [None]

    def report(fraction, *values):
        if values and (last[0] is None or values[0] > last[0]):
            last[0] = values[0]
            marks.append(time.monotonic())

    dst = tmp_path / ("итог.pdf" if op == "merge" else "части")
    if op == "merge":
        dst.touch()
    else:
        dst.mkdir()
    args = {"op": op, "sources": [str(src)] * (2 if op == "merge" else 1), "dst": str(dst),
            "estimate": 0, "limits": {}}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pdf, "_clean_uri", slow_clean)
        started = time.monotonic()
        pdf.child_task(args, report)
    gaps = [b - a for a, b in zip([started] + marks, marks)]
    assert gaps and max(gaps) < stall, max(gaps)


def test_review3c_mailto_semicolon_value_dropped():
    assert pdf._clean_mailto("mailto:a@b?subject=x;attach=C:/x&body=ok") == "mailto:a@b?body=ok"
    assert pdf._clean_mailto("mailto:a@b?subject=x%3Battach%3DC:/x") == "mailto:a@b"
    assert pdf._clean_mailto("mailto:a@b?subject=x%253B") == "mailto:a@b"
    assert pdf._clean_mailto("mailto:a@b?subject=ok") == "mailto:a@b?subject=ok"
