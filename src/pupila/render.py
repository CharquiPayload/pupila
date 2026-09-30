"""From Matrix events to Rich text: names, times, bodies, quotes and reactions."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from markdown_it import MarkdownIt
from rich.markdown import Markdown
from rich.text import Text

from .model import Event, Room, Store

PALETTE = ["#f5a97f", "#a6da95", "#8aadf4", "#eed49f", "#f5bde6", "#8bd5ca", "#ee99a0", "#91d7e3",
           "#b7bdf8", "#f0c6c6"]
URL = re.compile(r"https?://[^\s<>\"')\]]+")
ICONS = {"m.image": "🖼", "m.sticker": "🖼", "m.video": "🎞", "m.audio": "🎵", "m.file": "📎"}
_md = MarkdownIt("commonmark", {"breaks": True, "linkify": False}).enable(["strikethrough", "table"])


def clock(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000).strftime("%H:%M")


def day_label(ts: int) -> str:
    d = datetime.fromtimestamp(ts / 1000)
    today = datetime.now().date()
    if d.date() == today:
        return "today"
    if (today - d.date()).days == 1:
        return "yesterday"
    label = d.strftime("%A, %B ") + str(d.day)
    return label if d.year == today.year else f"{label}, {d.year}"


def color_for(user_id: str, colors: dict[str, str]) -> str:
    if user_id in colors:
        return colors[user_id]
    h = int(hashlib.sha1(user_id.encode()).hexdigest(), 16)
    return PALETTE[h % len(PALETTE)]


def human_size(n: int | None) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _hard_breaks(text: str) -> str:
    """In a chat a line break is a line break, not a space (as it would be in Markdown)."""
    out, in_code = [], False
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        following = lines[i + 1] if i + 1 < len(lines) else ""
        if not in_code and line.strip() and following.strip() and not line.lstrip().startswith("|"):
            line += "  "
        out.append(line)
    return "\n".join(out)


def body(e: Event, store: Store, room: Room):
    """A message's content as something Rich can draw."""
    if e.redacted:
        return Text("message deleted", style="italic dim")
    msgtype = e.msgtype
    text = e.text
    if msgtype == "m.notice":
        return Text(text, style="italic #b4a7f5")
    if msgtype == "m.emote":
        return Text.assemble(("* ", "dim"), (store.user_name(e.sender, room), "bold"), " ", text)
    if msgtype in ICONS:
        return attachment(e)
    if not text.strip():
        return Text("")
    if "formatted_body" in e.content or any(s in text for s in ("**", "`", "\n- ", "\n1. ", "# ", "](")):
        return Markdown(_hard_breaks(text), hyperlinks=True, code_theme="monokai")
    t = Text(text)
    t.highlight_regex(URL, "underline #8aadf4")
    return t


def attachment(e: Event) -> Text:
    from .media import duration, is_animated

    info = e.content.get("info") or {}
    name = e.content.get("filename") or e.content.get("body") or "file"
    if is_animated(e):
        return Text.assemble(("GIF", "bold #b4a7f5"), ("  click to open it large", "dim italic"))
    if e.msgtype == "m.video":
        parts = [("▶ ", "bold #b4a7f5"), (duration(e) or "video", "bold")]
    else:
        parts = [ICONS.get(e.msgtype, "📎") + " ", (name, "bold")]
    if info.get("size"):
        parts.append((f"  {human_size(info['size'])}", "dim"))
    parts.append(("  click to " + ("play" if e.msgtype == "m.video" else "open"), "dim italic"))
    return Text.assemble(*parts)


def caption(e: Event) -> str:
    """An image's text, when it's more than just the file name."""
    c = e.content
    if c.get("filename") and c.get("body") and c["body"] != c["filename"]:
        return c["body"]
    return ""


def quote(e: Event, store: Store, room: Room, colors: dict[str, str]) -> Text | None:
    if not e.reply_to:
        return None
    original = room.index.get(e.reply_to)
    if not original:
        return Text("╭─ (reply to an older message)", style="dim")
    summary = "message deleted" if original.redacted else (
        original.text.split("\n")[0] if original.msgtype not in ICONS
        else ICONS[original.msgtype] + " " + (original.content.get("body") or "file"))
    if len(summary) > 80:
        summary = summary[:79] + "…"
    return Text.assemble(("╭─ ", "dim"), (store.user_name(original.sender, room),
                                           f"bold {color_for(original.sender, colors)}"),
                         (": " + summary, "dim"))


def reactions(e: Event, me: str) -> Text | None:
    if not e.reactions:
        return None
    t = Text()
    for key, who in e.reactions.items():
        mine = me in who
        t.append(f" {key} {len(who)} ", style="bold on #3b3452" if mine else "on #2a2633")
        t.append(" ")
    return t


def header(e: Event, store: Store, room: Room, colors: dict[str, str]) -> Text:
    return Text.assemble((store.user_name(e.sender, room), f"bold {color_for(e.sender, colors)}"),
                         ("  " + clock(e.ts), "dim"))


def suffix(e: Event) -> Text | None:
    if e.failed:
        return Text("couldn't send", style="bold #f38ba8")
    if e.pending:
        return Text("sending…", style="dim italic")
    if e.edited and not e.redacted and e.msgtype != "m.notice":  # notices are status lines: edits are noise
        return Text("(edited)", style="dim")
    return None


def to_html(text: str) -> str | None:
    """The message's Markdown as HTML for formatted_body; None if it has no formatting."""
    html = _md.render(text).strip()
    simple = re.fullmatch(r"<p>(.*)</p>", html, re.S)
    if simple and "<" not in simple.group(1).replace("<br />", ""):
        return None
    return html
