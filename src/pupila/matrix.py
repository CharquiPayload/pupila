"""A minimal client for the Matrix client-server API (no encryption).

Only what Pupila needs: log in, sync, send, edit, redact, react, upload and download
files, typing notices and read markers.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from urllib.parse import quote

import httpx

API = "/_matrix/client/v3"
MEDIA = "/_matrix/client/v1/media"

FILTER = {
    "room": {
        "state": {"lazy_load_members": True},
        "timeline": {"limit": 25, "lazy_load_members": True},
        "ephemeral": {"types": ["m.typing", "m.receipt"]},
    },
    "presence": {"not_types": ["*"]},
    "account_data": {"types": ["m.direct", "m.push_rules"]},
}


class MatrixError(Exception):
    def __init__(self, status: int, errcode: str, error: str):
        super().__init__(f"{status} {errcode}: {error}")
        self.status, self.errcode, self.error = status, errcode, error


def q(s: str) -> str:
    return quote(s, safe="")


def mxc_parts(mxc: str) -> tuple[str, str]:
    server, _, media_id = mxc.removeprefix("mxc://").partition("/")
    return server, media_id


class Matrix:
    def __init__(self, homeserver: str, token: str | None = None, user_id: str | None = None,
                 device_id: str | None = None):
        self.homeserver = homeserver.rstrip("/")
        self.token, self.user_id, self.device_id = token, user_id, device_id
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10),
                                      headers={"User-Agent": "Pupila"})

    async def close(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, path: str, *, json_: Any = None, params: dict | None = None,
                       content: bytes | None = None, content_type: str | None = None,
                       timeout: float | None = None, raw: bool = False) -> Any:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if content_type:
            headers["Content-Type"] = content_type
        extra = {"timeout": httpx.Timeout(timeout, connect=10)} if timeout else {}
        r = await self.http.request(method, self.homeserver + path, json=json_, params=params,
                                    content=content, headers=headers, **extra)
        if r.status_code >= 400:
            try:
                d = r.json()
            except ValueError:
                d = {}
            raise MatrixError(r.status_code, d.get("errcode", "?"), d.get("error", r.text[:200]))
        return r.content if raw else (r.json() if r.content else {})

    # --- session ---

    async def login(self, user: str, password: str, device_name: str) -> dict:
        d = await self._request("POST", f"{API}/login", json_={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": user},
            "password": password,
            "initial_device_display_name": device_name,
        })
        self.token, self.user_id, self.device_id = d["access_token"], d["user_id"], d.get("device_id")
        return d

    async def logout(self) -> None:
        await self._request("POST", f"{API}/logout", json_={})
        self.token = None

    async def whoami(self) -> dict:
        return await self._request("GET", f"{API}/account/whoami")

    # --- sync ---

    async def sync(self, since: str | None, timeout_ms: int = 30000) -> dict:
        params = {"filter": json.dumps(FILTER, separators=(",", ":")), "timeout": str(timeout_ms),
                  "set_presence": "online"}
        if since:
            params["since"] = since
        else:
            params["timeout"] = "0"
        return await self._request("GET", f"{API}/sync", params=params, timeout=timeout_ms / 1000 + 30)

    async def messages(self, room: str, start: str, limit: int = 30) -> dict:
        return await self._request("GET", f"{API}/rooms/{q(room)}/messages", params={
            "from": start, "dir": "b", "limit": str(limit),
            "filter": json.dumps({"lazy_load_members": True}),
        })

    async def profile(self, user: str) -> dict:
        return await self._request("GET", f"{API}/profile/{q(user)}")

    # --- sending ---

    async def send(self, room: str, content: dict, event_type: str = "m.room.message",
                   txn: str | None = None) -> str:
        txn = txn or uuid.uuid4().hex
        d = await self._request("PUT", f"{API}/rooms/{q(room)}/send/{q(event_type)}/{txn}", json_=content)
        return d["event_id"]

    async def edit(self, room: str, event_id: str, new: dict) -> str:
        body = dict(new)
        body["m.new_content"] = dict(new)
        body["body"] = "* " + new.get("body", "")
        if "formatted_body" in new:
            body["formatted_body"] = "* " + new["formatted_body"]
        body["m.relates_to"] = {"rel_type": "m.replace", "event_id": event_id}
        return await self.send(room, body)

    async def redact(self, room: str, event_id: str, reason: str | None = None) -> None:
        await self._request("PUT", f"{API}/rooms/{q(room)}/redact/{q(event_id)}/{uuid.uuid4().hex}",
                            json_={"reason": reason} if reason else {})

    async def react(self, room: str, event_id: str, key: str) -> str:
        return await self.send(room, {"m.relates_to": {
            "rel_type": "m.annotation", "event_id": event_id, "key": key}}, event_type="m.reaction")

    async def typing(self, room: str, typing: bool, ms: int = 6000) -> None:
        body = {"typing": typing, "timeout": ms} if typing else {"typing": False}
        await self._request("PUT", f"{API}/rooms/{q(room)}/typing/{q(self.user_id)}", json_=body)

    async def read_marker(self, room: str, event_id: str) -> None:
        await self._request("POST", f"{API}/rooms/{q(room)}/read_markers",
                            json_={"m.fully_read": event_id, "m.read": event_id})

    async def join(self, room: str) -> dict:
        return await self._request("POST", f"{API}/join/{q(room)}", json_={})

    async def leave(self, room: str) -> None:
        await self._request("POST", f"{API}/rooms/{q(room)}/leave", json_={})

    # --- files ---

    async def upload(self, data: bytes, content_type: str, filename: str) -> str:
        d = await self._request("POST", "/_matrix/media/v3/upload", params={"filename": filename},
                                content=data, content_type=content_type, timeout=300)
        return d["content_uri"]

    async def download(self, mxc: str) -> bytes:
        server, media_id = mxc_parts(mxc)
        return await self._request("GET", f"{MEDIA}/download/{q(server)}/{q(media_id)}",
                                   timeout=300, raw=True)

    async def thumbnail(self, mxc: str, width: int = 640, height: int = 480, method: str = "scale") -> bytes:
        server, media_id = mxc_parts(mxc)
        try:
            return await self._request("GET", f"{MEDIA}/thumbnail/{q(server)}/{q(media_id)}", params={
                "width": str(width), "height": str(height), "method": method}, timeout=60, raw=True)
        except MatrixError:
            return await self.download(mxc)


async def retry(fn, *args, attempts: int = 3, **kw):
    """For sends: retries network errors, not Matrix errors."""
    for i in range(attempts):
        try:
            return await fn(*args, **kw)
        except (httpx.TransportError, httpx.TimeoutException):
            if i == attempts - 1:
                raise
            await asyncio.sleep(1.5 * (i + 1))
