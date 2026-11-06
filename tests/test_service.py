import asyncio

import pytest

from tensorart_api import service
from tensorart_api.upstream import UpstreamError

from .fake import fake_upstream


def run(coro):
    return asyncio.run(coro)


def drain(**kw):
    """Follow cursors to the end; return every id seen."""

    async def go():
        up = fake_upstream()
        ids, cursor = [], None
        while True:
            page = await service.search(up, cursor=cursor, **kw)
            assert len(page.items) <= kw.get("limit", 20)
            ids += [p.id for p in page.items]
            cursor = page.next_cursor
            if not cursor:
                return ids

    return run(go())


@pytest.mark.parametrize("q", ["item", ""])
def test_cursor_pages_are_exact_and_complete(q):
    ids = drain(q=q, source="tensorhub", limit=7, generation="any")
    assert ids == [f"hub{i:04d}0" for i in range(50)]


def test_generation_switch():
    allowed = drain(q="item", source="tensorart", limit=9)
    blocked = drain(q="item", source="tensorart", limit=9, generation="blocked")
    assert len(allowed) == 33 and len(blocked) == 17
    assert not set(allowed) & set(blocked)


def test_all_sources_interleave():
    async def go():
        return await service.search(fake_upstream(), q="item", source="all", limit=4, generation="any")

    page = run(go())
    assert [p.source for p in page.items] == ["tensorart", "tensorhub", "tensorart", "tensorhub"]
    assert page.total == 100
    assert len(drain(q="item", source="all", limit=6, generation="any")) == 100


def test_bad_inputs():
    with pytest.raises(UpstreamError):
        service.sources_for("nope")
    with pytest.raises(UpstreamError):
        service.decode_cursor("!!!", service.sources_for("all"), True)


def test_check_reports_owner_only_and_missing():
    async def go():
        return await service.check(fake_upstream(), ["m1", "self1", "missing", "m1"])

    m1, self1, missing = run(go())
    assert m1.found and m1.generation.allowed and m1.trigger_words == ["strapless bra", "black hairband"]
    assert self1.found and self1.generation.reason == "owner_only"
    assert not missing.found and not missing.generation.allowed


def test_project_detail_versions():
    async def go():
        return await service.get_project(fake_upstream(), "p1")

    p = run(go())
    assert [v.id for v in p.versions] == ["m1", "self1"]
    assert p.versions[1].generation.reason == "owner_only"
    assert p.model_id == "m1" and p.categories == ["character"] and p.generation.allowed
    with pytest.raises(UpstreamError) as e:
        run(service.get_project(fake_upstream(), "nope"))
    assert e.value.status == 404
