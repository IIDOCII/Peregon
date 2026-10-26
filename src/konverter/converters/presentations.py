"""Конвертер презентаций (этап 5): PPTX, PPT, ODP → PDF и PNG слайдов — через LibreOffice.

Варианты (вход — коды detect "pptx", "ppt", "odp"):
* «PDF» (`pdf`) — LibreOffice (`impress_pdf_Export`), из результата удаляются
  метаданные документа (см. `_office`); пресет не влияет;
* «Слайды в PNG» (`slides_png`) — LibreOffice → PDF → существующий рендер
  страниц PDF (`pdf.PdfConverter`, «Страницы в PNG»): результат — ПАПКА
  `имя/` с `стр-001.png` …; пресет — разрешение (как у PDF → PNG).
Все варианты `is_long=True`, очередь "office" (по одной задаче). Если модуля
нет — интерфейс спрашивает разрешение на скачивание (Ф15), а задача без
модуля завершается понятной ошибкой.
"""

from __future__ import annotations

from pathlib import Path

from konverter.strings import tr

from . import _office
from .base import CancelToken, Category, ConversionError, Preset, ProgressCallback, Target

INPUTS = frozenset({"pptx", "ppt", "odp"})
# (id, расширение ("" — папка), uses_preset, что просит LibreOffice, дообработка)
_OUTPUTS = (
    ("pdf", "pdf", False, "pdf", _office.POST_PDF),
    ("slides_png", "", True, "pdf", _office.POST_PAGES_PNG),
)
TARGET_ORDER = tuple(item[0] for item in _OUTPUTS)


class PresentationConverter:
    """Конвертер презентаций (см. докстринг модуля)."""

    category = Category.PRESENTATION
    target_order = TARGET_ORDER
    queue = _office.QUEUE

    def targets(self, fmt: str) -> list[Target]:
        if (fmt or "").lower() not in INPUTS:
            return []
        return [
            Target(id=tid, label=tr(f"target_slides_{tid}"), extension=ext, uses_preset=uses,
                   is_long=True)
            for tid, ext, uses, _kind, _post in _OUTPUTS
        ]

    def module_for(self, target_id: str, fmt: str) -> str | None:
        return _office.MODULE if (fmt or "").lower() in INPUTS else None

    def convert(
        self,
        sources: list[Path],
        dst: Path,
        target: Target,
        preset: Preset,
        progress: ProgressCallback,
        cancel: CancelToken,
        *,
        formats: list[str],
    ) -> None:
        plan = {tid: (kind, post) for tid, _e, _u, kind, post in _OUTPUTS}.get(target.id)
        if plan is None:
            raise ConversionError("Внутренняя ошибка: неизвестный вариант конвертации")
        kind, post = plan
        _office.convert(sources, dst, target, preset, progress, cancel, formats, kind=kind, post=post)
