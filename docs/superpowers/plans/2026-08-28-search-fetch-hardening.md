# Search/Fetch 强化改进 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 借鉴 mcp-searxng(ihor-sokoliuk)的工程实践,为 searxng-mcp 的 search/fetch 补齐答案直通、相关性过滤、缓存、SSRF 防护、大小限制、PDF 支持、错误可读性,同时保留 trafilatura 正文提取优势。

**Architecture:** 新增两个内聚基础组件(`cache.py` TTL 缓存、`ssrf.py` SSRF 防护),fetch/search 改造为消费方。fetch 重写为:SSRF 检查 → 手动重定向逐跳验证 → 流式读取字节上限 → Content-Type 分流(HTML→trafilatura / PDF→pypdf / JSON·文本→围栏 / 二进制→拒绝)→ section 过滤 → 缓存。search 增加 SearXNG 原生 answers/infoboxes/suggestions/corrections 直通、score 透传与 min_score 过滤、TTL 缓存。工具签名只增不改,server.py 无需变动(FastMCP 从函数签名生成 schema)。

**Tech Stack:** Python 3.11+ / uv / httpx(MockTransport 测试)/ trafilatura / pypdf / pytest + pytest-asyncio

**Spec:** 讨论记录(2026-08-28 会话):search 3 项(answers 直通、score/min_score、TTL 缓存)+ fetch 7 项(流式大小限制、Content-Type 分流+PDF、轻量 SSRF、TTL 缓存、错误可读性、section 参数、UA 修正)+ CLAUDE.md 参考项目记录。反面教训:PDF 提取异常少时必须带 warning,不得静默截断。

## Global Constraints

- 依赖管理一律用 `uv add` / `uv add --dev`,不手改 pyproject.toml
- Python >= 3.11,类型注解用内置泛型(`dict | None`),不用 typing.Optional
- 工具函数签名只增参数且全部带默认值,返回 dict 结构向后兼容(旧字段名不变)
- 私网段列表:IPv4 覆盖 0.0.0.0/8, 10/8, 100.64/10, 127/8, 169.254/16, 172.16/12, 192.168/16, 224/4, 240/4;IPv6 覆盖 ::1, fc00::/7, fe80::/10, ff00::/8,以及 IPv4-mapped 形式归一化
- 提交约束(going-out 模式):**全程禁止 git commit**,完成后等用户审核与提交指令
- 实现代码保持现有注释密度(少量关键中文注释)
- DNS rebinding 的 resolve-then-connect TOCTOU 残留风险在个人场景接受,写入 CLAUDE.md 已知短板

---

### Task 1: TTLCache 组件 + 测试基建

**Files:**
- Create: `src/tools/cache.py`
- Create: `tests/__init__.py`(空)、`tests/test_cache.py`
- Modify: `pyproject.toml`(uv add 自动改)

**Interfaces:**
- Produces: `TTLCache(maxsize: int = 256, ttl: float = 3600.0, clock: Callable[[], float] = time.monotonic)`,方法 `get(key: str) -> Any | None`(过期或未命中返回 None)、`set(key: str, value: Any) -> None`。Task 3/4/5 消费。

- [ ] **Step 1: 安装依赖**

```bash
uv add pypdf
uv add --dev pytest pytest-asyncio fpdf2
```

- [ ] **Step 2: 在 pyproject.toml 追加 pytest 配置(uv add 之后用 Edit 加)**

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

- [ ] **Step 3: 写失败测试 `tests/test_cache.py`**

```python
from src.tools.cache import TTLCache


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_get_miss_returns_none():
    cache = TTLCache()
    assert cache.get("k") is None


def test_set_then_get():
    cache = TTLCache()
    cache.set("k", {"v": 1})
    assert cache.get("k") == {"v": 1}


def test_expired_entry_returns_none():
    clock = FakeClock()
    cache = TTLCache(ttl=10.0, clock=clock)
    cache.set("k", "v")
    clock.now += 11.0
    assert cache.get("k") is None


def test_unexpired_entry_survives():
    clock = FakeClock()
    cache = TTLCache(ttl=10.0, clock=clock)
    cache.set("k", "v")
    clock.now += 9.0
    assert cache.get("k") == "v"


def test_maxsize_evicts_oldest():
    clock = FakeClock()
    cache = TTLCache(maxsize=2, ttl=100.0, clock=clock)
    cache.set("a", 1)
    clock.now += 1
    cache.set("b", 2)
    clock.now += 1
    cache.set("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
```

