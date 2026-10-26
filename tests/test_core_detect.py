"""Тесты определения формата по содержимому (core/detect.py)."""

from __future__ import annotations

import gzip
import io
import os
import struct
import tarfile
import time
import tracemalloc
import wave
import zipfile
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image, features

from konverter.converters.base import Category
from konverter.core import detect as detect_mod
from konverter.core.detect import UnreadableFileError, detect


def _image(path: Path, fmt: str, **kwargs) -> Path:
    Image.new("RGB", (16, 16), (200, 30, 30)).save(path, fmt, **kwargs)
    return path


def _check(path: Path, fmt: str, category: Category, mismatch: bool = False):
    result = detect(path)
    assert result is not None, path
    assert result.fmt == fmt, (path, result)
    assert result.category == category
    assert result.extension_mismatch is mismatch, result
    assert result.label
    return result


# --- Изображения ------------------------------------------------------------


def test_png_renamed_to_jpg(tmp_path):
    path = _image(tmp_path / "картинка.jpg", "PNG")
    result = _check(path, "png", Category.IMAGE, mismatch=True)
    assert result.label == "PNG"


@pytest.mark.parametrize(
    "name, pil_format, code",
    [
        ("a.jpg", "JPEG", "jpg"),
        ("a.jpeg", "JPEG", "jpg"),
        ("a.png", "PNG", "png"),
        ("a.gif", "GIF", "gif"),
        ("a.bmp", "BMP", "bmp"),
        ("a.webp", "WEBP", "webp"),
        ("a.tif", "TIFF", "tiff"),
        ("a.tiff", "TIFF", "tiff"),
        ("a.ico", "ICO", "ico"),
    ],
)
def test_images(tmp_path, name, pil_format, code):
    path = _image(tmp_path / name, pil_format)
    _check(path, code, Category.IMAGE)


def test_heic(tmp_path):
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    path = _image(tmp_path / "photo.heic", "HEIF")
    _check(path, "heic", Category.IMAGE)


def test_avif(tmp_path):
    if not features.check("avif"):
        pytest.skip("Pillow собран без AVIF")
    path = _image(tmp_path / "photo.avif", "AVIF")
    _check(path, "avif", Category.IMAGE)


def test_svg(tmp_path):
    path = tmp_path / "logo.svg"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- комментарий -->\n"
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
        '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<rect width="10" height="10"/></svg>\n',
        encoding="utf-8",
    )
    _check(path, "svg", Category.IMAGE)


def test_svg_without_prolog_wrong_extension(tmp_path):
    path = tmp_path / "logo.txt"
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding="utf-8")
    _check(path, "svg", Category.IMAGE, mismatch=True)


def test_raw_cr2(tmp_path):
    path = tmp_path / "IMG_0001.CR2"
    path.write_bytes(b"II*\x00\x10\x00\x00\x00CR\x02\x00" + b"\x00" * 200)
    result = _check(path, "raw", Category.IMAGE)
    assert "CR2" in result.label


def test_raw_cr3(tmp_path):
    path = tmp_path / "IMG_0002.cr3"
    path.write_bytes(struct.pack(">I", 24) + b"ftypcrx \x00\x00\x00\x01crx isom" + b"\x00" * 100)
    result = _check(path, "raw", Category.IMAGE)
    assert "CR3" in result.label


@pytest.mark.parametrize(
    "header, marker",
    [
        (b"IIRO\x08\x00\x00\x00", "ORF"),
        (b"IIU\x00\x08\x00\x00\x00", "RW2"),
        (b"FUJIFILMCCD-RAW 0201", "RAF"),
    ],
)
def test_raw_signatures(tmp_path, header, marker):
    path = tmp_path / "raw.bin"
    path.write_bytes(header + b"\x00" * 300)
    result = _check(path, "raw", Category.IMAGE, mismatch=True)
    assert marker in result.label


