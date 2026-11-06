"""Signed client for the Tensor.Art community web API.

The public website signs every API call with an HMAC that uses a static key
shipped in its JavaScript bundle. Reproducing the signature is all that is
needed: no account, cookie or token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx



def load_dotenv(path: str = ".env") -> None:
    """Read KEY=value lines from ./.env into the environment without overriding what is already set."""
    try:
        with open(path) as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and key and not key.startswith("#"):
                    os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    except FileNotFoundError:
        pass


load_dotenv()
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SEARCH_VISIBILITY = "FLAW"  # what both sites send for model search


@dataclass(frozen=True)
class Source:
    name: str
    host: str
    package_id: str
    list_visibility: str
    key_env: str

    @property
    def sign_key(self) -> str:
        """Per-catalogue key, falling back to TENSORART_SIGN_KEY."""
        key = os.environ.get(self.key_env) or os.environ.get("TENSORART_SIGN_KEY")
        if not key:
            raise UpstreamError(f"{self.key_env} is not set (see .env.example)", 500)
        return key

    @property
    def api(self) -> str:
        return f"https://api.{self.host}"

    @property
    def site(self) -> str:
        return f"https://{self.host}"


SOURCES: dict[str, Source] = {
    "tensorart": Source("tensorart", "tensor.art", "3000", "NORMAL", "TENSORART_SIGN_KEY"),
    "tensorhub": Source("tensorhub", "tensorhub.art", "3023", "FLAW", "TENSORHUB_SIGN_KEY"),
}


class UpstreamError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


def canonical_query(query: dict[str, Any] | None) -> str:
    return "&".join(f"{quote(str(k))}={quote(str(v))}" for k, v in sorted((query or {}).items()))


def sign(path: str, query: dict[str, Any] | None, timestamp: str, key: str) -> str:
    """base64(hex(HMAC-SHA256(key, path + sorted query + timestamp))). The body is not signed."""
    digest = hmac.new(key.encode(), (path + canonical_query(query) + timestamp).encode(), hashlib.sha256)
    return base64.b64encode(digest.hexdigest().encode()).decode()


class Upstream:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        ttl: float = float(os.environ.get("CACHE_TTL", 300)),
        max_entries: int = 4096,
        concurrency: int = int(os.environ.get("UPSTREAM_CONCURRENCY", 8)),
    ):
        self.http = httpx.AsyncClient(transport=transport, timeout=20, headers={"user-agent": USER_AGENT})
        self.ttl = ttl
        self.max_entries = max_entries
        self.cache: dict[str, tuple[float, Any]] = {}
        self.gate = asyncio.Semaphore(concurrency)
        self.device_id = uuid.uuid4().hex[:21]

    async def aclose(self) -> None:
        await self.http.aclose()

    def _headers(self, source: Source, path: str, query: dict[str, Any] | None) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        return {
            "content-type": "application/json",
            "origin": source.site,
            "referer": source.site + "/",
            "x-device-id": self.device_id,
            "x-request-lang": "en-US",
            "x-request-package-id": source.package_id,
            "x-request-package-sign-version": "0.0.1",
            "x-request-sign": sign(path, query, ts, source.sign_key),
            "x-request-sign-type": "HMAC_SHA256",
            "x-request-sign-version": "v1",
            "x-request-timestamp": ts,
        }

    async def call(self, source: Source, path: str, body: dict | None = None, query: dict | None = None) -> Any:
        """Return the `data` field of a successful upstream response, cached for `ttl` seconds."""
        key = json.dumps([source.name, path, body, query], sort_keys=True)
        hit = self.cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]

        data = await self._fetch(source, path, body, query)
        # ponytail: whole-cache sweep when full, fine for one process; use an LRU if hit rate matters.
        if len(self.cache) >= self.max_entries:
            now = time.monotonic()
            self.cache = {k: v for k, v in self.cache.items() if v[0] > now}
            if len(self.cache) >= self.max_entries:
                self.cache.clear()
        self.cache[key] = (time.monotonic() + self.ttl, data)
        return data

    async def _fetch(
        self, source: Source, path: str, body: dict | None, query: dict | None, headers: dict | None = None,
        timeout: float | None = None,
    ) -> Any:
        """One uncached signed call. `headers` are merged over the signed ones and `timeout` over the
        client's (both used for account calls)."""
        url = source.api + path
        for attempt in range(3):
            try:
                async with self.gate:
                    r = await self.http.request(
                        "POST" if body is not None else "GET",
                        url,
                        params=query,
                        content=json.dumps(body) if body is not None else None,
                        headers={**self._headers(source, path, query), **(headers or {})},
                        **({"timeout": timeout} if timeout else {}),
                    )
            except httpx.TransportError as e:
                if attempt == 2:
                    raise UpstreamError(f"upstream unreachable: {e}", 504) from e
            else:
                if r.status_code == 405:
                    raise UpstreamError(f"{source.host} rejected the request signature (check {source.key_env})", 502)
                if r.status_code < 500 and r.status_code != 429:
                    break
                if attempt == 2:
                    raise UpstreamError(f"upstream returned HTTP {r.status_code}", 502)
            await asyncio.sleep(0.5 * 3**attempt)

        try:
            payload = r.json()
        except ValueError as e:
            raise UpstreamError(f"upstream returned non-JSON (HTTP {r.status_code})") from e
        if str(payload.get("code")) != "0":
            message = payload.get("message") or payload.get("msg") or "upstream error"
            raise UpstreamError(message, 404 if "not found" in message.lower() else 502)
        return payload.get("data")

    # Endpoints -----------------------------------------------------------

    async def search(
        self,
        source: Source,
        query: str,
        offset: int = 0,
        limit: int = 20,
        sort: str | None = None,
        base_models: list[str] | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "query": query,
            "visibility": SEARCH_VISIBILITY,
            "type": "MODEL",
            "limit": limit,
            "offset": offset,
        }
        if sort:
            body["sort"] = sort
        if base_models:
            body["filter"] = {"baseModels": base_models}
        data = await self.call(source, "/community-web/v1/search/general/v2", body)
        projects = (data or {}).get("projectData") or {}
        return {"items": projects.get("projects") or [], "total": int(projects.get("count") or 0)}

    async def browse(
        self,
        source: Source,
        cursor: str = "0",
        size: int = 40,
        sort: str = "HOT_TODAY",
        base_models: list[str] | None = None,
        model_types: list[str] | None = None,
    ) -> dict:
        flt: dict[str, Any] = {}
        if base_models:
            flt["baseModels"] = base_models
        if model_types:
            flt["modelTypes"] = model_types
        body = {
            "size": str(size),
            "cursor": cursor,
            "visibility": source.list_visibility,
            "sort": sort,
            "filter": flt,
        }
        data = await self.call(source, "/community-web/v1/project/portal/list/v3", body) or {}
        return {"items": data.get("items") or [], "cursor": data.get("cursor") or "0"}

    async def project(self, source: Source, project_id: str) -> dict:
        data = await self.call(source, "/community-web/v1/project/detail", query={"projectId": project_id})
        return (data or {}).get("project") or {}

    async def model(self, source: Source, model_id: str) -> dict:
        data = await self.call(source, "/community-web/v1/model/detail", query={"modelId": model_id})
        return (data or {}).get("model") or {}
