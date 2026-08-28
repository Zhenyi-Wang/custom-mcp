import json

import httpx
import pytest

from src.tools import search as search_mod

SEARXNG_RESPONSE = {
    "query": "test",
    "answers": ["42"],
    "suggestions": ["testing", "pytest"],
    "corrections": [],
    "infoboxes": [{"infobox": "Test", "content": "A test"}],
    "results": [
        {"title": "A", "url": "https://a.com", "content": "a", "engine": "google", "score": 10.0},
        {"title": "B", "url": "https://b.com", "content": "b", "engine": "bing", "score": 0.5},
    ],
}


def transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)


@pytest.fixture(autouse=True)
def clean_cache():
    search_mod._cache = search_mod.TTLCache(ttl=60)
    yield


async def test_direct_answers_passthrough():
    async with transport(
        lambda req: httpx.Response(200, json=SEARXNG_RESPONSE)
    ) as client:
        result = await search_mod._search_with_client(client, "test")
    assert result["answers"] == ["42"]
    assert result["suggestions"] == ["testing", "pytest"]
    assert result["infoboxes"] == [{"infobox": "Test", "content": "A test"}]


async def test_score_passthrough():
    async with transport(
        lambda req: httpx.Response(200, json=SEARXNG_RESPONSE)
    ) as client:
        result = await search_mod._search_with_client(client, "test")
    assert result["results"][0]["score"] == 10.0


async def test_min_score_filters():
    async with transport(
        lambda req: httpx.Response(200, json=SEARXNG_RESPONSE)
    ) as client:
        result = await search_mod._search_with_client(client, "test", min_score=1.0)
    assert [r["title"] for r in result["results"]] == ["A"]


async def test_min_score_keeps_missing_score():
    """min_score>0 时,score 缺失的结果不做过滤(保留),不静默丢弃。"""
    resp_data = {**SEARXNG_RESPONSE, "results": [
        {"title": "A", "url": "https://a.com", "content": "a", "engine": "g", "score": 10.0},
        {"title": "B", "url": "https://b.com", "content": "b", "engine": "b", "score": 0.5},
        {"title": "NoScore", "url": "https://c.com", "content": "c", "engine": "x"},
    ]}
    async with transport(
        lambda req: httpx.Response(200, json=resp_data)
    ) as client:
        result = await search_mod._search_with_client(client, "test", min_score=1.0)
    titles = [r["title"] for r in result["results"]]
    assert titles == ["A", "NoScore"]
    assert result["results"][1]["score"] == 0.0


async def test_cache_hit_skips_http():
    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(200, json=SEARXNG_RESPONSE)

    async with transport(handler) as client:
        await search_mod._search_with_client(client, "test", category="general")
        await search_mod._search_with_client(client, "test", category="general")
    assert len(calls) == 1


async def test_timeout_readable_error():
    def slow(req):
        raise httpx.ReadTimeout("timed out")

    async with transport(slow) as client:
        result = await search_mod._search_with_client(client, "test")
    assert "error" in result
    assert "超时" in result["error"]


async def test_unresponsive_engines_passthrough_with_hint():
    """上游引擎被限流导致 0 结果时,透传引擎状态并给出 hint。"""
    resp_data = {
        **SEARXNG_RESPONSE,
        "results": [],
        "unresponsive_engines": [
            ["duckduckgo", "CAPTCHA"],
            ["brave", "Suspended: too many requests"],
        ],
    }
    async with transport(
        lambda req: httpx.Response(200, json=resp_data)
    ) as client:
        result = await search_mod._search_with_client(client, "test")
    assert result["count"] == 0
    assert result["unresponsive_engines"] == [
        "duckduckgo: CAPTCHA",
        "brave: Suspended: too many requests",
    ]
    assert "限流" in result["hint"]


async def test_unresponsive_engines_absent_no_key():
    async with transport(
        lambda req: httpx.Response(200, json=SEARXNG_RESPONSE)
    ) as client:
        result = await search_mod._search_with_client(client, "test")
    assert "unresponsive_engines" not in result
    assert "hint" not in result
