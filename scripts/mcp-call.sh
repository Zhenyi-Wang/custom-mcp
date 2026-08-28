#!/bin/bash
# 调用本地 mcp-searxng 工具(试跑环境,见 docker-compose.local.yml):
#   scripts/mcp-call.sh <tool_name> '<json_arguments>'
# 示例:
#   scripts/mcp-call.sh searxng_web_search '{"query":"Open Doors 禱告日曆"}'
#   scripts/mcp-call.sh web_url_read '{"url":"https://example.com"}'
# 环境变量:
#   MCP_BASE_URL   MCP 端点(默认 http://127.0.0.1:8100/mcp)
#   MCP_OUT_CHARS  输出截断长度(默认 3000)
set -e
cd "$(dirname "$0")/.."
TOKEN=$(grep MCP_TOKEN .env | cut -d= -f2)
BASE=${MCP_BASE_URL:-http://127.0.0.1:8100/mcp}
H=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")

# legacy 协议(2025-06-18)是 stateful 的:initialize 拿 session id,后续请求携带
SID=$(curl -s -m 15 -D /tmp/mcp-headers.txt "${H[@]}" "$BASE" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cli","version":"1.0"}}}' \
  -o /dev/null && grep -i '^mcp-session-id:' /tmp/mcp-headers.txt | tr -d '\r' | cut -d' ' -f2)
curl -s -m 15 "${H[@]}" -H "mcp-session-id: $SID" "$BASE" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' > /dev/null

curl -s -m 120 "${H[@]}" -H "mcp-session-id: $SID" "$BASE" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" \
| python3 -c "
import sys, json, os
raw = sys.stdin.read()
for line in raw.splitlines():
    if line.startswith('data:'): raw = line[5:].strip(); break
d = json.loads(raw)
if 'error' in d: print('RPC-ERROR:', json.dumps(d['error'], ensure_ascii=False)); exit()
r = d['result']
if r.get('isError'): print('TOOL-ERROR:'); print(r['content'][0]['text'][:800]); exit()
txt = r['content'][0]['text']
n = int(os.environ.get('MCP_OUT_CHARS', '3000'))
print(f'[len={len(txt)}]')
print(txt[:n])
"
