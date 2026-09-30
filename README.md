# Pupila

Un cliente de [Matrix](https://matrix.org) para la terminal, **sin modos de Vim**: escribes
directo, `Enter` envía y el mouse funciona. Hecho con [Textual](https://textual.textualize.io).

Pensado para un servidor propio sin cifrado (no soporta salas cifradas).

## Instalar

Con [uv](https://docs.astral.sh/uv/) (en Arch/CachyOS: `sudo pacman -S uv`):

```
uv tool install git+https://github.com/CharquiPayload/pupila
pupila
```

Para actualizar: `uv tool upgrade pupila`.

La primera vez pide la dirección del servidor, tu usuario y tu contraseña. La sesión queda en
`~/.local/state/pupila/sesion.json`, que solo tu usuario puede leer. `pupila --salir` la cierra.

Para pegar imágenes con `Ctrl+V` hace falta `wl-clipboard` (Wayland) o `xclip` (X11). Las
imágenes se ven nítidas en terminales con gráficos (foot, kitty, WezTerm, Ghostty) y
pixeladas en las demás (Alacritty).

## Teclas

| Tecla | Qué hace |
|---|---|
| `Enter` | envía |
| `Alt+Enter` | salto de línea (`Shift+Enter` si tu terminal lo distingue) |
| `Ctrl+K` | buscar una sala por nombre |
| `Alt+↑` / `Alt+↓` | sala anterior / siguiente |
| `↑` (con la barra vacía) | editar tu último mensaje |
| `Ctrl+R` | responder al último mensaje de otra persona |
| `Ctrl+V` | pegar (si hay una imagen en el portapapeles, la envía) |
| `Esc` | cancelar respuesta o edición |
| `Ctrl+B` | mostrar u ocultar la barra de salas |
| `Ctrl+Q` | salir |

**Clic en un mensaje**: responder, reaccionar, copiar, editar, borrar o abrir el archivo.
**Arrastrar un archivo** a la terminal lo envía.

Comandos: `/me acción`, `/subir ruta`, `/ayuda`. Para enviar algo que empiece con `/`,
escribe `//`.

## Personalizar

- `~/.config/pupila/config.toml`: notificaciones, imágenes, orden de las salas y colores de
  personas. Se crea solo la primera vez, con comentarios.
- `~/.config/pupila/pupila.tcss`: el aspecto (colores, anchos, márgenes), en el CSS de Textual.
  Se aplica encima del de fábrica ([`src/pupila/pupila.tcss`](src/pupila/pupila.tcss)) y se
  recarga en vivo mientras Pupila está abierta. Por ejemplo:

```css
#barra { width: 40; }
#entrada { border: heavy $accent; }
.mensaje.-grupo { margin-top: 0; }
```