- [ ] **Step 4: 运行确认失败**

Run: `uv run pytest tests/test_cache.py -v`
Expected: FAIL(`ModuleNotFoundError: src.tools.cache`)

- [ ] **Step 5: 实现 `src/tools/cache.py`**

```python
"""进程内 TTL 缓存。

FastMCP 在单事件循环中运行,无并发写竞争,不加锁。
个人场景用 dict + TTL 即可,无需 LFU 等复杂淘汰策略。
"""

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any


class TTLCache:
    """带 TTL 与容量上限的缓存;超容量时先清过期项,再淘汰最旧写入。"""

    def __init__(
        self,
        maxsize: int = 256,
        ttl: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        ts, value = item
        if self._clock() - ts > self._ttl:
            del self._data[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        now = self._clock()
        self._data[key] = (now, value)
        self._data.move_to_end(key)
        expired = [k for k, (ts, _) in self._data.items() if now - ts > self._ttl]
        for k in expired:
            del self._data[k]
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/test_cache.py -v`
Expected: 5 passed

---

### Task 2: SSRF 防护组件

**Files:**
- Create: `src/tools/ssrf.py`
- Create: `tests/test_ssrf.py`

**Interfaces:**
- Produces:
  - `class SSRFError(ValueError)`
  - `is_private_ip(ip: str) -> bool`(非法字符串视为私有,拒绝)
  - `assert_url_allowed(url: str) -> str`(scheme/字面 IP 检查,返回 hostname;不通过抛 SSRFError)
  - `resolve_and_check(hostname: str, resolver: Callable = socket.getaddrinfo) -> list[str]`(解析全部地址,任一私网抛 SSRFError;resolver 可注入供测试)
- Consumes: 无。Task 3 消费。

- [ ] **Step 1: 写失败测试 `tests/test_ssrf.py`**

```python
import pytest

from src.tools.ssrf import SSRFError, assert_url_allowed, is_private_ip, resolve_and_check


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.255",
        "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
        "224.0.0.1", "240.0.0.1", "::1", "fe80::1", "fc00::1",
        "::ffff:10.0.0.1", "::ffff:169.254.169.254", "not-an-ip",
    ],
)
def test_private_ips_blocked(ip):
    assert is_private_ip(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_ips_allowed(ip):
    assert is_private_ip(ip) is False


def test_non_http_scheme_rejected():
    with pytest.raises(SSRFError, match="协议"):
        assert_url_allowed("file:///etc/passwd")


def test_literal_private_ip_url_rejected():
    with pytest.raises(SSRFError, match="私有"):
        assert_url_allowed("http://169.254.169.254/latest/meta-data/")


def test_public_url_returns_hostname():
    assert assert_url_allowed("https://example.com/path") == "example.com"


def test_resolve_all_private_blocked():
    def fake_resolver(host, port):
        return [(2, 1, 6, "", ("10.0.0.5", 0)), (2, 1, 6, "", ("192.168.0.9", 0))]

    with pytest.raises(SSRFError, match="私有"):
        resolve_and_check("evil.com", resolver=fake_resolver)


def test_resolve_mixed_addresses_blocked():
    """部分地址私网也拒绝(防 DNS rebinding 挑公网地址绕过)。"""

    def fake_resolver(host, port):
        return [(2, 1, 6, "", ("8.8.8.8", 0)), (2, 1, 6, "", ("10.0.0.5", 0))]

    with pytest.raises(SSRFError):
        resolve_and_check("evil.com", resolver=fake_resolver)


def test_resolve_public_passes():
    def fake_resolver(host, port):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    assert resolve_and_check("example.com", resolver=fake_resolver) == ["93.184.216.34"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_ssrf.py -v`
Expected: FAIL(`ModuleNotFoundError: src.tools.ssrf`)

- [ ] **Step 3: 实现 `src/tools/ssrf.py`**

