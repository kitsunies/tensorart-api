"""Your own account: a session token that renews itself, and signed calls made as you.

The website keeps its login in a 30-day token. `GET /user-web/v1/auth/sync` swaps a valid
token for a fresh one (the old one stays valid), so renewing well inside 30 days keeps the
session alive until you change your password, log out everywhere or remove the device.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .upstream import SOURCES, Source, Upstream, UpstreamError

log = logging.getLogger("tensorart_api.account")
RENEW_SECONDS = float(os.environ.get("SESSION_RENEW_HOURS", 24)) * 3600


def claims(token: str) -> dict:
    """The token's payload. Not verified: we only read our own expiry, user and device ids."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError) as e:
        raise UpstreamError("TENSORART_SESSION is not a session token", 500) from e


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def write_private(path: Path, data: dict) -> None:
    """Atomic write, readable by the owner only (the file holds a login token)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


class Account:
    def __init__(self, upstream: Upstream, state_dir: Path, token: str | None = None, source: Source | None = None):
        self.up = upstream
        self.source = source or SOURCES[os.environ.get("ACCOUNT_SOURCE", "tensorhub")]
        self.path = Path(state_dir) / "session.json"
        saved = json.loads(self.path.read_text()) if self.path.exists() else {}
        # The newest token wins, so a renewed token on disk outlives the original one in the env.
        tokens = [t for t in (token, saved.get("token")) if t]
        self.token = max(tokens, key=lambda t: claims(t).get("exp", 0)) if tokens else None
        self.renewed_at = saved.get("renewed_at") if self.token and self.token == saved.get("token") else None
        self._channels: dict[str, str] = {}

    @classmethod
    def from_env(cls, upstream: Upstream, state_dir: Path) -> "Account | None":
        account = cls(upstream, state_dir, os.environ.get("TENSORART_SESSION", "").strip() or None)
        return account if account.token else None

    def info(self) -> dict:
        c = claims(self.token)
        return {"user_id": str(c.get("userId", "")), "token_expires_at": iso(c.get("exp")), "renewed_at": iso(self.renewed_at)}

    async def call(self, path: str, body: dict | None = None, query: dict | None = None) -> Any:
        """Uncached signed call as the account's user."""
        headers = {"authorization": f"Bearer {self.token}", "x-device-id": str(claims(self.token).get("deviceId", ""))}
        try:
            return await self.up._fetch(self.source, path, body, query, headers)
        except UpstreamError as e:
            if "need login" in e.message.lower():
                raise UpstreamError("the tensor.art session is no longer valid: set a fresh TENSORART_SESSION", 503) from e
            raise

    async def renew(self) -> None:
        data = await self.call("/user-web/v1/auth/sync") or {}
        token = data.get("accessToken")
        if not token or claims(token).get("userId") != claims(self.token).get("userId"):
            raise UpstreamError("session renewal returned no token for this user", 502)
        self.token, self.renewed_at = token, time.time()
        write_private(self.path, {"token": token, "renewed_at": self.renewed_at})
        log.info("session renewed, expires %s", iso(claims(token).get("exp")))

    async def keep_alive(self) -> None:
        """Renew once per RENEW_SECONDS (default daily); retry hourly on failure."""
        while True:
            due = (self.renewed_at or 0) + RENEW_SECONDS - time.time()
            if due > 0:
                await asyncio.sleep(due)
            try:
                await self.renew()
            except Exception as e:  # keep the loop alive; the old token stays valid until it expires
                log.warning("session renewal failed: %s", e)
                await asyncio.sleep(3600)

    async def channel_id(self, name: str) -> str:
        """Channel ids by English name (anime, character, illustration, realistic, ...)."""
        if not self._channels:
            data = await self.call("/community-web/v1/channel/list") or {}
            for c in data.get("results") or []:
                label = ((c.get("i18n") or {}).get("enName") or c.get("name") or "").lower()
                self._channels[label] = str(c["id"])
        if not self._channels:
            raise UpstreamError("no channels available for uploads", 502)
        return self._channels.get(name.lower()) or self._channels.get("anime") or next(iter(self._channels.values()))
