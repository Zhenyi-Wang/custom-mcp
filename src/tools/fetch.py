"""web_fetch:抓取 URL → 正文提取 → Markdown。

参考 ihor-sokoliuk/mcp-searxng 的工程做法:
- SSRF 双层检查 + 手动重定向逐跳验证
- 流式读取字节上限(超限断流,不全量进内存)
- Content-Type 分流:HTML→trafilatura 正文提取(优于全 DOM 转 markdown),
  PDF→pypdf,JSON/文本→围栏输出,二进制→明确拒绝
- PDF 提取异常少时返回 warning,不静默截断

truncated 标志语义:可能来自三层截断中的任一层——2MiB 流式上限、
256KB 缓存条目上限、10000 字符返回截断。
"""

import asyncio
import codecs
import json
import logging
from io import BytesIO

import httpx
import trafilatura
from pypdf import PdfReader

from .cache import TTLCache
from .ssrf import SSRFError, assert_url_allowed, resolve_and_check

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_BYTES = 2 * 1024 * 1024  # 响应体上限 2MiB
MAX_REDIRECTS = 5
MAX_CONTENT_LENGTH = 10000
CACHE_MAX_CONTENT = 256 * 1024  # 缓存单条正文上限,防大文本撑爆内存

_cache: TTLCache = TTLCache(maxsize=256, ttl=24 * 3600)
logger = logging.getLogger(__name__)


def _content_type(headers: httpx.Headers) -> str:
    return headers.get("content-type", "").split(";")[0].strip().lower()


def _looks_like_pdf(prefix: bytes) -> bool:
    return prefix[:5] == b"%PDF-"


def _extract_pdf(data: bytes) -> tuple[str, int]:
    reader = PdfReader(BytesIO(data))
    texts = [(page.extract_text() or "") for page in reader.pages]
    text = "\n\n".join(t for t in texts if t.strip())
    return text, len(reader.pages)


def _extract_section(md: str, title: str) -> str | None:
    """按标题子串匹配提取小节,到下一个标题为止。"""
    lines = md.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") and title.lower() in line.lower():
            start = i
            break
    if start is None:
        return None
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line.lstrip().startswith("#"):
            break
        out.append(line)
    return "\n".join(out).strip() or None


async def _read_stream(resp: httpx.Response) -> tuple[bytes, bool]:
    """流式读取响应体,超 MAX_BYTES 断流。返回 (内容, 是否截断)。"""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > MAX_BYTES:
            chunks.append(chunk[: MAX_BYTES - (total - len(chunk))])
            truncated = True
            break
        chunks.append(chunk)
    return b"".join(chunks), truncated


