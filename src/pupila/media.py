"""GIFs and videos: frames to animate them inside the chat, video thumbnails, and a small
player that plays a video inside its own message.

GIFs from Discord and WhatsApp arrive as short silent MP4 videos (with the fi.mau.gif flag
set by the mautrix bridges); Tenor ones from Sable arrive as image/gif. MP4s need ffmpeg;
without it they're shown as regular videos.
"""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import signal
import tempfile
from pathlib import Path
from typing import Callable

from PIL import Image, ImageSequence

from .model import Event

WIDTH = 320        # px: plenty for 12 terminal lines
MAX_FRAMES = 90
MAX_SECONDS = 10
VIDEO_FPS = 10


def info(ev: Event) -> dict:
    return ev.content.get("info") or {}


def is_animated(ev: Event) -> bool:
    i = info(ev)
    if ev.msgtype in ("m.image", "m.sticker"):
        return i.get("mimetype") == "image/gif"
    if ev.msgtype == "m.video":
        return bool(i.get("fi.mau.gif") or (i.get("fi.mau.loop") and i.get("fi.mau.autoplay")))
    return False


def is_video(ev: Event) -> bool:
    return ev.msgtype == "m.video" and not is_animated(ev)


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def duration(ev: Event) -> str:
    ms = info(ev).get("duration")
    return clock(int(ms) / 1000) if ms else ""


def _shrink(im: Image.Image) -> Image.Image:
    im = im.convert("RGBA")
    im.thumbnail((WIDTH, WIDTH))
    return im


def gif_frames(data: bytes) -> list[tuple[Image.Image, float]]:
    """(frame, seconds it lasts) for a GIF."""
    out = []
    with Image.open(io.BytesIO(data)) as im:
        for n, frame in enumerate(ImageSequence.Iterator(im)):
            if n >= MAX_FRAMES:
                break
            lasts = (frame.info.get("duration") or 100) / 1000
            out.append((_shrink(frame.copy()), max(lasts, 0.04)))
    return out


async def _ffmpeg_frames(data: bytes, *args: str) -> list[Image.Image]:
    if not shutil.which("ffmpeg"):
        return []
    with tempfile.TemporaryDirectory(prefix="pupila-") as tmp:
        src = Path(tmp) / "video"
        src.write_bytes(data)
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(src), *args, str(Path(tmp) / "f%04d.png"),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(p.wait(), 60)
        except asyncio.TimeoutError:
            p.kill()
            return []
        out = []
        for f in sorted(Path(tmp).glob("f*.png")):
            with Image.open(f) as im:
                out.append(_shrink(im.copy()))
        return out


async def video_frames(data: bytes) -> list[tuple[Image.Image, float]]:
    frames = await _ffmpeg_frames(data, "-t", str(MAX_SECONDS), "-vf", f"fps={VIDEO_FPS},scale={WIDTH}:-2",
                                  "-frames:v", str(MAX_FRAMES))
    return [(f, 1 / VIDEO_FPS) for f in frames]


async def first_frame(data: bytes) -> Image.Image | None:
    frames = await _ffmpeg_frames(data, "-vf", f"scale={WIDTH}:-2", "-frames:v", "1")
    return frames[0] if frames else None


def mpv_output(image_style: str) -> list[str]:
    """How mpv draws video inside the terminal, depending on what the terminal can do.

    kitty: its graphics protocol; foot and other sixel terminals: sixel; the rest (Alacritty):
    coloured blocks. The "blocks" and "text" image styles force blocks.
    """
    from textual_image.renderable import Image as Detected
    from textual_image.renderable import SixelImage, TGPImage

    if image_style not in ("blocks", "text"):
        if Detected is TGPImage:
            return ["--vo=kitty", "--vo-kitty-use-shm=yes", "--profile=sw-fast"]
        if Detected is SixelImage:
            return ["--vo=sixel", "--profile=sw-fast"]
    return ["--vo=tct"]


