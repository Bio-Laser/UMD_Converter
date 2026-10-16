# -*- coding: utf-8 -*-
"""umd2epub: 把文本型 UMD 电子书转换成 EPUB, 只用 Python 标准库。"""
from __future__ import annotations

from .convert import Report, convert_file
from .epub import EpubOptions, build_epub
from .umd import (
    Chapter,
    UmdBook,
    UmdError,
    UmdFormatError,
    UnsupportedUmdError,
    parse_umd,
)

__version__ = "1.0.0"

__all__ = [
    "Chapter",
    "EpubOptions",
    "Report",
    "UmdBook",
    "UmdError",
    "UmdFormatError",
    "UnsupportedUmdError",
    "build_epub",
    "convert_file",
    "parse_umd",
    "__version__",
]
