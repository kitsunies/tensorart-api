import asyncio
import json

import httpx
import pytest

from tensorart_api.upstream import SOURCES, Upstream, UpstreamError, sign


def test_sign_matches_a_browser_request():
    # Captured from the live website: path, x-request-timestamp, x-request-sign.
    assert sign("/community-web/v1/project/portal/list/v3", None, "1793939724949") == (
        "ZDliYmVhODBkNTI5NmE2YTNlN2RkNjVlYjQwZmM3ZjI4ODcwYThmYmMzOWQ4YzdlMTYzZTcyN2Y5YTE2N2M4NQ=="
    )


def test_sign_includes_sorted_query():
    assert sign("/p", {"b": 2, "a": 1}, "1") == sign("/pa=1&b=2", None, "1")


def run(coro):
    return asyncio.run(coro)


def test_call_signs_caches_and_unwraps():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"code": "0", "data": {"ok": True}})

    async def go():
        up = Upstream(transport=httpx.MockTransport(handler))
        src = SOURCES["tensorhub"]
        a = await up.call(src, "/x", {"q": 1})
        b = await up.call(src, "/x", {"q": 1})
        await up.aclose()
        return a, b

    a, b = run(go())
    assert a == b == {"ok": True}
    assert len(seen) == 1
    req = seen[0]
    assert req.url.host == "api.tensorhub.art"
    assert req.headers["x-request-package-id"] == "3023"
    assert req.headers["x-request-sign"] == sign("/x", None, req.headers["x-request-timestamp"])
    assert json.loads(req.content) == {"q": 1}


def test_upstream_error_codes():
    def handler(request: httpx.Request):
        if request.url.path == "/missing":
            return httpx.Response(200, json={"code": "-1", "message": "project not found", "data": None})
        return httpx.Response(405, json={"code": 20000, "msg": "Error"})

    async def go(path):
        up = Upstream(transport=httpx.MockTransport(handler))
        try:
            await up.call(SOURCES["tensorart"], path)
        finally:
            await up.aclose()

    with pytest.raises(UpstreamError) as e:
        run(go("/missing"))
    assert e.value.status == 404
    with pytest.raises(UpstreamError) as e:
        run(go("/signed-wrong"))
    assert "signature" in e.value.message