```python
"""轻量 SSRF 防护(参考 ihor-sokoliuk/mcp-searxng 的双层思路)。

第一层:URL 字面检查(scheme + literal IP)。
第二层:DNS 解析检查,全部解析结果任一命中私网即拒绝(防 rebinding)。
已知残留风险:resolve-then-connect 的 TOCTOU 窗口,个人场景接受。
"""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse

_BLOCKED_V4 = [
    ipaddress.ip_network(c)
    for c in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
        "224.0.0.0/4", "240.0.0.0/4",
    )
]
_BLOCKED_V6 = [
    ipaddress.ip_network(c)
    for c in ("::1/128", "fc00::/7", "fe80::/10", "ff00::/8")
]


class SSRFError(ValueError):
    """URL 指向私有/保留地址或无法安全验证。"""


def is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # 非法地址按拒绝处理
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped  # ::ffff:10.0.0.1 归一化为 IPv4 判断
    nets = _BLOCKED_V4 if isinstance(addr, ipaddress.IPv4Address) else _BLOCKED_V6
    return any(addr in n for n in nets)


def assert_url_allowed(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFError(f"不支持的协议 {parsed.scheme!r},仅允许 http/https")
    host = parsed.hostname
    if not host:
        raise SSRFError("URL 缺少主机名")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host  # 域名,交给 resolve_and_check
    if is_private_ip(host):
        raise SSRFError(f"拒绝访问私有/保留地址: {host}")
    return host


def resolve_and_check(
    hostname: str,
    resolver: Callable = socket.getaddrinfo,
) -> list[str]:
    try:
        infos = resolver(hostname, None)
    except socket.gaierror as e:
        raise SSRFError(f"域名解析失败: {hostname}") from e
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise SSRFError(f"域名无解析结果: {hostname}")
    for addr in addresses:
        if is_private_ip(addr):
            raise SSRFError(f"{hostname} 解析到私有/保留地址 {addr},已拦截")
    return addresses
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_ssrf.py -v`
Expected: 全部 passed

---

### Task 3: fetch 核心重写(UA/SSRF/流式上限/类型分流+PDF/错误可读)

**Files:**
- Modify: `src/tools/fetch.py`(整体重写)
- Create: `tests/test_fetch.py`

**Interfaces:**
- Consumes: `TTLCache`(Task 1)、`SSRFError`/`assert_url_allowed`/`resolve_and_check`(Task 2)
- Produces: `async def web_fetch(url: str, section: str = "") -> dict`。返回 dict 字段:`url, content, length`(成功);`url, content_type, truncated, warning`(视情况出现);`error`(失败,人类可读)。`_extract_section(md, title) -> str | None` 供 section 过滤。

- [ ] **Step 1: 写失败测试 `tests/test_fetch.py`**

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: FAIL(`_fetch_with_client` 不存在)

- [ ] **Step 3: 重写 `src/tools/fetch.py`**

```python
"""web_fetch:抓取 URL → 正文提取 → Markdown。

参考 ihor-sokoliuk/mcp-searxng 的工程做法:
- SSRF 双层检查 + 手动重定向逐跳验证
- 流式读取字节上限(超限断流,不全量进内存)
- Content-Type 分流:HTML→trafilatura 正文提取(优于全 DOM 转 markdown),
  PDF→pypdf,JSON/文本→围栏输出,二进制→明确拒绝
- PDF 提取异常少时返回 warning,不静默截断
"""

import asyncio
import json
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

_cache: TTLCache = TTLCache(maxsize=256, ttl=24 * 3600)


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


async def _convert(content: bytes, ctype: str) -> dict:
    """Content-Type 分流转换。返回 {content, content_type, warning?} 或 {error}。"""
    if ctype in ("text/html", "application/xhtml+xml") or (
        not ctype and content[:1024].lstrip().lower().startswith(b"<")
    ):
        extracted = trafilatura.extract(
            content.decode("utf-8", errors="replace"),
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
            pretty = json.dumps(json.loads(content), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pretty = content.decode("utf-8", errors="replace")
        return {"content": pretty, "content_type": "json"}

    if ctype.startswith("text/") or ctype in (
        "application/xml", "application/yaml", "application/toml",
    ):
        return {"content": content.decode("utf-8", errors="replace"),
                "content_type": "text"}

    if b"\x00" in content[:1024]:
        return {"error": f"该地址是二进制文件({ctype or '未知类型'}),不做解析"}

    # 未知类型按文本尝试
    return {"content": content.decode("utf-8", errors="replace"),
            "content_type": ctype or "unknown"}


async def _fetch_with_client(client: httpx.AsyncClient, url: str) -> dict:
    try:
        assert_url_allowed(url)
    except SSRFError as e:
        return {"url": url, "error": str(e)}

    current = url
    for _ in range(MAX_REDIRECTS):
        try:
            host = assert_url_allowed(current)
        except SSRFError as e:
            return {"url": url, "error": f"重定向目标已拦截: {e}"}
        try:
            resolve_and_check(host)
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

    data, truncated = await _read_stream(resp)
    await resp.aclose()  # 读毕关闭流
    converted = await _convert(data, _content_type(resp.headers))
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
        result = dict(cached)  # 副本上做切片,避免污染缓存
        result["cached"] = True
    else:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False
        ) as client:
            result = await _fetch_with_client(client, url)
        if "error" in result:
            return result
        _cache.set(url, dict(result))  # 缓存未切片的原始结果

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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: 全部 passed

- [ ] **Step 5: 全量回归**

Run: `uv run pytest -v`
Expected: cache + ssrf + fetch 全部 passed

---

### Task 4: search 改造(answers 直通/score/min_score/缓存)

**Files:**
- Modify: `src/tools/search.py`
- Create: `tests/test_search.py`

**Interfaces:**
- Consumes: `TTLCache`(Task 1)
- Produces: `async def web_search(query: str, category: str = "general", language: str = "en", pageno: int = 1, time_range: str = "", min_score: float = 0.0) -> dict`。返回 dict 新增:`answers, suggestions, corrections, infoboxes`(SearXNG 原生直通);`results[]` 每条新增 `score`。

- [ ] **Step 1: 写失败测试 `tests/test_search.py`**

```python
import json

