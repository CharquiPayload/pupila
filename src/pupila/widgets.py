"""The pieces of the screen: room sidebar, message timeline, composer and dialogs."""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (Button, Checkbox, DirectoryTree, Input, Label, OptionList, Select, Static, Switch,
                             TextArea, Tree)
from textual.widgets.option_list import Option
from textual_image.widget import HalfcellImage, UnicodeImage
from textual_image.renderable.tgp import Image as _TGPRenderable
from textual_image.widget import Image as TerminalImage
from textual_image.widget._base import Image as _BaseImage

from . import config, media, render
from .model import Event, Room

if TYPE_CHECKING:
    from .app import Pupila

MEDIA_TYPES = {"m.image", "m.sticker", "m.video"}


class KittyImage(_BaseImage, Renderable=_TGPRenderable):
    """An image widget for kitty's graphics protocol that doesn't blink between frames.

    textual-image deletes the old picture from the terminal before the new one is on screen,
    so every GIF frame showed a black gap. Here the old picture is deleted a moment later,
    once the new one has replaced it.
    """

    def render(self):
        old, self._renderable = self._renderable, None
        result = super().render()
        if old is not None:
            self.app.set_timer(0.25, old.cleanup)
        return result

    def on_unmount(self) -> None:
        if self._renderable is not None:
            self.app.set_timer(0.25, self._renderable.cleanup)
            self._renderable = None


def _auto_image_class():
    from textual_image.renderable import Image as Detected
    from textual_image.renderable import TGPImage

    return KittyImage if Detected is TGPImage else TerminalImage


IMAGE_STYLES = {"auto": _auto_image_class(), "blocks": HalfcellImage, "text": UnicodeImage}


def cells_wide(img, lines: int) -> int:
    """How many columns a picture `lines` tall takes, so its bubble fits it exactly."""
    from textual_image._terminal import get_cell_size

    cell = get_cell_size()
    if not img.height:
        return 1
    return max(1, round(lines * cell.height * img.width / img.height / cell.width))


# --------------------------------------------------------------------------- messages


