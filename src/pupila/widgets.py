"""The pieces of the screen: room sidebar, message timeline, composer and dialogs."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static, TextArea, Tree
from textual.widgets.option_list import Option
from textual_image.widget import HalfcellImage, UnicodeImage
from textual_image.widget import Image as TerminalImage

from . import media, render
from .model import Event, Room

if TYPE_CHECKING:
    from .app import Pupila

MEDIA_TYPES = {"m.image", "m.sticker", "m.video"}
IMAGE_STYLES = {"auto": TerminalImage, "blocks": HalfcellImage, "text": UnicodeImage}


# --------------------------------------------------------------------------- messages


class MessageView(Vertical):
    """One chat message: header (if it starts a group), quote, body, picture and reactions."""

    class Clicked(Message):
        def __init__(self, view: "MessageView") -> None:
            super().__init__()
            self.view = view

    class Open(Message):
        def __init__(self, view: "MessageView") -> None:
            super().__init__()
            self.view = view

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
        p = self.pupila
        store, colors = p.store, p.cfg.colors
        if self.group:
            yield Static(render.header(self.ev, store, self.room, colors), classes="header")
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
            yield Static(render.body(self.ev, store, self.room), classes="body" + (
                " notice" if self.ev.msgtype == "m.notice" else ""))
        s = render.suffix(self.ev)
        if s:
            yield Static(s, classes="suffix")
        r = render.reactions(self.ev, store.me)
        if r:
            yield Static(r, classes="reactions")

    def _picture(self, img):
        cls = IMAGE_STYLES.get(self.pupila.cfg.image_style, TerminalImage)
        w = cls(img, classes="image media")
        w.styles.height = self.pupila.cfg.image_height
        w.styles.width = "auto"
        return w

    def on_mount(self) -> None:
        if not self._has_media():
            return
        url = self.ev.content["url"]
        if self._animated() and url in self.pupila.animations:
            self._start_animation(self.pupila.animations[url])
        elif (self._animated() and url not in self.pupila.animations) or \
                self._still_key() not in self.pupila.images:
            self.load_media()

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
        if self.is_on_screen:
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
        on_media = event.widget is not None and event.widget.has_class("media")
        if on_media and media.is_video(self.ev) and self.pupila.cfg.video_player == "chat" \
                and media.can_play_inline():
            self.toggle_video()
        elif on_media:
            self.post_message(self.Open(self))  # a click on the picture or video opens it directly
        else:
            self.post_message(self.Clicked(self))


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
        if m:
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
            if self.text.strip():
                self.post_message(self.Submitted(self.text))
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


QUICK_REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏", "🌸", "✅", "🔥", "👀"]


class ReactionPicker(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("React", classes="title")
            with Horizontal(classes="emojis"):
                for i, e in enumerate(QUICK_REACTIONS):
                    yield Button(e, id=f"e{i}", classes="emoji")
            yield Input(placeholder="or another emoji / text, then Enter")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(str(event.button.label))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


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
