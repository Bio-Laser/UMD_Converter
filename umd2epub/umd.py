# -*- coding: utf-8 -*-
"""UMD(掌上书院/早期掌阅 .umd 电子书)文本型格式解析。

文件结构(经真实 UMD 文件核对):

    功能块  0x23 | 功能ID | b2 | b3 | 块长(1 字节) | 负载(块长 - 5)
    数据块  0x24 | 数据ID(4 字节小端) | 块长(4 字节小端, 含 9 字节头) | 负载(块长 - 9)

正文是若干 zlib 流按文件出现顺序拼接而成, 内容为 UTF-16LE,
段落分隔符是 U+2029(字节序列 29 20), 需要还原成换行。
"""
from __future__ import annotations

import dataclasses
import struct
import zlib
from typing import Dict, List, Optional, Tuple

MAGIC = b"\x89\x9b\x9a\xde"

TAG_FUNC = 0x23
TAG_DATA = 0x24

FUNC_HEADER_LEN = 5
DATA_HEADER_LEN = 9

TYPE_TEXT = 1
TYPE_COMIC = 2

FUNC_VERSION = 1
FUNC_TITLE = 2
FUNC_AUTHOR = 3
FUNC_YEAR = 4
FUNC_MONTH = 5
FUNC_DAY = 6
FUNC_CATEGORY = 7
FUNC_PUBLISHER = 8
FUNC_VENDOR = 9
FUNC_CONTENT_ID = 10
FUNC_CONTENT_LENGTH = 11
FUNC_EOF = 12
FUNC_CONTENT_LIST = 0x81
FUNC_COVER = 0x82
FUNC_CHAPTER_OFFSETS = 0x83
FUNC_CHAPTER_TITLES = 0x84
FUNC_PAGE_OFFSET = 0x87
FUNC_CDS_KEY = 0xF0
FUNC_LICENSE = 0xF1

_META_FUNCS = {
    FUNC_TITLE: "title",
    FUNC_AUTHOR: "author",
    FUNC_YEAR: "year",
    FUNC_MONTH: "month",
    FUNC_DAY: "day",
    FUNC_CATEGORY: "category",
    FUNC_PUBLISHER: "publisher",
    FUNC_VENDOR: "vendor",
}

_IMAGE_EXT_BY_TYPE = {0: "bmp", 1: "jpg", 2: "gif"}

FALLBACK_CHAPTER_TITLE = "正文"


class UmdError(Exception):
    """UMD 处理相关异常的基类。"""


class UmdFormatError(UmdError):
    """不是 UMD 文件, 或文件损坏/无法解析。"""


class UnsupportedUmdError(UmdError):
    """是 UMD, 但属于当前不支持的变体。"""


@dataclasses.dataclass
class Chapter:
    title: str
    text: str


@dataclasses.dataclass
class UmdBook:
    meta: Dict[str, str]
    chapters: List[Chapter]
    cover: Optional[bytes] = None
    cover_ext: Optional[str] = None
    warnings: List[str] = dataclasses.field(default_factory=list)
    stats: Dict[str, object] = dataclasses.field(default_factory=dict)

    @property
    def title(self) -> str:
        return (self.meta.get("title") or "").strip()

    @property
    def author(self) -> str:
        return (self.meta.get("author") or "").strip()

    @property
    def degraded(self) -> bool:
        """是否走了无章节块兜底。"""
        return bool(self.stats.get("degraded"))

    @property
    def total_chars(self) -> int:
        return sum(len(chapter.text) for chapter in self.chapters)

    def date_text(self) -> str:
        parts = [self.meta.get(key, "").strip() for key in ("year", "month", "day")]
        return "-".join(part for part in parts if part)


class _Blocks:
    """一次块遍历的中间结果。"""

    def __init__(self) -> None:
        self.funcs: List[Tuple[int, bytes]] = []
        self.data: Dict[int, bytes] = {}
        self.order: List[int] = []
        self.warnings: List[str] = []
        self.end = 4


