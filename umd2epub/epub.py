# -*- coding: utf-8 -*-
"""EPUB 2.0(OPF 2.0 + NCX) 打包, 仅用标准库。

EPUB 就是一个 ZIP: 其中 mimetype 条目必须第一个写入且不压缩,
其余条目压缩与否都合法。
"""
from __future__ import annotations

import dataclasses
import html
import io
import os
import re
import uuid
import zipfile
from collections import OrderedDict
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

from .umd import UmdBook

MIMETYPE = "application/epub+zip"

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

STYLE_CSS = """body { line-height: 1.6; margin: 0 0.6em; }
h1 { font-size: 1.3em; margin: 1.4em 0 0.8em; text-align: center; }
p { text-indent: 2em; margin: 0.5em 0; }
div.illus { margin: 0.8em 0; text-align: center; page-break-inside: avoid; }
div.illus img { max-width: 100%; height: auto; }
div.cover { margin: 0; text-align: center; }
div.cover img { max-width: 100%; height: auto; }
"""

_XHTML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="../style.css"/>
</head>
<body>
{heading}{body}
</body>
</html>
"""

_MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
}

_ALLOWED_TAGS = {
    "p", "div", "br", "i", "b", "em", "strong", "u", "span",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
_VOID_TAGS = {"br"}
_BLOCK_HINTS = ("<br", "<BR", "<p>", "<p ", "<P>", "<P ", "<div", "<DIV", "<span", "<SPAN")

_XML_ILLEGAL = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")

_ILLUSTRATION_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="../style.css"/>
</head>
<body>
<div class="illus"><img src="{src}" alt="{alt}"/></div>
</body>
</html>
"""

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_COVER_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="../style.css"/>
</head>
<body>
<div class="cover"><img src="{src}" alt="{alt}"/></div>
</body>
</html>
"""


def store_illustration(image, store: "OrderedDict[str, bytes]") -> str:
    """把插图登记进 EPUB 文件表, 返回包内路径(同一张图只存一份)。"""
    stem = os.path.splitext(getattr(image, "name", "") or "")[0]
    stem = _SAFE_NAME_RE.sub("_", stem).strip("._") or "illustration"
    ext = (getattr(image, "ext", "") or "jpg").lower().lstrip(".") or "jpg"
    href = "Images/%s.%s" % (stem, ext)
    if href not in store:
        store[href] = image.data
    return href


def xml_safe(text: str) -> str:
    """去掉 XML 1.0 不允许的控制字符(XML 里出现就会让整本书解析失败)。"""
    return _XML_ILLEGAL.sub("", text)


def _esc(value: object) -> str:
    return html.escape(xml_safe(str(value)), quote=True)


class _Normalizer(HTMLParser):
    """把 UMD 正文里夹杂的 HTML 归一化成严格合法的 XHTML 片段。

    只保留白名单标签, 其余标签整体转义成可见文本;
    未闭合的标签会在结尾补上, 保证 XML 结构完整。
    """

    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self._parts: List[str] = []
        self._stack: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _VOID_TAGS:
            self._parts.append("<br/>")
        elif tag in _ALLOWED_TAGS:
            self._parts.append("<%s>" % tag)
            self._stack.append(tag)
        else:
            self._parts.append(html.escape(self.get_starttag_text() or ""))

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _VOID_TAGS:
            self._parts.append("<br/>")
        elif tag in _ALLOWED_TAGS:
            self._parts.append("<%s/>" % tag)
        else:
            self._parts.append(html.escape(self.get_starttag_text() or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if tag in self._stack:
            while self._stack:
                top = self._stack.pop()
                self._parts.append("</%s>" % top)
                if top == tag:
                    return
        elif tag in _ALLOWED_TAGS:
            return
        else:
            self._parts.append(html.escape("</%s>" % tag))

    def handle_data(self, data: str) -> None:
        self._parts.append(html.escape(data))

    def result(self) -> str:
        while self._stack:
            self._parts.append("</%s>" % self._stack.pop())
        return "".join(self._parts)


def _paragraphs(text: str) -> str:
    body = "\n".join(
        "<p>%s</p>" % html.escape(line) for line in text.split("\n") if line.strip()
    )
    return body or "<p></p>"


def render_body(text: str) -> str:
    """正文 -> XHTML 片段。"""
    text = xml_safe(text.replace("\r\n", "\n").replace("\r", "\n"))
    if not text.strip():
        return "<p></p>"
    if any(hint in text for hint in _BLOCK_HINTS):
        parser = _Normalizer()
        try:
            parser.feed(text)
            parser.close()
        except Exception:
            return _paragraphs(text)
        body = parser.result()
        return body if body.strip() else "<p></p>"
    return _paragraphs(text)


def split_text(text: str, limit: int) -> List[str]:
    """按段落边界把超长章节切成多片, 避免单个 XHTML 过大。"""
    if limit <= 0 or len(text) <= limit:
        return [text]
    parts: List[str] = []
    current: List[str] = []
    size = 0
    for paragraph in text.split("\n"):
        addition = len(paragraph) + 1
        if current and size + addition > limit:
            parts.append("\n".join(current))
            current = []
            size = 0
        current.append(paragraph)
        size += addition
    if current:
        parts.append("\n".join(current))
    return parts or [text]


@dataclasses.dataclass
class EpubOptions:
    title: str = ""
    author: str = ""
    language: str = "zh"
    include_cover: bool = True
    split_chars: int = 20000
    identifier: str = ""


def build_epub(book: UmdBook, options: Optional[EpubOptions] = None) -> bytes:
    """把 UmdBook 打包成 EPUB 字节流(全内存)。"""
    opts = options or EpubOptions()
    title = xml_safe(opts.title.strip() or book.title or "未命名").strip() or "未命名"
    author = xml_safe(opts.author.strip() or book.author or "佚名").strip() or "佚名"
    language = xml_safe(opts.language.strip() or "zh").strip() or "zh"
    identifier = opts.identifier.strip() or ("urn:uuid:%s" % uuid.uuid4())

    cover = book.cover if (opts.include_cover and book.cover) else None
    cover_ext = (book.cover_ext or "jpg").lower()
    cover_href = "cover.%s" % cover_ext
    cover_media_type = _MEDIA_TYPES.get(cover_ext, "image/jpeg")

    manifest: List[str] = [
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine: List[str] = []
    body_docs: List[Tuple[str, bytes]] = []
    chapter_targets: List[str] = []
    illustration_files: "OrderedDict[str, bytes]" = OrderedDict()

    # 封面页放在 spine 最前面: 部分阅读器只认封面页, 不认 opf 里的 meta
    if cover is not None:
        cover_doc = _COVER_TEMPLATE.format(
            lang=_esc(language),
            title=_esc(title),
            src=_esc("../" + cover_href),
            alt=_esc("封面"),
        )
        body_docs.append(("OEBPS/text/cover.xhtml", cover_doc.encode("utf-8")))
        manifest.append(
            '<item id="coverpage" href="text/cover.xhtml" media-type="application/xhtml+xml"/>'
        )
        spine.append('<itemref idref="coverpage"/>')

    for chapter_index, chapter in enumerate(book.chapters, 1):
        name = xml_safe(chapter.title).strip() or "第%d章" % chapter_index
        chunks = split_text(chapter.text, opts.split_chars)
        if not chapter.text.strip() and chapter.images:
            chunks = []          # 只放插图的章节, 不再生成空白正文页
        target = ""
        for part_index, chunk in enumerate(chunks, 1):
            file_name = "text/c%04d_p%02d.xhtml" % (chapter_index, part_index)
            item_id = "c%04dp%02d" % (chapter_index, part_index)
            if len(chunks) > 1 and part_index > 1:
                doc_title = "%s（续 %d/%d）" % (name, part_index, len(chunks))
            else:
                doc_title = name
            heading = ""
            if part_index == 1:
                display = name if len(chunks) == 1 else "%s（1/%d）" % (name, len(chunks))
                heading = "<h1>%s</h1>\n" % _esc(display)
            doc = _XHTML_TEMPLATE.format(
                lang=_esc(language),
                title=_esc(doc_title),
                heading=heading,
                body=render_body(chunk),
            )
            body_docs.append(("OEBPS/" + file_name, doc.encode("utf-8")))
            manifest.append(
                '<item id="%s" href="%s" media-type="application/xhtml+xml"/>' % (item_id, file_name)
            )
            spine.append('<itemref idref="%s"/>' % item_id)
            target = target or file_name

        for image_index, image in enumerate(chapter.images, 1):
            href = store_illustration(image, illustration_files)
            file_name = "text/c%04d_i%02d.xhtml" % (chapter_index, image_index)
            item_id = "c%04di%02d" % (chapter_index, image_index)
            doc = _ILLUSTRATION_TEMPLATE.format(
                lang=_esc(language),
                title=_esc("%s 插图 %d" % (name, image_index)),
                src=_esc("../" + href),
                alt=_esc("插图 %d" % image_index),
            )
            body_docs.append(("OEBPS/" + file_name, doc.encode("utf-8")))
            manifest.append(
                '<item id="%s" href="%s" media-type="application/xhtml+xml"/>' % (item_id, file_name)
            )
            spine.append('<itemref idref="%s"/>' % item_id)
            target = target or file_name
        chapter_targets.append(target)

    if not body_docs:
        file_name = "text/c0001_p01.xhtml"
        doc = _XHTML_TEMPLATE.format(
            lang=_esc(language), title=_esc("正文"), heading="", body="<p></p>",
        )
        body_docs.append(("OEBPS/" + file_name, doc.encode("utf-8")))
        manifest.append(
            '<item id="c0001p01" href="%s" media-type="application/xhtml+xml"/>' % file_name
        )
        spine.append('<itemref idref="c0001p01"/>')
        chapter_targets.append(file_name)

    for number, href in enumerate(illustration_files, 1):
        ext = href.rsplit(".", 1)[-1].lower()
        manifest.append(
            '<item id="img%04d" href="%s" media-type="%s"/>'
            % (number, href, _MEDIA_TYPES.get(ext, "image/jpeg"))
        )

    if cover is not None:
        manifest.append(
            '<item id="coverimg" href="%s" media-type="%s"/>' % (cover_href, cover_media_type)
        )

    fallback_target = next((item for item in chapter_targets if item), "text/c0001_p01.xhtml")
    nav_points: List[str] = []
    for chapter_index, chapter in enumerate(book.chapters, 1):
        name = xml_safe(chapter.title).strip() or "第%d章" % chapter_index
        target = chapter_targets[chapter_index - 1] or fallback_target
        nav_points.append(
            '<navPoint id="n%d" playOrder="%d"><navLabel><text>%s</text></navLabel>'
            '<content src="%s"/></navPoint>' % (chapter_index, chapter_index, _esc(name), target)
        )

    guide: List[str] = []
    if cover is not None:
        guide.append('<reference type="cover" title="封面" href="text/cover.xhtml"/>')
    guide.append('<reference type="text" title="正文" href="%s"/>' % fallback_target)

    opf_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">',
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">',
        '<dc:title>%s</dc:title>' % _esc(title),
        '<dc:creator opf:role="aut">%s</dc:creator>' % _esc(author),
        '<dc:language>%s</dc:language>' % _esc(language),
        '<dc:identifier id="bookid">%s</dc:identifier>' % _esc(identifier),
    ]
    if cover is not None:
        opf_lines.append('<meta name="cover" content="coverimg"/>')
    opf_lines += [
        '</metadata>',
        '<manifest>',
        "\n".join(manifest),
        '</manifest>',
        '<spine toc="ncx">',
        "\n".join(spine),
        '</spine>',
    ]
    if guide:
        opf_lines += ['<guide>', "\n".join(guide), '</guide>']
    opf_lines.append('</package>')

    ncx_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">',
        '<head>',
        '<meta name="dtb:uid" content="%s"/>' % _esc(identifier),
        '<meta name="dtb:depth" content="1"/>',
        '<meta name="dtb:totalPageCount" content="0"/>',
        '<meta name="dtb:maxPageNumber" content="0"/>',
        '</head>',
        '<docTitle><text>%s</text></docTitle>' % _esc(title),
        '<navMap>',
        "\n".join(nav_points),
        '</navMap>',
        '</ncx>',
    ]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype", (1980, 1, 1, 0, 0, 0)),
            MIMETYPE,
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", CONTAINER_XML)
        archive.writestr("OEBPS/style.css", STYLE_CSS)
        if cover is not None:
            archive.writestr("OEBPS/" + cover_href, cover)
        for href, data in illustration_files.items():
            archive.writestr("OEBPS/" + href, data)
        for name, payload in body_docs:
            archive.writestr(name, payload)
        archive.writestr("OEBPS/content.opf", "\n".join(opf_lines) + "\n")
        archive.writestr("OEBPS/toc.ncx", "\n".join(ncx_lines) + "\n")
    return buffer.getvalue()
