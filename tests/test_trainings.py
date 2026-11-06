import asyncio
import base64
import json
import struct
import time
import zlib

import httpx
import pytest
from fastapi.testclient import TestClient

from tensorart_api import training as tr
from tensorart_api.account import Account
from tensorart_api.app import app
from tensorart_api.upstream import Upstream, UpstreamError


def png(w: int, h: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    chunk = lambda kind, body: struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def jpg(w: int, h: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def webp(w: int, h: int) -> bytes:
    body = b"WEBPVP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", len(body)) + body


def token() -> str:
    body = base64.urlsafe_b64encode(json.dumps({"userId": "u1", "deviceId": "d1", "exp": int(time.time() + 86400)}).encode()).decode()
    return f"h.{body.rstrip('=')}.s"


class Fake:
    """Trainer, model bucket and catalogue: the run walks PENDING, TRAINING, SUCCESS with samples rendering, then done."""

    def __init__(self, credits_ok: bool = True):
        self.calls: dict[str, list] = {}
        self.images: list[dict] = []
        self.status_seq = ["PENDING", "TRAINING", "SUCCESS", "SUCCESS"]
        self.polls = 0
        self.credits_ok = credits_ok
        self.hosts: set[str] = set()

    def ok(self, data):
        return httpx.Response(200, json={"code": "0", "data": data})

    def task(self) -> dict:
        status = self.status_seq[min(self.polls, len(self.status_seq) - 1)]
        rendering = self.polls < 3
        models = [{"id": f"tm{e}", "meta": {"epochs": e}} for e in (2, 4)] if status == "SUCCESS" else []
        runs = {f"tm{e}": {"status": "PROCESSING" if rendering else "SUCCESS",
                           "imageUrls": [] if rendering else [f"https://samples.test/e{e}-{i}.png" for i in range(4)]} for e in (2, 4)}
        return {"status": status, "costCredits": "108.0", "images": self.images, "models": models,
                "progress": {"currentSteps": 5 if status == "TRAINING" else 10, "totalSteps": 10},
                "sampleTasks": [{"runInfo": {"modelRunInfos": runs}}] if models else []}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "bucket.test":
            return httpx.Response(200)
        self.hosts.add(request.url.host if path.startswith("/train-web") else "")
        body = json.loads(request.content) if request.content else None
        self.calls.setdefault(path, []).append(body)
        if path.endswith("/create-task"):
            return self.ok({"result": {"id": "task1"}})
        if path.endswith("/upload/pre_sign"):
            n = len(self.calls[path])
            return self.ok({"uploadUrl": f"https://bucket.test/put/{n}", "displayUrl": f"https://img.test/{n}.png"})
        if path.endswith("/upload-image"):
            url = body["images"][0]["imageUrl"]
            self.images.append({"id": f"img{len(self.images)}", "isReg": False, "width": body["images"][0]["width"]})
            return self.ok({"results": {url: f"img{len(self.images)}"}, "items": []})
        if path.endswith("/tag-image"):
            return self.ok({"tags": ["1girl"]})
        if path.endswith("/update-task-params"):
            return self.ok(None)
        if path.endswith("/calc-credits"):
            return self.ok({"credits": 108.0})
        if path.endswith("/train-task/start"):
            if not self.credits_ok:
                return httpx.Response(200, json={"code": "400003", "message": "rpc error: code = Unknown desc = user insufficient credits"})
            return self.ok(None)
        if path == "/train-web/v1/train-task/task1":
            if self.calls.get("/train-web/v1/train-task/start"):
                self.polls += 1
            return self.ok({"result": self.task()})
        if path.endswith("/prepare-publish"):
            return self.ok({"trainModelId": "0"})
        if path.startswith("/train-web/v1/train-model/"):
            return self.ok({"result": {"meta": {"publishBucket": "tusi-modelhub", "publishEndpoint": "oss.test",
                                                "publishObjectKey": "users/LORA/u1/abc.TA_trained.safetensors"}}})
        if path.endswith("/channel/list"):
            return self.ok({"results": [{"id": "107", "i18n": {"enName": "Character"}}, {"id": "101", "i18n": {"enName": "Anime"}}]})
        if path.endswith("/project/create"):
            return self.ok({"projectId": "p1"})
        if path.endswith("/model/create"):
            return self.ok({"modelId": "m1"})
        if path.endswith("/model/detail"):
            return self.ok({"model": {"syncStatus": "FINISHED", "canRun": True, "accessKey": "key-1", "cover": [{"url": "https://cdn.test/c.png"}]}})
        return httpx.Response(405)


def make(tmp_path, fake=None):
    fake = fake or Fake()
    transport = httpx.MockTransport(fake)
    account = Account(Upstream(transport=transport), tmp_path, token())
    t = tr.Trainings(account, tmp_path, http=httpx.AsyncClient(transport=transport), poll_seconds=0, publish_poll_seconds=0)
    return fake, t


async def settle(t: tr.Trainings) -> None:
    while any(not task.done() for task in t.tasks.values()):
        await asyncio.gather(*t.tasks.values())


def test_image_size_reads_headers_only():
    assert tr.image_size(png(1024, 1536)) == (1024, 1536)
    assert tr.image_size(jpg(1200, 900)) == (1200, 900)
    assert tr.image_size(webp(800, 1280)) == (800, 1280)
    assert tr.image_size(b"GIF89a....") is None and tr.image_type(jpg(1, 1)) == "jpg"


def test_train_pick_publish_as_a_private_model(tmp_path):
    fake, t = make(tmp_path)

    async def go():
        job = await t.create("Inky Lines", "style")
        assert await t.add_images(job, [("a.png", png(1024, 1024)), ("b.jpg", jpg(1200, 900)), ("c.webp", webp(800, 1280))]) == 3
        await t.start(job)
        await settle(t)
        trained = tr.public(job)
        await t.publish(job, 4)
        await settle(t)
        return trained, tr.public(job)

    trained, done = asyncio.run(go())
    assert trained["state"] == "trained" and trained["credits"] == 108.0 and trained["trigger_word"] == "inkylines"
    assert [e["epoch"] for e in trained["epochs"]] == [2, 4] and len(trained["epochs"][1]["samples"]) == 4
    assert done["state"] == "published" and done["published"] == {"epoch": 4, "model_id": "m1", "access_key": "key-1", "cover_url": "https://cdn.test/c.png"}
    params = fake.calls["/train-web/v1/train-task/update-task-params"][0]["params"]["lora"]
    assert params["repeat"] == 20 and params["baseModelId"] == tr.BASE_MODEL["id"] and params["prompt"].startswith("inkylines, ")
    assert [i["width"] for i in fake.images] == [1024, 1200, 800]  # dimensions from the headers
    assert all(c["tagger"] == tr.TAGGER for c in fake.calls["/train-web/v1/train-task/tag-image"])
    assert fake.hosts - {""} == {"api.tensor.art"}  # train calls spend the main site's credits
    model = fake.calls["/community-web/v1/model/create"][0]
    assert model["meta"]["trainModelId"] == "tm4" and model["imageUrls"] == ["https://samples.test/e4-0.png"]
    assert model["modelFiles"][0]["ossBucket"] == "tusi-modelhub" and model["modelFiles"][0]["name"] == "abc.TA_trained.safetensors"
    assert fake.calls["/community-web/v1/project/create"][0]["flags"]["onlyAccessibleBySelf"] is True
    assert fake.calls["/community-web/v1/project/create"][0]["exclusiveChoice"] == "ORIGINAL"  # own work, not a reprint


def test_rejects_non_images_and_late_images(tmp_path):
    _, t = make(tmp_path)

    async def go():
        job = await t.create("x", "character")
        with pytest.raises(UpstreamError) as e:
            await t.add_images(job, [("notes.txt", b"hello")])
        assert e.value.status == 400
        job["state"] = "queued"
        with pytest.raises(UpstreamError) as e:
            await t.add_images(job, [("a.png", png(8, 8))])
        assert e.value.status == 409
        with pytest.raises(UpstreamError) as e:
            await t.publish(job, 2)
        assert e.value.status == 409

    asyncio.run(go())


def test_not_enough_credits_keeps_the_draft(tmp_path):
    _, t = make(tmp_path, Fake(credits_ok=False))

    async def go():
        job = await t.create("x", "character", external_id="ext-1")
        assert (await t.create("x", "character", external_id="ext-1"))["id"] == job["id"]
        await t.add_images(job, [("a.png", png(1024, 1024))])
        with pytest.raises(UpstreamError) as e:
            await t.start(job)
        return job, e.value

    job, err = asyncio.run(go())
    assert err.status == 402 and "108.0 needed" in err.message
    assert job["state"] == "draft" and "insufficient credits" in job["error"]


def test_publishing_resumes_after_a_restart(tmp_path):
    fake, t = make(tmp_path)

    async def go():
        job = await t.create("Inky", "style")
        await t.add_images(job, [("a.png", png(1024, 1024))])
        await t.start(job)
        await settle(t)
        job.update(state="publishing", pick=2, file={"bucket": "b", "endpoint": "e", "object_key": "k/x.safetensors"}, project_id="p0")
        t.save(job)
        again = tr.Trainings(t.account, tmp_path, http=t.http, poll_seconds=0, publish_poll_seconds=0)
        again.resume()
        await settle(again)
        return again.jobs[job["id"]]

    job = asyncio.run(go())
    assert job["state"] == "published" and job["published"]["epoch"] == 2
    assert "/community-web/v1/project/create" not in fake.calls  # the saved project was reused
    assert "/train-web/v1/train-model/prepare-publish" not in fake.calls


def test_training_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "t0k")
    auth = {"authorization": "Bearer t0k"}
    with TestClient(app) as c:
        fake, t = make(tmp_path)
        c.app.state.account, c.app.state.trainings = t.account, t
        made = c.post("/v1/trainings", headers=auth, json={"name": "Yor", "kind": "character"})
        assert made.status_code == 201 and made.json()["state"] == "draft"
        tid = made.json()["id"]
        r = c.post(f"/v1/trainings/{tid}/images", headers=auth, files=[("files", ("a.png", png(1024, 1024), "image/png")),
                                                                         ("files", ("b.png", png(1024, 1024), "image/png"))])
        assert r.json() == {"count": 2}
        assert c.post(f"/v1/trainings/{tid}/publish", headers=auth, json={"epoch": 2}).status_code == 409
        assert c.get("/v1/trainings", headers=auth).json()["items"][0]["id"] == tid
        assert c.get("/v1/trainings/nope", headers=auth).status_code == 404
        assert c.post("/v1/trainings", json={"name": "x", "kind": "style"}).status_code == 401
