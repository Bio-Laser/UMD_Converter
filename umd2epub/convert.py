# -*- coding: utf-8 -*-
"""编排层: 单个 UMD 文件 -> EPUB 文件。日后做图形界面可直接复用本模块。"""
from __future__ import annotations

import dataclasses
import os
from typing import Dict, List, Optional

from .epub import EpubOptions, build_epub
from .umd import UmdError, UmdFormatError, UnsupportedUmdError, parse_umd

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_FORMAT = 3
EXIT_UNSUPPORTED = 4
EXIT_EXISTS = 5

DEFAULT_SPLIT_CHARS = 20000

# 批量转换时用来挑"最严重"的退出码(数值越小越严重)
SEVERITY = {
    EXIT_ERROR: 0,
    EXIT_FORMAT: 1,
    EXIT_UNSUPPORTED: 2,
    EXIT_EXISTS: 3,
}


@dataclasses.dataclass
class Report:
    src: str
    dst: Optional[str] = None
    ok: bool = False
    exit_code: int = EXIT_OK
    error: str = ""
    chapters: int = 0
    chars: int = 0
    size: int = 0
    degraded: bool = False
    dry_run: bool = False
    written: bool = False
    has_cover: bool = False
    cover_ext: Optional[str] = None
    cover_size: int = 0
    meta: Dict[str, str] = dataclasses.field(default_factory=dict)
    warnings: List[str] = dataclasses.field(default_factory=list)
    stats: Dict[str, object] = dataclasses.field(default_factory=dict)


def default_output_path(src: str) -> str:
    return os.path.splitext(src)[0] + ".epub"


def convert_file(
    src: str,
    dst: Optional[str] = None,
    *,
    force: bool = False,
    dry_run: bool = False,
    title: Optional[str] = None,
    author: Optional[str] = None,
    lang: str = "zh",
    split_chars: int = DEFAULT_SPLIT_CHARS,
    cover: bool = True,
) -> Report:
    """把一个 .umd 转成 .epub; 失败时通过 Report 返回错误与退出码。"""
    report = Report(src=src, dry_run=dry_run)

    if not os.path.isfile(src):
        report.error = "文件不存在"
        report.exit_code = EXIT_ERROR
        return report

    try:
        with open(src, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        report.error = "读取失败: %s" % exc
        report.exit_code = EXIT_ERROR
        return report

    try:
        book = parse_umd(data)
    except UnsupportedUmdError as exc:
        report.error = str(exc)
        report.exit_code = EXIT_UNSUPPORTED
        return report
    except UmdFormatError as exc:
        report.error = str(exc)
        report.exit_code = EXIT_FORMAT
        return report
    except UmdError as exc:
        report.error = str(exc)
        report.exit_code = EXIT_ERROR
        return report

    report.meta = dict(book.meta)
    report.chapters = len(book.chapters)
    report.chars = book.total_chars
    report.degraded = book.degraded
    report.warnings = list(book.warnings)
    report.stats = dict(book.stats)
    report.has_cover = book.cover is not None
    report.cover_ext = book.cover_ext
    report.cover_size = len(book.cover) if book.cover else 0

    if dry_run:
        report.ok = True
        return report

    out_path = dst or default_output_path(src)
    report.dst = out_path
    if os.path.exists(out_path) and not force:
        report.error = "输出文件已存在, 加 --force 覆盖: %s" % out_path
        report.exit_code = EXIT_EXISTS
        return report

    options = EpubOptions(
        title=title or "",
        author=author or "",
        language=lang,
        include_cover=cover,
        split_chars=split_chars,
    )
    try:
        payload = build_epub(book, options)
    except Exception as exc:  # 打包失败也要给出可读信息, 不抛栈给用户
        report.error = "生成 EPUB 失败: %s" % exc
        report.exit_code = EXIT_ERROR
        return report

    try:
        with open(out_path, "wb") as handle:
            handle.write(payload)
    except OSError as exc:
        report.error = "写入失败: %s" % exc
        report.exit_code = EXIT_ERROR
        return report

    report.ok = True
    report.written = True
    report.size = len(payload)
    return report
