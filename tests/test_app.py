import pytest
from fastapi.testclient import TestClient

from tensorart_api.app import app

from .fake import fake_upstream


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.app.state.upstream = fake_upstream()
        yield c


def test_search_filters_and_paging(client):
    r = client.get("/v1/search", params={"q": "item", "source": "tensorhub", "type": "lora,dora", "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 5 and body["next_cursor"] and body["total"] == 50
    assert all(p["generation"]["allowed"] and p["type"] == "lora" for p in body["items"])
    nxt = client.get("/v1/search", params={"q": "item", "source": "tensorhub", "limit": 5, "cursor": body["next_cursor"]})
    assert not {p["id"] for p in nxt.json()["items"]} & {p["id"] for p in body["items"]}


def test_characters_and_styles(client):
    assert len(client.get("/v1/characters", params={"q": "item", "limit": 3}).json()["items"]) == 3
    assert client.get("/v1/styles", params={"q": "item"}).json()["items"] == []


def test_validation_and_errors(client):
    assert client.get("/v1/search", params={"source": "elsewhere"}).status_code == 422
    assert client.get("/v1/search", params={"limit": 0}).status_code == 422
    assert client.get("/v1/search", params={"cursor": "%%%"}).status_code == 400
    r = client.get("/v1/models/missing")
    assert r.status_code == 404 and r.json() == {"error": "model not found"}
    assert client.get("/v1/models/self1").status_code == 403


def test_detail_and_check(client):
    assert client.get("/v1/models/m1").json()["trigger_words"] == ["strapless bra", "black hairband"]
    assert [v["id"] for v in client.get("/v1/projects/p1").json()["versions"]] == ["m1", "self1"]
    checks = client.post("/v1/models/check", json={"model_ids": ["m1", "self1"]}).json()
    assert [c["generation"]["allowed"] for c in checks] == [True, False]
    assert client.post("/v1/models/check", json={"model_ids": []}).status_code == 422


def test_facets_and_docs(client):
    assert "noobai" in client.get("/v1/facets").json()["families"]
    assert client.get("/openapi.json").status_code == 200