def test_raw_nef_by_make_tag(tmp_path):
    """TIFF-подобный RAW с маркой NIKON: .nef → raw, .tif → обычный TIFF."""
    nef = tmp_path / "DSC_0001.nef"
    Image.new("RGB", (8, 8)).save(nef, "TIFF", tiffinfo={271: "NIKON CORPORATION"})
    result = _check(nef, "raw", Category.IMAGE)
    assert "NEF" in result.label

    tif = tmp_path / "scan.tif"
    Image.new("RGB", (8, 8)).save(tif, "TIFF", tiffinfo={271: "NIKON CORPORATION"})
    _check(tif, "tiff", Category.IMAGE)


# --- Аудио и видео ----------------------------------------------------------


def _wav(path: Path, seconds: float = 0.2) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x01" * int(8000 * seconds))
    return path


def test_wav(tmp_path):
    _check(_wav(tmp_path / "звук.wav"), "wav", Category.AUDIO)


def _encode_audio(path: Path, container: str, codec: str) -> Path:
    av = pytest.importorskip("av")
    import numpy as np  # noqa: F401 — av сам использует numpy для кадров

    try:
        with av.open(str(path), "w", format=container) as out:
            stream = out.add_stream(codec, rate=44100)
            stream.layout = "mono"
            samples = 1152
            for i in range(20):
                frame = av.AudioFrame.from_ndarray(
                    np.zeros((1, samples), dtype=np.int16), format="s16", layout="mono"
                )
                frame.sample_rate = 44100
                frame.pts = i * samples
                frame.time_base = Fraction(1, 44100)
                for packet in stream.encode(frame):
                    out.mux(packet)
            for packet in stream.encode(None):
                out.mux(packet)
    except Exception as exc:  # кодека может не быть в сборке av
        pytest.skip(f"av не смог закодировать {codec}: {exc}")
    return path


def test_mp3_via_av(tmp_path):
    pytest.importorskip("numpy")
    path = _encode_audio(tmp_path / "song.mp3", "mp3", "mp3")
    _check(path, "mp3", Category.AUDIO)


def test_mp4_via_av(tmp_path):
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    path = tmp_path / "clip.mp4"
    try:
        with av.open(str(path), "w", format="mp4") as out:
            stream = out.add_stream("mpeg4", rate=10)
            stream.width, stream.height, stream.pix_fmt = 32, 32, "yuv420p"
            for i in range(5):
                frame = av.VideoFrame.from_ndarray(
                    np.zeros((32, 32, 3), dtype=np.uint8), format="rgb24"
                )
                frame.pts = i
                for packet in stream.encode(frame):
                    out.mux(packet)
            for packet in stream.encode(None):
                out.mux(packet)
    except Exception as exc:
        pytest.skip(f"av не смог записать mp4: {exc}")
    _check(path, "mp4", Category.VIDEO)


def test_mp4_brand_variants(tmp_path):
    def ftyp(major: bytes, *compat: bytes) -> bytes:
        body = major + b"\x00\x00\x02\x00" + b"".join(compat)
        return struct.pack(">I", 8 + len(body)) + b"ftyp" + body + b"\x00" * 64

    cases = [
        ("a.mov", ftyp(b"qt  ", b"qt  "), "mov", Category.VIDEO),
        ("a.m4a", ftyp(b"M4A ", b"M4A ", b"isom"), "m4a", Category.AUDIO),
        ("a.3gp", ftyp(b"3gp5", b"3gp5", b"isom"), "3gp", Category.VIDEO),
        ("b.m4a", ftyp(b"isom", b"isom", b"mp42"), "m4a", Category.AUDIO),
        ("a.avif", ftyp(b"mif1", b"mif1", b"avif", b"miaf"), "avif", Category.IMAGE),
        ("a.heic", ftyp(b"mif1", b"mif1", b"heic"), "heic", Category.IMAGE),
    ]
    for name, data, code, category in cases:
        path = tmp_path / name
        path.write_bytes(data)
        _check(path, code, category)


def test_amr(tmp_path):
    path = tmp_path / "voice.amr"
    path.write_bytes(b"#!AMR\n" + b"\x3c" + b"\x00" * 300)
    _check(path, "amr", Category.AUDIO)


