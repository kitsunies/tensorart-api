"""Copy a model file from any URL into the account as a private model, resumably.

Flow, as the website does it: presigned multipart upload to the model bucket (all part URLs
come back at once and last 24 h), then a private project and a model version pointing at the
uploaded object. The source is read with HTTP range requests, one part at a time, so nothing
touches disk and an interrupted copy resumes from its last finished part.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx

from .account import Account, iso, write_private
from .upstream import UpstreamError

log = logging.getLogger("tensorart_api.uploads")

MIN_PART, MAX_PART, MAX_PARTS = 10 * 1024 * 1024, 100 * 1024 * 1024, 1000  # upstream limits
URL_LIFETIME = 23 * 3600  # part URLs last 24 h; restart the multipart before they lapse
TYPES = {"lora": "LORA", "locon": "LORA", "lycoris": "LORA", "dora": "LORA", "checkpoint": "CHECKPOINT"}
DONE = ("ready", "failed")


def part_size(total: int, minimum: int = MIN_PART) -> int:
    return min(MAX_PART, max(minimum, math.ceil(total / MAX_PARTS)))


def public(job: dict) -> dict:
    total = job.get("bytes_total") or 0
    keys = ("id", "external_id", "state", "bytes_done", "bytes_total", "project_id", "model_id", "access_key",
            "cover_url", "error", "created_at", "updated_at")
    out = {k: job.get(k) for k in keys}
    out["progress"] = 1.0 if job["state"] == "ready" else round(job.get("bytes_done", 0) / total, 4) if total else 0.0
    return out


class Uploads:
    def __init__(self, account: Account, state_dir: Path, http: httpx.AsyncClient | None = None,
                 min_part: int = MIN_PART, poll_seconds: float = 5.0):
        self.account = account
        self.dir = Path(state_dir) / "uploads"
        self.dir.mkdir(parents=True, exist_ok=True)
        # Source files and part PUTs go direct, never through a proxy meant for API calls.
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60, read=300), follow_redirects=True, trust_env=False)
        self.min_part = min_part
        self.poll_seconds = poll_seconds
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.gate = asyncio.Semaphore(int(os.environ.get("UPLOAD_CONCURRENCY", 2)))
        self.part_gate = int(os.environ.get("UPLOAD_PART_CONCURRENCY", 4))
        for p in self.dir.glob("*.json"):
            if not p.name.endswith(".urls.json"):
                job = json.loads(p.read_text())
                self.jobs[job["id"]] = job

    # Store -----------------------------------------------------------------

    def save(self, job: dict) -> None:
        job["updated_at"] = iso(time.time())
        write_private(self.dir / f"{job['id']}.json", job)

    def find(self, external_id: str) -> list[dict]:
        return [j for j in self.jobs.values() if j.get("external_id") == external_id]

    def resume(self) -> None:
        for job in self.jobs.values():
            if job["state"] not in DONE:
                self.start(job)

    def start(self, job: dict) -> None:
        if not (t := self.tasks.get(job["id"])) or t.done():
            self.tasks[job["id"]] = asyncio.create_task(self.run(job))

    def create(self, request: dict) -> dict:
        """New job, or the existing one for the same external_id (a failed one restarts with the new request)."""
        ext = request.get("external_id")
        for job in self.find(ext) if ext else []:
            if job["state"] == "failed":
                # Finished parts survive a fresh link to the same file; copy() starts over if the size changed.
                job.update(request=request, state="queued", error=None)
                self.save(job)
                self.start(job)
            return job
        now = iso(time.time())
        job = {"id": uuid.uuid4().hex[:16], "external_id": ext, "request": request, "state": "queued",
               "bytes_done": 0, "bytes_total": 0, "multipart": None, "parts": {}, "file": None,
               "project_id": None, "model_id": None, "access_key": None, "cover_url": None,
               "error": None, "created_at": now, "updated_at": now}
        self.jobs[job["id"]] = job
        self.save(job)
        self.start(job)
        return job

    async def aclose(self) -> None:
        for t in self.tasks.values():
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.http.aclose()

    # Work ------------------------------------------------------------------

    async def run(self, job: dict) -> None:
        async with self.gate:
            try:
                if not job.get("file"):
                    job["state"] = "copying"
                    self.save(job)
                    await self.copy(job)
                job["state"] = "publishing"
                self.save(job)
                await self.publish(job)
                job["state"] = "ready"
            except asyncio.CancelledError:
                raise  # shutdown: the job resumes on next start
            except Exception as e:
                while isinstance(e, ExceptionGroup):
                    e = e.exceptions[0]
                log.warning("upload %s failed: %s", job["id"], e)
                job.update(state="failed", error=getattr(e, "message", None) or str(e) or type(e).__name__)
            self.save(job)

    async def probe(self, url: str) -> tuple[int, str]:
        """Total size and a file name, from a one-byte range request."""
        async with self.http.stream("GET", url, headers={"range": "bytes=0-0"}) as r:
            if r.status_code >= 400:
                raise UpstreamError(f"source link failed (HTTP {r.status_code}); request the copy again with a fresh url", 502)
            size = r.headers.get("content-range", "").rpartition("/")[2]
            if r.status_code != 206 or not size.isdigit():
                raise UpstreamError("source does not support range requests", 502)
            total = int(size)
            disposition = r.headers.get("content-disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition)
        return total, unquote(m.group(1)) if m else unquote(Path(urlparse(url).path).name)

    async def copy(self, job: dict) -> None:
        req = job["request"]
        total, name = await self.probe(req["url"])
        if job.get("bytes_total") and job["bytes_total"] != total:
            job.update(multipart=None, parts={})  # a different file now: start over
        job["bytes_total"] = total
        req.setdefault("file_name", None)
        req["file_name"] = req["file_name"] or name or "model.safetensors"
        urls_path = self.dir / f"{job['id']}.urls.json"
        mp = job.get("multipart")
        if not mp or time.time() - mp["created_at"] > URL_LIFETIME or not urls_path.exists():
            size = part_size(total, self.min_part)
            ext = os.path.splitext(req["file_name"])[1] or ".safetensors"
            data = await self.account.call("/community-web/v1/cloudflare/multi_upload/create", {
                "count": math.ceil(total / size), "contentType": "application/octet-stream",
                "contentDisposition": f'attachment;filename="{quote(req["file_name"])}"', "scene": "MODEL", "ext": ext})
            mp = {"object_key": data["objectKey"], "upload_id": data["uploadId"], "part_size": size, "created_at": time.time()}
            write_private(urls_path, {"urls": [i["url"] for i in data["items"]]})
            job.update(multipart=mp, parts={})
        urls = json.loads(urls_path.read_text())["urls"]
        size = mp["part_size"]
        job["bytes_done"] = sum(min(size, total - (int(n) - 1) * size) for n in job["parts"])
        self.save(job)

        gate = asyncio.Semaphore(self.part_gate)

        async def send(n: int) -> None:
            start, end = (n - 1) * size, min(n * size, total) - 1
            async with gate:
                for attempt in range(3):
                    try:
                        src = await self.http.get(req["url"], headers={"range": f"bytes={start}-{end}"})
                        if src.status_code in (401, 403, 404, 410):
                            raise UpstreamError(f"source link failed (HTTP {src.status_code}); request the copy again with a fresh url", 502)
                        src.raise_for_status()
                        chunk = src.content
                        put = await self.http.put(urls[n - 1], content=chunk, headers={"content-type": "application/octet-stream"})
                        put.raise_for_status()
                        break
                    except (httpx.TransportError, httpx.HTTPStatusError):
                        if attempt == 2:
                            raise
                        await asyncio.sleep(2 * 3**attempt)
            job["parts"][str(n)] = put.headers["etag"]
            job["bytes_done"] += len(chunk)
            self.save(job)

        async with asyncio.TaskGroup() as tg:  # one failed part cancels the rest
            for n in range(1, len(urls) + 1):
                if str(n) not in job["parts"]:
                    tg.create_task(send(n))
        done = await self.account.call("/community-web/v1/cloudflare/multi_upload/complete", {
            "items": [{"partNumber": int(n), "etag": e} for n, e in sorted(job["parts"].items(), key=lambda p: int(p[0]))],
            "objectKey": mp["object_key"], "uploadId": mp["upload_id"], "scene": "MODEL"})
        job["file"] = {"bucket": done["bucket"], "endpoint": done["endPoint"], "object_key": done["objectKey"]}
        urls_path.unlink(missing_ok=True)

    async def publish(self, job: dict) -> None:
        req, a = job["request"], self.account
        if not job.get("project_id"):
            data = await a.call("/community-web/v1/project/create", {
                "name": req["name"], "modality": "IMAGE", "type": TYPES[req["type"]], "tagNames": [],
                "description": req.get("description") or "",
                "flags": {"allowOnlineGenerate": True, "isProjectOriginal": False, "onlyAccessibleBySelf": True, "grantDownload": False},
                "originalUrl": req.get("source_url") or "", "exclusiveChoice": "REPRINT",
                "channelId": await a.channel_id(req.get("channel") or "anime")})
            job["project_id"] = str(data["projectId"])
            self.save(job)
        if not job.get("model_id"):
            f = job["file"]
            data = await a.call("/community-web/v1/model/create", {
                "projectId": job["project_id"], "name": req.get("version_name") or "v1", "baseModel": req["base_model"],
                "trainedWords": req.get("trigger_words") or [], "description": req.get("description") or "",
                "meta": {"hideChat": True}, "flags": {"freeDownload": False, "grantDownload": False, "freeRun": True, "grantRun": False},
                "modelFiles": [{"name": req["file_name"], "floatingPoint": "FP16", "fileType": "SAFE_TENSOR", "modelSize": "FULL",
                                "ossBucket": f["bucket"], "ossEndpoint": f["endpoint"], "ossPath": f["object_key"]}],
                "imageIds": {"value": []}, "imageUrls": [req["cover_url"]], "coverShowcaseIds": {}, "subscribePlanId": "0"})
            job["model_id"] = str(data["modelId"])
            self.save(job)
        deadline = time.time() + 3600
        while True:
            model = ((await a.call("/community-web/v1/model/detail", query={"modelId": job["model_id"]})) or {}).get("model") or {}
            status = model.get("syncStatus")
            if status == "FINISHED" and model.get("canRun"):
                job["access_key"] = model.get("accessKey") or None
                job["cover_url"] = next((c.get("url") for c in model.get("cover") or [] if c.get("url")), None)
                return
            if status and "FAIL" in status:
                raise UpstreamError(f"the model failed to process upstream ({status})", 502)
            if time.time() > deadline:
                raise UpstreamError("the model did not become runnable within an hour", 504)
            await asyncio.sleep(self.poll_seconds)
