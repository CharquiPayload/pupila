"""The application: connects the screen to the server."""
from __future__ import annotations

import asyncio
import io
import mimetypes
import shutil
import subprocess
import time
import uuid
from collections import OrderedDict
from pathlib import Path

import httpx
from PIL import Image as PILImage
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.actions import SkipAction
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.widgets import Button, Footer, Static

from . import config, media, render
from .matrix import Matrix, MatrixError, retry
from .model import Changes, Event, Store
from .emoji import EmojiPicker
from .widgets import (Composer, Confirm, ContextMenu, Login, LogoutRequested, MessageView, Settings, Sidebar,
                      Timeline)

BUILTIN_STYLE = Path(__file__).with_name("pupila.tcss")

THEME = Theme(
    name="pupila", primary="#9b87f5", secondary="#7c6fd6", accent="#c9b8ff",
    foreground="#e6e1f5", background="#16141d", surface="#1d1a26", panel="#2a2536",
    success="#a6da95", warning="#f0c674", error="#f38ba8", dark=True,
)


class RoomSearch(Provider):
    """Ctrl+K: find a room by name."""

    async def search(self, query: str) -> Hits:
        app: Pupila = self.app  # type: ignore[assignment]
        if not app.store:
            return
        matcher = self.matcher(query)
        for r in app.store.rooms.values():
            if r.is_space:
                continue
            name = app.store.room_name(r)
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), lambda rid=r.room_id: app.open_room(rid),
                          text=name, help=app.space_of(r.room_id))

    async def discover(self) -> Hits:
        app: Pupila = self.app  # type: ignore[assignment]
        if not app.store:
            return
        rooms = [r for r in app.store.rooms.values() if not r.is_space and not r.invited]
        rooms.sort(key=lambda r: (-(r.highlights * 1000 + r.unread), -r.last_ts))
        for r in rooms[:20]:
            name = app.store.room_name(r)
            label = f"{name}  ({r.unread})" if r.unread else name
            yield DiscoveryHit(label, lambda rid=r.room_id: app.open_room(rid), text=name,
                               help=app.space_of(r.room_id))