class MessageView(Vertical):
    """One chat message: header (if it starts a group), quote, body, picture and reactions."""

    class Open(Message):
        def __init__(self, view: "MessageView") -> None:
            super().__init__()
            self.view = view

    class ContextRequested(Message):
        """Right click: the quick menu opens where the cursor is."""

        def __init__(self, view: "MessageView", x: int, y: int) -> None:
            super().__init__()
            self.view, self.x, self.y = view, x, y

    def __init__(self, ev: Event, room: Room, group: bool) -> None:
        super().__init__(classes="message -group" if group else "message")
        self.ev, self.room, self.group = ev, room, group
        self._frames: list = []
        self._frame = 0
        self.player: media.InlinePlayer | None = None

    @property
    def pupila(self) -> "Pupila":
        return self.app  # type: ignore[return-value]

    def _has_media(self) -> bool:
        return (self.ev.msgtype in MEDIA_TYPES and self.pupila.cfg.images and not self.ev.redacted
                and bool(self.ev.content.get("url")))

    def _animated(self) -> bool:
        return self.pupila.cfg.animate and media.is_animated(self.ev)

    def _still_key(self) -> str:
        """Where the app keeps the still picture: the photo itself, or the video's thumbnail."""
        url = self.ev.content["url"]
        return "video:" + url if self.ev.msgtype == "m.video" else url

    def compose(self) -> ComposeResult:
        if not self.pupila.cfg.avatars:
            yield from self._content()
            return
        with Horizontal(classes="with-avatar"):
            with Vertical(classes="avatar-slot"):
                if self.group:
                    yield self._avatar()
            with Vertical(classes="content"):
                yield from self._content()

    def _avatar_url(self) -> str | None:
        return self.pupila.store.avatar_of(self.ev.sender, self.room)

    def _avatar(self):
        """The sender's profile picture, or their initial on their colour while it loads."""
        p = self.pupila
        url = self._avatar_url()
        img = p.images.get("avatar:" + url) if url else None
        name = p.store.user_name(self.ev.sender, self.room).lstrip("@")
        color = render.color_for(self.ev.sender, p.cfg.colors)
        if img is None and media.has_graphics(p.cfg.image_style):
            img = media.initial_avatar(name[:1].upper() or "?", color)
        if img is not None:
            cls = IMAGE_STYLES.get(p.cfg.image_style, IMAGE_STYLES["auto"])
            w = cls(img, classes="avatar")
            w.styles.height = 2
            w.styles.width = cells_wide(img, 2)
            return w
        w = Static(name[:1].upper() or "?", classes="initial")
        w.styles.background = color
        return w

    def _needs_avatar(self) -> bool:
        url = self._avatar_url() if self.group and self.pupila.cfg.avatars else None
        return bool(url) and ("avatar:" + url) not in self.pupila.images

    @work(exclusive=True, group="avatar")
    async def load_avatar(self) -> None:
        url = self._avatar_url()
        if not url or await self.pupila.avatar(url) is None or not self.is_mounted:
            return
        # From the /tmp cache the picture can arrive before this message's own parts are
        # mounted; swapping then would find no slot and leave the initial there for good.
        self.call_after_refresh(self._swap_avatar)

    async def _swap_avatar(self) -> None:
        url = self._avatar_url()
        photo = self.pupila.images.get("avatar:" + url) if url else None
        if not self.is_mounted or photo is None:
            return
        for slot in self.query(".avatar-slot"):
            if any(getattr(c, "_image", None) is photo for c in slot.children):
                continue  # already showing it
            await slot.remove_children()
            await slot.mount(self._avatar())

    def _content(self) -> ComposeResult:
        p = self.pupila
        if not p.cfg.bubbles:
            if self.group:
                yield Static(render.header(self.ev, p.store, self.room, p.cfg.colors), classes="header")
            yield from self._inside()
            yield from self._reactions()
            return
        color = render.color_for(self.ev.sender, p.cfg.colors)
        bubble = Vertical(classes="bubble")
        bubble.styles.border = ("round", color)
        if self.group:
            bubble.border_title = f"{p.store.user_name(self.ev.sender, self.room)} · {render.clock(self.ev.ts)}"
            bubble.styles.border_title_color = color
            bubble.styles.border_title_style = "bold"
        with bubble:
            yield from self._inside()
        yield from self._reactions()

    def _inside(self) -> ComposeResult:
        """What goes in the bubble: quote, text or picture, and the (edited)/sending note."""
        p = self.pupila
        store, colors = p.store, p.cfg.colors
        q = render.quote(self.ev, store, self.room, colors)
        if q:
            yield Static(q, classes="quote")
        if self._has_media():
            frames = p.animations.get(self.ev.content["url"]) if self._animated() else None
            img = frames[0][0] if frames else p.images.get(self._still_key())
            if img is not None:
                yield self._picture(img)
            if img is None or media.is_video(self.ev) or (self.ev.msgtype == "m.video" and not frames):
                yield Static(render.attachment(self.ev), classes="body media status")
            text = render.caption(self.ev)
            if text:
                yield Static(text, classes="body")
        else:
            body = render.body(self.ev, store, self.room)
            if isinstance(body, Markdown):
                body = render.flatten(body, self._text_room())  # so it can be selected and copied
            w = Static(body, classes="body" + (" notice" if self.ev.msgtype == "m.notice" else ""))
            if p.cfg.bubbles and isinstance(body, Text):
                # A bubble sized by its content: long text would overflow it, so measure the
                # widest line drawn at the room's width.
                w.styles.width = self._fit_width(body)
            yield w
        s = render.suffix(self.ev)
        if s:
            yield Static(s, classes="suffix")

    def _text_room(self) -> int:
        """How wide a message's text can be: the bubble's 90% minus its border and padding."""
        room_width = self._room_width()
        return max(16, int(room_width * 0.9) - 6) if self.pupila.cfg.bubbles else max(16, room_width - 4)

    def _fit_width(self, renderable) -> int:
        available = self._text_room()
        console = Console(width=available, file=io.StringIO(), color_system=None)
        lines = console.render_lines(renderable, console.options.update_width(available), pad=False)
        widest = max((cell_len("".join(seg.text for seg in line).rstrip()) for line in lines), default=1)
        return max(1, min(available, widest))

    def _reactions(self) -> ComposeResult:
        r = render.reactions(self.ev, self.pupila.store.me)
        if r:
            yield Static(r, classes="reactions")

    def _picture(self, img):
        cls = IMAGE_STYLES.get(self.pupila.cfg.image_style, IMAGE_STYLES["auto"])
        w = cls(img, classes="image media")
        height = self.pupila.cfg.image_height
        width = cells_wide(img, height)
        widest = self._room_width() - 8  # wide screenshots: shrink to the room instead of spilling out
        if width > widest:
            width = max(4, widest)
            height = max(1, round(height * width / cells_wide(img, height)))
        w.styles.height = height
        w.styles.width = width
        return w

    def _room_width(self) -> int:
        try:
            width = self.app.query_one("#timeline").size.width
        except Exception:
            width = 0
        if width < 20:
            width = max(40, self.app.size.width - 36)
        return width - (6 if self.pupila.cfg.avatars else 0)

    def _needs_media(self) -> bool:
        if not self._has_media():
            return False
        url = self.ev.content["url"]
        if self._animated():
            return url not in self.pupila.animations
        return self._still_key() not in self.pupila.images

    def on_mount(self) -> None:
        if self._has_media() and self._animated() and self.ev.content["url"] in self.pupila.animations:
            self._start_animation(self.pupila.animations[self.ev.content["url"]])
        if self._needs_avatar():
            self.load_avatar()  # small and shared by many messages: no need to wait until it's in view
        if self._needs_media():
            self.set_timer(0.1, self._load_when_visible)

    def _load_when_visible(self) -> None:
        """Pictures, GIFs and profile pictures load when they scroll into view, not all at once."""
        if not self.is_mounted:
            return
        if self.is_on_screen:
            if self._needs_media():
                self.load_media()
        else:
            self.set_timer(0.5, self._load_when_visible)

    def on_unmount(self) -> None:
        if self.player:
            self.player.stop()

    @work(exclusive=True, group="media")
    async def load_media(self) -> None:
        p = self.pupila
        frames = await p.animation(self.ev) if self._animated() else None
        if not frames:
            if self.ev.msgtype == "m.video":
                await p.video_thumbnail(self.ev)
            else:
                await p.image(self.ev.content["url"])
        if self.is_mounted:
            await self.recompose()
            if frames:
                self._start_animation(frames)

    # --- animation: each frame lasts its own time; nothing is drawn while off screen ---

    def _start_animation(self, frames: list) -> None:
        if len(frames) < 2 or self._frames:
            return
        self._frames, self._frame = frames, 0
        self.set_timer(frames[0][1], self._next_frame)

    def _next_frame(self) -> None:
        if not self.is_mounted or not self._frames:
            return
        self._frame = (self._frame + 1) % len(self._frames)
        if self.is_on_screen and self.pupila.focused_app:  # nothing moves while you look elsewhere
            self._draw(self._frames[self._frame][0])
        self.set_timer(self._frames[self._frame][1], self._next_frame)

    def _draw(self, img, same_size: bool = True) -> None:
        for w in self.query(".image"):
            if same_size:
                w._image = img  # same size: no need to redo the layout
                w.refresh()
            else:
                w.image = img

    # --- video inside the message ---

    def _status(self, text: Text) -> None:
        for w in self.query(".status"):
            w.update(text)

    @work(exclusive=True, group="player")
    async def toggle_video(self) -> None:
        if self.player and self.player.state == "playing":
            self.player.pause()
            self._status(self._time_text())
            return
        if self.player and self.player.state == "paused":
            self.player.resume()
            self._status(self._time_text())
            return
        self._status(Text("⋯ loading", style="italic #b4a7f5"))
        path = await self.pupila.download(self.ev)
        if not path or not self.is_mounted:
            self._status(Text("couldn't download the video", style="#f38ba8"))
            return
        first = [True]

        def frame(img) -> None:
            if self.is_mounted:
                self._draw(img, same_size=not first[0])
                first[0] = False

        self.player = media.InlinePlayer(path, frame, lambda at, length: self._status(self._time_text()),
                                         self._video_ended)
        await self.player.play()

    def _time_text(self) -> Text:
        p = self.player
        if not p:
            return render.attachment(self.ev)
        at = media.clock(p._clock()) + (" / " + media.clock(p.length) if p.length else "")
        if p.state == "paused":
            return Text.assemble(("▶ ", "bold #b4a7f5"), (at, "bold"), ("  click to resume", "dim italic"))
        return Text.assemble(("⏸ ", "bold #b4a7f5"), (at, "bold"), ("  click to pause", "dim italic"))

    def _video_ended(self) -> None:
        self.player = None
        if self.is_mounted:
            self.call_later(self.recompose)  # back to the thumbnail

    async def refresh_view(self) -> None:
        if not self.player:
            await self.recompose()

    def on_click(self, event) -> None:
        event.stop()
        if event.button == 3:
            self.post_message(self.ContextRequested(self, event.screen_x, event.screen_y))
            return
        if event.button != 1:
            return
        on_media = event.widget is not None and event.widget.has_class("media")
        if on_media and media.is_video(self.ev) and self.pupila.cfg.video_player == "chat" \
                and media.can_play_inline():
            self.toggle_video()
        elif on_media:
            self.post_message(self.Open(self))  # a click on the picture or video opens it directly
        # a left click on text does nothing, so you can drag to select it