import httpx
import pytest

from src.tools import search as search_mod

SEARXNG_RESPONSE = {
    "query": "test",
    "answers": ["42"],
    "suggestions": ["testing", "pytest"],
    "corrections": [],
    "infoboxes": [{"infobox": "Test", "content": "A test"}],
    "results": [
        {"title": "A", "url": "https://a.com", "content": "a", "engine": "google", "score": 10.0},
        {"title": "B", "url": "https://b.com", "content": "b", "engine": "bing", "score": 0.5},
    ],
}


def transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)


@pytest.fixture(autouse=True)
def clean_cache():
    search_mod._cache = search_mod.TTLCache(ttl=60)
    yield


async def test_direct_answers_passthrough():
    async with transport(
        lambda req: httpx.Response(200, json=SEARXNG_RESPONSE)
    ) as client:
        result = await search_mod._search_with_client(client, "test")
    assert result["answers"] == ["42"]
    assert result["suggestions"] == ["testing", "pytest"]
    assert result["infoboxes"] == [{"infobox": "Test", "content": "A test"}]


async def test_score_passthrough():
    async with transport(
        lambda req: httpx.Response(200, json=SEARXNG_RESPONSE)
    ) as client:
        result = await search_mod._search_with_client(client, "test")
    assert result["results"][0]["score"] == 10.0


async def test_min_score_filters():
    async with transport(
        lambda req: httpx.Response(200, json=SEARXNG_RESPONSE)
    ) as client:
        result = await search_mod._search_with_client(client, "test", min_score=1.0)
    assert [r["title"] for r in result["results"]] == ["A"]


async def test_cache_hit_skips_http():
    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(200, json=SEARXNG_RESPONSE)

    async with transport(handler) as client:
        await search_mod._search_with_client(client, "test", category="general")
        await search_mod._search_with_client(client, "test", category="general")
    assert len(calls) == 1


