"""The emoji picker: a search box, categories and your most used ones, like Discord's.

The emoji list comes from Unicode's emoji-test.txt (emoji_data.json): fully-qualified emojis
up to Emoji 15.1, without skin tones or ZWJ sequences, which most terminals and emoji fonts
can't draw as one glyph.
"""
from __future__ import annotations

import json
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static

GROUPS: list[dict] = json.loads(Path(__file__).with_name("emoji_data.json").read_text(encoding="utf-8"))
NAMES: dict[str, str] = {e: name for g in GROUPS for e, name in g["emojis"]}
ICONS = {
    "Smileys & Emotion": "😀", "People & Body": "👋", "Animals & Nature": "🐻", "Food & Drink": "🍔",
    "Travel & Places": "🚗", "Activities": "⚽", "Objects": "💡", "Symbols": "🔣", "Flags": "🏁",
}
COLUMNS = 12
CELL = 3  # screen columns per emoji: 2 for the emoji, 1 of space


class EmojiGrid(Static):
    """A block of emojis drawn as plain text (one widget per category, not one per emoji)."""

    class Picked(Message):
        def __init__(self, emoji: str) -> None:
            super().__init__()
            self.emoji = emoji

    class Hovered(Message):
        def __init__(self, emoji: str) -> None:
            super().__init__()
            self.emoji = emoji

    def __init__(self, emojis: list[str], **kw) -> None:
        super().__init__(**kw)
        self.emojis: list[str] = []
        self.show(emojis)

    def show(self, emojis: list[str]) -> None:
        self.emojis = emojis
        t = Text()
        for i, e in enumerate(emojis):
            if i and i % COLUMNS == 0:
                t.append("\n")
            t.append(e + " ")
        self.update(t)
        self.display = bool(emojis)

    def _at(self, x: int, y: int) -> str | None:
        col = x // CELL
        i = y * COLUMNS + col
        return self.emojis[i] if 0 <= col < COLUMNS and 0 <= i < len(self.emojis) else None

    def on_click(self, event) -> None:
        e = self._at(event.x, event.y)
        if e:
            event.stop()
            self.post_message(self.Picked(e))

    def on_mouse_move(self, event) -> None:
        e = self._at(event.x, event.y)
        if e:
            self.post_message(self.Hovered(e))


def search(query: str, limit: int = 120) -> list[str]:
    words = query.lower().split()
    return [e for e, name in NAMES.items() if all(w in name for w in words)][:limit]


class EmojiPicker(ModalScreen[str | None]):
    """Returns the chosen emoji (or any text typed and confirmed with Enter), or None."""

    BINDINGS = [("escape", "dismiss(None)", "Close")]
    WIDTH = 50
    HEIGHT = 22

    def __init__(self, anchor: tuple[int, int], frequent: list[str], above: bool = False) -> None:
        super().__init__()
        self.anchor, self.frequent, self.above = anchor, frequent, above

    def compose(self) -> ComposeResult:
        with Vertical(id="emoji-picker"):
            yield Input(placeholder="Search: heart, fire, smile…", id="emoji-search")
            with Horizontal(id="emoji-main"):
                with Vertical(id="emoji-cats"):
                    if self.frequent:
                        yield Static("🕘", classes="cat", name="cat-frequent")
                    for i, g in enumerate(GROUPS):
                        yield Static(ICONS.get(g["group"], "•"), classes="cat", name=f"cat-{i}")
                with VerticalScroll(id="emoji-scroll"):
                    yield EmojiGrid([], id="emoji-results")
                    yield Static("No emoji by that name. Enter sends what you typed.", id="emoji-none")
                    if self.frequent:
                        yield Static("Frequently used", classes="cat-title", id="cat-frequent")
                        yield EmojiGrid(self.frequent, classes="cat-grid")
                    for i, g in enumerate(GROUPS):
                        yield Static(g["group"], classes="cat-title", id=f"cat-{i}")
                        yield EmojiGrid([e for e, _ in g["emojis"]], classes="cat-grid")
            yield Static("", id="emoji-preview")

    def on_mount(self) -> None:
        box = self.query_one("#emoji-picker")
        x, y = self.anchor
        if self.above:  # from the composer's button: open upwards, ending at the button
            x, y = x - self.WIDTH + 4, y - self.HEIGHT
        x = max(0, min(x, self.size.width - self.WIDTH - 1))
        y = max(0, min(y, self.size.height - self.HEIGHT - 1))
        box.styles.offset = (x, y)
        self.query_one("#emoji-none").display = False
        self.query_one("#emoji-search", Input).focus()

    def _searching(self, on: bool) -> None:
        for w in self.query(".cat-title, .cat-grid"):
            w.display = not on

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        results = self.query_one("#emoji-results", EmojiGrid)
        if not query:
            results.show([])
            self.query_one("#emoji-none").display = False
            self._searching(False)
            return
        found = search(query)
        if not query.isascii() and query not in found:
            found.insert(0, query)  # an emoji typed or pasted straight in
        results.show(found)
        self.query_one("#emoji-none").display = not found
        self._searching(True)
        self.query_one("#emoji-scroll").scroll_home(animate=False)
        if found:
            self._preview(found[0])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        results = self.query_one("#emoji-results", EmojiGrid)
        if results.emojis:
            self.dismiss(results.emojis[0])
        elif event.value.strip():
            self.dismiss(event.value.strip())

    def on_emoji_grid_picked(self, event: EmojiGrid.Picked) -> None:
        self.dismiss(event.emoji)

    def on_emoji_grid_hovered(self, event: EmojiGrid.Hovered) -> None:
        self._preview(event.emoji)

    def _preview(self, emoji: str) -> None:
        self.query_one("#emoji-preview", Static).update(
            Text.assemble((emoji + "  ", ""), (NAMES.get(emoji, ""), "dim")))

    def on_click(self, event) -> None:
        w = event.widget
        if w is not None and w.has_class("cat"):
            self.query_one("#emoji-search", Input).value = ""
            target = self.query_one(f"#{w.name}")
            self.query_one("#emoji-scroll").scroll_to_widget(target, top=True, animate=False)
        elif w is self:
            self.dismiss(None)  # a click outside closes it
