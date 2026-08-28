import httpx
import pytest
from fpdf import FPDF

from src.tools import fetch as fetch_mod


HTML = """<html><head><title>T</title></head><body>
<nav>menu junk</nav>
<article>
<h1>Real Content</h1>
<p>Hello world body text. This paragraph carries enough words for the main
extractor to recognise the document as an article with meaningful prose
content that deserves full extraction.</p>
<h2>Why X?</h2>
<p>alpha content with some more words here so the section is treated as
real article prose rather than a fragment that gets dropped.</p>
<h2>Next</h2>
<p>beta content with additional words to make this section substantial
enough for the extraction heuristics as well.</p>
</article>
<footer>footer junk</footer></body></html>"""
# 注意:trafilatura 2.0.0 对过短的迷你文档会退回降级提取(nav 不剔除、markdown
# 无标题标记),夹具必须保持文章型正文量,上述断言才成立。


def make_pdf(text: str) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text=text)
    return bytes(pdf.output())


def transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)


@pytest.fixture(autouse=True)
def clean_cache():
    fetch_mod._cache = fetch_mod.TTLCache(ttl=60)
    yield


@pytest.fixture
def no_resolver(monkeypatch):
    """默认不真做 DNS,返回公网地址;SSRF 用例单独覆盖。"""
    monkeypatch.setattr(
        fetch_mod, "resolve_and_check",
        lambda host, resolver=None: ["93.184.216.34"],
    )


async def test_html_extracted_with_trafilatura(no_resolver):
    async with transport(
        lambda req: httpx.Response(200, text=HTML,
                                   headers={"content-type": "text/html"})
    ) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/a")
    assert "Hello world body text" in result["content"]
    assert "menu junk" not in result["content"]
    assert result["content_type"] == "html"


async def test_pdf_extracted(no_resolver):
    pdf_bytes = make_pdf("Prayer calendar September content")
    async with transport(
        lambda req: httpx.Response(200, content=pdf_bytes,
                                   headers={"content-type": "application/pdf"})
    ) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/x.pdf")
    assert "Prayer calendar" in result["content"]
    assert result["content_type"] == "pdf"


async def test_pdf_with_tiny_text_gets_warning(no_resolver):
    """多页但文本极少 → warning,不得静默截断(反面教训)。"""
    pdf = FPDF()
    pdf.add_page(); pdf.set_font("Helvetica", size=12); pdf.cell(text="hi")
    pdf.add_page()
    async with transport(
        lambda req: httpx.Response(200, content=bytes(pdf.output()),
                                   headers={"content-type": "application/pdf"})
    ) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/y.pdf")
    assert "warning" in result


async def test_binary_rejected(no_resolver):
    async with transport(
        lambda req: httpx.Response(200, content=b"\x00\x01\x02binary",
                                   headers={"content-type": "application/zip"})
    ) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/f.zip")
    assert "error" in result
    assert "二进制" in result["error"]


async def test_redirect_to_private_ip_blocked(monkeypatch):
    def strict_resolver(host, resolver=None):
        from src.tools.ssrf import SSRFError
        if host == "169.254.169.254":
            raise SSRFError(f"{host} 解析到私有/保留地址 {host},已拦截")
        return ["93.184.216.34"]
    monkeypatch.setattr(fetch_mod, "resolve_and_check", strict_resolver)

    def handler(req):
        if req.url.host == "evil.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/meta"})
        return httpx.Response(200, text=HTML)

    async with transport(handler) as client:
        result = await fetch_mod._fetch_with_client(client, "https://evil.example/start")
    assert "error" in result
    assert "169.254" in result["error"] or "私有" in result["error"]


async def test_oversized_response_truncated(no_resolver, monkeypatch):
    monkeypatch.setattr(fetch_mod, "MAX_BYTES", 100)
    async with transport(
        lambda req: httpx.Response(200, text=HTML * 50,
                                   headers={"content-type": "text/plain"})
    ) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/big")
    assert result.get("truncated") is True


async def test_timeout_readable_error(no_resolver):
    def slow_handler(req):
        raise httpx.ConnectTimeout("timed out")

    async with transport(slow_handler) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/slow")
    assert "error" in result
    assert "超时" in result["error"]


async def test_404_readable_error(no_resolver):
    async with transport(lambda req: httpx.Response(404)) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/none")
    assert "error" in result
    assert "404" in result["error"]


async def test_private_literal_url_rejected():
    result = await fetch_mod.web_fetch("http://127.0.0.1:8000/health")
    assert "error" in result
    assert "私有" in result["error"]