class Timeline(VerticalScroll):
    """The open room's history."""

    class LoadHistory(Message):
        pass

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.room: Room | None = None
        self.by_id: dict[str, MessageView] = {}
        self.fetching = False

    def _widgets(self, ev: Event, prev: Event | None) -> list:
        out = []
        new_day = prev is None or render.day_label(prev.ts) != render.day_label(ev.ts)
        if new_day:
            out.append(Static(f"── {render.day_label(ev.ts)} ──", classes="day"))
        group = (new_day or prev.sender != ev.sender or ev.ts - prev.ts > 5 * 60 * 1000
                 or bool(ev.reply_to))
        m = MessageView(ev, self.room, group)  # type: ignore[arg-type]
        self.by_id[ev.event_id] = m
        out.append(m)
        return out

    def on_mount(self) -> None:
        self.anchor()

    async def show(self, room: Room | None, at_end: bool = True) -> None:
        self.room = room
        self.by_id.clear()
        await self.remove_children()
        if room is None:
            await self.mount(Static("Pick a room on the left, or press Ctrl+K to search.", classes="empty"))
            return
        widgets: list = []
        if room.prev_batch:
            widgets.append(Static("⬆  older messages (scroll up to load them)", classes="more"))
        prev = None
        for ev in room.events:
            if ev.redacted:  # deleted messages disappear, like in Discord
                continue
            widgets.extend(self._widgets(ev, prev))
            prev = ev
        if not room.events:
            widgets.append(Static("No messages here yet.", classes="empty"))
        await self.mount_all(widgets)
        if at_end:
            self.anchor()

    async def sync(self) -> None:
        """Appends the room's events that aren't shown yet."""
        if not self.room:
            return
        self.reindex()
        prev, new = None, []
        for ev in self.room.events:
            if ev.redacted:
                continue
            if ev.event_id not in self.by_id:
                new.extend(self._widgets(ev, prev))
            prev = ev
        if new:
            for w in self.query(".empty"):
                await w.remove()
            await self.mount_all(new)  # if it was at the bottom, the anchor keeps it there

    def reindex(self) -> None:
        self.by_id = {w.ev.event_id: w for w in self.query(MessageView)}

    async def update_event(self, event_id: str) -> None:
        m = self.by_id.get(event_id)
        if m and m.ev.redacted:
            await self.remove_event(event_id)
        elif m:
            await m.refresh_view()

    async def remove_event(self, event_id: str) -> None:
        m = self.by_id.pop(event_id, None)
        if m:
            await m.remove()

    def watch_scroll_y(self, old: float, new: float) -> None:
        super().watch_scroll_y(old, new)
        if new <= 0 and old > 0 and self.room and self.room.prev_batch and not self.fetching:
            self.post_message(self.LoadHistory())


