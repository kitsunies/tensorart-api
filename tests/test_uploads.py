import asyncio
import base64
import json
import os
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tensorart_api.account import Account
from tensorart_api.app import app
from tensorart_api.upstream import Upstream
from tensorart_api.uploads import Uploads, public

DATA = bytes(range(256)) * 4  # 1024 bytes -> 11 parts of 100


def token(exp: float, user: str = "u1") -> str:
    body = base64.urlsafe_b64encode(json.dumps({"userId": user, "deviceId": "d1", "exp": int(exp)}).encode()).decode()
    return f"h.{body.rstrip('=')}.s"


class Fake:
    """Catalogue API, model bucket and a range-capable file host in one MockTransport."""

    def __init__(self):
        self.puts: dict[int, bytes] = {}
        self.calls: dict[str, list] = {}
        self.files = {"https://files.test/model.safetensors": DATA}
        self.renewed = token(time.time() + 40 * 86400)

    def ok(self, data):
        return httpx.Response(200, json={"code": "0", "data": data})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, path = str(request.url).split("?")[0], request.url.path
        if request.url.host == "files.test":
            data = self.files.get(url)
            if data is None:
                return httpx.Response(403)
            start, end = (int(x) for x in request.headers["range"].removeprefix("bytes=").split("-"))
            end = min(end, len(data) - 1)
            return httpx.Response(206, content=data[start:end + 1], headers={
                "content-range": f"bytes {start}-{end}/{len(data)}",
                "content-disposition": 'attachment; filename="model.safetensors"'})
        if request.url.host == "r2.test":
            n = int(path.rsplit("/", 1)[1])
            self.puts[n] = request.content
            return httpx.Response(200, headers={"etag": f'"etag-{n}"'})
        body = json.loads(request.content) if request.content else None
        self.calls.setdefault(path, []).append(body)
        if path.endswith("/auth/sync"):
            return self.ok({"accessToken": self.renewed})
        if path.endswith("/channel/list"):
            return self.ok({"results": [{"id": "107", "i18n": {"enName": "Character"}}, {"id": "101", "i18n": {"enName": "Anime"}}]})
        if path.endswith("/multi_upload/create"):
            return self.ok({"objectKey": "files/u1/x.safetensors", "uploadId": "up1",
                            "items": [{"url": f"https://r2.test/part/{n}"} for n in range(1, body["count"] + 1)]})
        if path.endswith("/multi_upload/complete"):
            return self.ok({"endPoint": "cloudflarestorage.com", "bucket": "tensor-models", "objectKey": body["objectKey"]})
        if path.endswith("/project/create"):
            return self.ok({"projectId": "p9"})
        if path.endswith("/model/create"):
            return self.ok({"modelId": "m9"})
        if path.endswith("/model/detail"):
            return self.ok({"model": {"syncStatus": "FINISHED", "canRun": True, "accessKey": "key-9", "cover": [{"url": "https://img.test/c.jpg"}]}})
        return httpx.Response(405)


def make(tmp_path, fake=None, session=None):
    fake = fake or Fake()
    transport = httpx.MockTransport(fake)
    account = Account(Upstream(transport=transport), tmp_path, session or token(time.time() + 86400))
    uploads = Uploads(account, tmp_path, http=httpx.AsyncClient(transport=transport), min_part=100, poll_seconds=0)
    return fake, account, uploads


REQUEST = {"url": "https://files.test/model.safetensors", "name": "Watercolor", "type": "lora", "base_model": "Illustrious",
           "cover_url": "https://img.test/src.jpg", "version_name": "v2", "trigger_words": ["wc"], "description": "d",
           "source_url": "https://example.test/models/1", "file_name": None, "channel": "anime", "external_id": "ext:1"}


async def settle(uploads: Uploads) -> None:
    await asyncio.gather(*uploads.tasks.values())


def test_account_keeps_the_newest_token(tmp_path):
    old, new = token(time.time() + 86400), token(time.time() + 20 * 86400)
    (tmp_path / "session.json").write_text(json.dumps({"token": new, "renewed_at": 123.0}))
    a = Account(Upstream(), tmp_path, old)
    assert a.token == new and a.renewed_at == 123.0
    fresher = token(time.time() + 29 * 86400)
    b = Account(Upstream(), tmp_path, fresher)
    assert b.token == fresher and b.renewed_at is None  # a freshly pasted token renews on start


def test_renew_saves_a_private_session(tmp_path):
    fake, account, _ = make(tmp_path)

    async def go():
        await account.renew()
        await account.up.aclose()

    asyncio.run(go())
    saved = tmp_path / "session.json"
    assert account.token == fake.renewed and json.loads(saved.read_text())["token"] == fake.renewed
    assert oct(saved.stat().st_mode & 0o777) == "0o600"
    assert account.info()["user_id"] == "u1" and account.info()["renewed_at"]


