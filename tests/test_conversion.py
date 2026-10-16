# -*- coding: utf-8 -*-
"""自测: 合成 UMD 样本 -> 解析 -> EPUB -> CLI。只用标准库, 不需要任何外部文件。"""
from __future__ import annotations

import contextlib
import io
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
import xml.etree.ElementTree as ET

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from umd2epub import parse_umd  # noqa: E402
from umd2epub import cli, wenku8  # noqa: E402
from umd2epub.convert import (  # noqa: E402
    EXIT_EXISTS,
    EXIT_FORMAT,
    EXIT_OK,
    EXIT_UNSUPPORTED,
    convert_file,
)
from umd2epub.epub import EpubOptions, build_epub  # noqa: E402
from umd2epub.umd import Chapter, Illustration, UmdFormatError, UnsupportedUmdError  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 28 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

NCX_NS = "{http://www.daisy.org/z3986/2005/ncx/}"
OPF_NS = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}


def _u16(text: str) -> bytes:
    return text.encode("utf-16-le")


def _func(func_id: int, payload: bytes) -> bytes:
    length = 5 + len(payload)
    assert length <= 255, "功能块长度只有一个字节"
    return bytes([0x23, func_id, 0x00, 0x00, length]) + payload


def _data(data_id: int, payload: bytes) -> bytes:
    length = 9 + len(payload)
    return b"\x24" + struct.pack("<I", data_id) + struct.pack("<I", length) + payload