async def _convert(content: bytes, ctype: str, encoding: str = "utf-8") -> dict:
    """Content-Type 分流转换。返回 {content, content_type, warning?} 或 {error}。"""
    if not content:
        return {"error": "响应体为空"}

    if ctype in ("text/html", "application/xhtml+xml") or (
        not ctype and content[:1024].lstrip().lower().startswith(b"<")
    ):
        # trafilatura 是 CPU 密集调用,放线程池避免阻塞事件循环
        extracted = await asyncio.to_thread(
            trafilatura.extract,
            content.decode(encoding, errors="replace"),  # 尊重响应头 charset
            output_format="markdown",  # 默认 txt 无标题标记,section 定位依赖它
            include_tables=True,
            include_images=False,
            include_links=True,
        )
        if not extracted:
            return {"error": "未能从该页面提取到正文(可能是 SPA/JS 渲染页面或非文章页)"}
        return {"content": extracted, "content_type": "html"}

    if ctype == "application/pdf" or _looks_like_pdf(content[:16]):
        try:
            text, pages = await asyncio.to_thread(_extract_pdf, content)
        except Exception as e:
            return {"error": f"PDF 解析失败: {e}"}
        if not text.strip():
            return {"error": "PDF 无文本层(可能为扫描件,不支持 OCR)"}
        result = {"content": text, "content_type": "pdf"}
        if pages > 1 and len(text) < 200:
            result["warning"] = (
                f"PDF 共 {pages} 页但仅提取到 {len(text)} 字符,内容可能不完整"
            )
        return result

    if ctype == "application/json" or ctype.endswith("+json"):
        try:
            # json.loads 接 bytes,自带 UTF-8/16/32 编码探测
            pretty = json.dumps(json.loads(content), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pretty = content.decode(encoding, errors="replace")
        return {"content": pretty, "content_type": "json"}

    if ctype.startswith("text/") or ctype in (
        "application/xml", "application/yaml", "application/toml",
    ):
        return {"content": content.decode(encoding, errors="replace"),
                "content_type": "text"}

    if b"\x00" in content[:1024]:
        return {"error": f"该地址是二进制文件({ctype or '未知类型'}),不做解析"}

    # 未知类型按文本尝试
    return {"content": content.decode(encoding, errors="replace"),
            "content_type": ctype or "unknown"}


async def _fetch_with_client(client: httpx.AsyncClient, url: str) -> dict:
    try:
        assert_url_allowed(url)
    except SSRFError as e:
        return {"url": url, "error": str(e)}

    current = url
    for _ in range(MAX_REDIRECTS + 1):  # +1:最多跟 MAX_REDIRECTS 跳
        try:
            host = assert_url_allowed(current)
        except SSRFError as e:
            return {"url": url, "error": f"重定向目标已拦截: {e}"}
        try:
            # getaddrinfo 是阻塞系统调用,放线程池避免 DNS 慢时挂住事件循环
            await asyncio.to_thread(resolve_and_check, host)
        except SSRFError as e:
            return {"url": url, "error": str(e)}

        try:
            # stream=True 惰性下载响应体;client.get() 会全量缓冲,失去大小限制意义
            req = client.build_request("GET", current, headers={"User-Agent": UA})
            resp = await client.send(req, stream=True)
        except httpx.TimeoutException:
            return {"url": url, "error": "请求超时,可稍后重试"}
        except httpx.HTTPError as e:
            return {"url": url, "error": f"请求失败: {type(e).__name__}: {e}"}

        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "")
            await resp.aclose()  # 重定向响应体不需要读
            if not loc:
                return {"url": url, "error": f"重定向缺少 Location 头(HTTP {resp.status_code})"}
            current = str(resp.url.join(loc))
            continue
        break

    if resp.status_code in (301, 302, 303, 307, 308):
        return {"url": url, "error": f"重定向超过 {MAX_REDIRECTS} 次,已放弃"}

    if resp.status_code >= 400:
        await resp.aclose()
        hint = {
            404: "页面不存在,链接可能已失效",
            403: "被目标站拒绝(可能有反爬),换 User-Agent 或稍后重试",
            429: "被限流,稍后重试",
        }.get(resp.status_code, "")
        msg = f"HTTP {resp.status_code}" + (f": {hint}" if hint else "")
        return {"url": str(resp.url), "error": msg}

    try:
        data, truncated = await _read_stream(resp)
    except httpx.HTTPError as e:  # 流式读取中途断连/超时,同样要可读
        return {"url": str(resp.url), "error": f"读取响应失败: {type(e).__name__}: {e}"}
    finally:
        await resp.aclose()  # 读毕(或出错)关闭流
    encoding = resp.charset_encoding or "utf-8"
    try:
        info = codecs.lookup(encoding)
    except (LookupError, ValueError):  # 畸形 charset 头
        info = None
    # base64/zlib 等 lookup 成功但非文本编码,decode 仍会抛 LookupError
    if info is None or not getattr(info, "_is_text_encoding", True):
        encoding = "utf-8"
    converted = await _convert(data, _content_type(resp.headers), encoding)
    result = {"url": str(resp.url), **converted}
    if truncated:
        result["truncated"] = True
    return result


async def web_fetch(url: str, section: str = "") -> dict:
    """Fetch a web page and extract its content as Markdown.

    Supports HTML pages, PDFs (text extraction), JSON and plain text.
    Binary files are rejected. Private/internal addresses are blocked.

    Args:
        url: The URL to fetch
        section: Optional heading text to extract only that section
    """
    try:
        assert_url_allowed(url)
    except SSRFError as e:
        return {"url": url, "error": str(e)}

    cached = _cache.get(url)
    if cached is not None:
        # 浅拷贝:content 为不可变 str,后续只替换顶层键,不 mutate 嵌套 list
        result = dict(cached)
        result["cached"] = True
    else:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False
        ) as client:
            try:
                result = await _fetch_with_client(client, url)
            except Exception as e:  # 兜底:任何未预期异常都以可读错误返回
                logger.exception("web_fetch 内部错误: %s", url)
                return {"url": url, "error": f"抓取内部错误: {type(e).__name__}: {e}"}
        if "error" in result:
            return result
        to_cache = dict(result)  # 缓存未切片的原始结果
        if len(to_cache.get("content", "")) > CACHE_MAX_CONTENT:
            to_cache["content"] = to_cache["content"][:CACHE_MAX_CONTENT]
            to_cache["truncated"] = True
        _cache.set(url, to_cache)

    # section 切片与截断只作用于本次返回的副本
    if section and "content" in result:
        sliced = _extract_section(result["content"], section)
        if sliced is None:
            result["warning"] = f"未找到标题含 {section!r} 的小节,返回全文"
        else:
            result["content"] = sliced

    if "content" in result:
        if len(result["content"]) > MAX_CONTENT_LENGTH:
            result["content"] = result["content"][:MAX_CONTENT_LENGTH]
            result["truncated"] = True
        result["length"] = len(result["content"])
    return result
