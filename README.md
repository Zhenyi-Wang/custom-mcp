# searxng-mcp

自托管 Web 搜索 + 网页抓取 MCP Server，替代第三方依赖（web-search-prime、web-reader 等）。

## 架构

```
Claude Code / AI Agent
    ↓ MCP Protocol (stdio)
searxng-mcp (本项目)
    ├── web_search  → SearXNG (Docker, 元搜索引擎)
    └── web_fetch   → trafilatura (HTML → Markdown)
```

## 功能

- **web_search**: 通过 SearXNG 聚合 Google/Bing/DuckDuckGo 等搜索结果
- **web_fetch**: 抓取网页内容并转为 LLM 友好的 Markdown

## 依赖

- Python 3.11+
- SearXNG（Docker 容器，需单独部署）
- trafilatura（HTML 解析）
- FastMCP（MCP Server 框架）

## 配置

通过环境变量或 `.env` 文件配置：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SEARXNG_URL` | SearXNG 实例地址 | `http://localhost:9999` |
| `MCP_TOKEN` | MCP 访问令牌 | 无（可选） |
