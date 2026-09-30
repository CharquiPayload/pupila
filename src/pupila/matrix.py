"""Cliente mínimo de la API cliente-servidor de Matrix (sin cifrado).

Solo lo que usa Pupila: entrar, sincronizar, enviar, editar, borrar, reaccionar,
subir y bajar archivos, "escribiendo" y marcas de lectura.
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

FILTRO = {
    "room": {
        "state": {"lazy_load_members": True},
        "timeline": {"limit": 25, "lazy_load_members": True},
        "ephemeral": {"types": ["m.typing", "m.receipt"]},
    },
    "presence": {"not_types": ["*"]},
    "account_data": {"types": ["m.direct", "m.push_rules"]},
}


class ErrorMatrix(Exception):
    def __init__(self, status: int, errcode: str, error: str):
        super().__init__(f"{status} {errcode}: {error}")
        self.status, self.errcode, self.error = status, errcode, error


def q(s: str) -> str:
    return quote(s, safe="")


def partes_mxc(mxc: str) -> tuple[str, str]:
    servidor, _, media_id = mxc.removeprefix("mxc://").partition("/")
    return servidor, media_id


class Matrix:
    def __init__(self, homeserver: str, token: str | None = None, user_id: str | None = None,
                 device_id: str | None = None):
        self.homeserver = homeserver.rstrip("/")
        self.token, self.user_id, self.device_id = token, user_id, device_id
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10),
                                      headers={"User-Agent": "Pupila"})

    async def cerrar(self) -> None:
        await self.http.aclose()

    async def _pedir(self, metodo: str, ruta: str, *, json_: Any = None, params: dict | None = None,
                     contenido: bytes | None = None, tipo: str | None = None,
                     timeout: float | None = None, crudo: bool = False) -> Any:
        cabeceras = {}
        if self.token:
            cabeceras["Authorization"] = f"Bearer {self.token}"
        if tipo:
            cabeceras["Content-Type"] = tipo
        extra = {"timeout": httpx.Timeout(timeout, connect=10)} if timeout else {}
        r = await self.http.request(metodo, self.homeserver + ruta, json=json_, params=params,
                                    content=contenido, headers=cabeceras, **extra)
        if r.status_code >= 400:
            try:
                d = r.json()
            except ValueError:
                d = {}
            raise ErrorMatrix(r.status_code, d.get("errcode", "?"), d.get("error", r.text[:200]))
        return r.content if crudo else (r.json() if r.content else {})

    # --- sesión ---

    async def entrar(self, usuario: str, clave: str, dispositivo: str) -> dict:
        d = await self._pedir("POST", f"{API}/login", json_={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": usuario},
            "password": clave,
            "initial_device_display_name": dispositivo,
        })
        self.token, self.user_id, self.device_id = d["access_token"], d["user_id"], d.get("device_id")
        return d

    async def salir(self) -> None:
        await self._pedir("POST", f"{API}/logout", json_={})
        self.token = None

    async def quien_soy(self) -> dict:
        return await self._pedir("GET", f"{API}/account/whoami")

    # --- sincronización ---

    async def sync(self, desde: str | None, espera_ms: int = 30000) -> dict:
        params = {"filter": json.dumps(FILTRO, separators=(",", ":")), "timeout": str(espera_ms),
                  "set_presence": "online"}
        if desde:
            params["since"] = desde
        else:
            params["timeout"] = "0"
        return await self._pedir("GET", f"{API}/sync", params=params, timeout=espera_ms / 1000 + 30)

    async def mensajes(self, sala: str, desde: str, limite: int = 30) -> dict:
        return await self._pedir("GET", f"{API}/rooms/{q(sala)}/messages", params={
            "from": desde, "dir": "b", "limit": str(limite),
            "filter": json.dumps({"lazy_load_members": True}),
        })

    async def perfil(self, usuario: str) -> dict:
        return await self._pedir("GET", f"{API}/profile/{q(usuario)}")

    # --- enviar ---

    async def enviar(self, sala: str, contenido: dict, tipo: str = "m.room.message",
                     txn: str | None = None) -> str:
        txn = txn or uuid.uuid4().hex
        d = await self._pedir("PUT", f"{API}/rooms/{q(sala)}/send/{q(tipo)}/{txn}", json_=contenido)
        return d["event_id"]

    async def editar(self, sala: str, evento: str, nuevo: dict) -> str:
        cuerpo = dict(nuevo)
        cuerpo["m.new_content"] = dict(nuevo)
        cuerpo["body"] = "* " + nuevo.get("body", "")
        if "formatted_body" in nuevo:
            cuerpo["formatted_body"] = "* " + nuevo["formatted_body"]
        cuerpo["m.relates_to"] = {"rel_type": "m.replace", "event_id": evento}
        return await self.enviar(sala, cuerpo)

    async def borrar(self, sala: str, evento: str, motivo: str | None = None) -> None:
        await self._pedir("PUT", f"{API}/rooms/{q(sala)}/redact/{q(evento)}/{uuid.uuid4().hex}",
                          json_={"reason": motivo} if motivo else {})

    async def reaccionar(self, sala: str, evento: str, clave: str) -> str:
        return await self.enviar(sala, {"m.relates_to": {
            "rel_type": "m.annotation", "event_id": evento, "key": clave}}, tipo="m.reaction")

    async def escribiendo(self, sala: str, si: bool, ms: int = 6000) -> None:
        cuerpo = {"typing": si, "timeout": ms} if si else {"typing": False}
        await self._pedir("PUT", f"{API}/rooms/{q(sala)}/typing/{q(self.user_id)}", json_=cuerpo)

    async def leido(self, sala: str, evento: str) -> None:
        await self._pedir("POST", f"{API}/rooms/{q(sala)}/read_markers",
                          json_={"m.fully_read": evento, "m.read": evento})

    async def unirse(self, sala: str) -> dict:
        return await self._pedir("POST", f"{API}/join/{q(sala)}", json_={})

    async def rechazar(self, sala: str) -> None:
        await self._pedir("POST", f"{API}/rooms/{q(sala)}/leave", json_={})

    # --- archivos ---

    async def subir(self, datos: bytes, tipo: str, nombre: str) -> str:
        d = await self._pedir("POST", "/_matrix/media/v3/upload", params={"filename": nombre},
                              contenido=datos, tipo=tipo, timeout=300)
        return d["content_uri"]

    async def bajar(self, mxc: str) -> bytes:
        servidor, media_id = partes_mxc(mxc)
        return await self._pedir("GET", f"{MEDIA}/download/{q(servidor)}/{q(media_id)}",
                                 timeout=300, crudo=True)

    async def miniatura(self, mxc: str, ancho: int = 640, alto: int = 480) -> bytes:
        servidor, media_id = partes_mxc(mxc)
        try:
            return await self._pedir("GET", f"{MEDIA}/thumbnail/{q(servidor)}/{q(media_id)}", params={
                "width": str(ancho), "height": str(alto), "method": "scale"}, timeout=60, crudo=True)
        except ErrorMatrix:
            return await self.bajar(mxc)


async def reintentar(fn, *args, intentos: int = 3, **kw):
    """Para envíos: reintenta errores de red, no los de Matrix."""
    for i in range(intentos):
        try:
            return await fn(*args, **kw)
        except (httpx.TransportError, httpx.TimeoutException):
            if i == intentos - 1:
                raise
            await asyncio.sleep(1.5 * (i + 1))