def test_mpeg_ts(tmp_path):
    packet = b"\x47\x40\x00\x10" + b"\xff" * 184
    path = tmp_path / "00001.mts"
    path.write_bytes(packet * 20)
    _check(path, "mts", Category.VIDEO)

    # M2TS: 4 байта метки времени перед каждым пакетом.
    path2 = tmp_path / "00002.m2ts"
    path2.write_bytes((b"\x00\x00\x00\x00" + packet) * 20)
    _check(path2, "mts", Category.VIDEO)


def _ogg(path: Path, payload: bytes) -> Path:
    page = b"OggS\x00\x02" + b"\x00" * 20 + b"\x01" + bytes([len(payload)]) + payload
    path.write_bytes(page + b"\x00" * 200)
    return path


def test_ogg_opus_vs_vorbis(tmp_path):
    _check(_ogg(tmp_path / "a.opus", b"OpusHead\x01\x01"), "opus", Category.AUDIO)
    _check(_ogg(tmp_path / "b.ogg", b"\x01vorbis\x00\x00"), "ogg", Category.AUDIO)
    # Opus в файле .ogg — расширение допустимо.
    _check(_ogg(tmp_path / "c.ogg", b"OpusHead\x01\x01"), "opus", Category.AUDIO)


def test_asf_wma_and_wmv(tmp_path):
    header_guid = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
    audio = bytes.fromhex("409e69f84d5bcf11a8fd00805f5c442b")
    video = bytes.fromhex("c0ef19bc4d5bcf11a8fd00805f5c442b")

    def asf(*stream_guids: bytes) -> bytes:
        body = b"\x00" * 16 + b"".join(stream_guids) + b"\x00" * 32
        return header_guid + struct.pack("<Q", 30 + len(body)) + b"\x00" * 6 + body

    (tmp_path / "a.wma").write_bytes(asf(audio))
    (tmp_path / "v.wmv").write_bytes(asf(audio, video))
    _check(tmp_path / "a.wma", "wma", Category.AUDIO)
    _check(tmp_path / "v.wmv", "wmv", Category.VIDEO)


# --- Документы, книги, архивы -----------------------------------------------


def test_pdf(tmp_path):
    path = tmp_path / "doc.pdf"
    path.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    _check(path, "pdf", Category.PDF)


def test_plain_zip(tmp_path):
    path = tmp_path / "архив.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", "hello")
        zf.writestr("data/1.bin", b"\x00" * 10)
    _check(path, "zip", Category.ARCHIVE)


def test_docx_like_zip(tmp_path):
    path = tmp_path / "report.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", "<Relationships/>")
        zf.writestr("word/document.xml", "<w:document/>")
    _check(path, "docx", Category.DOCUMENT, mismatch=True)


@pytest.mark.parametrize(
    "inner, code, category",
    [
        ("xl/workbook.xml", "xlsx", Category.SPREADSHEET),
        ("ppt/presentation.xml", "pptx", Category.PRESENTATION),
    ],
)
def test_ooxml_variants(tmp_path, inner, code, category):
    path = tmp_path / f"file.{code}"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(inner, "<x/>")
    _check(path, code, category)


def _odf(path: Path, mimetype: str, first: bool = True) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        if not first:
            zf.writestr("content.xml", "<x/>")
        zf.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", "<container/>")
    return path


def test_epub_like_zip(tmp_path):
    _check(_odf(tmp_path / "book.epub", "application/epub+zip"), "epub", Category.BOOK)


def test_odt_ods_odp(tmp_path):
    _check(
        _odf(tmp_path / "a.odt", "application/vnd.oasis.opendocument.text"),
        "odt", Category.DOCUMENT,
    )
    _check(
        _odf(tmp_path / "a.ods", "application/vnd.oasis.opendocument.spreadsheet", first=False),
        "ods", Category.SPREADSHEET,
    )
    _check(
        _odf(tmp_path / "a.odp", "application/vnd.oasis.opendocument.presentation"),
        "odp", Category.PRESENTATION,
    )


def test_zip_with_huge_entry_count_not_parsed(tmp_path, monkeypatch):
    """При слишком большом каталоге ZIP содержимое не анализируется — просто zip."""
    monkeypatch.setattr(detect_mod, "MAX_ZIP_ENTRIES", 2)
    path = tmp_path / "many.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("a.txt", "1")
        zf.writestr("word/document.xml", "<w/>")
    _check(path, "zip", Category.ARCHIVE, mismatch=True)


