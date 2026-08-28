# searxng-mcp

自托管 Web 搜索 + 网页抓取 MCP Server，替代第三方 MCP 依赖。

## 架构

```
Claude Code / AI Agent
    ↓ MCP Streamable HTTP + Bearer Token
searxng-mcp (FastMCP 3.x, port 8000)
    ├── web_search  → SearXNG (Docker, JSON API)
    └── web_fetch   → trafilatura (HTML → Markdown)
```

## 快速开始

```bash
# 1. 创建 .env
echo "MCP_TOKEN=$(openssl rand -hex 32)" > .env
echo "SEARXNG_URL=http://searxng:8080" >> .env

# 2. 启动
docker compose up -d

# 3. 验证
curl http://localhost:8000/health
```

## 功能

- **web_search**: SearXNG 元搜索，聚合 Google/DuckDuckGo/Startpage 等，支持分类、语言、时间过滤、分页；直通 SearXNG 原生 answers/infoboxes/suggestions/corrections（常可免 fetch 直接得到答案）；`min_score` 相关性过滤；结果带 relevance score；1h 缓存
- **web_fetch**: 抓取网页 → trafilatura 提取正文 → Markdown；支持 PDF 文本提取（pypdf，提取异常少时返回 warning）、JSON/纯文本、`section` 标题小节定位；SSRF 防护（字面 IP + DNS 解析双层检查、重定向逐跳验证）；响应体 2MiB 流式上限；24h 缓存；二进制文件明确拒绝

## 依赖

- Python 3.11+
- SearXNG（Docker，不暴露端口，仅内网通信）
- FastMCP 3.x（MCP Server 框架）
- trafilatura（HTML 解析）

## 配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SEARXNG_URL` | SearXNG 实例地址 | `http://searxng:8080` |
| `MCP_TOKEN` | Bearer Token 鉴权 | 空（不鉴权） |

## 测试

```bash
uv run pytest    # 58 用例（缓存 / SSRF / fetch / search）
```

## 本地试跑

与生产 `docker-compose.yml` 完全隔离的试跑环境（含 mcp-searxng 对照组）：

```bash
docker compose -f docker-compose.local.yml up -d
scripts/mcp-call.sh searxng_web_search '{"query":"test"}'
scripts/mcp-call.sh web_url_read '{"url":"https://example.com"}'
```

## 部署

```bash
# 同步到服务器
./sync.sh

# 同步 + 重建容器
./sync.sh --rebuild
```

## Claude Code 接入

```json
{
  "custom-mcp": {
    "type": "http",
    "url": "https://mcp.346751.xyz/mcp",
    "headers": {
      "Authorization": "Bearer <token>"
    }
  }
}
```

放入 `~/.claude.json` 的 `mcpServers` 中。