def _u32(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _u16(data: bytes) -> str:
    return data.decode("utf-16-le", "replace")


def _clean_text(value: str) -> str:
    return value.replace("\x00", "").strip()


def _inflate(blob: bytes) -> Tuple[bytes, bool]:
    """尝试 zlib 解压, 失败则原样返回(部分变体的正文块没有压缩)。"""
    try:
        return zlib.decompress(blob), True
    except zlib.error:
        return blob, False


def _walks_like_text(blob: bytes) -> bool:
    """判断一段字节是否像 UTF-16LE 正文, 用于没有正文块列表时的兜底扫描。"""
    if len(blob) < 4 or len(blob) % 2:
        return False
    try:
        text = blob.decode("utf-16-le")
    except UnicodeDecodeError:
        return False
    if not text:
        return False
    acceptable = "\r\n\t\u2028\u2029"
    printable = sum(1 for ch in text if ch in acceptable or ch.isprintable())
    return printable / len(text) >= 0.9


def _walk(data: bytes) -> _Blocks:
    blocks = _Blocks()
    total = len(data)
    offset = 4
    while offset + FUNC_HEADER_LEN <= total:
        tag = data[offset]
        if tag == TAG_FUNC:
            func_id = data[offset + 1]
            length = data[offset + 4]
            if length < FUNC_HEADER_LEN or offset + length > total:
                blocks.warnings.append("功能块长度异常, 解析到此停止")
                break
            blocks.funcs.append((func_id, data[offset + FUNC_HEADER_LEN:offset + length]))
            offset += length
            if func_id == FUNC_EOF:
                break
        elif tag == TAG_DATA:
            if offset + DATA_HEADER_LEN > total:
                blocks.warnings.append("数据块头部不完整, 解析到此停止")
                break
            data_id = _u32(data, offset + 1)
            length = _u32(data, offset + 5)
            if length < DATA_HEADER_LEN or offset + length > total:
                blocks.warnings.append("数据块长度异常, 解析到此停止")
                break
            blocks.data[data_id] = data[offset + DATA_HEADER_LEN:offset + length]
            blocks.order.append(data_id)
            offset += length
        else:
            blocks.warnings.append("遇到未知块标记 0x%02x, 解析到此停止" % tag)
            break
    blocks.end = offset
    return blocks


def _collect_meta(blocks: _Blocks, warnings: List[str]) -> Dict[str, object]:
    meta: Dict[str, str] = {}
    file_type: Optional[int] = None
    pointers: Dict[str, Optional[int]] = {
        "content_list": None,
        "offsets": None,
        "titles": None,
        "cover": None,
    }
    content_length: Optional[int] = None
    cover_type: Optional[int] = None

    for func_id, payload in blocks.funcs:
        if func_id == FUNC_VERSION:
            file_type = payload[0] if payload else None
        elif func_id in _META_FUNCS:
            meta[_META_FUNCS[func_id]] = _clean_text(_u16(payload))
        elif func_id == FUNC_CONTENT_LENGTH and len(payload) >= 4:
            content_length = _u32(payload)
        elif func_id == FUNC_CONTENT_LIST and len(payload) >= 4:
            pointers["content_list"] = _u32(payload)
        elif func_id == FUNC_COVER and len(payload) >= 5:
            cover_type = payload[0]
            pointers["cover"] = _u32(payload, 1)
        elif func_id == FUNC_CHAPTER_OFFSETS and len(payload) >= 4:
            pointers["offsets"] = _u32(payload)
        elif func_id == FUNC_CHAPTER_TITLES and len(payload) >= 4:
            pointers["titles"] = _u32(payload)

    if file_type == TYPE_COMIC:
        raise UnsupportedUmdError("这是漫画型 UMD, 当前版本只支持文本型 UMD")
    if file_type is None:
        warnings.append("没有读到文件类型标记, 按文本型处理")
        meta["type"] = "unknown"
    elif file_type == TYPE_TEXT:
        meta["type"] = "text"
    else:
        warnings.append("未识别的文件类型标记 %s, 按文本型处理" % file_type)
        meta["type"] = "unknown"

    return {
        "meta": meta,
        "pointers": pointers,
        "content_length": content_length,
        "cover_type": cover_type,
    }


def _assemble_content(
    blocks: _Blocks,
    content_list_id: Optional[int],
    structural_ids: set,
    warnings: List[str],
) -> Tuple[bytes, bool]:
    """还原正文块. 返回 (原始字节, 是否走了兜底扫描)."""
    if content_list_id is not None and content_list_id in blocks.data:
        payload = blocks.data[content_list_id]
        wanted = [_u32(payload, i * 4) for i in range(len(payload) // 4)]
        missing = [item for item in wanted if item not in blocks.data]
        if missing:
            warnings.append("正文块列表中有 %d 个块缺失" % len(missing))
        wanted_set = set(wanted)
        chosen = [item for item in blocks.order if item in wanted_set]
        if chosen:
            parts: List[bytes] = []
            raw_count = 0
            for data_id in chosen:
                blob, compressed = _inflate(blocks.data[data_id])
                if not compressed:
                    raw_count += 1
                parts.append(blob)
            if raw_count:
                warnings.append("%d 个正文块不是 zlib 压缩, 已按原始数据使用" % raw_count)
            return b"".join(parts), False
        warnings.append("正文块列表指向的块都不存在, 改用数据块扫描")

    # 兜底: 没有可用的正文块列表, 扫描全部数据块挑出像正文的块
    parts = []
    for data_id in blocks.order:
        if data_id in structural_ids:
            continue
        blob, _ = _inflate(blocks.data[data_id])
        if _walks_like_text(blob):
            parts.append(blob)
    if parts:
        warnings.append("缺少可用的正文块列表(0x81), 已扫描全部数据块还原正文")
    return b"".join(parts), True


def _read_offsets(payload: Optional[bytes]) -> List[int]:
    if not payload:
        return []
    return [_u32(payload, i * 4) for i in range(len(payload) // 4)]


def _read_titles(payload: Optional[bytes]) -> List[str]:
    if not payload:
        return []
    titles: List[str] = []
    offset = 0
    while offset < len(payload):
        length = payload[offset]
        titles.append(_clean_text(_u16(payload[offset + 1:offset + 1 + length])))
        offset += 1 + length
    return titles


def _chapter_name(title: str, index: int) -> str:
    return title or "第%d章" % index


def _build_chapters(
    content_raw: bytes,
    content_end: int,
    offsets: List[int],
    titles: List[str],
) -> Tuple[List[Chapter], bool, str]:
    reasons: List[str] = []
    usable = bool(offsets) and bool(titles) and len(offsets) == len(titles)
    if usable:
        if all(0 <= value < content_end for value in offsets):
            chapters = []
            for index, title in enumerate(titles):
                start = offsets[index]
                end = offsets[index + 1] if index + 1 < len(offsets) else content_end
                if end <= start:
                    end = min(start, content_end)
                chapters.append(Chapter(
                    title=_chapter_name(title, index + 1),
                    text=_clean_text(_u16(content_raw[start:end])),
                ))
            return chapters, False, ""
        reasons.append("章节偏移超出正文范围")
    elif offsets and titles:
        reasons.append("章节偏移表(%d)与章节标题表(%d)数量不一致" % (len(offsets), len(titles)))
    elif not offsets and not titles:
        reasons.append("缺少章节偏移表(0x83)与章节标题表(0x84)")
    elif not offsets:
        reasons.append("缺少章节偏移表(0x83)")
    else:
        reasons.append("缺少章节标题表(0x84)")

    title = _chapter_name(titles[0], 1) if titles else FALLBACK_CHAPTER_TITLE
    chapters = [Chapter(title=title, text=_clean_text(_u16(content_raw[:content_end])))]
    return chapters, True, "；".join(reasons)


def _sniff_image(blob: Optional[bytes]) -> Optional[str]:
    if not blob:
        return None
    if blob[:3] == b"\xff\xd8\xff":
        return "jpg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if blob[:4] in (b"GIF8",):
        return "gif"
    if blob[:2] == b"BM":
        return "bmp"
    return None


def parse_umd(data: bytes) -> UmdBook:
    """解析文本型 UMD, 返回 UmdBook。"""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("parse_umd() 需要 bytes")
    data = bytes(data)
    if len(data) < 4 or data[:4] != MAGIC:
        raise UmdFormatError("不是 UMD 文件(文件头魔数不符)")
    if len(data) < 32:
        raise UmdFormatError("文件过小, 可能是损坏的 UMD")

    blocks = _walk(data)
    warnings: List[str] = list(blocks.warnings)
    info = _collect_meta(blocks, warnings)
    meta = info["meta"]
    pointers = info["pointers"]
    content_length = info["content_length"]
    cover_type = info["cover_type"]

    structural_ids = {value for key, value in pointers.items() if value is not None}
    content_raw, scanned = _assemble_content(blocks, pointers["content_list"], structural_ids, warnings)
    if not content_raw:
        raise UmdFormatError("未能从 UMD 中解出正文")

    content_raw = content_raw.replace(b"\x29\x20", b"\x0a\x00")  # U+2029 -> 换行
    content_end = len(content_raw)
    if content_length is not None:
        if content_length <= 0:
            warnings.append("声明的正文长度为 0, 已按实际解出长度处理")
        elif content_length < content_end:
            if content_end - content_length > 64:
                warnings.append(
                    "声明正文长度(%d)小于实际解出长度(%d), 已按声明长度截取"
                    % (content_length, content_end)
                )
            content_end = content_length
        elif content_length > content_end:
            warnings.append(
                "声明正文长度(%d)大于实际解出长度(%d), 已按实际长度处理"
                % (content_length, content_end)
            )

    offsets = _read_offsets(blocks.data.get(pointers["offsets"]) if pointers["offsets"] else None)
    titles = _read_titles(blocks.data.get(pointers["titles"]) if pointers["titles"] else None)
    chapters, degraded, reason = _build_chapters(content_raw, content_end, offsets, titles)
    if degraded and reason:
        warnings.append("无章节结构, 已整篇作为单章输出: %s" % reason)

    cover = None
    cover_ext = None
    cover_id = pointers["cover"]
    if cover_id is not None:
        blob = blocks.data.get(cover_id)
        if blob is None:
            warnings.append("封面数据块缺失, 已跳过封面")
        else:
            cover_ext = _sniff_image(blob)
            if cover_ext is None:
                cover_ext = _IMAGE_EXT_BY_TYPE.get(cover_type)
                if cover_ext:
                    warnings.append("封面格式未识别, 按 UMD 声明类型 %s 使用" % cover_ext)
            if cover_ext:
                cover = blob
            else:
                warnings.append("封面数据无法识别, 已跳过封面")

    if blocks.end != len(data):
        warnings.append(
            "解析在 %d/%d 字节处提前结束(文件可能被截断或含未知块)"
            % (blocks.end, len(data))
        )

    stats: Dict[str, object] = {
        "size": len(data),
        "parsed_end": blocks.end,
        "complete": blocks.end == len(data),
        "data_blocks": len(blocks.data),
        "func_ids": sorted({func_id for func_id, _ in blocks.funcs}),
        "content_bytes": content_end,
        "declared_content_bytes": content_length,
        "degraded": degraded,
        "degrade_reason": reason,
        "scanned_content": scanned,
    }

    return UmdBook(
        meta=meta,
        chapters=chapters,
        cover=cover,
        cover_ext=cover_ext,
        warnings=warnings,
        stats=stats,
    )
