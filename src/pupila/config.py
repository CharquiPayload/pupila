"""Where Pupila keeps its things, and its settings.

- ~/.config/pupila/config.toml        preferences (created with defaults)
- ~/.config/pupila/pupila.tcss        your own style, on top of the built-in one (optional)
- ~/.local/state/pupila/session.json  the session token (readable only by you)
- /tmp/pupila-<uid>/                   downloaded images and files: /tmp is emptied on every
                                       reboot, so nothing piles up (see media_dir)
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default) / "pupila"


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config")
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state")
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache")
CONFIG_FILE = CONFIG_DIR / "config.toml"
STYLE_FILE = CONFIG_DIR / "pupila.tcss"
SESSION_FILE = STATE_DIR / "session.json"
LAST_ROOM_FILE = STATE_DIR / "last_room"

TEMPLATE = """\
# Pupila settings. Read at startup; style changes go in pupila.tcss (same folder),
# which is reloaded live while Pupila is running.

[notifications]
enabled = {notify}          # desktop notifications (notify-send)
show_text = {notify_text}        # include the message in the notification
bell = {bell}             # also ring the terminal bell

[chat]
bubbles = {bubbles}          # each message inside its own bubble, edged in the sender's colour

[images]
enabled = {images}          # show images inside the terminal
height = {image_height}              # height in lines
style = "{image_style}"           # "auto": sharp if the terminal can (kitty, foot), blocks otherwise
                         # "blocks": always coloured blocks, pixel-art style
                         # "text": drawn with characters, the most retro
animate = {animate}          # GIFs move inside the chat (Discord/WhatsApp ones need ffmpeg)
avatars = {avatars}          # profile pictures next to messages

[videos]
# What clicking a video does:
#   "chat":     plays it inside the message, with sound (needs ffmpeg; sound via ffplay or mpv)
#   "terminal": full screen in this same terminal with mpv (q to come back)
#   "window":   in its own window (mpv, or the system's default app)
player = "{video_player}"

[rooms]
# Spaces whose rooms are sorted by name (the rest, by recent activity).
sort_by_name = {sort_by_name}

