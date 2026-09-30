# Pupila

A [Matrix](https://matrix.org) client for the terminal, **without Vim modes**: you just type,
`Enter` sends and the mouse works. Built with [Textual](https://textual.textualize.io).

Made for a self-hosted server without encryption (encrypted rooms aren't supported).

## Install

With [uv](https://docs.astral.sh/uv/) (on Arch/CachyOS: `sudo pacman -S uv`):

```
uv tool install git+https://github.com/CharquiPayload/pupila
pupila
```

To update: `uv tool upgrade pupila`.

On first run it asks for your server address, user and password. The session is kept in
`~/.local/state/pupila/session.json`, readable only by your user. `pupila --logout` ends it.

Optional, but recommended:

- `wl-clipboard` (Wayland) or `xclip` (X11), to paste images with `Ctrl+V`.
- `ffmpeg`, to animate GIFs from Discord and WhatsApp and to play videos inside the chat
  (`ffplay`, which comes with it, or `mpv` plays the sound).
- `mpv`, to open videos in their own window or full screen in the terminal.

Images are sharp in terminals with graphics support (kitty, foot, WezTerm, Ghostty) and drawn
with coloured blocks in the others (Alacritty). If you like the pixel look, you can force it
with `style = "blocks"` in the `[images]` section of `config.toml`.

## Keys

| Key | What it does |
|---|---|
| `Enter` | send |
| `Alt+Enter` | new line (`Shift+Enter` if your terminal tells them apart) |
| `Ctrl+K` | find a room by name |
| `Alt+↑` / `Alt+↓` | previous / next room |
| `↑` (with the composer empty) | edit your last message |
| `Ctrl+R` | reply to the last message from someone else |
| `Ctrl+V` | paste (an image on the clipboard goes to the tray above the composer) |
| `Esc` | cancel a reply or an edit |
| `Ctrl+B` | show or hide the room sidebar |
| `Ctrl+S` | settings |
| `Ctrl+Q` | quit |

**Right-click a message** for its quick menu, right next to the cursor: your most used
reactions (＋ opens the emoji picker), reply, copy, edit, delete, open or save to your
Downloads folder.
**The ☺ button** next to the composer opens the emoji picker: search by name (heart, fire,
smile…), browse by category or pick from your most used ones.
**Drag over text** to select it and `Ctrl+C` to copy it.
**Click an image or GIF** to open it large; **click a video** to play it right there.
**Drag a file** onto the terminal, paste an image or use `/upload path` and it waits in a tray
above the composer, like Discord's: `Enter` sends it along with your message, its ✕ takes it
out and `Esc` empties the tray.

Commands: `/me action`, `/upload path`, `/settings`, `/help`. To send something that starts with `/`,
type `//`.

## GIFs and videos

- **GIFs move inside the chat**, including the ones from Discord and WhatsApp (which arrive as
  short videos; those need `ffmpeg`).
- **Videos** show their first frame and length. Clicking one plays it **inside the message,
  with sound**, like Discord does; click again to pause. "Open in a player" in the message
  menu opens it in its own window.
- In the `[videos]` section of `config.toml`, `player` picks what a click does:
  `"chat"` (inside the message), `"terminal"` (full screen in the same terminal with mpv:
  sharp in kitty and foot, coloured blocks in Alacritty; `q` comes back) or `"window"`.

Downloaded pictures, GIFs and videos are kept in `/tmp/pupila-<your uid>/`, so they load
instantly while you use Pupila and are gone after the next reboot. To keep one, use
"Save to Downloads" in the message menu.

## Customising

- **`Ctrl+S`** opens the settings: notifications, message bubbles, pictures, profile
  pictures, GIFs, videos, which spaces sort their rooms by name, and logging out.
- `~/.config/pupila/config.toml`: the same settings as a file (plus people's colours).
  Created on first run, with comments.
- `~/.config/pupila/pupila.tcss`: the look (colours, widths, margins), in Textual's CSS.
  It's applied on top of the built-in one ([`src/pupila/pupila.tcss`](src/pupila/pupila.tcss))
  and reloaded live while Pupila is running. For example:

```css
#sidebar { width: 40; }
#composer { border: heavy $accent; }
.message.-group { margin-top: 0; }
```