def test_tar_gz(tmp_path):
    path = tmp_path / "backup.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        data = b"hello"
        info = tarfile.TarInfo("hello.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    _check(path, "tar_gz", Category.ARCHIVE)


def test_plain_gzip_unsupported(tmp_path):
    path = tmp_path / "log.gz"
    path.write_bytes(gzip.compress(b"just text " * 100))
    assert detect(path) is None


def test_7z_and_rar_signatures(tmp_path):
    (tmp_path / "a.7z").write_bytes(b"7z\xbc\xaf\x27\x1c\x00\x04" + b"\x00" * 100)
    (tmp_path / "a.rar").write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 100)
    _check(tmp_path / "a.7z", "7z", Category.ARCHIVE)
    _check(tmp_path / "a.rar", "rar", Category.ARCHIVE)


def _cfb(stream_names: list[str]) -> bytes:
    """Минимальный составной файл OLE (CFB v3) с заданными именами потоков."""
    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HHHHH", header, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<I", header, 0x2C, 1)  # число секторов FAT
    struct.pack_into("<I", header, 0x30, 1)  # первый сектор каталога
    struct.pack_into("<I", header, 0x38, 4096)
    struct.pack_into("<IIII", header, 0x3C, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0)
    difat = [0] + [0xFFFFFFFF] * 108
    struct.pack_into("<109I", header, 0x4C, *difat)

    fat = [0xFFFFFFFD, 0xFFFFFFFE] + [0xFFFFFFFF] * 126
    fat_sector = struct.pack("<128I", *fat)

    def entry(name: str, obj_type: int) -> bytes:
        e = bytearray(128)
        encoded = (name + "\x00").encode("utf-16-le")
        e[: len(encoded)] = encoded
        struct.pack_into("<HB", e, 0x40, len(encoded), obj_type)
        return bytes(e)

    entries = [entry("Root Entry", 5)] + [entry(n, 2) for n in stream_names]
    entries += [bytes(128)] * (4 - len(entries))
    return bytes(header) + fat_sector + b"".join(entries[:4])


@pytest.mark.parametrize(
    "streams, code, category",
    [
        (["WordDocument", "1Table"], "doc", Category.DOCUMENT),
        (["Workbook"], "xls", Category.SPREADSHEET),
        (["PowerPoint Document", "Current User"], "ppt", Category.PRESENTATION),
    ],
)
def test_ole_by_stream_names(tmp_path, streams, code, category):
    path = tmp_path / "old.bin"  # расширение не подсказывает
    path.write_bytes(_cfb(streams))
    _check(path, code, category, mismatch=True)


def test_ole_unknown_streams_unsupported(tmp_path):
    path = tmp_path / "setup.msi"
    path.write_bytes(_cfb(["SummaryInformation"]))
    assert detect(path) is None


def test_fb2(tmp_path):
    path = tmp_path / "книга.fb2"
    path.write_bytes(
        '<?xml version="1.0" encoding="windows-1251"?>\n'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
        "<body><p>Текст</p></body></FictionBook>".encode("cp1251")
    )
    _check(path, "fb2", Category.BOOK)


def test_mobi(tmp_path):
    path = tmp_path / "book.mobi"
    header = b"Book_Title".ljust(32, b"\x00") + b"\x00" * 28 + b"BOOKMOBI"
    path.write_bytes(header + b"\x00" * 500)
    _check(path, "mobi", Category.BOOK)


def test_rtf(tmp_path):
    path = tmp_path / "a.rtf"
    path.write_bytes(rb"{\rtf1\ansi\deff0 {\fonttbl {\f0 Times;}} Hello\par}")
    _check(path, "rtf", Category.DOCUMENT)


# --- Текстовые форматы ------------------------------------------------------