class Pupila(App):
    TITLE = "Pupila"
    COMMAND_PALETTE_BINDING = "ctrl+k"
    COMMANDS = {RoomSearch}
    BINDINGS = [
        Binding("ctrl+k", "command_palette", "Find room", show=False, priority=True),
        Binding("alt+up", "move(-1)", "Previous room", show=False, priority=True),
        Binding("alt+down", "move(1)", "Next room", show=False, priority=True),
        Binding("ctrl+r", "reply_last", "Reply", priority=True),
        Binding("ctrl+b", "sidebar", "Sidebar", priority=True),
        Binding("ctrl+s", "settings", "Settings", priority=True),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+c", "copy_selection", "Copy", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, cfg: config.Config) -> None:
        styles = [BUILTIN_STYLE] + ([config.STYLE_FILE] if config.STYLE_FILE.exists() else [])
        super().__init__(css_path=styles, watch_css=True)
        self.cfg = cfg
        self.mx: Matrix | None = None
        self.store: Store | None = None
        self.current: str | None = None
        self.focused_app = True
        self.connected = False
        self.images: dict[str, PILImage.Image | None] = {}
        self.animations: OrderedDict[str, list] = OrderedDict()  # url -> [(frame, seconds)]
        self.replying: Event | None = None
        self.editing: Event | None = None
        self._read: dict[str, str] = {}
        self._typing_until = 0.0
        self._sidebar_pending = False
        self.media_slots = asyncio.Semaphore(2)  # downloads and decodes at once: the rest wait

    # ------------------------------------------------------------------ screen

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield Sidebar(id="sidebar")
            with Vertical(id="center"):
                yield Static(id="header")
                yield Timeline(id="timeline")
                yield Static(id="typing")
                yield Static(id="action")
                with Horizontal(id="compose-row"):
                    yield Composer(id="composer")
                    yield Button("☺", id="emoji-button")
        yield Footer()

    async def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "pupila"
        await self.query_one(Timeline).show(None)
        self.paint_header()
        session = config.read_session()
        if session:
            self.start(session)
        else:
            self.push_screen(Login(), self._after_login)

    def _after_login(self, session: dict | None) -> None:
        if not session:
            self.exit()
            return
        config.save_session(session)
        self.start(session)

    def start(self, session: dict) -> None:
        self.mx = Matrix(session["homeserver"], session["token"], session["user_id"], session.get("device_id"))
        self.store = Store(session["user_id"])
        self.query_one(Composer).focus()
        self.run_worker(self.sync_loop(), group="sync", exclusive=True)

    def paint_header(self) -> None:
        header = self.query_one("#header", Static)
        status = Text(" ● connected" if self.connected else " ○ connecting…",
                      style="#a6da95" if self.connected else "#f0c674")
        if not (self.store and self.current in self.store.rooms):
            header.update(Text.assemble(("Pupila", "bold #b4a7f5"), "  ", status))
            return
        r = self.store.rooms[self.current]
        t = Text.assemble((self.store.room_name(r), "bold"))
        space = self.space_of(r.room_id)
        if space:
            t = Text.assemble((space + " › ", "dim"), t)
        if r.topic:
            topic = r.topic.split("\n")[0]
            t.append("  " + (topic[:90] + "…" if len(topic) > 90 else topic), style="dim")
        t.append_text(status)
        header.update(t)

    def paint_typing(self) -> None:
        w = self.query_one("#typing", Static)
        r = self.store.rooms.get(self.current) if self.store and self.current else None
        if not r or not r.typing:
            w.update("")
            return
        names = [self.store.user_name(u, r) for u in sorted(r.typing)]
        who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        w.update(Text(f"{who} {'is' if len(names) == 1 else 'are'} typing…", style="italic #b4a7f5"))

    def paint_action(self) -> None:
        w = self.query_one("#action", Static)
        if self.editing:
            w.update(Text.assemble(("✎ Editing your message", "bold #f0c674"), ("   Esc cancels", "dim")))
        elif self.replying:
            r = self.store.rooms[self.current]
            who = self.store.user_name(self.replying.sender, r)
            summary = self.replying.text.split("\n")[0][:70]
            w.update(Text.assemble(("↪ Replying to ", "#b4a7f5"), (who, "bold"),
                                   (": " + summary, "dim"), ("   Esc cancels", "dim")))
        else:
            w.update("")
        w.set_class(bool(self.editing or self.replying), "-visible")

    def space_ancestors(self, rid: str | None) -> set[str]:
        """The spaces that contain the room, all the way up (to expand them in the sidebar)."""
        out: set[str] = set()
        pending = [rid] if rid else []
        while pending and self.store:
            current = pending.pop()
            for r in self.store.rooms.values():
                if r.is_space and current in r.children and r.room_id not in out:
                    out.add(r.room_id)
                    pending.append(r.room_id)
        return out

    def space_of(self, rid: str) -> str:
        if not self.store:
            return ""
        for r in self.store.rooms.values():
            if r.is_space and rid in r.children:
                return self.store.room_name(r)
        return ""

    def request_sidebar(self) -> None:
        """Rebuilds the sidebar at most every half second."""
        if self._sidebar_pending:
            return
        self._sidebar_pending = True

        def do() -> None:
            self._sidebar_pending = False
            self.query_one(Sidebar).rebuild(self)

        self.set_timer(0.5, do)

    # ------------------------------------------------------------------ sync

    async def sync_loop(self) -> None:
        since: str | None = None
        wait = 1.0
        while True:
            try:
                d = await self.mx.sync(since)
            except MatrixError as e:
                if e.errcode in ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN"):
                    config.delete_session()
                    self.notify("The session is no longer valid: please log in again.", severity="error")
                    self.push_screen(Login(), self._after_login)
                    return
                self._disconnected(f"the server answered {e.status}")
                await asyncio.sleep(wait)
                wait = min(wait * 2, 30)
                continue
            except (httpx.TransportError, httpx.TimeoutException) as e:
                self._disconnected(type(e).__name__)
                await asyncio.sleep(wait)
                wait = min(wait * 2, 30)
                continue
            initial = since is None
            changes = self.store.apply_sync(d, initial)
            since = d.get("next_batch", since)
            wait = 1.0
            if not self.connected:
                self.connected = True
                self.paint_header()
            await self.apply(changes, initial)

    def _disconnected(self, reason: str) -> None:
        if self.connected:
            self.connected = False
            self.paint_header()
            self.log(f"sync: {reason}")

    async def apply(self, c: Changes, initial: bool) -> None:
        if initial:
            self.query_one(Sidebar).rebuild(self)
            last = config.LAST_ROOM_FILE.read_text().strip() if config.LAST_ROOM_FILE.exists() else ""
            if last not in self.store.rooms:
                order = self.query_one(Sidebar).visual_order
                last = order[0] if order else ""
            if last:
                self.open_room(last)
            return
        if c.structure or c.rooms:
            self.request_sidebar()
        if self.current in c.rooms:
            timeline = self.query_one(Timeline)
            if self.current in c.limited:
                await timeline.show(self.store.rooms[self.current])
            else:
                await timeline.sync()
                for eid in c.updated:
                    await timeline.update_event(eid)
            self.paint_typing()
            self.mark_read()
        for rid, ev in c.new[:3]:
            self.notify_message(rid, ev)

    # ------------------------------------------------------------------ rooms

    def open_room(self, rid: str) -> None:
        self.run_worker(self._open_room(rid), group="open-room", exclusive=True)

    async def _open_room(self, rid: str) -> None:
        r = self.store.rooms.get(rid) if self.store else None
        if not r:
            return
        if r.invited:
            who = self.store.user_name(r.invited_by) if r.invited_by else "Someone"
            if await self.push_screen_wait(Confirm(
                    f"{who} invited you to “{self.store.room_name(r)}”. Join?", "Join", "Not now")):
                try:
                    await self.mx.join(rid)
                except MatrixError as e:
                    self.notify(f"Couldn't join: {e.error}", severity="error")
            return
        if r.is_space:
            return
        self.current = rid
        self.replying = self.editing = None
        self.paint_action()
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.LAST_ROOM_FILE.write_text(rid)
        await self.query_one(Timeline).show(r)
        self.paint_header()
        self.paint_typing()
        self.mark_read()
        self.request_sidebar()
        self.query_one(Composer).focus()

    @on(Sidebar.NodeSelected)
    def _node_selected(self, event: Sidebar.NodeSelected) -> None:
        rid = event.node.data
        if rid and not rid.startswith("#") and not event.node.allow_expand:
            self.open_room(rid)

    def action_move(self, step: int) -> None:
        order = self.query_one(Sidebar).visual_order
        if not order:
            return
        i = order.index(self.current) if self.current in order else -1
        self.open_room(order[(i + step) % len(order)])

    def action_copy_selection(self) -> None:
        """Ctrl+C copies text selected with the mouse in the chat; otherwise the composer copies its own."""
        text = self.screen.get_selected_text()
        if not text:
            raise SkipAction()
        self.copy_to_clipboard(text)
        self.screen.clear_selection()
        self.notify("Copied.", timeout=2)

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)  # the terminal's way (OSC 52)
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
            if shutil.which(cmd[0]):
                self.run_worker(self._run(*cmd, stdin=text.encode()), group="clipboard")
                break

    def action_sidebar(self) -> None:
        self.query_one(Sidebar).toggle_class("-hidden")

    # ------------------------------------------------------------------ settings

    def action_settings(self) -> None:
        spaces = sorted({self.store.room_name(r) for r in self.store.rooms.values() if r.is_space}) \
            if self.store else []
        account = f"Logged in as {self.mx.user_id} on {self.mx.homeserver}" if self.mx else "Not logged in"
        self.push_screen(Settings(self.cfg, spaces, account), self._apply_settings)

    def _apply_settings(self, cfg: config.Config | None) -> None:
        if cfg is None:
            return
        config.save(cfg)
        self.cfg = cfg
        self.notify("Settings saved.", timeout=2)
        self.query_one(Sidebar).rebuild(self)
        if self.store and self.current in self.store.rooms:
            self.open_room(self.current)  # draws the room again with the new settings

    @on(LogoutRequested)
    @work(exclusive=True, group="logout")
    async def _logout(self) -> None:
        if not await self.push_screen_wait(Confirm(
                "Log out of this device? You'll need your password to come back.", "Log out", "Cancel")):
            return
        try:
            await self.mx.logout()
        except (MatrixError, httpx.HTTPError):
            pass  # the local session goes anyway
        config.delete_session()
        self.exit(message="Logged out.")

    @on(Timeline.LoadHistory)
    async def _load_history(self) -> None:
        timeline = self.query_one(Timeline)
        r = timeline.room
        if not r or not r.prev_batch or timeline.fetching:
            return
        timeline.fetching = True
        try:
            d = await self.mx.messages(r.room_id, r.prev_batch)
            self.store.apply_history(r.room_id, d)
            if timeline.room is r:
                height_before = timeline.virtual_size.height
                await timeline.show(r, at_end=False)
                self.call_after_refresh(lambda: timeline.scroll_to(
                    y=max(0, timeline.virtual_size.height - height_before), animate=False))
        except (MatrixError, httpx.HTTPError) as e:
            self.notify(f"Couldn't fetch older messages: {e}", severity="warning")
        finally:
            timeline.fetching = False

    def mark_read(self) -> None:
        if not (self.current and self.focused_app and self.store):
            return
        r = self.store.rooms.get(self.current)
        last = next((e.event_id for e in reversed(r.events) if not e.event_id.startswith("~")), None) if r else None
        if not last or self._read.get(self.current) == last:
            return
        self._read[self.current] = last
        r.unread = r.highlights = 0
        self.request_sidebar()
        self.run_worker(self._send_read(self.current, last), group="read")

    async def _send_read(self, rid: str, eid: str) -> None:
        try:
            await self.mx.read_marker(rid, eid)
        except (MatrixError, httpx.HTTPError):
            pass

    def on_app_focus(self) -> None:
        self.focused_app = True
        self.mark_read()

    def on_app_blur(self) -> None:
        self.focused_app = False

    # ------------------------------------------------------------------ notifications

    def notify_message(self, rid: str, ev: Event) -> None:
        if not self.cfg.notify:
            return
        r = self.store.rooms.get(rid)
        if not r or (rid == self.current and self.focused_app):
            return
        if not r.unread and not self.store.mentions_me(ev):
            return  # a room muted on the server
        who = self.store.user_name(ev.sender, r)
        room = self.store.room_name(r)
        title = who if room == who else f"{who} · {room}"
        if not self.cfg.notify_text:
            text = "New message"
        elif ev.msgtype in render.ICONS:
            text = render.ICONS[ev.msgtype] + " " + (ev.content.get("body") or "file")
        else:
            text = ev.text
        if shutil.which("notify-send"):
            self.run_worker(self._run("notify-send", "-a", "Pupila", "-i", "mail-message-new",
                                      title, text[:300]), group="notifications")
        if self.cfg.bell:
            self.bell()

    async def _run(self, *cmd: str, stdin: bytes | None = None) -> tuple[int, bytes]:
        try:
            p = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(stdin), 20)
            return p.returncode or 0, out
        except (OSError, asyncio.TimeoutError):
            return 1, b""

    # ------------------------------------------------------------------ writing and sending

    @on(Composer.Changed)
    def _composer_changed(self, event: Composer.Changed) -> None:
        if not (self.current and self.mx):
            return
        now = time.monotonic()
        if event.text_area.text and now > self._typing_until:
            self._typing_until = now + 4
            self.run_worker(self._typing(self.current, True), group="typing")
        elif not event.text_area.text and self._typing_until:
            self._typing_until = 0
            self.run_worker(self._typing(self.current, False), group="typing")

    async def _typing(self, rid: str, typing: bool) -> None:
        try:
            await self.mx.typing(rid, typing)
        except (MatrixError, httpx.HTTPError):
            pass

    @on(Composer.Submitted)
    async def _submitted(self, event: Composer.Submitted) -> None:
        if not self.current:
            self.notify("Open a room first.", severity="warning")
            return
        composer = self.query_one(Composer)
        text = event.text.rstrip()
        composer.clear()
        if text.startswith("/") and not text.startswith("//"):
            await self.command(text)
            return
        if text.startswith("//"):
            text = text[1:]
        await self.send_text(self.current, text)

    async def send_text(self, rid: str, text: str, msgtype: str = "m.text") -> None:
        content: dict = {"msgtype": msgtype, "body": text}
        html = render.to_html(text)
        if html:
            content.update(format="org.matrix.custom.html", formatted_body=html)
        if self.editing:
            ev, self.editing = self.editing, None
            self.paint_action()
            try:
                await retry(self.mx.edit, rid, ev.event_id, content)
            except (MatrixError, httpx.HTTPError) as e:
                self.notify(f"Couldn't edit: {e}", severity="error")
            return
        if self.replying:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": self.replying.event_id}}
            self.replying = None
            self.paint_action()
        await self.send_content(rid, content)

    async def send_content(self, rid: str, content: dict) -> None:
        txn = uuid.uuid4().hex
        echo = self.store.echo(rid, "~" + txn, content, int(time.time() * 1000))
        timeline = self.query_one(Timeline)
        if rid == self.current:
            await timeline.sync()
            timeline.anchor()  # what you send is always in view
        self._typing_until = 0
        self.run_worker(self._typing(rid, False), group="typing")
        try:
            real = await retry(self.mx.send, rid, content, txn=txn)
        except (MatrixError, httpx.HTTPError) as e:
            echo.failed = True
            await timeline.update_event(echo.event_id)
            self.notify(f"Couldn't send: {e}", severity="error")
            return
        e = self.store.rename(rid, "~" + txn, real)
        if rid == self.current:
            if e is not echo:  # the sync brought it first: the echo is left over
                await timeline.remove_event("~" + txn)
            timeline.reindex()
            await timeline.update_event(real)
        self.mark_read()

    async def command(self, text: str) -> None:
        name, _, rest = text[1:].partition(" ")
        rest = rest.strip()
        if name == "me" and rest:
            await self.send_text(self.current, rest, "m.emote")
        elif name == "upload" and rest:
            await self.upload(Path(rest).expanduser())
        elif name == "quit":
            self.exit()
        elif name == "settings":
            self.action_settings()
        elif name == "help":
            self.notify("/me action · /upload path · /settings · //text sends something starting with / · "
                        "Ctrl+V pastes images · ↑ edits your last message · click a message: "
                        "reply, react, delete…", timeout=12)
        else:
            self.notify(f"Unknown command /{name}. Try /help (or // to send it as text).", severity="warning")

    def action_cancel(self) -> None:
        if self.editing or self.replying:
            if self.editing:
                self.query_one(Composer).clear()
            self.editing = self.replying = None
            self.paint_action()
        self.query_one(Composer).focus()

    @on(Button.Pressed, "#emoji-button")
    @work(exclusive=True, group="emoji")
    async def _emoji_button(self) -> None:
        button = self.query_one("#emoji-button")
        region = button.region
        emoji = await self.push_screen_wait(
            EmojiPicker((region.right, region.y), config.frequent_emojis(), above=True))
        composer = self.query_one(Composer)
        if emoji:
            composer.insert(emoji)
            config.count_emoji(emoji)
        composer.focus()

    @on(Composer.EditLast)
    def _edit_last(self) -> None:
        r = self.store.rooms.get(self.current) if self.store and self.current else None
        if not r:
            return
        mine = next((e for e in reversed(r.events) if e.sender == self.store.me and not e.redacted
                     and not e.pending and e.msgtype in ("m.text", "m.emote", "m.notice")), None)
        if mine:
            self.edit(mine)

    def edit(self, ev: Event) -> None:
        self.editing, self.replying = ev, None
        composer = self.query_one(Composer)
        composer.load_text(ev.text)
        composer.move_cursor(composer.document.end)
        composer.focus()
        self.paint_action()

    def action_reply_last(self) -> None:
        r = self.store.rooms.get(self.current) if self.store and self.current else None
        if not r:
            return
        ev = next((e for e in reversed(r.events) if e.sender != self.store.me and not e.redacted), None)
        if ev:
            self.reply(ev)

    def reply(self, ev: Event) -> None:
        self.replying, self.editing = ev, None
        self.paint_action()
        self.query_one(Composer).focus()

    # ------------------------------------------------------------------ clicking a message

    @on(MessageView.ContextRequested)
    @work(exclusive=True, group="menu")
    async def _message_menu(self, event: MessageView.ContextRequested) -> None:
        ev = event.view.ev
        if ev.event_id.startswith("~"):
            return
        options = []
        if not ev.redacted:
            options.append(("reply", "Reply"))
        if ev.msgtype in render.ICONS and ev.content.get("url"):
            options.append(("open", "Open in a player" if ev.msgtype == "m.video" else "Open"))
            options.append(("save", "Save to Downloads"))
        elif not ev.redacted:
            options.append(("copy", "Copy text"))
        if ev.sender == self.store.me and not ev.redacted:
            if ev.msgtype in ("m.text", "m.emote"):
                options.append(("edit", "Edit"))
            options.append(("delete", "Delete"))
        reactions = config.top_reactions() if not ev.redacted else []
        choice = await self.push_screen_wait(ContextMenu(event.x, event.y, reactions, options))
        if not choice:
            return
        if choice.startswith("react:"):
            await self.react(ev, choice[6:])
        elif choice == "react-more":
            key = await self.push_screen_wait(EmojiPicker((event.x, event.y), config.frequent_emojis()))
            if key:
                await self.react(ev, key)
        elif choice == "open":
            await self.open_file(ev, force_window=True)
        elif choice == "save":
            await self.save_file(ev)
        elif choice == "reply":
            self.reply(ev)
        elif choice == "copy":
            await self.copy(ev.text)
        elif choice == "edit":
            self.edit(ev)
        elif choice == "delete":
            if await self.push_screen_wait(Confirm("Delete this message? This can't be undone.",
                                                   "Delete", "Cancel")):
                try:
                    await self.mx.redact(self.current, ev.event_id)
                except (MatrixError, httpx.HTTPError) as e:
                    self.notify(f"Couldn't delete: {e}", severity="error")

    async def react(self, ev: Event, key: str) -> None:
        mine = ev.reactions.get(key, {}).get(self.store.me)
        try:
            if mine:
                await self.mx.redact(self.current, mine)  # picking the same reaction removes it
            else:
                await self.mx.react(self.current, ev.event_id, key)
                config.count_emoji(key)
        except (MatrixError, httpx.HTTPError) as e:
            self.notify(f"Couldn't react: {e}", severity="error")

    async def copy(self, text: str) -> None:
        self.copy_to_clipboard(text)
        self.notify("Copied.", timeout=2)

    # ------------------------------------------------------------------ files

    async def image(self, mxc: str) -> PILImage.Image | None:
        if mxc in self.images:
            return self.images[mxc]
        cache = config.media_dir() / "thumbnails" / mxc.removeprefix("mxc://").replace("/", "_")
        try:
            if cache.exists():
                data = cache.read_bytes()
            else:
                data = await self.mx.thumbnail(mxc)
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(data)
            img = PILImage.open(io.BytesIO(data))
            img.load()
        except Exception as e:  # broken image, odd format, network
            self.log(f"image {mxc}: {e}")
            img = None
        self.images[mxc] = img
        return img

    async def avatar(self, mxc: str) -> PILImage.Image | None:
        """A profile picture, small and square."""
        key = "avatar:" + mxc
        if key in self.images:
            return self.images[key]
        cache = config.media_dir() / "avatars" / mxc.removeprefix("mxc://").replace("/", "_")
        try:
            if cache.exists():
                data = cache.read_bytes()
            else:
                data = await self.mx.thumbnail(mxc, 96, 96, "crop")
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(data)
            img = PILImage.open(io.BytesIO(data))
            img.load()
            img = img.convert("RGBA")
        except Exception as e:  # broken image, network
            self.log(f"avatar {mxc}: {e}")
            img = None
        self.images[key] = img
        return img

    async def download(self, ev: Event) -> Path | None:
        """The whole file, kept in the cache (downloaded only once)."""
        mxc = ev.content.get("url", "")
        name = Path(ev.content.get("filename") or ev.content.get("body") or "file").name
        dest = config.media_dir() / "files" / f"{mxc.rsplit('/', 1)[-1]}_{name}"
        if not dest.exists():
            try:
                data = await self.mx.download(mxc)
            except (MatrixError, httpx.HTTPError) as e:
                self.log(f"download {mxc}: {e}")
                return None
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return dest

    async def animation(self, ev: Event) -> list | None:
        """The frames of a GIF (or of a Discord/WhatsApp video-GIF) to animate it in the chat."""
        url = ev.content.get("url", "")
        if url in self.animations:
            self.animations.move_to_end(url)
            return self.animations[url]
        if (media.info(ev).get("size") or 0) > 20 * 1024 * 1024:
            return None
        async with self.media_slots:
            if url in self.animations:  # another message with the same GIF got it meanwhile
                return self.animations[url]
            path = await self.download(ev)
            if not path:
                return None
            try:
                if ev.msgtype == "m.video":
                    frames = await media.video_frames(path)
                else:
                    frames = await asyncio.to_thread(media.gif_frames, path.read_bytes())
            except Exception as e:  # broken GIF, odd video
                self.log(f"animation {url}: {e}")
                frames = []
        if not frames:
            return None
        self.animations[url] = frames
        while len(self.animations) > 16:
            self.animations.popitem(last=False)
        return frames

    async def video_thumbnail(self, ev: Event) -> PILImage.Image | None:
        key = "video:" + ev.content.get("url", "")
        if key in self.images:
            return self.images[key]
        img = None
        thumb = media.info(ev).get("thumbnail_url")
        if thumb:
            img = await self.image(thumb)
        elif (media.info(ev).get("size") or 0) <= 25 * 1024 * 1024:
            async with self.media_slots:
                path = await self.download(ev)
                if path:
                    img = await media.first_frame(path.read_bytes())
        self.images[key] = img
        return img

    async def open_file(self, ev: Event, force_window: bool = False) -> None:
        name = ev.content.get("filename") or ev.content.get("body") or "file"
        self.notify(f"Opening {name}…", timeout=2)
        path = await self.download(ev)
        if not path:
            self.notify("Couldn't download it.", severity="error")
            return
        is_video = ev.msgtype == "m.video" or media.is_animated(ev)
        mpv = shutil.which("mpv")
        if is_video and mpv:
            loop = ["--loop-file=inf"] if media.is_animated(ev) else []
            if self.cfg.video_player == "terminal" and not force_window:
                with self.suspend():  # mpv draws in this same terminal; q comes back to Pupila
                    subprocess.run([mpv, *media.mpv_output(self.cfg.image_style), "--really-quiet",
                                    *loop, str(path)])
                return
            await asyncio.create_subprocess_exec(
                mpv, "--force-window=immediate", "--really-quiet", *loop, str(path),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        elif shutil.which("xdg-open"):
            p = await asyncio.create_subprocess_exec("xdg-open", str(path), stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            try:  # if it failed within a few seconds, no program handles that kind of file
                failed = await asyncio.wait_for(p.wait(), 4) != 0
            except asyncio.TimeoutError:
                failed = False
            if failed:
                advice = ("Install mpv (sudo pacman -S mpv) and Pupila will use it for videos."
                          if is_video else "Your system has no program for this kind of file.")
                self.notify(f"Nothing can open {name}. {advice}\nIt was saved to {path}",
                            severity="warning", timeout=12)
        else:
            self.notify(f"Saved to {path}")

    async def save_file(self, ev: Event) -> None:
        """Copies a file out of the temporary folder into Downloads, without overwriting anything."""
        path = await self.download(ev)
        if not path:
            self.notify("Couldn't download it.", severity="error")
            return
        folder = config.downloads_dir()
        folder.mkdir(parents=True, exist_ok=True)
        name = Path(ev.content.get("filename") or ev.content.get("body") or path.name).name
        dest = folder / name
        n = 1
        while dest.exists():
            dest = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        await asyncio.to_thread(shutil.copyfile, path, dest)
        self.notify(f"Saved to {dest}", timeout=5)

    @on(MessageView.Open)
    @work(group="open-file")
    async def _open_media(self, event: MessageView.Open) -> None:
        await self.open_file(event.view.ev)

    async def upload(self, path: Path | None = None, data: bytes | None = None,
                     content_type: str | None = None, name: str | None = None) -> None:
        rid = self.current
        if not rid:
            return
        if path is not None:
            if not path.is_file():
                self.notify(f"{path} doesn't exist", severity="error")
                return
            data, name = path.read_bytes(), path.name
        assert data is not None and name
        content_type = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        info: dict = {"mimetype": content_type, "size": len(data)}
        msgtype = "m.file"
        if content_type.startswith("image/"):
            msgtype = "m.image"
            try:
                with PILImage.open(io.BytesIO(data)) as im:
                    info["w"], info["h"] = im.size
            except Exception:
                pass
        elif content_type.startswith("video/"):
            msgtype = "m.video"
        elif content_type.startswith("audio/"):
            msgtype = "m.audio"
        self.notify(f"Uploading {name} ({render.human_size(len(data))})…", timeout=3)
        try:
            mxc = await self.mx.upload(data, content_type, name)
        except (MatrixError, httpx.HTTPError) as e:
            self.notify(f"Couldn't upload: {e}", severity="error")
            return
        content = {"msgtype": msgtype, "body": name, "filename": name, "url": mxc, "info": info}
        if msgtype == "m.image":
            try:
                img = PILImage.open(io.BytesIO(data))
                img.load()
                self.images[mxc] = img
            except Exception:
                pass
        await self.send_content(rid, content)

    @on(Composer.Paste)
    @work(exclusive=True, group="paste")
    async def _paste(self) -> None:
        composer = self.query_one(Composer)
        if shutil.which("wl-paste"):
            _, types = await self._run("wl-paste", "--list-types")
            image = next((t for t in types.decode(errors="replace").split() if t.startswith("image/")), None)
            if image:
                _, data = await self._run("wl-paste", "--type", image)
                if data and await self.push_screen_wait(Confirm(
                        f"Send the image on the clipboard ({render.human_size(len(data))})?", "Send", "No")):
                    ext = mimetypes.guess_extension(image) or ".png"
                    await self.upload(data=data, content_type=image, name=f"image{ext}")
                return
            _, text = await self._run("wl-paste", "--no-newline")
        elif shutil.which("xclip"):
            _, text = await self._run("xclip", "-selection", "clipboard", "-o")
        else:
            self.notify("Pasting needs wl-clipboard (Wayland) or xclip.", severity="warning")
            return
        if text:
            composer.insert(text.decode(errors="replace"))

    @on(Composer.FileDropped)
    @work(exclusive=True, group="paste")
    async def _file_dropped(self, event: Composer.FileDropped) -> None:
        p = event.path
        if await self.push_screen_wait(Confirm(f"Send {p.name} ({render.human_size(p.stat().st_size)})?",
                                               "Send", "No")):
            await self.upload(p)
