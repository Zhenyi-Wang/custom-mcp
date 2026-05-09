# 实施计划

## 1. 项目初始化
- [x] 创建项目目录和基础文件
- [x] pyproject.toml（mcp, httpx, trafilatura, python-dotenv）
- [ ] `uv sync` 安装依赖

## 2. SearXNG 部署配置
- [ ] docker-compose.yml（SearXNG 服务，挂载配置允许 JSON API）
- [ ] settings.yml（开启 json format、绑定 0.0.0.0、启用中文引擎）
- [ ] 一键启停脚本

## 3. MCP Server 核心
- [ ] `server.py` — FastMCP 主入口
- [ ] `tools/search.py` — web_search 工具
  - 调用 SearXNG JSON API
  - 支持分类（general/images/news/science）
  - 支持时间过滤、语言、分页
  - 返回结构化结果（标题、URL、摘要、引擎来源）
- [ ] `tools/fetch.py` — web_fetch 工具
  - httpx 抓取网页
  - trafilatura 转 Markdown
  - 超时和错误处理
- [ ] `auth.py` — Token 鉴权中间件
  - 从环境变量读取 token
  - 请求头或启动参数校验

## 4. 测试
- [ ] search 工具单元测试
- [ ] fetch 工具单元测试
- [ ] 鉴权测试

## 5. 集成
- [ ] Claude Code MCP 配置示例
- [ ] .env.example
