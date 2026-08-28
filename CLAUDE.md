# searxng-mcp

自托管 web_search + web_fetch MCP server(FastMCP + SearXNG + trafilatura)。

常用命令:

```bash
uv run pytest                                      # 全量测试(58 用例)
docker compose -f docker-compose.local.yml up -d   # 本地试跑环境(与生产隔离)
scripts/mcp-call.sh <tool> '<json>'                # 调用本地 MCP 工具验证
./sync.sh --rebuild                                # 部署生产(oracle-main)
```

- 工具签名只增带默认值参数,返回 dict 保留旧字段(向后兼容)
- search 缓存 1h(key 含全部查询参数),fetch 缓存 24h(key 为 URL、存未切片原文)
- fetch 上限:响应体 2MiB 流式截断、正文 10000 字符截断、重定向 5 跳

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

## 已知短板与残留风险

- SPA/JS 渲染页面抓不到(实测 Power Apps Portal 站点仅拿到骨架);SearXNG
  搜索引擎收录的快照可作部分兜底
- DNS rebinding 的 resolve-then-connect TOCTOU 窗口:个人场景接受
- pypdf 对结构损坏的 PDF 可能解析失败:已按"异常少→warning"处理,不静默截断
  (mcp-searxng 实测 3 页 PDF 只返回 1 页且无警告,本项目以此为反面教训)
- trafilatura 2.0.0 的 markdown 输出在部分多节文档上会追加一份重复正文,
  属上游行为;若发现 fetch length 偏大先想到这一层
- trafilatura 对部分真实大文档(docs.python.org 实测)也可能不产出标题标记,
  section 定位随之退化为 warning+全文;上游启发式行为,非本实现 bug
- charset 仅声明在 `<meta>` 标签(响应头无 charset)的老页面仍按 utf-8 硬解,
  可能耗出 U+FFFD;如需支持可引入 charset 探测兜底
- `section` 定位按标题行切分,不识别代码围栏内的 `#` 注释,含 shell 注释的
  技术文档小节可能提前结束

## 工程约定

- 提交:Conventional Commits 中文,不提及 Claude,不加 Co-Authored-By,
  提交前等用户明确指示,按功能点分次提交
- 测试:trafilatura 对过短文档会退化为降级提取(nav 不剔除、无标题标记),
  测试夹具必须保持文章型正文量(tests/test_fetch.py 的 HTML 夹具有注释说明)
- 本地验证用 httpx.MockTransport,DNS 用 resolver 注入 mock,不打真网
