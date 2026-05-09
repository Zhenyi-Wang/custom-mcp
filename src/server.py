import os

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Scope, Receive, Send

from .tools.search import web_search
from .tools.fetch import web_fetch

mcp = FastMCP("searxng-mcp")
mcp.tool()(web_search)
mcp.tool()(web_fetch)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"status": "ok"})


_base_app: ASGIApp = mcp.http_app()


class TokenAuthMiddleware:
    def __init__(self, asgi_app: ASGIApp) -> None:
        self.app = asgi_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/health":
            await self.app(scope, receive, send)
            return

        token = os.environ.get("MCP_TOKEN", "")
        if not token:
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        provided = auth.split(" ", 1)[1] if " " in auth else ""
        if provided != token:
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


app = TokenAuthMiddleware(_base_app)


def main():
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
