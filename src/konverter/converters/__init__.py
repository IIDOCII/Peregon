"""Конвертеры, сгруппированные по категориям форматов.

При импорте пакета каждый конвертер регистрируется в `base.REGISTRY`.
"""

from .base import register
from .books import BookConverter
from .documents import DocumentConverter
from .images import ImageConverter
from .media import AudioConverter, VideoConverter
from .pdf import PdfConverter
from .presentations import PresentationConverter
from .tables import SpreadsheetConverter

register(ImageConverter())
register(VideoConverter())
register(AudioConverter())
register(PdfConverter())
register(BookConverter())
register(DocumentConverter())
register(SpreadsheetConverter())
register(PresentationConverter())