def test_upload_copies_every_part_then_publishes_privately(tmp_path):
    fake, _, uploads = make(tmp_path)

    async def go():
        job = uploads.create(dict(REQUEST))
        await settle(uploads)
        return job

    job = public(asyncio.run(go()))
    assert job["state"] == "ready" and job["progress"] == 1.0 and job["bytes_done"] == len(DATA)
    assert (job["model_id"], job["access_key"], job["cover_url"]) == ("m9", "key-9", "https://img.test/c.jpg")
    assert b"".join(fake.puts[n] for n in sorted(fake.puts)) == DATA and len(fake.puts) == 11
    done = fake.calls["/community-web/v1/cloudflare/multi_upload/complete"][0]
    assert [i["partNumber"] for i in done["items"]] == list(range(1, 12)) and done["items"][0]["etag"] == '"etag-1"'
    project = fake.calls["/community-web/v1/project/create"][0]
    assert project["flags"]["onlyAccessibleBySelf"] is True and project["exclusiveChoice"] == "REPRINT"
    assert (project["type"], project["channelId"], project["originalUrl"]) == ("LORA", "101", "https://example.test/models/1")
    model = fake.calls["/community-web/v1/model/create"][0]
    assert model["imageUrls"] == ["https://img.test/src.jpg"] and model["baseModel"] == "Illustrious"
    assert model["modelFiles"][0]["name"] == "model.safetensors" and model["modelFiles"][0]["ossPath"] == "files/u1/x.safetensors"
    assert not list(tmp_path.glob("uploads/*.urls.json"))  # part URLs are dropped once complete


def test_same_external_id_dedupes_and_a_failed_copy_restarts(tmp_path):
    fake, _, uploads = make(tmp_path)

    async def go():
        first = uploads.create(dict(REQUEST, url="https://files.test/expired"))
        assert uploads.create(dict(REQUEST, url="https://files.test/expired"))["id"] == first["id"]
        await settle(uploads)
        failed = public(first)
        again = uploads.create(dict(REQUEST))
        await settle(uploads)
        return failed, again

    failed, again = asyncio.run(go())
    assert failed["state"] == "failed" and "source link failed (HTTP 403)" in failed["error"]
    assert again["id"] == failed["id"] and again["state"] == "ready"


def test_interrupted_copy_resumes_from_the_last_finished_part(tmp_path):
    fake, account, _ = make(tmp_path)
    jobs = tmp_path / "uploads"  # created by make()
    (jobs / "j1.urls.json").write_text(json.dumps({"urls": [f"https://r2.test/part/{n}" for n in range(1, 12)]}))
    job = {"id": "j1", "external_id": "ext:2", "request": dict(REQUEST, file_name="model.safetensors"), "state": "copying",
           "bytes_done": 200, "bytes_total": len(DATA), "parts": {"1": '"etag-1"', "2": '"etag-2"'}, "file": None,
           "multipart": {"object_key": "files/u1/x.safetensors", "upload_id": "up1", "part_size": 100, "created_at": time.time()},
           "project_id": None, "model_id": None, "access_key": None, "cover_url": None, "error": None,
           "created_at": "t", "updated_at": "t"}
    (jobs / "j1.json").write_text(json.dumps(job))

    async def go():
        u = Uploads(account, tmp_path, http=httpx.AsyncClient(transport=httpx.MockTransport(fake)), min_part=100, poll_seconds=0)
        u.resume()
        await settle(u)
        return u.jobs["j1"]

    done = asyncio.run(go())
    assert done["state"] == "ready" and sorted(fake.puts) == list(range(3, 12))
    assert "/community-web/v1/cloudflare/multi_upload/create" not in fake.calls
    assert len(fake.calls["/community-web/v1/cloudflare/multi_upload/complete"][0]["items"]) == 11


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "t0k")
    with TestClient(app) as c:
        yield c


def test_account_endpoints_need_the_bearer_token_and_a_session(client, tmp_path):
    assert client.get("/v1/account").status_code == 401
    auth = {"authorization": "Bearer t0k"}
    assert client.get("/v1/account", headers=auth).status_code == 503  # no TENSORART_SESSION
    fake, account, uploads = make(tmp_path)
    client.app.state.account, client.app.state.uploads = account, uploads
    assert client.get("/v1/account", headers=auth).json()["user_id"] == "u1"
    assert client.get("/v1/uploads/nope", headers=auth).status_code == 404
    assert client.post("/v1/uploads", headers=auth, json={"url": "x"}).status_code == 422
    assert client.get("/v1/uploads", headers=auth, params={"external_id": "none"}).json() == {"items": []}