async def test_timeout_readable_error():
    def slow(req):
        raise httpx.ReadTimeout("timed out")

    async with transport(slow) as client:
        result = await search_mod._search_with_client(client, "test")
    assert "error" in result
    assert "超时" in result["error"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_search.py -v`
Expected: FAIL(`_search_with_client` 不存在)

- [ ] **Step 3: 重写 `src/tools/search.py`**

```python
"""web_search:SearXNG 元搜索透传,附答案框直通与相关性过滤。

answers/infoboxes/suggestions/corrections 是 SearXNG JSON API 原生返回,
直接透传给 LLM 可省去后续 fetch 轮次。
"""

import json

import httpx

from .cache import TTLCache

SEARCH_TTL = 3600  # 1h;search 结果时效性比 fetch 敏感
_cache: TTLCache = TTLCache(maxsize=256, ttl=SEARCH_TTL)


def _searxng_url() -> str:
    import os
    return os.environ.get("SEARXNG_URL", "http://searxng:8080")


async def _search_with_client(
    client: httpx.AsyncClient,
    query: str,
    category: str = "general",
    language: str = "en",
    pageno: int = 1,
    time_range: str = "",
    min_score: float = 0.0,
) -> dict:
    cache_key = repr((query, category, language, pageno, time_range, min_score))
    cached = _cache.get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    params = {
        "q": query,
        "format": "json",
        "categories": category,
        "language": language,
        "pageno": str(pageno),
    }
    if time_range:
        params["time_range"] = time_range

    try:
        resp = await client.get(f"{_searxng_url()}/search", params=params)
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        return {"error": f"SearXNG 请求超时({_searxng_url()}),可稍后重试"}
    except httpx.HTTPStatusError as e:
        return {"error": f"SearXNG 返回 HTTP {e.response.status_code},检查实例状态"}
    except json.JSONDecodeError:
        return {"error": "SearXNG 返回非 JSON 响应,检查实例是否启用 json format"}

    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "engine": r.get("engine", ""),
            "score": r.get("score", 0.0),
        }
        for r in data.get("results", [])
        if r.get("score", 0.0) >= min_score
    ]

    result = {
        "query": data.get("query", query),
        "count": len(results),
        "results": results,
        "answers": data.get("answers", []),
        "suggestions": data.get("suggestions", []),
        "corrections": data.get("corrections", []),
        "infoboxes": data.get("infoboxes", []),
    }
    _cache.set(cache_key, result)
    return result


