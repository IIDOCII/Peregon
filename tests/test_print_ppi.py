"""«Подготовить к печати (PPI)»: ядро (converters/images.py), окно
(ui/print_ppi.py), главное окно («Применить ко всем»), расширение результата
в core/jobs.py. Картинки маленькие, генерируются в tmp_path."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest
from PIL import Image, ImageCms

from konverter.converters import images
from konverter.converters.base import CancelToken, ConversionError, Preset
from konverter.converters.images import ImageConverter
from konverter.core.jobs import JobManager, JobStatus
from konverter.ui.main_window import MainWindow
from konverter.ui.print_ppi import PrintChoice, PrintPpiDialog

CONV = ImageConverter()
SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
DETECT_TIMEOUT = 30000
JOB_TIMEOUT = 60000
CM_600 = 5.08  # при 300 PPI ровно 600 px


def _img(size=(40, 20), mode="RGB") -> Image.Image:
    img = Image.new(mode, size, (200, 60, 30, 255) if mode == "RGBA" else (200, 60, 30))
    img.paste((20, 120, 220), (0, 0, size[0] // 2, size[1] // 2))
    return img


def _make(folder: Path, fmt: str, size=(40, 20), **save) -> Path:
    folder.mkdir(exist_ok=True)
    path = folder / f"картинка.{fmt}"
    img = _img(size)
    if fmt == "jpg":
        img.save(path, "JPEG", **save)
    elif fmt == "heic":
        images._ensure_heif()
        img.save(path, "HEIF", **save)
    else:
        img.save(path, fmt.upper() if fmt != "tiff" else "TIFF", **save)
    return path


def _run(src: Path, fmt: str, ppi, cm, tmp_path: Path, cancel=None) -> tuple[Path, str | None]:
    target = next(t for t in CONV.targets(fmt) if t.id == "print_ppi")
    dst = tmp_path / "результат.tmp"
    ext = CONV.convert(
        [src], dst, target, Preset.MEDIUM, lambda p: None, cancel or CancelToken(),
        formats=[fmt], print_ppi=(ppi, cm),
    )
    return dst, ext


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Ядро --------------------------------------------------------------------


def test_target_listed():
    for fmt in images.INPUT_FORMATS:
        target = next(t for t in CONV.targets(fmt) if t.id == "print_ppi")
        assert not target.uses_preset and not target.many_to_one


@pytest.mark.parametrize("fmt", ["jpg", "png", "tiff", "bmp"])
def test_same_format_size_and_ppi(tmp_path, fmt):
    src = _make(tmp_path / "in", fmt)
    dst, ext = _run(src, fmt, 300, CM_600, tmp_path)
    assert ext == fmt
    with Image.open(dst) as out:
        assert out.format == {"jpg": "JPEG", "png": "PNG", "tiff": "TIFF", "bmp": "BMP"}[fmt]
        assert out.size == (600, 300)
        assert out.info["dpi"] == pytest.approx((300, 300), abs=1)


def test_jpg_quality_444(tmp_path):
    src = _make(tmp_path / "in", "jpg")
    dst, _ = _run(src, "jpg", 300, CM_600, tmp_path)
    with Image.open(dst) as out:
        assert out.layer[0][1:3] == (1, 1) and out.layer[1][1:3] == (1, 1)  # 4:4:4


@pytest.mark.parametrize(("size", "expected"), [((20, 40), (300, 600)), ((40, 20), (600, 300)),
                                                 ((30, 30), (600, 600))])
def test_long_side_orientation(tmp_path, size, expected):
    src = _make(tmp_path / "in", "png", size)
    dst, _ = _run(src, "png", 300, CM_600, tmp_path)
    with Image.open(dst) as out:
        assert out.size == expected


def test_proportions_rounded(tmp_path):
    src = _make(tmp_path / "in", "png", (30, 20))
    dst, _ = _run(src, "png", 100, 2.54, tmp_path)  # длинная 100 px, вторая 66,67 → 67
    with Image.open(dst) as out:
        assert out.size == (100, 67)


def test_exif_rotation_counts(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # повернуть: 40×20 станет 20×40
    src = _make(tmp_path / "in", "jpg", exif=exif)
    dst, _ = _run(src, "jpg", 300, CM_600, tmp_path)
    with Image.open(dst) as out:
        assert out.size == (300, 600)


def test_no_resample_when_same_size(tmp_path):
    src = _make(tmp_path / "in", "png", (600, 300))
    dst, _ = _run(src, "png", 300, CM_600, tmp_path)
    with Image.open(dst) as out, Image.open(src) as orig:
        assert out.size == orig.size and out.tobytes() == orig.tobytes()


def test_webp_alpha_to_png(tmp_path):
    src = tmp_path / "in.webp"
    img = _img(mode="RGBA")
    img.putpixel((0, 0), (0, 0, 0, 0))
    img.save(src, "WEBP", lossless=True)
    dst, ext = _run(src, "webp", 300, CM_600, tmp_path)
    assert ext == "png"
    with Image.open(dst) as out:
        assert out.format == "PNG" and out.mode == "RGBA" and out.size == (600, 300)
        assert out.info["dpi"] == pytest.approx((300, 300), abs=1)


def test_opaque_alpha_to_jpg(tmp_path):
    """Альфа-канал есть, но не используется — это JPG."""
    src = tmp_path / "in.webp"
    _img(mode="RGBA").save(src, "WEBP", lossless=True)
    dst, ext = _run(src, "webp", 300, CM_600, tmp_path)
    assert ext == "jpg"
    with Image.open(dst) as out:
        assert out.format == "JPEG"


def test_heic_to_jpg(tmp_path):
    src = _make(tmp_path / "in", "heic")
    dst, ext = _run(src, "heic", 200, CM_600, tmp_path)
    assert ext == "jpg"
    with Image.open(dst) as out:
        assert out.format == "JPEG" and out.size == (400, 200)
        assert out.info["dpi"] == pytest.approx((200, 200), abs=1)


def test_svg_to_jpg(tmp_path):
    src = tmp_path / "in.svg"
    src.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
        '<rect width="200" height="100" fill="#3080ff"/></svg>', encoding="utf-8")
    dst, ext = _run(src, "svg", 254, 10, tmp_path)  # 1000 px по длинной
    assert ext == "jpg"
    with Image.open(dst) as out:
        assert out.size == (1000, 500)


def test_animated_gif_first_frame(tmp_path):
    src = tmp_path / "in.gif"
    frames = [Image.new("RGB", (40, 20), c) for c in ("red", "blue")]
    frames[0].save(src, "GIF", save_all=True, append_images=frames[1:], duration=50)
    dst, ext = _run(src, "gif", 300, CM_600, tmp_path)
    assert ext == "jpg"
    with Image.open(dst) as out:
        assert out.size == (600, 300) and out.getpixel((300, 150))[0] > 200  # красный


@pytest.mark.parametrize("fmt", ["png", "jpg"])
def test_icc_kept(tmp_path, fmt):
    src = _make(tmp_path / "in", fmt, icc_profile=SRGB)
    dst, _ = _run(src, fmt, 300, CM_600, tmp_path)
    with Image.open(dst) as out:
        assert out.info["icc_profile"] == SRGB


def test_original_untouched(tmp_path):
    src = _make(tmp_path / "in", "png")
    before = _sha(src)
    _run(src, "png", 300, CM_600, tmp_path)
    assert _sha(src) == before


@pytest.mark.parametrize("ppi", [0, -300, 2401, 300.5, "300", None])
def test_bad_ppi(tmp_path, ppi):
    src = _make(tmp_path / "in", "png")
    with pytest.raises(ConversionError):
        _run(src, "png", ppi, CM_600, tmp_path)


@pytest.mark.parametrize("cm", [0, -1, math.nan, math.inf, "5", None, 1e9])
def test_bad_cm(tmp_path, cm):
    src = _make(tmp_path / "in", "png")
    with pytest.raises(ConversionError):
        _run(src, "png", 300, cm, tmp_path)


def test_too_many_pixels(tmp_path):
    """Длинная сторона в пределах, но квадрат больше MAX_PIXELS."""
    src = _make(tmp_path / "in", "png", (100, 100))
    with pytest.raises(ConversionError, match="Мп"):
        _run(src, "png", 1000, 150, tmp_path)


def test_missing_params(tmp_path):
    src = _make(tmp_path / "in", "png")
    target = next(t for t in CONV.targets("png") if t.id == "print_ppi")
    with pytest.raises(ConversionError):
        CONV.convert([src], tmp_path / "x.tmp", target, Preset.MEDIUM, lambda p: None,
                     CancelToken(), formats=["png"])


def test_job_takes_extension_from_converter(tmp_path):
    src = tmp_path / "in.webp"
    img = _img(mode="RGBA")
    img.putpixel((0, 0), (0, 0, 0, 0))
    img.save(src, "WEBP", lossless=True)
    target = next(t for t in CONV.targets("webp") if t.id == "print_ppi")
    manager = JobManager(max_workers=1)
    try:
        job = manager.submit([src], CONV, target, Preset.MEDIUM, tmp_path, formats=["webp"],
                             options={"print_ppi": (300, CM_600)})
        import time
        deadline = time.monotonic() + 30
        while not job.is_finished and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        manager.shutdown()
    assert job.status == JobStatus.DONE, job.error_message
    assert job.result_path == tmp_path / "in.png"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.png", "in.webp"]


# --- Окно --------------------------------------------------------------------


def test_dialog_reads_size_and_ppi(qtbot, tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6
    src = _make(tmp_path / "in", "jpg", exif=exif, dpi=(150, 150))
    dialog = PrintPpiDialog(src, "jpg")
    qtbot.addWidget(dialog)
    assert dialog.size == (20, 40) and dialog.current_ppi == 150


def test_dialog_as_is(qtbot, tmp_path):
    src = _make(tmp_path / "a", "png", (300, 150), dpi=(150, 150))
    dialog = PrintPpiDialog(src, "png")
    qtbot.addWidget(dialog)
    dialog.cm_edit.setText("1")
    dialog.as_is_button.click()
    assert float(dialog.cm_edit.text()) == pytest.approx(5.08)
    # PPI в файле нет — считаем 96.
    src = _make(tmp_path / "b", "png", (300, 150))
    dialog = PrintPpiDialog(src, "png")
    qtbot.addWidget(dialog)
    dialog.as_is_button.click()
    assert float(dialog.cm_edit.text()) == pytest.approx(300 / 96 * 2.54, abs=0.01)


def test_dialog_make_and_cancel(qtbot, tmp_path):
    src = _make(tmp_path / "in", "png")
    dialog = PrintPpiDialog(src, "png")
    qtbot.addWidget(dialog)
    assert dialog.ppi_edit.text() == "300"
    dialog.cancel_button.click()
    assert dialog.choice.kind == "cancel"

    dialog = PrintPpiDialog(src, "png")
    qtbot.addWidget(dialog)
    dialog.ppi_edit.setText("150")
    dialog.cm_edit.setText("12,5")
    dialog.make_button.click()
    assert dialog.choice == PrintChoice("one", 150, 12.5)


@pytest.mark.parametrize(("ppi", "cm"), [("0", "10"), ("-3", "10"), ("2401", "10"), ("abc", "10"),
                                         ("", "10"), ("300.5", "10"), ("300", "0"),
                                         ("300", "-1"), ("300", ""), ("300", "x"),
                                         ("300", "nan"), ("300", "100000")])
def test_dialog_blocks_bad_values(qtbot, tmp_path, ppi, cm):
    src = _make(tmp_path / "in", "png")
    dialog = PrintPpiDialog(src, "png", total=2)
    qtbot.addWidget(dialog)
    dialog.ppi_edit.setText(ppi)
    dialog.cm_edit.setText(cm)
    assert not dialog.make_button.isEnabled() and not dialog.apply_all_button.isEnabled()
    assert dialog.error_label.text()
    dialog.make_button.click()
    assert dialog.choice.kind == "cancel"


def test_dialog_blocks_too_many_pixels(qtbot, tmp_path):
    src = _make(tmp_path / "in", "png", (100, 100))
    dialog = PrintPpiDialog(src, "png")
    qtbot.addWidget(dialog)
    dialog.ppi_edit.setText("1000")
    dialog.cm_edit.setText("150")
    assert not dialog.make_button.isEnabled() and "Мп" in dialog.error_label.text()


def test_dialog_result_and_upscale_warning(qtbot, tmp_path):
    src = _make(tmp_path / "in", "png", (100, 50))
    dialog = PrintPpiDialog(src, "png")
    qtbot.addWidget(dialog)
    dialog.set_values(300, 2.54)  # 300×150: увеличение в 3 раза
    assert "300×150" in dialog.result_label.text() and "1.3 см" in dialog.result_label.text()
    assert dialog.warn_label.text()
    dialog.set_values(100, 2.54)  # 100×50: без изменений
    assert not dialog.warn_label.text()


def test_dialog_unknown_size_svg(qtbot, tmp_path):
    src = tmp_path / "in.svg"
    src.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
    dialog = PrintPpiDialog(src, "svg")
    qtbot.addWidget(dialog)
    assert dialog.size is None and not dialog.as_is_button.isEnabled()
    assert dialog.make_button.isEnabled()
    dialog.make_button.click()
    assert dialog.choice == PrintChoice("one", 300, 30.0)


def test_dialog_apply_all(qtbot, tmp_path):
    src = _make(tmp_path / "in", "png")
    assert PrintPpiDialog(src, "png").apply_all_button is None
    dialog = PrintPpiDialog(src, "png", index=1, total=3)
    qtbot.addWidget(dialog)
    dialog.set_values(200, 7)
    dialog.apply_all_button.click()
    assert dialog.choice == PrintChoice("all", 200, 7.0)


# --- Главное окно ------------------------------------------------------------


@pytest.fixture
def window(qtbot):
    win = MainWindow(warm_up_detector=False)
    qtbot.addWidget(win)
    yield win
    win.shutdown()


def _image_group(qtbot, win: MainWindow, folder: Path, count: int) -> list[Path]:
    sources = [_make(folder, "png", (40 + i, 20)).rename(folder / f"к{i}.png")
               for i in range(count)]
    win.add_paths(sources)
    qtbot.waitUntil(lambda: not win.is_busy_detecting and len(win.group_panels) >= 1,
                    timeout=DETECT_TIMEOUT)
    assert win.group_panels[0].set_target("print_ppi")
    return sources


def _wait_jobs(qtbot, win: MainWindow, count: int) -> list:
    qtbot.waitUntil(lambda: len(win.job_manager.jobs()) == count
                    and all(j.is_finished for j in win.job_manager.jobs()), timeout=JOB_TIMEOUT)
    return win.job_manager.jobs()


def test_group_apply_all(qtbot, window, tmp_path, monkeypatch):
    sources = _image_group(qtbot, window, tmp_path / "г", 3)
    hashes = [_sha(s) for s in sources]
    asked: list[int] = []

    def fake_ask(source, fmt, index, total):
        asked.append(index)
        return PrintChoice("all", 200, CM_600)

    monkeypatch.setattr(window, "_ask_print_ppi", fake_ask)
    window.group_panels[0].convert_button.click()
    assert asked == [1]
    jobs = _wait_jobs(qtbot, window, 3)
    assert all(j.status == JobStatus.DONE for j in jobs), [j.error_message for j in jobs]
    assert all(j.options == {"print_ppi": (200, CM_600)} for j in jobs)
    for job in jobs:
        with Image.open(job.result_path) as out:
            assert max(out.size) == 400 and out.format == "PNG"
    assert [_sha(s) for s in sources] == hashes


def test_group_cancel_one_by_one(qtbot, window, tmp_path, monkeypatch):
    _image_group(qtbot, window, tmp_path / "г", 2)
    answers = iter([PrintChoice("cancel"), PrintChoice("one", 300, CM_600)])
    monkeypatch.setattr(window, "_ask_print_ppi", lambda *a: next(answers))
    window.group_panels[0].convert_button.click()
    jobs = _wait_jobs(qtbot, window, 1)
    assert jobs[0].status == JobStatus.DONE and jobs[0].options == {"print_ppi": (300, CM_600)}


def test_real_dialog_in_main_window(qtbot, window, tmp_path, monkeypatch):
    _image_group(qtbot, window, tmp_path / "г", 1)
    from konverter.ui import main_window as module

    class AutoDialog(PrintPpiDialog):
        def exec(self) -> int:  # noqa: A003 — имя из Qt
            self.set_values(100, 2.54)
            self.make_button.click()
            return int(self.result())

    monkeypatch.setattr(module, "PrintPpiDialog", AutoDialog)
    window.group_panels[0].convert_button.click()
    jobs = _wait_jobs(qtbot, window, 1)
    assert jobs[0].status == JobStatus.DONE
    with Image.open(jobs[0].result_path) as out:
        assert out.size == (100, 50)


# --- Ревью: огромные числа, расширение, ICC, PPI в заголовке, HEIC ----------------


@pytest.mark.parametrize("cm", [1e308, 1e300])
def test_huge_cm_core(tmp_path, cm):
    src = _make(tmp_path / "in", "png")
    with pytest.raises(ConversionError):
        _run(src, "png", 2400, cm, tmp_path)


def test_huge_cm_dialog(qtbot, tmp_path):
    src = _make(tmp_path / "in", "png")
    dialog = PrintPpiDialog(src, "png")
    qtbot.addWidget(dialog)
    dialog.ppi_edit.setText("2400")
    dialog.cm_edit.setText("1e308")
    assert not dialog.make_button.isEnabled() and dialog.error_label.text()


@pytest.mark.parametrize(("returned", "expected"), [("png", "png"), ("../x", "jpg"), ("PNG", "jpg"),
                                                     ("toolong", "jpg"), ("", "jpg"), (5, "jpg")])
def test_job_extension_from_converter_is_checked(tmp_path, returned, expected):
    class Fake:
        category = CONV.category

        def convert(self, sources, dst, target, preset, progress, cancel, *, formats):
            dst.write_bytes(b"x")
            return returned

    target = next(t for t in CONV.targets("png") if t.id == "print_ppi")
    src = tmp_path / "in.src"
    src.write_bytes(b"x")
    manager = JobManager(max_workers=1)
    try:
        job = manager.submit([src], Fake(), target, Preset.MEDIUM, tmp_path, formats=["png"])
        import time
        deadline = time.monotonic() + 30
        while not job.is_finished and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        manager.shutdown()
    assert job.status == JobStatus.DONE, job.error_message
    assert job.result_path == tmp_path / f"in.{expected}"


def test_icc_dropped_if_colorspace_mismatch(tmp_path):
    """LA → RGBA: серый ICC не подходит RGBA и не сохраняется; RGB-профиль остаётся."""
    src = tmp_path / "la.png"
    Image.new("LA", (40, 20), (100, 0)).save(src, "PNG", icc_profile=b"\0" * 16 + b"GRAY" + b"\0" * 100)
    dst, _ = _run(src, "png", 300, CM_600, tmp_path)
    with Image.open(dst) as out:
        assert out.mode == "RGBA" and "icc_profile" not in out.info
    gray = tmp_path / "gray.png"
    Image.new("L", (40, 20), 100).save(gray, "PNG", icc_profile=b"\0" * 16 + b"GRAY" + b"\0" * 100)
    dst, _ = _run(gray, "png", 300, CM_600, tmp_path)
    with Image.open(dst) as out:
        assert out.mode == "L" and out.info["icc_profile"][16:20] == b"GRAY"


def test_peek_ppi_must_be_finite_positive(tmp_path, monkeypatch):
    src = _make(tmp_path / "in", "png")
    for bad in (math.nan, math.inf, 0, -5):
        monkeypatch.setattr(Image.Image, "info", {"dpi": (bad, bad)}, raising=False)
        assert images.peek_size(src, "png")[2] is None


def test_peek_size_matches_result_heic(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6
    src = tmp_path / "in.heic"
    images._ensure_heif()
    _img((40, 20)).save(src, "HEIF", exif=exif)
    width, height, _ = images.peek_size(src, "heic")
    dst, _ = _run(src, "heic", 300, CM_600, tmp_path)
    with Image.open(dst) as out:
        assert (out.width / out.height) == pytest.approx(width / height, rel=0.01)
        assert max(out.size) == 600