async def test_section_extracts_subtree(no_resolver):
    async with transport(
        lambda req: httpx.Response(200, text=HTML,
                                   headers={"content-type": "text/html"})
    ) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/sec")
    sliced = fetch_mod._extract_section(result["content"], "why x")
    assert sliced is not None
    assert "alpha content" in sliced
    assert "beta" not in sliced


async def test_section_missing_returns_none(no_resolver):
    async with transport(
        lambda req: httpx.Response(200, text=HTML,
                                   headers={"content-type": "text/html"})
    ) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/a")
    assert fetch_mod._extract_section(result["content"], "no-such-title") is None


def test_extract_section_cjk_title():
    md = "## 禱告日程\n\n九月內容在這裡\n\n## Next\n\nbeta"
    sliced = fetch_mod._extract_section(md, "禱告")
    assert sliced is not None
    assert "九月內容" in sliced
    assert "beta" not in sliced


async def test_cache_hit_skips_http_and_resists_section_pollution(
    no_resolver, monkeypatch
):
    """缓存命中标记 cached,且 section 切片不污染缓存(第二次返回全文)。"""
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, text=HTML,
                              headers={"content-type": "text/html"})

    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", factory)

    first = await fetch_mod.web_fetch("https://example.com/cache", section="why x")
    assert first.get("cached") is not True
    assert "alpha content" in first["content"]
    assert "beta" not in first["content"]  # 首次按 section 切片返回

    second = await fetch_mod.web_fetch("https://example.com/cache")
    assert second["cached"] is True
    assert "alpha content" in second["content"]
    assert "beta" in second["content"]  # 缓存未被切片污染,返回全文
    assert len(calls) == 1  # 第二次未发起 HTTP


async def test_redirect_chain_exhausted(no_resolver):
    def handler(req):
        return httpx.Response(302, headers={"location": "https://example.com/next"})

    async with transport(handler) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/loop")
    assert "重定向超过" in result["error"]


async def test_redirect_missing_location(no_resolver):
    async with transport(lambda req: httpx.Response(302)) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/noloc")
    assert "Location" in result["error"]


async def test_empty_body_rejected(no_resolver):
    async with transport(
        lambda req: httpx.Response(200, content=b"",
                                   headers={"content-type": "text/plain"})
    ) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/empty")
    assert "error" in result


async def test_gbk_page_decoded(no_resolver):
    """声明 GBK 的页面按 charset 解码,不得按 UTF-8 硬解出乱码。"""
    gbk_html = (
        '<html><head><meta charset="gbk"></head><body><article>'
        "<h1>中文标题</h1>"
        "<p>这是一段足够长的中文正文内容,用来验证字符集处理是否正确,"
        "确保 GBK 编码的页面能够被正确解码而不出现乱码,"
        "同时保持足够的正文长度让提取器正常工作。</p>"
        "</article></body></html>"
    )
    async with transport(
        lambda req: httpx.Response(200, content=gbk_html.encode("gbk"),
                                   headers={"content-type": "text/html; charset=gbk"})
    ) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/gbk")
    assert "�" not in result["content"]
    assert "GBK" in result["content"]


async def test_malformed_charset_falls_back(no_resolver):
    """畸形 charset 头回退 utf-8,不得以 LookupError 崩掉调用。"""
    async with transport(
        lambda req: httpx.Response(200, text=HTML,
                                   headers={"content-type": "text/html; charset=nonexistent-codec"})
    ) as client:
        result = await fetch_mod._fetch_with_client(client, "https://example.com/badcs")
    assert "content" in result
    assert "Hello world body text" in result["content"]


def web_client_factory(handler, monkeypatch):
    """让 web_fetch 内部 client 走 MockTransport(web_fetch 不接受注入)。"""
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(fetch_mod.httpx, "AsyncClient", factory)


async def test_max_content_length_truncates(no_resolver, monkeypatch):
    monkeypatch.setattr(fetch_mod, "MAX_CONTENT_LENGTH", 50)
    web_client_factory(
        lambda req: httpx.Response(200, text=HTML,
                                   headers={"content-type": "text/html"}),
        monkeypatch,
    )
    result = await fetch_mod.web_fetch("https://example.com/long")
    assert result.get("truncated") is True
    assert result["length"] == 50


async def test_error_result_not_cached(no_resolver, monkeypatch):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(404)

    web_client_factory(handler, monkeypatch)
    await fetch_mod.web_fetch("https://example.com/missing")
    await fetch_mod.web_fetch("https://example.com/missing")
    assert len(calls) == 2  # 错误不入缓存,第二次仍发起请求
