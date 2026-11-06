"""Train a LoRA from your own images on the catalogue's online trainer, then publish one epoch.

Images are uploaded and auto-tagged, the run uses a preset for the kind (character or style),
every saved epoch comes back with sample images, and the epoch you pick is published as a
private model through the same path as uploads. Training spends the site's own credits, so the
train calls go to TRAIN_SOURCE (default tensorart); publishing uses the account source.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import struct
import time
import uuid
from pathlib import Path

import httpx

from .account import Account, iso, write_private
from .upstream import SOURCES, UpstreamError
from .uploads import publish

log = logging.getLogger("tensorart_api.training")

TRAIN = "/train-web/v1"
# Illustrious v0.1: most Illustrious fine-tunes descend from it, so its LoRAs carry over best.
BASE_MODEL = {"id": "787353065419253689", "file_id": "787353065418205117", "label": "Illustrious"}
TAGGER = "wd-v1-4-vit-tagger-v2"  # free Danbooru-style tags, what Illustrious was trained on
IMAGE_REPEATS = 300  # images x repeats per epoch, so a run lands near 1500 steps whatever the set size
PRESETS = {
    "character": {"channel": "character", "prompt": "{t}, solo, looking at viewer, upper body, simple background, masterpiece, best quality"},
    "style": {"channel": "anime", "prompt": "{t}, 1girl, solo, outdoors, scenery, masterpiece, best quality"},
}
SAMPLE_TTL = 1800  # sample links last an hour upstream; refresh them well before
BUSY = ("queued", "training")


def image_type(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from the file header (png, jpg or webp), without decoding the image."""
    kind = image_type(data)
    if kind == "png" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if kind == "jpg":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # start of frame
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    if kind == "webp" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    return None


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())[:24] or "subject"


def public(job: dict) -> dict:
    keys = ("id", "external_id", "name", "kind", "trigger_word", "state", "images", "progress", "credits",
            "published", "error", "created_at", "updated_at")
    out = {k: job.get(k) for k in keys}
    out["epochs"] = [{"epoch": e["epoch"], "samples": e["samples"]} for e in job.get("epochs") or []]
    return out


def epochs_from(task: dict) -> list[dict]:
    """Saved epochs with their sample images; `done` once the sample run settled."""
    runs = {}
    for st in task.get("sampleTasks") or []:
        runs.update((st.get("runInfo") or {}).get("modelRunInfos") or {})
    out = []
    for m in task.get("models") or []:
        info = runs.get(m["id"]) or {}
        out.append({"epoch": int((m.get("meta") or {}).get("epochs") or 0), "model_id": str(m["id"]),
                    "samples": info.get("imageUrls") or [],
                    "done": bool(info) and info.get("status") not in ("DEFAULT", "INIT", "PENDING", "PROCESSING")})
    return sorted(out, key=lambda e: e["epoch"])