def can_play_inline() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class InlinePlayer:
    """Plays a video inside a message: pictures decoded by ffmpeg, sound by ffplay or mpv.

    Pausing freezes both processes (SIGSTOP), so picture and sound stay together.
    """

    FPS = 12

    def __init__(self, path: Path, on_frame: Callable[[Image.Image], None],
                 on_time: Callable[[float, float], None], on_end: Callable[[], None]) -> None:
        self.path = path
        self.on_frame, self.on_time, self.on_end = on_frame, on_time, on_end
        self.state = "idle"  # idle, playing, paused
        self.length = 0.0
        self._video: asyncio.subprocess.Process | None = None
        self._audio: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._t0 = 0.0
        self._paused_at = 0.0
        self._paused_total = 0.0
        self._resumed = asyncio.Event()

    async def _probe(self) -> bool:
        p = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json",
            str(self.path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await p.communicate()
        try:
            d = json.loads(out or b"{}")
        except ValueError:
            d = {}
        self.length = float((d.get("format") or {}).get("duration") or 0)
        return any(s.get("codec_type") == "audio" for s in d.get("streams", []))

    async def play(self) -> None:
        has_audio = await self._probe()
        # PPM frames carry their own size, so rotated phone videos come out right.
        self._video = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(self.path),
            "-vf", f"fps={self.FPS},scale={WIDTH}:-2", "-f", "image2pipe", "-vcodec", "ppm", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        if has_audio:
            if shutil.which("ffplay"):
                cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(self.path)]
            elif shutil.which("mpv"):
                cmd = ["mpv", "--no-video", "--really-quiet", str(self.path)]
            else:
                cmd = []
            if cmd:
                self._audio = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
        loop = asyncio.get_running_loop()
        self._t0, self._paused_total = loop.time(), 0.0
        self.state = "playing"
        self._resumed.set()
        self._task = asyncio.create_task(self._run())

    def _clock(self) -> float:
        now = self._paused_at if self.state == "paused" else asyncio.get_running_loop().time()
        return now - self._t0 - self._paused_total

    async def _read_frame(self) -> Image.Image:
        out = self._video.stdout
        magic = (await out.readline()).strip()
        if magic != b"P6":
            raise asyncio.IncompleteReadError(magic, None)
        width, height = (int(x) for x in (await out.readline()).split())
        await out.readline()  # maximum value, always 255
        data = await out.readexactly(width * height * 3)
        return Image.frombytes("RGB", (width, height), data)

    async def _run(self) -> None:
        n = 0
        try:
            while True:
                frame = await self._read_frame()
                at = n / self.FPS
                n += 1
                while True:
                    await self._resumed.wait()
                    ahead = at - self._clock()
                    if ahead <= 0:
                        break
                    await asyncio.sleep(min(ahead, 0.05))
                if self._clock() - at > 2 / self.FPS:
                    continue  # running late: skip this picture, keep the sound going
                self.on_frame(frame)
                self.on_time(at, self.length)
        except (asyncio.IncompleteReadError, ValueError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            self._kill()
            self.state = "idle"
            self.on_end()

    def _signal(self, sig: int) -> None:
        for p in (self._video, self._audio):
            if p and p.returncode is None:
                try:
                    p.send_signal(sig)
                except ProcessLookupError:
                    pass

    def pause(self) -> None:
        if self.state == "playing":
            self._signal(signal.SIGSTOP)
            self._paused_at = asyncio.get_running_loop().time()
            self.state = "paused"
            self._resumed.clear()

    def resume(self) -> None:
        if self.state == "paused":
            self._paused_total += asyncio.get_running_loop().time() - self._paused_at
            self._signal(signal.SIGCONT)
            self.state = "playing"
            self._resumed.set()

    def _kill(self) -> None:
        self._signal(signal.SIGCONT)
        for p in (self._video, self._audio):
            if p and p.returncode is None:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass

    def stop(self) -> None:
        self._kill()
        if self._task and not self._task.done():
            self._task.cancel()
