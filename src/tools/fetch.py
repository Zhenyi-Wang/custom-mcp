import httpx
import trafilatura

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; searxng-mcp/1.0; "
        "+https://github.com/example/searxng-mcp)"
    ),
}

MAX_CONTENT_LENGTH = 10000


async def web_fetch(url: str) -> dict:
    """Fetch a web page and extract its content as Markdown.

    Args:
        url: The URL to fetch and extract content from
    """
    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, headers=HEADERS
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    content = trafilatura.extract(
        html,
        include_tables=True,
        include_images=False,
        include_links=True,
    )

    if not content:
        return {
            "url": str(resp.url),
            "content": "",
            "error": "No readable content extracted from this URL",
        }

    return {
        "url": str(resp.url),
        "content": content[:MAX_CONTENT_LENGTH],
        "length": len(content),
    }