class Trainings:
    def __init__(self, account: Account, state_dir: Path, http: httpx.AsyncClient | None = None,
                 poll_seconds: float = 30.0, publish_poll_seconds: float = 5.0):
        self.account = account
        self.source = SOURCES[os.environ.get("TRAIN_SOURCE", "tensorart")]
        self.dir = Path(state_dir) / "trainings"
        self.dir.mkdir(parents=True, exist_ok=True)
        # Images go direct to the bucket, never through a proxy meant for API calls.
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(120, write=600, read=300), trust_env=False)
        self.poll_seconds, self.publish_poll_seconds = poll_seconds, publish_poll_seconds
        self.gate = asyncio.Semaphore(int(os.environ.get("TRAIN_UPLOAD_CONCURRENCY", 4)))
        self.jobs = {j["id"]: j for j in (json.loads(p.read_text()) for p in self.dir.glob("*.json"))}
        self.tasks: dict[str, asyncio.Task] = {}

    def save(self, job: dict) -> None:
        job["updated_at"] = iso(time.time())
        write_private(self.dir / f"{job['id']}.json", job)

    async def call(self, path: str, body: dict | None = None, query: dict | None = None):
        return await self.account.call(path, body, query, source=self.source)

    def get(self, training_id: str) -> dict:
        if training_id not in self.jobs:
            raise UpstreamError("training not found", 404)
        return self.jobs[training_id]

    def _run(self, job: dict, coro) -> None:
        if not (t := self.tasks.get(job["id"])) or t.done():
            self.tasks[job["id"]] = asyncio.create_task(coro)
        else:
            coro.close()

    def resume(self) -> None:
        for job in self.jobs.values():
            if job["state"] in BUSY:
                self._run(job, self.follow(job))
            elif job["state"] == "publishing":
                self._run(job, self._publish(job))

    async def aclose(self) -> None:
        for t in self.tasks.values():
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.http.aclose()

    # Steps -----------------------------------------------------------------

    async def create(self, name: str, kind: str, trigger_word: str | None = None, external_id: str | None = None) -> dict:
        for job in self.jobs.values():
            if external_id and job.get("external_id") == external_id:
                return job
        data = await self.call(f"{TRAIN}/train-task/create-task", {"taskType": "LORA"})
        now = iso(time.time())
        job = {"id": uuid.uuid4().hex[:16], "external_id": external_id, "name": name, "kind": kind,
               "trigger_word": trigger_word or slug(name), "task_id": str(data["result"]["id"]), "state": "draft",
               "images": 0, "progress": 0.0, "credits": None, "epochs": [], "samples_at": 0, "pick": None,
               "file": None, "project_id": None, "model_id": None, "access_key": None, "cover_url": None,
               "published": None, "error": None, "created_at": now, "updated_at": now}
        self.jobs[job["id"]] = job
        self.save(job)
        return job

    async def add_images(self, job: dict, files: list[tuple[str, bytes]]) -> int:
        """Upload, register and tag each image; returns the training's image count."""
        if job["state"] != "draft":
            raise UpstreamError("images can only be added before the training starts", 409)
        for name, data in files:
            if not image_size(data):
                raise UpstreamError(f"{name} is not a jpg, png or webp image", 400)

        async def one(data: bytes) -> None:
            kind, (w, h) = image_type(data), image_size(data)
            async with self.gate:
                link = await self.call("/community-web/v1/cloudflare/upload/pre_sign", {"scene": "TRAIN", "fileNameSuffix": kind})
                put = await self.http.put(link["uploadUrl"], content=data, headers={"content-type": f"image/{'jpeg' if kind == 'jpg' else kind}"})
                put.raise_for_status()
                url = link.get("displayUrl") or link.get("dbUrl")
                added = await self.call(f"{TRAIN}/train-task/upload-image", {"trainTaskId": job["task_id"], "images": [
                    {"imageUrl": url, "width": w, "height": h, "isReg": False, "isVideo": False}]})
                image_id = ((added or {}).get("results") or {}).get(url)
                if not image_id:
                    raise UpstreamError("an image was not accepted upstream", 502)
                await self.call(f"{TRAIN}/train-task/tag-image", {"imageId": image_id, "force": True, "tagger": TAGGER})

        try:
            async with asyncio.TaskGroup() as tg:
                for _, data in files:
                    tg.create_task(one(data))
        except ExceptionGroup as eg:
            raise eg.exceptions[0]
        task = (await self.call(f"{TRAIN}/train-task/{job['task_id']}") or {}).get("result") or {}
        job["images"] = sum(1 for i in task.get("images") or [] if not i.get("isReg"))
        self.save(job)
        return job["images"]

    async def start(self, job: dict) -> dict:
        if job["state"] in BUSY or job["state"] in ("trained", "publishing", "published"):
            return job
        if not job["images"]:
            raise UpstreamError("add images before starting the training", 409)
        t = job["trigger_word"]
        params = {
            "baseModelId": BASE_MODEL["id"], "baseModelFileId": BASE_MODEL["file_id"], "triggerWord": t,
            "repeat": max(2, min(20, round(IMAGE_REPEATS / job["images"]))), "epochs": 10, "saveEveryNEpochs": 2,
            "prompt": PRESETS[job["kind"]]["prompt"].format(t=t), "negativePrompt": "lowres, bad anatomy, worst quality, low quality",
            "width": 1024, "height": 1024, "sampler": "euler", "resolution": "1024,1024", "clipSkip": 2,
            "networkModule": "networks.lora", "seed": 0, "textEncoderLr": 1e-5, "unetLr": 1e-4, "lrScheduler": "constant",
            "lrSchedulerNumCycles": 1, "lrWarmupSteps": 0, "optimizer": "AdamW8bit", "networkRankDim": 32, "networkAlpha": 16,
            "factor": 8, "shuffleCaptions": False, "keepNToken": 1, "noiseOffset": 0.03, "multiresNoiseDiscount": 0.1,
            "multiresNoiseIterations": 10, "convDim": 0, "convAlpha": 0, "batchSize": 2, "liftPriority": False,
        }
        await self.call(f"{TRAIN}/train-task/update-task-params", {"taskId": job["task_id"], "params": {"lora": params}})
        cost = await self.call(f"{TRAIN}/train-task/calc-credits", {"trainTaskId": job["task_id"]}) or {}
        job["credits"] = cost.get("credits")
        try:
            await self.call(f"{TRAIN}/train-task/start", {"taskId": job["task_id"]})
        except UpstreamError as e:
            job["error"] = e.message
            self.save(job)
            if "insufficient credits" in e.message.lower():
                raise UpstreamError(f"not enough credits for this run ({job['credits']} needed)", 402) from e
            raise
        job.update(state="queued", error=None)
        self.save(job)
        self._run(job, self.follow(job))
        return job

    async def follow(self, job: dict) -> None:
        while job["state"] in BUSY:
            try:
                await self.refresh(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # transient upstream trouble: keep following
                log.warning("training %s refresh failed: %r", job["id"], e)
            if job["state"] in BUSY:
                await asyncio.sleep(self.poll_seconds)

    async def refresh(self, job: dict) -> None:
        task = (await self.call(f"{TRAIN}/train-task/{job['task_id']}") or {}).get("result") or {}
        status, p = task.get("status"), task.get("progress") or {}
        if task.get("costCredits") not in (None, ""):
            job["credits"] = float(task["costCredits"])
        if int(p.get("totalSteps") or 0):
            job["progress"] = round(int(p.get("currentSteps") or 0) / int(p["totalSteps"]), 4)
        if status == "PENDING":
            job["state"] = "queued"
        elif status == "TRAINING":
            job["state"] = "training"
        elif status in ("FAIL", "CANCEL"):
            job.update(state="failed", error=task.get("failedReason") or f"training {status.lower()}")
        elif status == "SUCCESS":
            epochs = epochs_from(task)
            job.update(progress=1.0, epochs=epochs, samples_at=time.time())
            if epochs and all(e["done"] for e in epochs):
                job["state"] = "trained"  # samples are in: the user picks an epoch
            else:
                job["state"] = "training"  # trained, sample images still rendering
        self.save(job)

    async def view(self, job: dict) -> dict:
        """The job, with sample links refreshed before they expire upstream."""
        if job["state"] == "trained" and time.time() - job.get("samples_at", 0) > SAMPLE_TTL:
            await self.refresh(job)
        return job

    async def publish(self, job: dict, epoch: int) -> dict:
        if job["state"] in ("publishing", "published"):
            return job
        if job["state"] != "trained":
            raise UpstreamError("pick an epoch once the training has finished", 409)
        if not any(e["epoch"] == epoch for e in job["epochs"]):
            raise UpstreamError(f"no saved epoch {epoch}", 400)
        if job.get("pick") != epoch:  # a different epoch is a different file
            job.update(file=None, project_id=None, model_id=None)
        job.update(state="publishing", pick=epoch, error=None)
        self.save(job)
        self._run(job, self._publish(job))
        return job

    async def _publish(self, job: dict) -> None:
        try:
            pick = next(e for e in job["epochs"] if e["epoch"] == job["pick"])
            if not job.get("file"):
                await self.call(f"{TRAIN}/train-model/prepare-publish", {"trainModelId": pick["model_id"]})
                for _ in range(120):
                    meta = ((await self.call(f"{TRAIN}/train-model/{pick['model_id']}") or {}).get("result") or {}).get("meta") or {}
                    if meta.get("publishBucket"):
                        break
                    await asyncio.sleep(self.publish_poll_seconds)
                else:
                    raise UpstreamError("the trained file was not ready to publish", 504)
                job["file"] = {"bucket": meta["publishBucket"], "endpoint": meta["publishEndpoint"], "object_key": meta["publishObjectKey"]}
                self.save(job)
            if time.time() - job.get("samples_at", 0) > SAMPLE_TTL / 2:  # the showcase link must outlive the copy
                await self.refresh(job)
                job["state"] = "publishing"
                pick = next(e for e in job["epochs"] if e["epoch"] == job["pick"])
            job["request"] = {
                "name": job["name"], "type": "lora", "version_name": f"epoch {pick['epoch']}", "base_model": BASE_MODEL["label"],
                "trigger_words": [job["trigger_word"]], "description": f"Trained on {job['images']} images.", "source_url": "",
                "channel": PRESETS[job["kind"]]["channel"], "file_name": Path(job["file"]["object_key"]).name,
                "cover_url": (pick["samples"] or [None])[0]}
            if not job["request"]["cover_url"]:
                raise UpstreamError("this epoch has no sample image to use as the showcase", 502)
            await publish(self.account, job, self.save, self.publish_poll_seconds, meta={"trainModelId": pick["model_id"]})
            job.update(state="published", published={"epoch": pick["epoch"], "model_id": job["model_id"],
                                                     "access_key": job.get("access_key"), "cover_url": job.get("cover_url")})
        except asyncio.CancelledError:
            raise  # shutdown: resumes on next start
        except Exception as e:
            log.warning("training %s publish failed: %r", job["id"], e)
            job.update(state="trained" if job.get("epochs") else "failed", error=getattr(e, "message", None) or str(e) or type(e).__name__)
        self.save(job)