# Colours for specific people: "@user:server" = "#rrggbb"
[colors]
{colors}"""


@dataclass
class Config:
    notify: bool = True
    notify_text: bool = True
    bell: bool = False
    bubbles: bool = True
    images: bool = True
    image_height: int = 12
    image_style: str = "auto"
    animate: bool = True
    avatars: bool = True
    video_player: str = "chat"
    sort_by_name: list[str] = field(default_factory=list)
    colors: dict[str, str] = field(default_factory=dict)

    def to_toml(self) -> str:
        def b(v: bool) -> str:
            return "true" if v else "false"

        return TEMPLATE.format(
            notify=b(self.notify), notify_text=b(self.notify_text), bell=b(self.bell),
            bubbles=b(self.bubbles), images=b(self.images), image_height=self.image_height, image_style=self.image_style,
            animate=b(self.animate), avatars=b(self.avatars), video_player=self.video_player,
            sort_by_name=json.dumps(self.sort_by_name, ensure_ascii=False),
            colors="".join(f'"{k}" = "{v}"\n' for k, v in self.colors.items()),
        )


def _from_spanish(d: dict) -> Config:
    """Settings written by Pupila 0.2 and earlier, when it spoke Spanish."""
    a, i, s, v = d.get("avisos", {}), d.get("imagenes", {}), d.get("salas", {}), d.get("videos", {})
    style = {"bloques": "blocks", "texto": "text"}.get(i.get("estilo", "auto"), "auto")
    player = "terminal" if v.get("reproductor") == "terminal" else "chat"
    return Config(
        notify=a.get("activos", True), notify_text=a.get("con_texto", True), bell=a.get("campana", False),
        images=i.get("activas", True), image_height=int(i.get("alto", 12)), image_style=style,
        animate=i.get("animar", True), video_player=player,
        sort_by_name=list(s.get("por_nombre", [])), colors=dict(d.get("colores", {})),
    )


def load() -> Config:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_state()
    remove_old_cache()
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(Config().to_toml())
    try:
        d = tomllib.loads(CONFIG_FILE.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        d = {}
    if "avisos" in d or "imagenes" in d or "salas" in d:
        cfg = _from_spanish(d)
        CONFIG_FILE.rename(CONFIG_FILE.with_name("config.toml.old"))
        CONFIG_FILE.write_text(cfg.to_toml())
        return cfg
    n, i, v, r = d.get("notifications", {}), d.get("images", {}), d.get("videos", {}), d.get("rooms", {})
    return Config(
        notify=n.get("enabled", True), notify_text=n.get("show_text", True), bell=n.get("bell", False),
        bubbles=bool(d.get("chat", {}).get("bubbles", True)),
        images=i.get("enabled", True), image_height=int(i.get("height", 12)),
        image_style=str(i.get("style", "auto")), animate=bool(i.get("animate", True)),
        avatars=bool(i.get("avatars", True)),
        video_player=str(v.get("player", "chat")), sort_by_name=list(r.get("sort_by_name", [])),
        colors=dict(d.get("colors", {})),
    )


REACTIONS_FILE = STATE_DIR / "reactions.json"
DEFAULT_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🔥", "🙏", "👀"]


def _emoji_counts() -> dict[str, int]:
    try:
        return json.loads(REACTIONS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def top_reactions(n: int = 6) -> list[str]:
    """The reactions you use most, topped up with the usual ones."""
    counts = _emoji_counts()
    used = sorted(counts, key=lambda k: -counts[k])
    return (used + [r for r in DEFAULT_REACTIONS if r not in used])[:n]


def frequent_emojis(n: int = 24) -> list[str]:
    """For the picker's "Frequently used" row: only what you've actually used."""
    counts = _emoji_counts()
    return sorted(counts, key=lambda k: -counts[k])[:n]


def count_emoji(key: str) -> None:
    counts = _emoji_counts()
    counts[key] = counts.get(key, 0) + 1
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REACTIONS_FILE.write_text(json.dumps(counts, ensure_ascii=False))


def save(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(cfg.to_toml())
    tmp.replace(CONFIG_FILE)


def _migrate_state() -> None:
    """Files named in Spanish by Pupila 0.2 and earlier."""
    for old, new in ((STATE_DIR / "sesion.json", SESSION_FILE), (STATE_DIR / "ultima_sala", LAST_ROOM_FILE)):
        if old.exists() and not new.exists():
            old.rename(new)


def media_dir() -> Path:
    """Where downloaded pictures, GIFs and files go: a private folder in /tmp."""
    d = Path(tempfile.gettempdir()) / f"pupila-{os.getuid()}"
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        if d.stat().st_uid != os.getuid() or d.is_symlink():
            raise PermissionError(d)  # someone else made it: don't use it
        d.chmod(0o700)
    except OSError:
        d = CACHE_DIR / "media"
        d.mkdir(parents=True, exist_ok=True)
    return d


def remove_old_cache() -> None:
    """Pupila 0.3.2 and earlier kept downloads in ~/.cache/pupila, which only grew."""
    for name in ("files", "thumbnails", "archivos", "miniaturas"):
        shutil.rmtree(CACHE_DIR / name, ignore_errors=True)
    try:
        CACHE_DIR.rmdir()
    except OSError:
        pass


def downloads_dir() -> Path:
    """The user's Downloads folder (XDG), for "Save to Downloads"."""
    try:
        for line in (Path.home() / ".config/user-dirs.dirs").read_text().splitlines():
            if line.startswith("XDG_DOWNLOAD_DIR="):
                return Path(os.path.expandvars(line.split("=", 1)[1].strip().strip('"')))
    except OSError:
        pass
    return Path.home() / "Downloads"


def read_session() -> dict | None:
    try:
        return json.loads(SESSION_FILE.read_text())
    except (OSError, ValueError):
        return None


def save_session(d: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, SESSION_FILE)


def delete_session() -> None:
    SESSION_FILE.unlink(missing_ok=True)
