# -*- coding: utf-8 -*-
"""轻小说文库(wenku8) 插图抓取 —— 可选功能, 默认关闭。

为什么需要联网: 站点的官方 TXT / UMD / JAR 打包都不含图片, 正文里只有
"（插图NNN）"这样的文字占位符; 真正的插画只存在于站内"每卷末尾的插图章节页"
的 HTML 里。所以要把插图补进 EPUB, 只能按书号去抓那些页面。

只用标准库(urllib / gzip / http.cookiejar)。站点对缺少 Sec-Fetch-* 头
的请求返回 403, 并且会限速(429), 所以这里内置了串行限速与退避重试。
"""
from __future__ import annotations

import dataclasses
import gzip
import http.cookiejar
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Callable, Dict, List, Optional

ROOT = "https://www.wenku8.net"

_MAGIC_EXT = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF8", "gif"),
    (b"BM", "bmp"),
)

_CHAPTER_RE = re.compile(r'<a\s+href="(\d+)\.htm"[^>]*>(.*?)</a>', re.I | re.S)
_IMAGE_RE = re.compile(r'<img[^>]*\bsrc="([^"]+)"', re.I)
_VOLUME_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百零〇]+)\s*卷")
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
_ILLUSTRATION_WORDS = ("插图", "插圖")

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    # 注意: 实测这个站点只写 "gzip" 会返回 403, 必须写成浏览器那样; 也不能加 br,
    # 否则服务端会返回 brotli, 而标准库解不了。
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class Wenku8Error(Exception):
    """抓取插图过程中的错误(不影响本地转换, 调用方自行降级)。"""


@dataclasses.dataclass
class FetchedIllustration:
    name: str
    ext: str
    data: bytes
    source_url: str


@dataclasses.dataclass
class IllustrationPage:
    volume: Optional[int]
    title: str
    url: str
    images: List[FetchedIllustration] = dataclasses.field(default_factory=list)