def build_umd(
    title: str = "测试书",
    author: str = "测试作者",
    chapters=(("第一章", "第一段\u2029第二段"), ("第二章", "第二章内容")),
    cover: bytes = None,
    include_chapter_blocks: bool = True,
    include_content_list: bool = True,
    title_count: int = None,
    raw_content: bool = False,
    file_type: int = 1,
) -> bytes:
    """按真实 UMD 的块布局合成测试文件(仅用于自测)。"""
    content = b"".join(_u16(text) for _, text in chapters)
    offsets = []
    cursor = 0
    for _, text in chapters:
        offsets.append(cursor)
        cursor += len(_u16(text))

    data_id_list, data_id_offsets, data_id_titles, data_id_cover = 0x2100, 0x2101, 0x2102, 0x1000

    content_blocks = []
    content_ids = []
    base = 0x2000
    if raw_content:
        content_ids = [base]
        content_blocks.append((base, content))
    else:
        cut = (len(content) // 2) // 2 * 2
        chunks = [content[:cut], content[cut:]] if 0 < cut < len(content) else [content]
        for index, chunk in enumerate(chunks):
            content_ids.append(base + index)
            content_blocks.append((base + index, zlib.compress(chunk)))

    funcs = [
        _func(1, bytes([file_type, 0x56, 0x65])),
        _func(2, _u16(title)),
        _func(3, _u16(author)),
        _func(4, _u16("2024")),
        _func(5, _u16("01")),
        _func(6, _u16("02")),
        _func(7, _u16("测试分类")),
        _func(8, _u16("测试出版社")),
        _func(9, _u16("测试发行商")),
        _func(10, struct.pack("<I", 0x2000)),
        _func(11, struct.pack("<I", len(content))),
    ]
    if include_content_list:
        funcs.append(_func(0x81, struct.pack("<I", data_id_list)))
    if cover is not None:
        funcs.append(_func(0x82, bytes([1]) + struct.pack("<I", data_id_cover)))
    if include_chapter_blocks:
        funcs.append(_func(0x83, struct.pack("<I", data_id_offsets)))
        funcs.append(_func(0x84, struct.pack("<I", data_id_titles)))

    data_blocks = []
    if include_content_list:
        data_blocks.append((data_id_list, b"".join(struct.pack("<I", item) for item in content_ids)))
    if include_chapter_blocks:
        titles = [name for name, _ in chapters]
        if title_count is not None and title_count > len(titles):
            titles = titles + ["补充标题%d" % i for i in range(len(titles) + 1, title_count + 1)]
        data_blocks.append((data_id_offsets, b"".join(struct.pack("<I", value) for value in offsets)))
        data_blocks.append((data_id_titles, b"".join(bytes([len(_u16(name))]) + _u16(name) for name in titles)))
    if cover is not None:
        data_blocks.append((data_id_cover, cover))
    data_blocks.extend(content_blocks)

    body = [b"\x89\x9b\x9a\xde"] + funcs + [_data(i, blob) for i, blob in data_blocks]
    payload = b"".join(body)
    return payload + _func(12, struct.pack("<I", len(payload) + 9))


def expected_text(chapters) -> str:
    return "".join(text for _, text in chapters).replace("\u2029", "\n")


def epub_archive(payload: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(payload))


def chapter_files(archive: zipfile.ZipFile):
    """正文/插图页, 不含封面页。"""
    return [name for name in archive.namelist()
            if name.startswith("OEBPS/text/") and not name.endswith("cover.xhtml")]


class ParseTests(unittest.TestCase):
    def test_text_umd_with_chapters(self):
        chapters = (("第一章", "第一段\u2029第二段"), ("第二章", "第二章内容"))
        book = parse_umd(build_umd(chapters=chapters))
        self.assertEqual(book.title, "测试书")
        self.assertEqual(book.author, "测试作者")
        self.assertEqual(book.date_text(), "2024-01-02")
        self.assertEqual(book.meta["publisher"], "测试出版社")
        self.assertEqual([item.title for item in book.chapters], ["第一章", "第二章"])
        self.assertEqual([item.text for item in book.chapters], ["第一段\n第二段", "第二章内容"])
        self.assertFalse(book.degraded)
        self.assertTrue(book.stats["complete"])

    def test_cover_is_extracted(self):
        book = parse_umd(build_umd(cover=JPEG))
        self.assertEqual(book.cover, JPEG)
        self.assertEqual(book.cover_ext, "jpg")

    def test_missing_chapter_blocks_falls_back_to_single_chapter(self):
        chapters = (("第一章", "甲\u2029乙"), ("第二章", "丙"))
        book = parse_umd(build_umd(chapters=chapters, include_chapter_blocks=False))
        self.assertTrue(book.degraded)
        self.assertEqual(len(book.chapters), 1)
        self.assertEqual(book.chapters[0].text, expected_text(chapters))

    def test_offsets_titles_mismatch_falls_back(self):
        chapters = (("第一章", "甲"), ("第二章", "乙"))
        book = parse_umd(build_umd(chapters=chapters, title_count=3))
        self.assertTrue(book.degraded)
        self.assertEqual(len(book.chapters), 1)
        self.assertEqual(book.chapters[0].text, expected_text(chapters))
        self.assertTrue(any("数量不一致" in item for item in book.warnings))

    def test_missing_content_list_scans_data_blocks(self):
        chapters = (("第一章", "甲\u2029乙"), ("第二章", "丙\u2029丁"))
        book = parse_umd(build_umd(chapters=chapters, include_content_list=False))
        self.assertEqual([item.text for item in book.chapters], ["甲\n乙", "丙\n丁"])

    def test_non_zlib_content_block_is_kept(self):
        book = parse_umd(build_umd(chapters=(("第一章", "未压缩正文"),), raw_content=True))
        self.assertEqual(book.chapters[0].text, "未压缩正文")
        self.assertTrue(any("不是 zlib" in item for item in book.warnings))

    def test_comic_type_is_rejected(self):
        with self.assertRaises(UnsupportedUmdError):
            parse_umd(build_umd(file_type=2))

    def test_bad_magic_is_rejected(self):
        with self.assertRaises(UmdFormatError):
            parse_umd(b"not a umd file" + b"\x00" * 64)

    def test_truncated_file_is_rejected(self):
        with self.assertRaises(UmdFormatError):
            parse_umd(build_umd()[:24])


class EpubTests(unittest.TestCase):
    def test_structure_and_metadata(self):
        chapters = (("第一章", "第一段\u2029第二段"), ("第二章", "第二章内容"))
        book = parse_umd(build_umd(chapters=chapters, cover=JPEG, title="结构测试", author="某人"))
        payload = build_epub(book, EpubOptions(split_chars=0))
        archive = epub_archive(payload)

        names = archive.namelist()
        self.assertEqual(names[0], "mimetype")
        self.assertEqual(archive.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
        self.assertEqual(archive.read("mimetype"), b"application/epub+zip")

        ET.fromstring(archive.read("META-INF/container.xml"))
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        ET.fromstring(archive.read("OEBPS/toc.ncx"))
        self.assertEqual(opf.findtext("opf:metadata/dc:title", namespaces=OPF_NS), "结构测试")
        self.assertEqual(opf.findtext("opf:metadata/dc:creator", namespaces=OPF_NS), "某人")
        self.assertEqual(archive.read("OEBPS/cover.jpg"), JPEG)

        files = chapter_files(archive)
        self.assertEqual(len(files), 2)
        # spine = 封面页 + 2 个正文章节页
        spine = opf.findall("opf:spine/opf:itemref", OPF_NS)
        self.assertEqual(len(spine), 3)
        self.assertEqual(spine[0].get("idref"), "coverpage")
        cover_page = "OEBPS/text/cover.xhtml"
        self.assertIn(cover_page, names)
        self.assertIn(b"../cover.jpg", archive.read(cover_page))
        ET.fromstring(archive.read(cover_page))
        self.assertIn('type="cover" title="封面" href="text/cover.xhtml"',
                      archive.read("OEBPS/content.opf").decode("utf-8"))
        navpoints = ET.fromstring(archive.read("OEBPS/toc.ncx")).findall(
            NCX_NS + "navMap/" + NCX_NS + "navPoint"
        )
        self.assertEqual(len(navpoints), 2)

        text = "\n".join("".join(ET.fromstring(archive.read(name)).itertext()) for name in files)
        self.assertIn("第一段", text)
        self.assertIn("第二段", text)
        self.assertIn("第二章内容", text)

    def test_long_chapter_is_split(self):
        long_chapter = "\u2029".join("段落%03d" % index for index in range(400))
        book = parse_umd(build_umd(chapters=(("第一章", long_chapter),)))
        payload = build_epub(book, EpubOptions(split_chars=500))
        archive = epub_archive(payload)

        files = chapter_files(archive)
        self.assertGreater(len(files), 3)
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        self.assertEqual(len(opf.findall("opf:spine/opf:itemref", OPF_NS)), len(files))
        navpoints = ET.fromstring(archive.read("OEBPS/toc.ncx")).findall(
            NCX_NS + "navMap/" + NCX_NS + "navPoint"
        )
        self.assertEqual(len(navpoints), 1)
        for name in files:
            ET.fromstring(archive.read(name))

    def test_html_snippet_is_normalized(self):
        book = parse_umd(build_umd(chapters=(("第一章", "<p>段落一<br>下一行</p>"),)))
        archive = epub_archive(build_epub(book))
        name = chapter_files(archive)[0]
        root = ET.fromstring(archive.read(name))
        self.assertIn("段落一", "".join(root.itertext()))
        self.assertIn("下一行", "".join(root.itertext()))

    def test_illegal_xml_chars_are_stripped(self):
        book = parse_umd(build_umd(title="坏\x01名", chapters=(("第一章", "正文\x02内容"),)))
        archive = epub_archive(build_epub(book))
        opf = ET.fromstring(archive.read("OEBPS/content.opf"))
        self.assertIn("坏名", ET.tostring(opf, encoding="unicode"))


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, payload: bytes) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_convert_then_refuse_overwrite_then_force(self):
        src = self._write("小说.umd", build_umd(title="CLI 测试"))
        code, _, _ = self._run([src, "--quiet"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "小说.epub")))

        code, _, err = self._run([src, "--quiet"])
        self.assertEqual(code, EXIT_EXISTS)
        self.assertIn("已存在", err)

        code, _, _ = self._run([src, "--quiet", "--force"])
        self.assertEqual(code, EXIT_OK)

    def test_dry_run_writes_nothing(self):
        src = self._write("dry.umd", build_umd(title="只解析"))
        code, out, _ = self._run([src, "--dry-run"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("只解析", out)
        self.assertIn("章节: 2 章", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "dry.epub")))

    def test_comic_and_bad_file_exit_codes(self):
        comic = self._write("comic.umd", build_umd(file_type=2))
        code, _, err = self._run([comic, "--quiet"])
        self.assertEqual(code, EXIT_UNSUPPORTED)
        self.assertIn("漫画型", err)

        broken = self._write("bad.umd", b"not a umd" + b"\x00" * 32)
        code, _, _ = self._run([broken, "--quiet"])
        self.assertEqual(code, EXIT_FORMAT)

    def test_batch_continues_after_failure(self):
        good = self._write("good.umd", build_umd(title="好的"))
        broken = self._write("bad.umd", b"not a umd" + b"\x00" * 32)
        code, out, err = self._run([good, broken, "--quiet"])
        self.assertEqual(code, EXIT_FORMAT)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "good.epub")))
        self.assertIn("汇总", out)

    def test_output_directory(self):
        src = self._write("d.umd", build_umd())
        out_dir = os.path.join(self.tmp, "out")
        os.makedirs(out_dir)
        code, _, _ = self._run([src, "-o", out_dir, "--quiet"])
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(os.path.exists(os.path.join(out_dir, "d.epub")))

    def test_split_option(self):
        long_chapter = "\u2029".join("段落%03d" % index for index in range(400))
        src = self._write("long.umd", build_umd(chapters=(("第一章", long_chapter),)))
        code, _, _ = self._run([src, "--quiet", "--split-chars", "500"])
        self.assertEqual(code, EXIT_OK)
        with open(os.path.join(self.tmp, "long.epub"), "rb") as handle:
            archive = epub_archive(handle.read())
        self.assertGreater(len(chapter_files(archive)), 3)

    def test_module_and_launcher_run(self):
        candidates = [
            [sys.executable, os.path.join(PROJECT_ROOT, "umd2epub.py"), "--version"],
            [sys.executable, "-m", "umd2epub", "--version"],
        ]
        for argv in candidates:
            proc = subprocess.run(
                argv,
                cwd=PROJECT_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.assertEqual(proc.returncode, 0, argv)


INDEX_HTML = """<html><head><title>测试书 - 轻小说文库</title></head><body>
<table><tr><td colspan="4">第一卷</td></tr>
<tr><td><a href="1001.htm">序章</a></td><td><a href="1002.htm">第一章 开端</a></td></tr>
<tr><td><a href="1003.htm">插图</a></td></tr>
<tr><td colspan="4">第二卷</td></tr>
<tr><td><a href="2001.htm">第一章</a></td></tr>
<tr><td><a href="2002.htm">插图</a></td></tr>
</table></body></html>"""


class Wenku8ParsingTests(unittest.TestCase):
    def test_parse_index_finds_illustration_pages_with_volumes(self):
        pages = wenku8.parse_index(INDEX_HTML, "https://example.com/novel/1/1234/index.htm")
        self.assertEqual([page.volume for page in pages], [1, 2])
        self.assertEqual(
            [page.url for page in pages],
            ["https://example.com/novel/1/1234/1003.htm",
             "https://example.com/novel/1/1234/2002.htm"],
        )
        self.assertEqual(wenku8.parse_page_title(INDEX_HTML), "测试书")

    def test_chinese_numeral_and_volume_ordinal(self):
        self.assertEqual(wenku8.chinese_numeral("六"), 6)
        self.assertEqual(wenku8.chinese_numeral("十二"), 12)
        self.assertEqual(wenku8.chinese_numeral("13"), 13)
        self.assertEqual(wenku8.volume_ordinal("第三卷 插图"), 3)
        self.assertIsNone(wenku8.volume_ordinal("后记"))

    def test_parse_image_urls_dedupes_and_resolves(self):
        html = ('<img src="../pic/a.jpg">'
                '<img src="https://cdn.example.com/b.png">'
                '<img src="../pic/a.jpg">')
        urls = wenku8.parse_image_urls(html, "https://example.com/novel/1/1234/1003.htm")
        self.assertEqual(
            urls,
            ["https://example.com/novel/1/pic/a.jpg", "https://cdn.example.com/b.png"],
        )

    def test_sniff_ext(self):
        self.assertEqual(wenku8.sniff_ext(JPEG), "jpg")
        self.assertEqual(wenku8.sniff_ext(PNG), "png")
        self.assertIsNone(wenku8.sniff_ext(b"not an image"))

    def test_book_urls(self):
        self.assertEqual(wenku8.book_index_url(3943), "https://www.wenku8.net/novel/3/3943/index.htm")
        self.assertEqual(wenku8.book_page_url(3943), "https://www.wenku8.net/book/3943.htm")
        self.assertEqual(wenku8.cover_url(3943), "http://img.wenku8.com/image/3/3943/3943s.jpg")


class IllustrationAttachTests(unittest.TestCase):
    @staticmethod
    def _pages():
        return [
            wenku8.IllustrationPage(volume=1, title="插图", url="u1",
                                    images=[wenku8.FetchedIllustration("219903.jpg", "jpg", JPEG, "u1")]),
            wenku8.IllustrationPage(volume=2, title="插图", url="u2",
                                    images=[wenku8.FetchedIllustration("219904.jpg", "jpg", PNG, "u2")]),
        ]

    def test_attach_matches_by_volume(self):
        chapters = [Chapter("第一卷 序章", "正文"), Chapter("第二卷 插图", ""),
                    Chapter("第一卷 插图", "")]
        attached = wenku8.attach_illustrations(chapters, self._pages(), log=lambda message: None)
        self.assertEqual(attached, 2)
        self.assertEqual(chapters[1].images[0].name, "219904.jpg")
        self.assertEqual(chapters[2].images[0].name, "219903.jpg")

    def test_attach_falls_back_to_order(self):
        chapters = [Chapter("插图", ""), Chapter("插图二", "")]
        attached = wenku8.attach_illustrations(chapters, self._pages(), log=lambda message: None)
        self.assertEqual(attached, 2)
        self.assertEqual(chapters[0].images[0].name, "219903.jpg")
        self.assertEqual(chapters[1].images[0].name, "219904.jpg")

    def test_attach_errors_without_target_chapter(self):
        with self.assertRaises(wenku8.Wenku8Error):
            wenku8.attach_illustrations([Chapter("第一章", "正文")], self._pages(),
                                        log=lambda message: None)


class IllustrationEpubTests(unittest.TestCase):
    def test_illustration_chapter_becomes_image_pages(self):
        book = parse_umd(build_umd(chapters=(("第一卷 序章", "正文内容"), ("第一卷 插图", "\u2029"))))
        book.chapters[1].images = [Illustration("219903.jpg", "jpg", JPEG, "u")]
        archive = epub_archive(build_epub(book, EpubOptions(split_chars=0)))
        names = archive.namelist()

        self.assertIn("OEBPS/Images/219903.jpg", names)
        self.assertEqual(archive.read("OEBPS/Images/219903.jpg"), JPEG)
        self.assertNotIn("OEBPS/text/c0002_p01.xhtml", names)
        illustration = "OEBPS/text/c0002_i01.xhtml"
        self.assertIn(illustration, names)
        self.assertIn(b"Images/219903.jpg", archive.read(illustration))
        ET.fromstring(archive.read(illustration))

        opf = archive.read("OEBPS/content.opf").decode("utf-8")
        self.assertIn('media-type="image/jpeg"', opf)
        self.assertIn("text/c0002_i01.xhtml", archive.read("OEBPS/toc.ncx").decode("utf-8"))

        xhtml_files = [name for name in names if name.endswith(".xhtml")]
        spine = ET.fromstring(archive.read("OEBPS/content.opf")).findall("opf:spine/opf:itemref", OPF_NS)
        self.assertEqual(len(spine), len(xhtml_files))

    def test_illustrations_after_normal_chapter(self):
        book = parse_umd(build_umd(chapters=(("第一卷 序章", "正文内容"),)))
        book.chapters[0].images = [Illustration("a.jpg", "jpg", JPEG, "u")]
        archive = epub_archive(build_epub(book, EpubOptions(split_chars=0)))
        names = archive.namelist()
        self.assertIn("OEBPS/text/c0001_p01.xhtml", names)
        self.assertIn("OEBPS/text/c0001_i01.xhtml", names)
        spine = ET.fromstring(archive.read("OEBPS/content.opf")).findall("opf:spine/opf:itemref", OPF_NS)
        self.assertEqual(len(spine), 2)


class FetchHookTests(unittest.TestCase):
    def _source(self, tmp):
        src = os.path.join(tmp, "3943.umd")
        with open(src, "wb") as handle:
            handle.write(build_umd(chapters=(("第一卷 序章", "正文"), ("第一卷 插图", "\u2029"))))
        return src

    def test_convert_with_injected_fetcher_embeds_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._source(tmp)

            def fake_fetcher(book_id, **kwargs):
                self.assertEqual(book_id, 3943)
                return [wenku8.IllustrationPage(
                    volume=1, title="插图", url="u",
                    images=[wenku8.FetchedIllustration("219903.jpg", "jpg", JPEG, "u")],
                )]

            report = convert_file(
                src,
                fetch_illustrations=True,
                illustration_fetcher=fake_fetcher,
                illustration_cache=os.path.join(tmp, "cache"),
            )
            self.assertTrue(report.ok, report.error)
            self.assertEqual(report.illustrations, 1)
            self.assertEqual(report.meta.get("book_id"), "3943")
            with open(os.path.join(tmp, "3943.epub"), "rb") as handle:
                archive = epub_archive(handle.read())
            self.assertIn("OEBPS/Images/219903.jpg", archive.namelist())

    def test_fetch_failure_only_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._source(tmp)

            def failing_fetcher(book_id, **kwargs):
                raise wenku8.Wenku8Error("模拟网络失败")

            report = convert_file(src, fetch_illustrations=True, illustration_fetcher=failing_fetcher)
            self.assertTrue(report.ok, report.error)
            self.assertEqual(report.illustrations, 0)
            self.assertTrue(any("插图抓取失败" in item for item in report.warnings))
            self.assertTrue(os.path.exists(os.path.join(tmp, "3943.epub")))

    def test_book_id_can_be_given_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "我的小说.umd")
            with open(src, "wb") as handle:
                handle.write(build_umd(chapters=(("第一卷 插图", "插图占位"),)))

            def fake_fetcher(book_id, **kwargs):
                self.assertEqual(book_id, 777)
                return []

            report = convert_file(src, fetch_illustrations=True, book_id=777,
                                   illustration_fetcher=fake_fetcher)
            self.assertTrue(report.ok, report.error)
            self.assertEqual(report.meta.get("book_id"), "777")

    def test_fetch_cover_adds_site_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = self._source(tmp)

            def fake_cover(book_id, **kwargs):
                self.assertEqual(book_id, 3943)
                return wenku8.FetchedIllustration("3943s.jpg", "jpg", JPEG, "u")

            report = convert_file(src, fetch_cover=True, cover_fetcher=fake_cover)
            self.assertTrue(report.ok, report.error)
            self.assertTrue(report.fetched_cover)
            with open(os.path.join(tmp, "3943.epub"), "rb") as handle:
                archive = epub_archive(handle.read())
            self.assertIn("OEBPS/cover.jpg", archive.namelist())
            self.assertIn("OEBPS/text/cover.xhtml", archive.namelist())
            opf = archive.read("OEBPS/content.opf").decode("utf-8")
            self.assertIn('<meta name="cover" content="coverimg"/>', opf)

    def test_umd_cover_wins_over_site_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "3943.umd")
            with open(src, "wb") as handle:
                handle.write(build_umd(cover=JPEG, chapters=(("第一章", "正文"),)))

            called = []

            def fake_cover(book_id, **kwargs):
                called.append(book_id)
                return wenku8.FetchedIllustration("3943s.jpg", "jpg", PNG, "u")

            report = convert_file(src, fetch_cover=True, cover_fetcher=fake_cover)
            self.assertTrue(report.ok, report.error)
            self.assertFalse(report.fetched_cover)
            self.assertEqual(called, [])
            with open(os.path.join(tmp, "3943.epub"), "rb") as handle:
                archive = epub_archive(handle.read())
            self.assertEqual(archive.read("OEBPS/cover.jpg"), JPEG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
