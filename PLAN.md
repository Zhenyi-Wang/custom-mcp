# 实施计划

## 1. 项目初始化
- [x] 创建项目目录和基础文件
- [x] pyproject.toml（fastmcp, httpx, trafilatura, python-dotenv）
- [x] `uv sync` 安装依赖

## 2. SearXNG 部署配置
- [x] docker-compose.yml（SearXNG + mcp-server 两容器）
- [x] settings.yml（JSON API, 0.0.0.0, limiter false）

## 3. MCP Server 核心
- [x] `server.py` — FastMCP 3.x 主入口，ASGI Token 鉴权中间件
- [x] `tools/search.py` — web_search 工具，调用 SearXNG JSON API
- [x] `tools/fetch.py` — web_fetch 工具，httpx + trafilatura

## 4. 部署
- [x] Dockerfile（python:3.11-slim + uv）
- [x] Caddy 反代 mcp.346751.xyz + Let's Encrypt
- [x] sync.sh 同步脚本
- [x] scc MCP 预设

## 5. 测试
- [x] 鉴权: 无 token → 401, 带 token → 正常
- [x] web_search: 多引擎搜索正常
- [x] web_fetch: HTML→Markdown 正常