# --------------------------------------------------------------------------- room sidebar


class Sidebar(Tree[str]):
    """Spaces and rooms, with unread counts."""

    def __init__(self, **kw) -> None:
        super().__init__("rooms", **kw)
        self.show_root = False
        self.guide_depth = 2
        self.first = True
        self.visual_order: list[str] = []

    def rebuild(self, p: "Pupila") -> None:
        store = p.store
        expanded = {n.data for n in self._all_nodes() if n.is_expanded} | p.space_ancestors(p.current)
        cursor = self.cursor_node.data if self.cursor_node else p.current
        self.clear()
        self.visual_order = []

        def label(r: Room) -> Text:
            t = Text(store.room_name(r), style="bold" if r.unread else "")
            if r.typing:
                t.append(" ✎", style="italic #b4a7f5")
            if r.highlights:
                t.append(f" {r.highlights}", style="bold #f38ba8")
            elif r.unread:
                t.append(f" {r.unread}", style="bold #b4a7f5")
            return t

        def total(r: Room, seen: set[str]) -> int:
            n = 0
            for c in r.children:
                child = store.rooms.get(c)
                if child and c not in seen:
                    seen.add(c)
                    n += total(child, seen) if child.is_space else child.unread
            return n

        def space_label(r: Room) -> Text:
            t = Text(store.room_name(r), style="bold")
            n = total(r, set())
            if n:
                t.append(f" {n}", style="#b4a7f5")
            return t

        def by_name(space: Room) -> bool:
            return store.room_name(space) in p.cfg.sort_by_name

        def ordered(rooms: list[Room], alphabetical: bool) -> list[Room]:
            spaces = sorted((r for r in rooms if r.is_space), key=lambda r: store.room_name(r).lower())
            rest = [r for r in rooms if not r.is_space]
            if alphabetical:
                rest.sort(key=lambda r: store.room_name(r).lower())
            else:
                rest.sort(key=lambda r: -r.last_ts)
            return spaces + rest

        def add(node, space: Room, path: set[str]) -> None:
            children = [store.rooms[c] for c in space.children if c in store.rooms and c not in path
                        and not store.rooms[c].invited]
            for child in ordered(children, by_name(space)):
                if child.is_space:
                    sub = node.add(space_label(child), data=child.room_id, expand=child.room_id in expanded)
                    add(sub, child, path | {child.room_id})
                else:
                    node.add_leaf(label(child), data=child.room_id)
                    self.visual_order.append(child.room_id)

        for root in ordered(store.roots(), True):
            node = self.root.add(space_label(root), data=root.room_id,
                                 expand=self.first or root.room_id in expanded)
            add(node, root, {root.room_id})
        orphans = store.orphans()
        if orphans:
            node = self.root.add(Text("Other rooms", style="bold"), data="#other",
                                 expand=self.first or "#other" in expanded)
            for r in ordered(orphans, False):
                node.add_leaf(label(r), data=r.room_id)
                self.visual_order.append(r.room_id)
        invites = [r for r in store.rooms.values() if r.invited]
        if invites:
            node = self.root.add(Text("Invites", style="bold #f0c674"), data="#invites", expand=True)
            for r in invites:
                node.add_leaf(Text("✉ " + store.room_name(r), style="#f0c674"), data=r.room_id)
        self.first = False
        # While you move around the sidebar the cursor stays put; otherwise it follows the open room.
        self.call_after_refresh(self._place_cursor, cursor if self.has_focus else p.current)

    def _place_cursor(self, data: str | None) -> None:
        for n in self._all_nodes():
            if n.data == data:
                self.move_cursor(n)
                return

    def _all_nodes(self):
        stack = list(self.root.children)
        while stack:
            n = stack.pop()
            yield n
            stack.extend(n.children)