def test_text_formats(tmp_path):
    (tmp_path / "notes.txt").write_text("Просто текст.\nВторая строка.\n" * 20, encoding="utf-8")
    (tmp_path / "readme.md").write_text(
        "# Заголовок\n\n- пункт\n- пункт\n\n**жирный** текст\n" * 10, encoding="utf-8"
    )
    (tmp_path / "data.csv").write_text("a,b,c\n1,2,3\n4,5,6\n" * 20, encoding="utf-8")
    (tmp_path / "page.html").write_text(
        "<!DOCTYPE html><html><head><title>t</title></head>"
        "<body><p>Привет</p></body></html>\n" * 5,
        encoding="utf-8",
    )
    _check(tmp_path / "notes.txt", "txt", Category.DOCUMENT)
    _check(tmp_path / "readme.md", "md", Category.DOCUMENT)
    _check(tmp_path / "data.csv", "csv", Category.SPREADSHEET)
    _check(tmp_path / "page.html", "html", Category.DOCUMENT)


def test_cp1251_text(tmp_path):
    path = tmp_path / "старый.txt"
    path.write_bytes("Текст в кодировке Windows-1251.\r\n".encode("cp1251") * 30)
    _check(path, "txt", Category.DOCUMENT)


# --- Ошибки и защита --------------------------------------------------------


def test_empty_file_is_none(tmp_path):
    path = tmp_path / "empty.png"
    path.write_bytes(b"")
    assert detect(path) is None


def test_garbage_does_not_crash(tmp_path):
    path = tmp_path / "garbage.dat"
    path.write_bytes(os.urandom(20000))
    result = detect(path)
    assert result is None or result.fmt in detect_mod.FORMATS


@pytest.mark.parametrize(
    "data",
    [
        b"PK\x03\x04" + os.urandom(500),  # битый ZIP
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\xff" * 600,  # битый OLE
        b"II*\x00\xff\xff\xff\x7f" + b"\x00" * 50,  # TIFF с IFD за пределами файла
        b"\x00\x00\x00\x08ftyp",  # обрезанный ftyp
        b"ID3\x04\x00\x00\x7f\x7f\x7f\x7f",  # ID3 с огромным размером
        b"\x1f\x8b\x08\x00" + os.urandom(100),  # битый gzip
        b"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]><FictionBook>",
    ],
)
def test_broken_structures_do_not_crash(tmp_path, data):
    path = tmp_path / "broken.bin"
    path.write_bytes(data)
    result = detect(path)
    assert result is None or result.fmt in detect_mod.FORMATS


def test_missing_file_raises(tmp_path):
    with pytest.raises(UnreadableFileError) as info:
        detect(tmp_path / "нет такого.png")
    assert info.value.reason == "Файл не найден"


def test_directory_raises(tmp_path):
    with pytest.raises(UnreadableFileError):
        detect(tmp_path)


def test_no_access_raises(tmp_path, monkeypatch):
    path = _image(tmp_path / "locked.png", "PNG")
    real_open = open

    def deny(file, mode="r", *args, **kwargs):
        if Path(file) == path:
            raise PermissionError(13, "Отказано в доступе (смоделировано)")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", deny)
    with pytest.raises(UnreadableFileError) as info:
        detect(path)
    assert info.value.reason == "Нет доступа к файлу"


def test_huge_file_not_read_entirely(tmp_path):
    path = tmp_path / "huge.bin"
    chunk = os.urandom(1024 * 1024)
    with open(path, "wb") as f:
        for _ in range(50):
            f.write(chunk)
    detect_mod.warm_up()  # загрузку модели не учитываем

    tracemalloc.start()
    started = time.perf_counter()
    try:
        detect(path)
    finally:
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    assert elapsed < 5, elapsed
    assert peak < 16 * 1024 * 1024, peak


def test_unicode_and_special_chars_in_name(tmp_path):
    path = _image(tmp_path / "Фото №1 (копия) #&' ☃ 日本.png", "PNG")
    _check(path, "png", Category.IMAGE)


def test_extension_helpers():
    assert detect_mod.file_extension("Архив.TAR.GZ") == "tar.gz"
    assert detect_mod.file_extension("без_расширения") == ""
    assert detect_mod.extension_matches("a.JPE", "jpg")
    assert detect_mod.extension_matches("a.tgz", "tar_gz")
    assert not detect_mod.extension_matches("a.png", "jpg")
