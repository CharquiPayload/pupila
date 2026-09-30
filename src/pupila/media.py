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
MAX_FRAMES = 60
MAX_SECONDS = 8
ANIMATION_FPS = 10  # GIFs are drawn at most this often: more only makes the terminal work harder


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


def _cap_rate(frames: list[tuple[Image.Image, float]]) -> list[tuple[Image.Image, float]]:
    """Merges frames that come faster than ANIMATION_FPS, keeping the total time.

    Frames are kept on a 1/ANIMATION_FPS grid, so a GIF at 11 fps stays at about 10.
    """
    step = 1 / ANIMATION_FPS
    out: list[list] = []
    t = next_slot = 0.0
    for frame, lasts in frames:
        if not out or t >= next_slot - 1e-6:
            out.append([frame, lasts])
            next_slot = (int(t / step + 1e-6) + 1) * step
        else:
            out[-1][1] += lasts
        t += lasts
    return [(f, d) for f, d in out]


def gif_frames(data: bytes) -> list[tuple[Image.Image, float]]:
    """(frame, seconds it lasts) for a GIF. Runs in a thread: decoding is slow."""
    raw = []
    with Image.open(io.BytesIO(data)) as im:
        for n, frame in enumerate(ImageSequence.Iterator(im)):
            if n >= MAX_FRAMES * 3:
                break
            lasts = (frame.info.get("duration") or 100) / 1000
            raw.append((frame.copy(), max(lasts, 0.02)))
    return [(_shrink(f), d) for f, d in _cap_rate(raw)[:MAX_FRAMES]]


async def _read_ppm(stream: asyncio.StreamReader) -> Image.Image:
    """One frame from an ffmpeg `-f image2pipe -vcodec ppm` stream."""
    magic = (await stream.readline()).strip()
    if magic != b"P6":
        raise asyncio.IncompleteReadError(magic, None)
    width, height = (int(x) for x in (await stream.readline()).split())
    await stream.readline()  # maximum value, always 255
    data = await stream.readexactly(width * height * 3)
    return Image.frombytes("RGB", (width, height), data)


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


async def video_frames(path: Path) -> list[tuple[Image.Image, float]]:
    """Frames of a short video (a Discord/WhatsApp GIF), read straight from ffmpeg's output."""
    if not shutil.which("ffmpeg"):
        return []
    p = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-nostdin", "-t", str(MAX_SECONDS), "-i", str(path),
        "-vf", f"fps={ANIMATION_FPS},scale={WIDTH}:-2", "-frames:v", str(MAX_FRAMES),
        "-f", "image2pipe", "-vcodec", "ppm", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    frames = []
    try:
        while True:
            frames.append((await _read_ppm(p.stdout), 1 / ANIMATION_FPS))
    except (asyncio.IncompleteReadError, ValueError):
        pass
    finally:
        if p.returncode is None:
            p.kill()
        await p.wait()
    return frames


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
        return await _read_ppm(self._video.stdout)

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


def has_graphics(image_style: str) -> bool:
    """True when the terminal draws real pictures (kitty, sixel) and the style doesn't ask for blocks."""
    from textual_image.renderable import Image as Detected
    from textual_image.renderable import SixelImage, TGPImage

    return image_style == "auto" and Detected in (TGPImage, SixelImage)


def round_avatar(img: Image.Image, size: int = 96) -> Image.Image:
    """A profile picture cut into a circle, like Discord's."""
    from PIL import ImageDraw, ImageOps

    img = ImageOps.fit(img.convert("RGBA"), (size, size))
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    img.putalpha(mask.resize((size, size), Image.LANCZOS))
    return img


def initial_avatar(letter: str, color: str, size: int = 96) -> Image.Image:
    """While a profile picture loads (or when there's none): the initial on the person's colour."""
    from PIL import ImageDraw, ImageFont

    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, big - 1, big - 1), fill=color)
    try:
        font = ImageFont.load_default(size=int(big * 0.55))
    except TypeError:  # Pillow older than 10.1
        font = ImageFont.load_default()
    d.text((big / 2, big / 2), letter, fill="#16141d", font=font, anchor="mm")
    return img.resize((size, size), Image.LANCZOS)


def _icon_canvas(size: int):
    from PIL import ImageDraw

    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img), big


def smiley_icon(color: str = "#b4a7f5", size: int = 64) -> Image.Image:
    """The emoji button's face (a line icon, like Discord's), one line tall."""
    img, d, big = _icon_canvas(size)
    stroke = big // 12
    pad = big * 0.08
    d.ellipse((pad, pad, big - pad, big - pad), outline=color, width=stroke)
    eye = big * 0.07
    for cx in (big * 0.36, big * 0.64):
        d.ellipse((cx - eye, big * 0.38 - eye, cx + eye, big * 0.38 + eye), fill=color)
    d.arc((big * 0.27, big * 0.27, big * 0.73, big * 0.73), start=25, end=155, fill=color, width=stroke)
    return img.resize((size, size), Image.LANCZOS)


def plus_icon(color: str = "#b4a7f5", size: int = 64) -> Image.Image:
    """The attach button: a plus in a filled circle, like Discord's."""
    img, d, big = _icon_canvas(size)
    pad = big * 0.08
    d.ellipse((pad, pad, big - pad, big - pad), fill=color)
    arm, stroke = big * 0.24, big // 11
    c = big / 2
    d.rectangle((c - arm, c - stroke / 2, c + arm, c + stroke / 2), fill="#16141d")
    d.rectangle((c - stroke / 2, c - arm, c + stroke / 2, c + arm), fill="#16141d")
    return img.resize((size, size), Image.LANCZOS)


def add_reaction_icon(color: str = "#b4a7f5", size: int = 64) -> Image.Image:
    """"More reactions": a face with a small plus, like Discord's."""
    img, d, big = _icon_canvas(size)
    stroke = big // 12
    face = big * 0.78
    x0, y0 = big * 0.04, big - face - big * 0.04
    d.ellipse((x0, y0, x0 + face, y0 + face), outline=color, width=stroke)
    eye = face * 0.075
    for cx in (x0 + face * 0.36, x0 + face * 0.64):
        cy = y0 + face * 0.4
        d.ellipse((cx - eye, cy - eye, cx + eye, cy + eye), fill=color)
    d.arc((x0 + face * 0.27, y0 + face * 0.27, x0 + face * 0.73, y0 + face * 0.73), start=25, end=155,
          fill=color, width=stroke)
    c, arm = big * 0.8, big * 0.17
    d.rectangle((c - arm, big * 0.2 - stroke / 2, c + arm, big * 0.2 + stroke / 2), fill=color)
    d.rectangle((c - stroke / 2, big * 0.2 - arm, c + stroke / 2, big * 0.2 + arm), fill=color)
    return img.resize((size, size), Image.LANCZOS)
