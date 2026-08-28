"""web_search:SearXNG 元搜索透传,附答案框直通与相关性过滤。

answers/infoboxes/suggestions/corrections 是 SearXNG JSON API 原生返回,
直接透传给 LLM 可省去后续 fetch 轮次。
"""

import json
import logging
import os

import httpx

from .cache import TTLCache

SEARCH_TTL = 3600  # 1h;search 结果时效性比 fetch 敏感
_cache: TTLCache = TTLCache(maxsize=256, ttl=SEARCH_TTL)
logger = logging.getLogger(__name__)


def _searxng_url() -> str:
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
    except httpx.HTTPError as e:
        return {"error": f"SearXNG 请求失败: {type(e).__name__}: {e}"}
    except ValueError:  # JSONDecodeError 与非 UTF 字节导致的 UnicodeDecodeError
        return {"error": "SearXNG 返回非 JSON 响应,检查实例是否启用 json format"}
    if not isinstance(data, dict):
        return {"error": "SearXNG 返回了非预期的 JSON 结构"}

    results = []
    for r in (data.get("results") or []):
        score = r.get("score")
        if isinstance(score, (int, float)):
            if score < min_score:
                continue
        else:
            score = 0.0  # score 缺失/无效:不做过滤,保留该条
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "engine": r.get("engine", ""),
            "score": score,
        })

    result = {
        "query": data.get("query", query),
        "count": len(results),
        "results": results,
        "answers": data.get("answers") or [],
        "suggestions": data.get("suggestions") or [],
        "corrections": data.get("corrections") or [],
        "infoboxes": data.get("infoboxes") or [],
    }
    _cache.set(cache_key, dict(result))
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
        min_score: Drop results with relevance score below this (0 = keep all);
            results without a score are always kept (score reported as 0)
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            return await _search_with_client(
                client, query, category, language, pageno, time_range, min_score
            )
    except Exception as e:  # 兜底:任何未预期异常都以可读错误返回
        logger.exception("web_search 内部错误: %s", query)
        return {"error": f"搜索内部错误: {type(e).__name__}: {e}"}