# --------------------------------------------------------------------------- attachments


@dataclass
class Attachment:
    """A file waiting to be sent: pasted, dragged in or added with /upload."""

    data: bytes
    content_type: str
    name: str
    preview: object | None = None  # a PIL image, for pictures


class AttachmentTray(Horizontal):
    """What you're about to send, above the composer, like Discord's."""

    class Removed(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    async def show(self, items: list[Attachment], style: str) -> None:
        await self.remove_children()
        cards = []
        for i, a in enumerate(items):
            card = Vertical(classes="attachment")
            parts: list = []
            if a.preview is not None:
                img = IMAGE_STYLES.get(style, IMAGE_STYLES["auto"])(a.preview, classes="attachment-picture")
                img.styles.height = 5
                img.styles.width = min(24, cells_wide(a.preview, 5))
                parts.append(img)
            else:
                icon = "🎞" if a.content_type.startswith("video/") else \
                    "🎵" if a.content_type.startswith("audio/") else "📄"
                parts.append(Static(icon, classes="attachment-icon"))
            name = a.name if len(a.name) <= 22 else a.name[:10] + "…" + a.name[-10:]
            parts.append(Static(Text.assemble((name, "bold"), ("  " + render.human_size(len(a.data)), "dim")),
                                classes="attachment-name"))
            remove = Static("✕", classes="attachment-remove", name=str(i))
            cards.append((card, parts, remove))
        for card, parts, remove in cards:
            await self.mount(card)
            await card.mount(remove, *parts)
        self.display = bool(items)

    def on_click(self, event) -> None:
        w = event.widget
        if w is not None and w.has_class("attachment-remove"):
            event.stop()
            self.post_message(self.Removed(int(w.name or 0)))


class IconButton(Static):
    """A small icon inside the composer's box, like Discord's: drawn in terminals with
    graphics, a character in the others."""

    class Pressed(Message):
        def __init__(self, button: "IconButton") -> None:
            super().__init__()
            self.button = button

        @property
        def control(self) -> "IconButton":  # lets @on(IconButton.Pressed, "#id") pick the button
            return self.button

    def __init__(self, glyph: str, draw, **kw) -> None:
        super().__init__(**kw)
        self.glyph, self.draw = glyph, draw

    def compose(self) -> ComposeResult:
        if media.has_graphics(self.app.cfg.image_style):  # type: ignore[attr-defined]
            w = IMAGE_STYLES["auto"](self.draw(), classes="icon")
            w.styles.height = 1
            w.styles.width = 2
            yield w
        else:
            yield Static(self.glyph, classes="icon-text")

    def on_click(self, event) -> None:
        event.stop()
        self.post_message(self.Pressed(self))


# --------------------------------------------------------------------------- composer


class Composer(TextArea):
    """Enter sends; Alt+Enter (or Shift+Enter if the terminal tells them apart) adds a new line."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class EditLast(Message):
        pass

    class Paste(Message):
        pass

    class FileDropped(Message):
        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

    def __init__(self, **kw) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, tab_behavior="focus", **kw)

    async def _on_key(self, event) -> None:
        key = event.key
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))  # empty is fine when there are attachments
            return
        if key in ("shift+enter", "alt+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if key == "up" and not self.text:
            event.stop()
            event.prevent_default()
            self.post_message(self.EditLast())
            return
        await super()._on_key(event)

    def action_paste(self) -> None:
        """Ctrl+V: Pupila looks at the system clipboard (it may hold an image)."""
        self.post_message(self.Paste())

    async def _on_paste(self, event) -> None:
        path = _dropped_file(event.text)
        if path:
            event.stop()
            event.prevent_default()
            self.post_message(self.FileDropped(path))
            return
        await super()._on_paste(event)


def _dropped_file(text: str) -> Path | None:
    """Dragging a file onto the terminal pastes its path: if it's an existing file, it's sent."""
    t = text.strip().strip("'\"")
    if t.startswith("file://"):
        from urllib.parse import unquote
        t = unquote(t[7:])
    if "\n" in t or not t.startswith(("/", "~")):
        return None
    p = Path(t).expanduser()
    return p if p.is_file() else None


# --------------------------------------------------------------------------- dialogs


class Menu(ModalScreen[str | None]):
    """A list of options; returns the chosen id or None."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, title: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self.title_text, self.options = title, options

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="title")
            yield OptionList(*[Option(text, id=oid) for oid, text in self.options])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class Confirm(ModalScreen[bool]):
    BINDINGS = [("escape", "dismiss(False)", "No"), ("enter", "dismiss(True)", "Yes")]

    def __init__(self, question: str, yes: str = "Yes", no: str = "No") -> None:
        super().__init__()
        self.question, self.yes, self.no = question, yes, no

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.question, classes="title")
            with Horizontal(classes="buttons"):
                yield Button(self.yes, variant="primary", id="yes")
                yield Button(self.no, id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class ContextMenu(ModalScreen[str | None]):
    """The quick menu of a message, right next to the cursor (right click), like Discord's.

    Returns "react:<emoji>", "react-more" or the id of an option.
    """

    BINDINGS = [("escape", "dismiss(None)", "Close")]
    WIDTH = 30

    def __init__(self, x: int, y: int, reactions: list[str], options: list[tuple[str, str]]) -> None:
        super().__init__()
        self.at = (x, y)
        self.reactions, self.options = reactions, options

    def compose(self) -> ComposeResult:
        with Vertical(id="context"):
            with Horizontal(classes="quick"):
                for e in self.reactions:
                    yield Static(e, classes="quick-reaction")
                if media.has_graphics(self.app.cfg.image_style):  # type: ignore[attr-defined]
                    more = IMAGE_STYLES["auto"](media.add_reaction_icon(), classes="quick-reaction more")
                    more.styles.width, more.styles.height = 2, 1
                    yield more
                else:
                    yield Static("+", classes="quick-reaction more")
            yield OptionList(*[Option(text, id=oid) for oid, text in self.options])

    def on_mount(self) -> None:
        box = self.query_one("#context")
        height = len(self.options) + 3
        x = max(0, min(self.at[0] + 1, self.size.width - self.WIDTH - 1))
        y = max(0, min(self.at[1], self.size.height - height - 1))
        box.styles.offset = (x, y)
        self.query_one(OptionList).focus()

    def on_click(self, event) -> None:
        w = event.widget
        if w is not None and w.has_class("more"):
            self.dismiss("react-more")
        elif w is not None and w.has_class("quick-reaction"):
            self.dismiss("react:" + str(w.content))
        elif w is self:
            self.dismiss(None)  # a click outside the menu closes it

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class Settings(ModalScreen["config.Config | None"]):
    """Ctrl+S: the same settings as config.toml, with switches."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, cfg: "config.Config", spaces: list[str], account: str) -> None:
        super().__init__()
        self.cfg, self.spaces, self.account = cfg, spaces, account

    @staticmethod
    def _switch(key: str, label: str, value: bool) -> Horizontal:
        return Horizontal(Switch(value=value, id=key), Label(label), classes="setting")

    @staticmethod
    def _choice(key: str, label: str, options: list[tuple[str, str]], value: str) -> Horizontal:
        return Horizontal(Label(label, classes="label"),
                          Select(options, value=value, allow_blank=False, id=key), classes="setting")

    def compose(self) -> ComposeResult:
        c = self.cfg
        with Vertical(classes="dialog settings"):
            yield Static("Settings", classes="title")
            with VerticalScroll(classes="settings-body"):
                yield Static("Notifications", classes="section")
                yield self._switch("notify", "Desktop notifications", c.notify)
                yield self._switch("notify_text", "Show the message in them", c.notify_text)
                yield self._switch("bell", "Also ring the terminal bell", c.bell)
                yield Static("Chat", classes="section")
                yield self._switch("bubbles", "Messages in bubbles", c.bubbles)
                yield Static("Pictures", classes="section")
                yield self._switch("images", "Show pictures in the chat", c.images)
                yield self._switch("avatars", "Show profile pictures", c.avatars)
                yield self._switch("animate", "Animate GIFs", c.animate)
                yield self._choice("image_style", "Drawn", [
                    ("sharp when the terminal can", "auto"), ("with coloured blocks", "blocks"),
                    ("with characters", "text")], c.image_style)
                yield Horizontal(Label("Height in lines", classes="label"),
                                 Input(str(c.image_height), type="integer", id="image_height"),
                                 classes="setting")
                yield Static("Videos", classes="section")
                yield self._choice("video_player", "A click on a video", [
                    ("plays it in the chat", "chat"), ("plays it full screen here (mpv)", "terminal"),
                    ("opens it in a window", "window")], c.video_player)
                if self.spaces:
                    yield Static("Rooms", classes="section")
                    yield Static("Spaces sorted by name (the rest go by recent activity):", classes="hint")
                    for name in self.spaces:
                        yield Checkbox(name, value=name in c.sort_by_name, classes="sort-space")
                yield Static("Account", classes="section")
                yield Static(self.account, classes="hint")
                yield Button("Log out", variant="error", id="logout")
                yield Static(f"The look (colours, sizes) goes in {config.STYLE_FILE}, reloaded live.",
                             classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "logout":
            self.app.post_message(LogoutRequested())
            self.dismiss(None)
        elif event.button.id == "save":
            self.dismiss(self._collected())

    def _collected(self) -> "config.Config":
        def on(key: str) -> bool:
            return self.query_one(f"#{key}", Switch).value

        try:
            height = max(2, min(60, int(self.query_one("#image_height", Input).value)))
        except ValueError:
            height = self.cfg.image_height
        return config.Config(
            notify=on("notify"), notify_text=on("notify_text"), bell=on("bell"), bubbles=on("bubbles"),
            images=on("images"), avatars=on("avatars"), animate=on("animate"),
            image_style=str(self.query_one("#image_style", Select).value), image_height=height,
            video_player=str(self.query_one("#video_player", Select).value),
            sort_by_name=[str(cb.label) for cb in self.query(".sort-space").results(Checkbox) if cb.value],
            colors=dict(self.cfg.colors),
        )


class FilePicker(ModalScreen[Path | None]):
    """For systems without a file dialog (zenity): pick a file from a tree of folders."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, start: Path) -> None:
        super().__init__()
        self.start = start

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog files"):
            yield Static("Attach a file", classes="title")
            yield DirectoryTree(str(self.start))
            yield Static("Enter or double-click a file to attach it · Esc closes", classes="hint")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.dismiss(Path(event.path))


class LogoutRequested(Message):
    pass


class Login(ModalScreen[dict | None]):
    """First run: server, user and password."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog login"):
            yield Static("Pupila", classes="logo")
            yield Static("Log in to your Matrix server", classes="title")
            yield Input(placeholder="https://server:8448", id="server")
            yield Input(placeholder="user (or @user:server)", id="user")
            yield Input(placeholder="password", password=True, id="password")
            yield Button("Log in", variant="primary", id="login")
            yield Static("", id="error")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        following = {"server": "#user", "user": "#password"}.get(event.input.id or "")
        if following:
            self.query_one(following, Input).focus()
        else:
            self.attempt()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.attempt()

    @work(exclusive=True)
    async def attempt(self) -> None:
        import socket

        from .matrix import Matrix, MatrixError

        server = self.query_one("#server", Input).value.strip()
        user = self.query_one("#user", Input).value.strip()
        password = self.query_one("#password", Input).value
        error = self.query_one("#error", Static)
        if not (server and user and password):
            error.update("Something's missing.")
            return
        if not server.startswith("http"):
            server = "https://" + server
        error.update("Logging in…")
        mx = Matrix(server)
        try:
            await mx.login(user, password, f"Pupila ({socket.gethostname()})")
        except MatrixError as e:
            error.update(f"Couldn't log in: {e.error}")
            return
        except Exception as e:  # network, certificate, mistyped address
            error.update(f"Couldn't connect: {e}")
            return
        finally:
            await mx.close()
        self.dismiss({"homeserver": server, "token": mx.token, "user_id": mx.user_id,
                      "device_id": mx.device_id})
