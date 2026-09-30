"""De eventos de Matrix a texto de Rich: nombres, horas, cuerpos, citas y reacciones."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from markdown_it import MarkdownIt
from rich.markdown import Markdown
from rich.text import Text

from .modelo import Almacen, Evento, Sala

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]
PALETA = ["#f5a97f", "#a6da95", "#8aadf4", "#eed49f", "#f5bde6", "#8bd5ca", "#ee99a0", "#91d7e3",
          "#b7bdf8", "#f0c6c6"]
URL = re.compile(r"https?://[^\s<>\"')\]]+")
_md = MarkdownIt("commonmark", {"breaks": True, "linkify": False}).enable(["strikethrough", "table"])


def hora(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000).strftime("%H:%M")


def dia(ts: int) -> str:
    d = datetime.fromtimestamp(ts / 1000)
    hoy = datetime.now().date()
    if d.date() == hoy:
        return "hoy"
    if (hoy - d.date()).days == 1:
        return "ayer"
    texto = f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"
    return texto if d.year == hoy.year else f"{texto} de {d.year}"


def color_de(user_id: str, colores: dict[str, str]) -> str:
    if user_id in colores:
        return colores[user_id]
    h = int(hashlib.sha1(user_id.encode()).hexdigest(), 16)
    return PALETA[h % len(PALETA)]


def tamano(n: int | None) -> str:
    if not n:
        return ""
    for unidad in ("B", "KB", "MB", "GB"):
        if n < 1024 or unidad == "GB":
            return f"{n:.0f} {unidad}" if unidad == "B" else f"{n:.1f} {unidad}"
        n /= 1024
    return ""


def _saltos_duros(texto: str) -> str:
    """En el chat un salto de línea es un salto de línea, no un espacio (como en Markdown)."""
    fuera, en_codigo = [], False
    lineas = texto.split("\n")
    for i, linea in enumerate(lineas):
        if linea.lstrip().startswith("```"):
            en_codigo = not en_codigo
        siguiente = lineas[i + 1] if i + 1 < len(lineas) else ""
        if not en_codigo and linea.strip() and siguiente.strip() and not linea.lstrip().startswith("|"):
            linea += "  "
        fuera.append(linea)
    return "\n".join(fuera)


def cuerpo(e: Evento, almacen: Almacen, sala: Sala):
    """El contenido de un mensaje como algo que Rich sabe dibujar."""
    if e.borrado:
        return Text("mensaje borrado", style="italic dim")
    c = e.contenido
    tipo = e.msgtype
    texto = e.texto
    if tipo == "m.notice":
        return Text(texto, style="italic #b4a7f5")
    if tipo == "m.emote":
        return Text.assemble(("* ", "dim"), (almacen.nombre_usuario(e.sender, sala), "bold"), " ", texto)
    if tipo in ("m.image", "m.sticker", "m.video", "m.audio", "m.file"):
        return adjunto(e)
    if not texto.strip():
        return Text("")
    if "formatted_body" in c or any(s in texto for s in ("**", "`", "\n- ", "\n1. ", "# ", "](")):
        return Markdown(_saltos_duros(texto), hyperlinks=True, code_theme="monokai")
    t = Text(texto)
    t.highlight_regex(URL, "underline #8aadf4")
    return t


ICONOS = {"m.image": "🖼", "m.sticker": "🖼", "m.video": "🎞", "m.audio": "🎵", "m.file": "📎"}


def adjunto(e: Evento) -> Text:
    from .medios import duracion, es_animado

    info = e.contenido.get("info") or {}
    nombre = e.contenido.get("filename") or e.contenido.get("body") or "archivo"
    if es_animado(e):
        return Text.assemble(("GIF", "bold #b4a7f5"), ("  clic para abrirlo en grande", "dim italic"))
    if e.msgtype == "m.video":
        partes = [("▶ ", "bold #b4a7f5"), (duracion(e) or "video", "bold")]
    else:
        partes = [ICONOS.get(e.msgtype, "📎") + " ", (nombre, "bold")]
    if info.get("size"):
        partes.append((f"  {tamano(info['size'])}", "dim"))
    partes.append(("  clic para " + ("verlo" if e.msgtype == "m.video" else "abrir"), "dim italic"))
    return Text.assemble(*partes)


def pie_de_foto(e: Evento) -> str:
    """El texto de una imagen, si no es solo el nombre del archivo."""
    c = e.contenido
    if c.get("filename") and c.get("body") and c["body"] != c["filename"]:
        return c["body"]
    return ""


def cita(e: Evento, almacen: Almacen, sala: Sala, colores: dict[str, str]) -> Text | None:
    if not e.responde_a:
        return None
    original = sala.indice.get(e.responde_a)
    if not original:
        return Text("╭─ (respuesta a un mensaje anterior)", style="dim")
    resumen = "mensaje borrado" if original.borrado else (
        original.texto.split("\n")[0] if original.msgtype not in ICONOS
        else ICONOS[original.msgtype] + " " + (original.contenido.get("body") or "archivo"))
    if len(resumen) > 80:
        resumen = resumen[:79] + "…"
    return Text.assemble(("╭─ ", "dim"), (almacen.nombre_usuario(original.sender, sala),
                                             f"bold {color_de(original.sender, colores)}"),
                         (": " + resumen, "dim"))


def reacciones(e: Evento, yo: str) -> Text | None:
    if not e.reacciones:
        return None
    t = Text()
    for clave, quienes in e.reacciones.items():
        mia = yo in quienes
        t.append(f" {clave} {len(quienes)} ", style="bold on #3b3452" if mia else "on #2a2633")
        t.append(" ")
    return t


def cabecera(e: Evento, almacen: Almacen, sala: Sala, colores: dict[str, str]) -> Text:
    t = Text.assemble((almacen.nombre_usuario(e.sender, sala), f"bold {color_de(e.sender, colores)}"),
                      ("  " + hora(e.ts), "dim"))
    return t


def sufijo(e: Evento) -> Text | None:
    if e.fallido:
        return Text("no se pudo enviar", style="bold #f38ba8")
    if e.pendiente:
        return Text("enviando…", style="dim italic")
    if e.editado and not e.borrado:
        return Text("(editado)", style="dim")
    return None


def a_html(texto: str) -> str | None:
    """Markdown del mensaje a HTML para formatted_body; None si no tiene formato."""
    html = _md.render(texto).strip()
    simple = re.fullmatch(r"<p>(.*)</p>", html, re.S)
    if simple and "<" not in simple.group(1).replace("<br />", ""):
        return None
    return html
