"""GIF y videos: cuadros para animarlos dentro del chat y miniaturas de los videos.

Los GIF de Discord y WhatsApp llegan como videos MP4 cortos sin sonido (con la marca
fi.mau.gif de los puentes mautrix); los de Tenor desde Sable, como image/gif. Para los MP4
hace falta ffmpeg; sin él se muestran como un video más.
"""
from __future__ import annotations

import asyncio
import io
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageSequence

from .modelo import Evento

ANCHO = 320        # px: de sobra para 12 líneas de terminal
MAX_CUADROS = 90
MAX_SEGUNDOS = 10
FPS_VIDEO = 10


def info(ev: Evento) -> dict:
    return ev.contenido.get("info") or {}


def es_animado(ev: Evento) -> bool:
    i = info(ev)
    if ev.msgtype in ("m.image", "m.sticker"):
        return i.get("mimetype") == "image/gif"
    if ev.msgtype == "m.video":
        return bool(i.get("fi.mau.gif") or (i.get("fi.mau.loop") and i.get("fi.mau.autoplay")))
    return False


def es_video(ev: Evento) -> bool:
    return ev.msgtype == "m.video" and not es_animado(ev)


def duracion(ev: Evento) -> str:
    ms = info(ev).get("duration")
    if not ms:
        return ""
    s = int(ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


def _achicar(im: Image.Image) -> Image.Image:
    im = im.convert("RGBA")
    im.thumbnail((ANCHO, ANCHO))
    return im


def cuadros_gif(datos: bytes) -> list[tuple[Image.Image, float]]:
    """(cuadro, segundos que dura) de un GIF."""
    fuera = []
    with Image.open(io.BytesIO(datos)) as im:
        for n, cuadro in enumerate(ImageSequence.Iterator(im)):
            if n >= MAX_CUADROS:
                break
            dura = (cuadro.info.get("duration") or 100) / 1000
            fuera.append((_achicar(cuadro.copy()), max(dura, 0.04)))
    return fuera


async def _ffmpeg(datos: bytes, *args: str) -> list[Image.Image]:
    if not shutil.which("ffmpeg"):
        return []
    with tempfile.TemporaryDirectory(prefix="pupila-") as tmp:
        entrada = Path(tmp) / "video"
        entrada.write_bytes(datos)
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(entrada), *args, str(Path(tmp) / "c%04d.png"),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(p.wait(), 60)
        except asyncio.TimeoutError:
            p.kill()
            return []
        fuera = []
        for f in sorted(Path(tmp).glob("c*.png")):
            with Image.open(f) as im:
                fuera.append(_achicar(im.copy()))
        return fuera


async def cuadros_video(datos: bytes) -> list[tuple[Image.Image, float]]:
    cuadros = await _ffmpeg(datos, "-t", str(MAX_SEGUNDOS), "-vf", f"fps={FPS_VIDEO},scale={ANCHO}:-2",
                            "-frames:v", str(MAX_CUADROS))
    return [(c, 1 / FPS_VIDEO) for c in cuadros]


async def primer_cuadro(datos: bytes) -> Image.Image | None:
    cuadros = await _ffmpeg(datos, "-vf", f"scale={ANCHO}:-2", "-frames:v", "1")
    return cuadros[0] if cuadros else None