def book_index_url(book_id: int) -> str:
    return "%s/novel/%d/%d/index.htm" % (ROOT, book_id // 1000, book_id)


def book_page_url(book_id: int) -> str:
    return "%s/book/%d.htm" % (ROOT, book_id)


def chinese_numeral(text: str) -> Optional[int]:
    """把 "六" / "十二" / "13" 这类卷号转成整数。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(text)


def volume_ordinal(title: str) -> Optional[int]:
    """从 "第三卷 插图" 这类标题里取出卷号。"""
    match = _VOLUME_RE.search(title or "")
    if not match:
        return None
    return chinese_numeral(match.group(1))


def strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", text or "")).strip()


def is_illustration_title(title: str) -> bool:
    return any(word in (title or "") for word in _ILLUSTRATION_WORDS)


def parse_index(html: str, base_url: str) -> List[IllustrationPage]:
    """按文档顺序取"插图"章节, 并用最近出现的卷标题给它标卷号。"""
    volume_marks = [
        (match.start(), chinese_numeral(match.group(1)))
        for match in _VOLUME_RE.finditer(html)
    ]
    pages: List[IllustrationPage] = []
    for match in _CHAPTER_RE.finditer(html):
        title = strip_tags(match.group(2))
        if not is_illustration_title(title):
            continue
        volume = None
        for position, ordinal in volume_marks:
            if position < match.start():
                volume = ordinal
            else:
                break
        pages.append(IllustrationPage(
            volume=volume,
            title=title,
            url=urllib.parse.urljoin(base_url, match.group(1) + ".htm"),
        ))
    return pages


def parse_image_urls(html: str, base_url: str) -> List[str]:
    """按文档顺序取章节页里的图片地址, 去重保序。"""
    urls: List[str] = []
    seen = set()
    for match in _IMAGE_RE.finditer(html):
        url = urllib.parse.urljoin(base_url, match.group(1).strip())
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def parse_page_title(html: str) -> str:
    match = _TITLE_RE.search(html)
    if not match:
        return ""
    return strip_tags(match.group(1)).split("-")[0].strip()


def sniff_ext(blob: bytes) -> Optional[str]:
    for magic, ext in _MAGIC_EXT:
        if blob.startswith(magic):
            return ext
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    return None


class Fetcher:
    """带限速、退避重试和 cookie 的抓取器。"""

    def __init__(
        self,
        *,
        delay: float = 2.0,
        timeout: float = 60.0,
        retries: int = 3,
        log: Callable[[str], None] = print,
    ) -> None:
        self.delay = max(0.0, float(delay))
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.log = log
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self._last_request = 0.0

    def _wait(self) -> None:
        if self.delay <= 0:
            return
        gap = self.delay - (time.monotonic() - self._last_request)
        if gap > 0:
            time.sleep(gap)

    def get(self, url: str, *, referer: Optional[str] = None) -> bytes:
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            self._wait()
            headers = dict(HEADERS)
            if referer:
                headers["Referer"] = referer
            request = urllib.request.Request(url, headers=headers)
            try:
                self._last_request = time.monotonic()
                response = self._opener.open(request, timeout=self.timeout)
                raw = response.read()
                encoding = (response.headers.get("Content-Encoding") or "").lower()
                if encoding == "gzip":
                    raw = gzip.decompress(raw)
                elif encoding == "deflate":
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                return raw
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (403, 429, 500, 502, 503, 504) and attempt < self.retries:
                    backoff = max(1.0, self.delay) * (2 ** attempt)
                    self.log("  站点返回 HTTP %d, %.0f 秒后重试(%d/%d)" % (
                        exc.code, backoff, attempt + 1, self.retries))
                    time.sleep(backoff)
                    continue
                raise Wenku8Error("请求失败 HTTP %d: %s" % (exc.code, url)) from exc
            except Exception as exc:  # 网络超时等
                last_error = exc
                if attempt < self.retries:
                    time.sleep(max(1.0, self.delay) * (attempt + 1))
                    continue
                raise Wenku8Error("请求失败: %s (%s)" % (exc, url)) from exc
        raise Wenku8Error("请求失败: %s (%s)" % (last_error, url))

    def get_text(self, url: str, *, referer: Optional[str] = None) -> str:
        return self.get(url, referer=referer).decode("gb18030", "replace")


def _cache_path(cache_dir: Optional[str], name: str) -> Optional[str]:
    if not cache_dir:
        return None
    return os.path.join(cache_dir, name)


def fetch_illustrations(
    book_id: int,
    *,
    delay: float = 2.0,
    timeout: float = 60.0,
    retries: int = 3,
    cache_dir: Optional[str] = None,
    log: Callable[[str], None] = print,
) -> List[IllustrationPage]:
    """抓取一本书所有"插图"章节页里的图片。

    返回按文档顺序(即分卷顺序)排列的 IllustrationPage 列表。
    """
    fetcher = Fetcher(delay=delay, timeout=timeout, retries=retries, log=log)
    index_url = book_index_url(book_id)
    log("读取目录页: %s" % index_url)
    index_html = fetcher.get_text(index_url, referer=book_page_url(book_id))
    pages = parse_index(index_html, index_url)
    if not pages:
        raise Wenku8Error("在目录页里没有找到“插图”章节")

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)

    for page in pages:
        label = "第%s卷" % page.volume if page.volume else page.title
        log("读取插图页: %s (%s)" % (page.url, label))
        page_html = fetcher.get_text(page.url, referer=index_url)
        for url in parse_image_urls(page_html, page.url):
            base_name = os.path.basename(urllib.parse.urlparse(url).path) or "image.jpg"
            stem, ext = os.path.splitext(base_name)
            ext = ext.lstrip(".").lower() or "jpg"
            cached = _cache_path(cache_dir, base_name)
            data: Optional[bytes] = None
            if cached and os.path.isfile(cached):
                with open(cached, "rb") as handle:
                    data = handle.read()
                log("  使用缓存: %s (%d 字节)" % (base_name, len(data)))
            if data is None:
                data = fetcher.get(url, referer=page.url)
                real_ext = sniff_ext(data)
                if real_ext and real_ext != ext:
                    ext = real_ext
                    base_name = stem + "." + ext
                    cached = _cache_path(cache_dir, base_name)
                if cached:
                    with open(cached, "wb") as handle:
                        handle.write(data)
                log("  下载: %s (%d 字节)" % (base_name, len(data)))
            else:
                real_ext = sniff_ext(data)
                if real_ext:
                    ext = real_ext
                    stem = os.path.splitext(base_name)[0]
                    base_name = stem + "." + ext
            page.images.append(FetchedIllustration(
                name=base_name, ext=ext, data=data, source_url=url,
            ))
    return pages


def attach_illustrations(
    chapters,
    pages: List[IllustrationPage],
    *,
    log: Callable[[str], None] = print,
) -> int:
    """把抓到的插图按卷挂到 UMD 的"第N卷 插图"章节上。

    优先按卷号匹配, 卷号对不上时退回按顺序匹配。返回挂上的图片总数。
    """
    targets = [(index, chapter) for index, chapter in enumerate(chapters)
               if is_illustration_title(chapter.title)]
    if not targets:
        raise Wenku8Error("这本 UMD 里没有“插图”章节, 无法放置插图(可先看 --dry-run 的章节列表)")

    ordered = sorted(pages, key=lambda page: (page.volume is None, page.volume or 0))
    used = [False] * len(ordered)
    attached = 0
    for _, chapter in targets:
        ordinal = volume_ordinal(chapter.title)
        pick = None
        if ordinal is not None:
            for slot, page in enumerate(ordered):
                if not used[slot] and page.volume == ordinal:
                    pick = slot
                    break
        if pick is None:
            for slot in range(len(ordered)):
                if not used[slot]:
                    pick = slot
                    break
        if pick is None:
            log("  没有更多插图页可供 “%s” 使用" % chapter.title)
            continue
        used[pick] = True
        page = ordered[pick]
        chapter.images = list(page.images)
        attached += len(page.images)
        log("  %s <- 抓到的 %d 张插图" % (chapter.title, len(page.images)))
    return attached
