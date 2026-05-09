import os
import httpx

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")


async def web_search(
    query: str,
    category: str = "general",
    language: str = "en",
    pageno: int = 1,
    time_range: str = "",
) -> dict:
    """Search the web using SearXNG metasearch engine.

    Args:
        query: Search query string
        category: Search category: general, images, news, science, it, videos, map, music, files, social_media
        language: Language code like en, zh-CN, ja, etc. Empty for all languages
        pageno: Page number starting from 1
        time_range: Time filter: day, week, month, year, or empty for no filter
    """
    params = {
        "q": query,
        "format": "json",
        "categories": category,
        "language": language,
        "pageno": str(pageno),
    }
    if time_range:
        params["time_range"] = time_range

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{SEARXNG_URL}/search", params=params)
        resp.raise_for_status()
        data = resp.json()

    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "engine": r.get("engine", ""),
        }
        for r in data.get("results", [])
    ]

    return {
        "query": data.get("query", query),
        "count": len(results),
        "results": results,
    }
