# -*- coding: utf-8 -*-
"""编排层: 单个 UMD 文件 -> EPUB 文件。日后做图形界面可直接复用本模块。"""
from __future__ import annotations

import dataclasses
import os
from typing import Callable, Dict, List, Optional

from . import wenku8
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
    illustrations: int = 0
    fetched_cover: bool = False
    has_cover: bool = False
    cover_ext: Optional[str] = None
    cover_size: int = 0
    meta: Dict[str, str] = dataclasses.field(default_factory=dict)
    warnings: List[str] = dataclasses.field(default_factory=list)
    stats: Dict[str, object] = dataclasses.field(default_factory=dict)


def default_output_path(src: str) -> str:
    return os.path.splitext(src)[0] + ".epub"


def guess_book_id(src: str) -> Optional[int]:
    """UMD 文件名通常就是轻小说文库书号(例如 3943.umd -> 3943)。"""
    stem = os.path.splitext(os.path.basename(src))[0]
    if stem.isdigit():
        return int(stem)
    return None


def default_illustration_cache(src: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(src)), ".umd2epub-illustrations")


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
    fetch_illustrations: bool = False,
    fetch_cover: bool = False,
    book_id: Optional[int] = None,
    illustration_cache: Optional[str] = None,
    net_delay: float = 2.0,
    illustration_fetcher: Optional[Callable] = None,
    cover_fetcher: Optional[Callable] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Report:
    """把一个 .umd 转成 .epub; 失败时通过 Report 返回错误与退出码。

    fetch_illustrations=True 时会联网到轻小说文库抓取插图(UMD 本身不含图片数据);
    抓取失败只记 warning, 不影响本地转换。
    """
    say = log or (lambda message: None)
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

    resolved_id: Optional[int] = None
    if fetch_illustrations or fetch_cover:
        resolved_id = book_id if book_id is not None else guess_book_id(src)
        if resolved_id is None:
            report.warnings.append(
                "无法从文件名推断轻小说文库书号, 已跳过联网抓取(可用 --book-id 指定)"
            )
        else:
            report.meta["book_id"] = str(resolved_id)

    if (fetch_illustrations or fetch_cover) and not dry_run and resolved_id is not None:
        cache_dir = illustration_cache or default_illustration_cache(src)
        # 封面: UMD 自带封面时以 UMD 为准, 只有没封面才去站点取
        if fetch_cover and cover and book.cover is None:
            try:
                cover_hook = cover_fetcher or wenku8.fetch_cover
                fetched = cover_hook(
                    resolved_id, delay=net_delay, cache_dir=cache_dir, log=say,
                )
                if fetched is not None:
                    book.cover = fetched.data
                    book.cover_ext = fetched.ext
                    report.fetched_cover = True
                    report.has_cover = True
                    report.cover_ext = fetched.ext
                    report.cover_size = len(fetched.data)
            except wenku8.Wenku8Error as exc:
                report.warnings.append("封面抓取失败, 已按无封面转换: %s" % exc)
            except Exception as exc:
                report.warnings.append("封面抓取出现意外错误, 已按无封面转换: %s" % exc)
        if fetch_illustrations:
            try:
                fetcher = illustration_fetcher or wenku8.fetch_illustrations
                pages = fetcher(
                    resolved_id, delay=net_delay, cache_dir=cache_dir, log=say,
                )
                report.illustrations = wenku8.attach_illustrations(
                    book.chapters, pages, log=say,
                )
            except wenku8.Wenku8Error as exc:
                report.warnings.append("插图抓取失败, 已按无插图转换: %s" % exc)
            except Exception as exc:  # 网络/解析异常都不该毁掉本地转换
                report.warnings.append("插图抓取出现意外错误, 已按无插图转换: %s" % exc)

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