async def web_search(
    query: str,
    category: str = "general",
    language: str = "en",
    pageno: int = 1,
    time_range: str = "",
    min_score: float = 0.0,
) -> dict:
    """Search the web using SearXNG metasearch engine.

    Returns results plus SearXNG native answers, infoboxes, suggestions
    and spelling corrections when available — often enough without fetching.

    Args:
        query: Search query string
        category: general, images, news, science, it, videos, map, music, files, social_media
        language: Language code like en, zh-CN, ja; empty for all languages
        pageno: Page number starting from 1
        time_range: day, week, month, year, or empty for no filter
        min_score: Drop results with relevance score below this (0 = keep all)
    """
    async with httpx.AsyncClient(timeout=30) as client:
        return await _search_with_client(
            client, query, category, language, pageno, time_range, min_score
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_search.py -v`
Expected: 全部 passed

- [ ] **Step 5: 全量回归**

Run: `uv run pytest -v`
Expected: 全部 passed

---

### Task 5: CLAUDE.md 参考记录 + README 更新

**Files:**
- Create: `CLAUDE.md`(项目级)
- Modify: `README.md`(功能描述与本地测试说明)

**Interfaces:** 无代码接口。文档供后续会话参考。

- [ ] **Step 1: 写项目级 `CLAUDE.md`**

内容要点(实际文件用完整段落写):

```markdown
# searxng-mcp

自托管 web_search + web_fetch MCP server(FastMCP + SearXNG + trafilatura)。
常用命令:`uv run pytest`、`docker compose -f docker-compose.local.yml up -d`(本地试跑环境)、
`scripts/mcp-call.sh <tool> '<json>'`(调用本地 MCP 工具验证)。

## 参考项目与取舍(2026-08-28 调研)

调研背景:对比开源同类项目决定自研改进方向。结论:正文提取与 PDF 完整性上
自研方案更优,工程防护层借鉴 mcp-searxng。

### ihor-sokoliuk/mcp-searxng(主要参考)
1169⭐ / MIT / Node.js,npm 月下载 9.9 万、Docker 拉取 42 万,唯一经大众验证的
同类项目。HTTP + hardened 鉴权部署方式与本项目一致。

已借鉴:
- search:SearXNG 原生 answers/infoboxes/suggestions/corrections 直通(数据
  已在 JSON 响应里,省一轮 fetch)、score 透传 + min_score 过滤、搜索缓存
- fetch:SSRF 双层防护(字面 IP + DNS 全解析检查)、手动重定向逐跳验证、
  流式字节上限、Content-Type 分流(二进制明确拒绝)、section 标题定位、
  可读错误信息
- 部署经验:hardened 模式 Host 白名单按容器内端口生成,端口映射后必须显式
  MCP_HTTP_ALLOWED_HOSTS(踩坑记录,见 docker-compose.local.yml)

明确不抄(及原因):
- 全 DOM 转 markdown(node-html-markdown):本项目 trafilatura 正文提取
  token 效率优近一半(GitHub 页实测 24K vs 46K 字符)
- 多实例 failover/fan-out:单实例部署无此需求
- LFU 缓存:个人场景 dict + TTL 足够
- Lite tools mode / instance_info 工具:面向小模型与多实例调试,不适用
- FlareSolverr/Byparr:只取 cookie 不渲染 JS,解决不了 SPA,暂不引入

### TadMSTR/searxng-mcp(思路可回看)
18⭐,单人项目零社区验证,依赖栈重(Valkey/Ollama/Firecrawl/NATS),不直接用。
可回看的思路:search_and_fetch 一体化工具(搜索→rerank→抓正文一次调用,
省 agent 轮次)、域名能力学习库(按域 30 天成功率自动跳级)、GitHub/YouTube/
Reddit 站点快速通道(走原生 API 而非抓 HTML)。

### one-search-mcp(负面教材)
仅 139⭐ 且质量堪忧:SearXNG provider 损坏 4 个月未修(issue #7)、onlyMainContent
参数声明但代码不读(死参数)、进程泄漏无人处理。不作为依赖或参考。

### 已知短板与残留风险
- SPA/JS 渲染页面抓不到(实测 Power Apps Portal 站点仅拿到骨架);SearXNG
  搜索引擎收录的快照可作部分兜底
- DNS rebinding 的 resolve-then-connect TOCTOU 窗口:个人场景接受
- pypdf 对结构损坏的 PDF 可能解析失败:已按"异常少→warning"处理,不静默截断
```

- [ ] **Step 2: 更新 `README.md` 功能一节**

在"功能"小节补充:`web_search` 支持 answers/infobox 直通、`min_score` 过滤、1h 缓存;`web_fetch` 支持 PDF 文本提取、JSON/文本、SSRF 防护、`section` 小节定位、24h 缓存、2MiB 上限。补充"本地试跑"小节说明 `docker-compose.local.yml` 与 `scripts/mcp-call.sh` 用法。

---

### Task 6: 端到端验证 + 部署生产

**Files:** 无新文件。只验证。

**Interfaces:** 无。

- [ ] **Step 1: 全量测试**

Run: `uv run pytest -v`
Expected: 全部 passed

- [ ] **Step 2: 起本地 SearXNG 实测(复用试跑环境)**

```bash
docker compose -f docker-compose.local.yml up -d searxng
SEARXNG_URL=http://127.0.0.1:8081 uv run python -c "
import asyncio
from src.tools.search import web_search
from src.tools.fetch import web_fetch

async def main():
    s = await web_search('SearXNG MCP server', min_score=0)
    print('answers:', s.get('answers'), '| top score:', s['results'][0]['score'] if s['results'] else None)
    f = await web_fetch('https://www.opendoors.org.hk/TC/resources/magpc-tc/')
    print('fetch keys:', sorted(f.keys()), '| len:', f.get('length'))
    pdf = await web_fetch('https://www.opendoors.org.hk/_entity/annotation/ae4d7796-0f9d-6ba3-9095-5db41eaec550')
    print('pdf:', pdf.get('content_type'), '| warning:', pdf.get('warning'), '| len:', len(pdf.get('content','')))
    bad = await web_fetch('http://127.0.0.1:9999/')
    print('ssrf:', bad.get('error'))

asyncio.run(main())
"
```

Expected: answers 列表非空或字段存在;fetch 正常返回 content(若失败,记录具体 error 供报告);PDF 提取为 text 且(若 <200 字符)带 warning;SSRF 返回可读拦截错误。已知 trafilatura 2.0.0 层行为:markdown 输出在部分多节文档上会追加一份重复正文,若 length 偏大先想到这是上游行为而非实现 bug。

- [ ] **Step 3: 部署生产(用户已授权)**

```bash
./sync.sh --rebuild   # rsync 到 oracle-main:/opt/custom-mcp 并重建容器
curl -s -m 15 https://mcp.346751.xyz/health   # 部署后验证
```

Expected: health 返回 `{"status":"ok"}`。注意:`pypdf` 是运行时依赖,`uv sync --no-dev` 会装进镜像;pytest/fpdf2 为 dev 依赖不进镜像。**不做 git commit**——代码留待用户审核后提交。
