"""Делает значок exe `packaging/icon.ico` из `src/konverter/ui/assets/icon.svg`.

Каждый размер (16…256 px) рендерится из SVG отдельно (resvg-py), а не
уменьшается из одной большой картинки: так мелкие значки остаются чёткими.
Пакеты — только уже стоящие в проекте (resvg-py, Pillow).

Запуск: `uv run python packaging/make_icon.py` (результат кладётся в репозиторий).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import resvg_py
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SVG_PATH = ROOT / "src" / "konverter" / "ui" / "assets" / "icon.svg"
ICO_PATH = ROOT / "packaging" / "icon.ico"

# Размеры значков в .ico (px).
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(svg: str, size: int) -> Image.Image:
    """Растр size×size (RGBA) из текста SVG."""
    png = resvg_py.svg_to_bytes(svg_string=svg, width=size, height=size)
    with Image.open(io.BytesIO(bytes(png))) as img:
        return img.convert("RGBA")


def make_icon(svg_path: Path = SVG_PATH, ico_path: Path = ICO_PATH) -> Path:
    """Пишет .ico со всеми размерами `SIZES` и возвращает путь к нему."""
    svg = svg_path.read_text(encoding="utf-8")
    images = [render(svg, size) for size in SIZES]
    largest = images[-1]
    # Pillow берёт для каждого размера из `sizes` картинку точно такого
    # размера из append_images (если есть), иначе уменьшает самую большую.
    largest.save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=images[:-1],
    )
    return ico_path


if __name__ == "__main__":
    path = make_icon()
    with Image.open(path) as ico:
        print(path, sorted(ico.info.get("sizes", [])))
    sys.exit(0)
