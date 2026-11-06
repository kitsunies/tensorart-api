import asyncio
import base64
import hashlib
import hmac
import json

import httpx
import pytest

from tensorart_api.upstream import SOURCES, Upstream, UpstreamError, sign


def test_sign_format():
    digest = hmac.new(b"test-key", b"/community-web/v1/x1793939724949", hashlib.sha256).hexdigest()
    assert sign("/community-web/v1/x", None, "1793939724949", "test-key") == base64.b64encode(digest.encode()).decode()


def test_sign_key_per_source(monkeypatch):
    assert SOURCES["tensorhub"].sign_key == "test-key"  # falls back to TENSORART_SIGN_KEY
    monkeypatch.setenv("TENSORHUB_SIGN_KEY", "hub-key")
    assert SOURCES["tensorhub"].sign_key == "hub-key"
    assert SOURCES["tensorart"].sign_key == "test-key"
    monkeypatch.delenv("TENSORART_SIGN_KEY")
    with pytest.raises(UpstreamError) as e:
        SOURCES["tensorart"].sign_key
    assert e.value.status == 500


def test_sign_includes_sorted_query():
    assert sign("/p", {"b": 2, "a": 1}, "1", "k") == sign("/pa=1&b=2", None, "1", "k")


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
    assert req.headers["x-request-sign"] == sign("/x", None, req.headers["x-request-timestamp"], "test-key")
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
