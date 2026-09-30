"""Las piezas de la pantalla: barra de salas, línea de mensajes, barra de escritura y diálogos."""
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
from textual_image.widget import Image as ImagenTerminal

from . import formato
from .modelo import Evento, Sala

if TYPE_CHECKING:
    from .app import Pupila

IMAGENES = {"m.image", "m.sticker"}
ESTILOS_IMAGEN = {"auto": ImagenTerminal, "bloques": HalfcellImage, "texto": UnicodeImage}


# --------------------------------------------------------------------------- mensajes


class Mensaje(Vertical):
    """Un mensaje del chat: cabecera (si abre grupo), cita, cuerpo, imagen y reacciones."""

    class Clic(Message):
        def __init__(self, mensaje: "Mensaje") -> None:
            super().__init__()
            self.mensaje = mensaje

    def __init__(self, ev: Evento, sala: Sala, grupo: bool) -> None:
        super().__init__(classes="mensaje -grupo" if grupo else "mensaje")
        self.ev, self.sala, self.grupo = ev, sala, grupo

    @property
    def pupila(self) -> "Pupila":
        return self.app  # type: ignore[return-value]

    def _con_imagen(self) -> bool:
        return (self.ev.msgtype in IMAGENES and self.pupila.cfg.imagenes and not self.ev.borrado
                and bool(self.ev.contenido.get("url")))

    def compose(self) -> ComposeResult:
        p = self.pupila
        al, colores = p.almacen, p.cfg.colores
        if self.grupo:
            yield Static(formato.cabecera(self.ev, al, self.sala, colores), classes="cabecera")
        c = formato.cita(self.ev, al, self.sala, colores)
        if c:
            yield Static(c, classes="cita")
        if self._con_imagen():
            img = p.imagenes.get(self.ev.contenido["url"])
            if img is not None:
                yield self._imagen(img)
            else:
                yield Static(formato.adjunto(self.ev), classes="cuerpo cargando")
            pie = formato.pie_de_foto(self.ev)
            if pie:
                yield Static(pie, classes="cuerpo")
        else:
            yield Static(formato.cuerpo(self.ev, al, self.sala), classes="cuerpo" + (
                " aviso" if self.ev.msgtype == "m.notice" else ""))
        s = formato.sufijo(self.ev)
        if s:
            yield Static(s, classes="sufijo")
        r = formato.reacciones(self.ev, al.yo)
        if r:
            yield Static(r, classes="reacciones")

    def _imagen(self, img):
        clase = ESTILOS_IMAGEN.get(self.pupila.cfg.estilo_imagen, ImagenTerminal)
        w = clase(img, classes="imagen")
        w.styles.height = self.pupila.cfg.alto_imagen
        w.styles.width = "auto"
        return w

    def on_mount(self) -> None:
        if self._con_imagen() and self.ev.contenido["url"] not in self.pupila.imagenes:
            self.cargar_imagen()

    @work(exclusive=True, group="imagen")
    async def cargar_imagen(self) -> None:
        img = await self.pupila.imagen(self.ev.contenido["url"])
        if img is not None and self.is_mounted:
            await self.recompose()

    async def refrescar(self) -> None:
        await self.recompose()

    def on_click(self, event) -> None:
        event.stop()
        self.post_message(self.Clic(self))


class Linea(VerticalScroll):
    """El historial de la sala abierta."""

    class PedirHistorial(Message):
        pass

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.sala: Sala | None = None
        self.por_id: dict[str, Mensaje] = {}
        self.cargando = False

    def _widgets(self, ev: Evento, prev: Evento | None) -> list:
        out = []
        nuevo_dia = prev is None or formato.dia(prev.ts) != formato.dia(ev.ts)
        if nuevo_dia:
            out.append(Static(f"── {formato.dia(ev.ts)} ──", classes="dia"))
        grupo = (nuevo_dia or prev.sender != ev.sender or ev.ts - prev.ts > 5 * 60 * 1000
                 or bool(ev.responde_a))
        m = Mensaje(ev, self.sala, grupo)  # type: ignore[arg-type]
        self.por_id[ev.event_id] = m
        out.append(m)
        return out

    def on_mount(self) -> None:
        self.anchor()

    async def mostrar(self, sala: Sala | None, al_final: bool = True) -> None:
        self.sala = sala
        self.por_id.clear()
        await self.remove_children()
        if sala is None:
            await self.mount(Static("Elige una sala a la izquierda, o Ctrl+K para buscarla.",
                                    classes="vacio"))
            return
        widgets: list = []
        if sala.prev_batch:
            widgets.append(Static("⬆  mensajes anteriores (sube para cargarlos)", id="mas", classes="mas"))
        prev = None
        for ev in sala.eventos:
            widgets.extend(self._widgets(ev, prev))
            prev = ev
        if not sala.eventos:
            widgets.append(Static("Todavía no hay mensajes aquí.", classes="vacio"))
        await self.mount_all(widgets)
        if al_final:
            self.anchor()

    async def sincronizar(self) -> None:
        """Agrega al final los eventos de la sala que todavía no se ven."""
        if not self.sala:
            return
        self.reindexar()
        prev, nuevos = None, []
        for ev in self.sala.eventos:
            if ev.event_id not in self.por_id:
                nuevos.extend(self._widgets(ev, prev))
            prev = ev
        if nuevos:
            for w in self.query(".vacio"):
                await w.remove()
            await self.mount_all(nuevos)  # si estaba abajo, el ancla lo mantiene abajo

    def reindexar(self) -> None:
        self.por_id = {w.ev.event_id: w for w in self.query(Mensaje)}

    async def actualizar(self, event_id: str) -> None:
        m = self.por_id.get(event_id)
        if m:
            await m.refrescar()

    async def quitar(self, event_id: str) -> None:
        m = self.por_id.pop(event_id, None)
        if m:
            await m.remove()

    def watch_scroll_y(self, viejo: float, nuevo: float) -> None:
        super().watch_scroll_y(viejo, nuevo)
        if nuevo <= 0 and viejo > 0 and self.sala and self.sala.prev_batch and not self.cargando:
            self.post_message(self.PedirHistorial())


# --------------------------------------------------------------------------- barra de salas


class Barra(Tree[str]):
    """Espacios y salas, con los no leídos."""

    def __init__(self, **kw) -> None:
        super().__init__("salas", **kw)
        self.show_root = False
        self.guide_depth = 2
        self.primera = True
        self.orden_visual: list[str] = []

    def reconstruir(self, p: "Pupila") -> None:
        al = p.almacen
        expandidos = {n.data for n in self._nodos() if n.is_expanded} | p.ancestros(p.actual)
        cursor = self.cursor_node.data if self.cursor_node else p.actual
        self.clear()
        self.orden_visual = []

        def etiqueta(s: Sala) -> Text:
            t = Text(al.nombre_sala(s), style="bold" if s.sin_leer else "")
            if s.escribiendo:
                t.append(" ✎", style="italic #b4a7f5")
            if s.menciones:
                t.append(f" {s.menciones}", style="bold #f38ba8")
            elif s.sin_leer:
                t.append(f" {s.sin_leer}", style="bold #b4a7f5")
            return t

        def total(s: Sala, vistos: set[str]) -> int:
            n = 0
            for h in s.hijos:
                hs = al.salas.get(h)
                if hs and h not in vistos:
                    vistos.add(h)
                    n += total(hs, vistos) if hs.es_espacio else hs.sin_leer
            return n

        def por_nombre(espacio: Sala) -> bool:
            return al.nombre_sala(espacio) in p.cfg.por_nombre

        def ordenar(salas: list[Sala], alfabetico: bool) -> list[Sala]:
            espacios = sorted((s for s in salas if s.es_espacio), key=lambda s: al.nombre_sala(s).lower())
            resto = [s for s in salas if not s.es_espacio]
            if alfabetico:
                resto.sort(key=lambda s: al.nombre_sala(s).lower())
            else:
                resto.sort(key=lambda s: -s.ultimo_ts)
            return espacios + resto

        def agregar(nodo, espacio: Sala, camino: set[str]) -> None:
            hijos = [al.salas[h] for h in espacio.hijos if h in al.salas and h not in camino
                     and not al.salas[h].invitacion]
            for h in ordenar(hijos, por_nombre(espacio)):
                if h.es_espacio:
                    n = total(h, set())
                    lab = Text(al.nombre_sala(h), style="bold")
                    if n:
                        lab.append(f" {n}", style="#b4a7f5")
                    sub = nodo.add(lab, data=h.room_id, expand=h.room_id in expandidos)
                    agregar(sub, h, camino | {h.room_id})
                else:
                    nodo.add_leaf(etiqueta(h), data=h.room_id)
                    self.orden_visual.append(h.room_id)

        for raiz in ordenar(al.raices(), True):
            n = total(raiz, set())
            lab = Text(al.nombre_sala(raiz), style="bold")
            if n:
                lab.append(f" {n}", style="#b4a7f5")
            nodo = self.root.add(lab, data=raiz.room_id,
                                 expand=self.primera or raiz.room_id in expandidos)
            agregar(nodo, raiz, {raiz.room_id})
        sueltas = al.sueltas()
        if sueltas:
            nodo = self.root.add(Text("Otras salas", style="bold"), data="#otras",
                                 expand=self.primera or "#otras" in expandidos)
            for s in ordenar(sueltas, False):
                nodo.add_leaf(etiqueta(s), data=s.room_id)
                self.orden_visual.append(s.room_id)
        invitaciones = [s for s in al.salas.values() if s.invitacion]
        if invitaciones:
            nodo = self.root.add(Text("Invitaciones", style="bold #f0c674"), data="#invitaciones",
                                 expand=True)
            for s in invitaciones:
                nodo.add_leaf(Text("✉ " + al.nombre_sala(s), style="#f0c674"), data=s.room_id)
        self.primera = False
        # Si estás moviéndote por la barra, el cursor se queda donde estaba; si no, sigue a la sala abierta.
        self.call_after_refresh(self._poner_cursor, cursor if self.has_focus else p.actual)

    def _poner_cursor(self, data: str | None) -> None:
        for n in self._nodos():
            if n.data == data:
                self.move_cursor(n)
                return

    def _nodos(self):
        pila = list(self.root.children)
        while pila:
            n = pila.pop()
            yield n
            pila.extend(n.children)


# --------------------------------------------------------------------------- barra de escritura


class Entrada(TextArea):
    """Enter envía; Alt+Enter (o Shift+Enter si la terminal lo distingue) hace salto de línea."""

    class Enviar(Message):
        def __init__(self, texto: str) -> None:
            super().__init__()
            self.texto = texto

    class EditarUltimo(Message):
        pass

    class Pegar(Message):
        pass

    class ArchivoSoltado(Message):
        def __init__(self, ruta: Path) -> None:
            super().__init__()
            self.ruta = ruta

    def __init__(self, **kw) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, tab_behavior="focus", **kw)

    async def _on_key(self, event) -> None:
        tecla = event.key
        if tecla == "enter":
            event.stop()
            event.prevent_default()
            if self.text.strip():
                self.post_message(self.Enviar(self.text))
            return
        if tecla in ("shift+enter", "alt+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if tecla == "up" and not self.text:
            event.stop()
            event.prevent_default()
            self.post_message(self.EditarUltimo())
            return
        await super()._on_key(event)

    def action_paste(self) -> None:
        """Ctrl+V: Pupila mira el portapapeles del sistema (puede traer una imagen)."""
        self.post_message(self.Pegar())

    async def _on_paste(self, event) -> None:
        ruta = _ruta_soltada(event.text)
        if ruta:
            event.stop()
            event.prevent_default()
            self.post_message(self.ArchivoSoltado(ruta))
            return
        await super()._on_paste(event)


def _ruta_soltada(texto: str) -> Path | None:
    """Arrastrar un archivo a la terminal pega su ruta: si es un archivo que existe, se sube."""
    t = texto.strip().strip("'\"")
    if t.startswith("file://"):
        from urllib.parse import unquote
        t = unquote(t[7:])
    if "\n" in t or not t.startswith(("/", "~")):
        return None
    p = Path(t).expanduser()
    return p if p.is_file() else None


# --------------------------------------------------------------------------- diálogos


class Menu(ModalScreen[str | None]):
    """Lista de opciones; devuelve el id elegido o None."""

    BINDINGS = [("escape", "dismiss(None)", "Cerrar")]

    def __init__(self, titulo: str, opciones: list[tuple[str, str]]) -> None:
        super().__init__()
        self.titulo, self.opciones = titulo, opciones

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialogo"):
            yield Static(self.titulo, classes="titulo")
            yield OptionList(*[Option(texto, id=oid) for oid, texto in self.opciones])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class Confirmar(ModalScreen[bool]):
    BINDINGS = [("escape", "dismiss(False)", "No"), ("enter", "dismiss(True)", "Sí")]

    def __init__(self, pregunta: str, si: str = "Sí", no: str = "No") -> None:
        super().__init__()
        self.pregunta, self.si, self.no = pregunta, si, no

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialogo"):
            yield Static(self.pregunta, classes="titulo")
            with Horizontal(classes="botones"):
                yield Button(self.si, variant="primary", id="si")
                yield Button(self.no, id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "si")


RAPIDAS = ["👍", "❤️", "😂", "😮", "😢", "🙏", "🌸", "✅", "🔥", "👀"]


class Reaccion(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss(None)", "Cerrar")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialogo"):
            yield Static("Reaccionar", classes="titulo")
            with Horizontal(classes="emojis"):
                for i, e in enumerate(RAPIDAS):
                    yield Button(e, id=f"e{i}", classes="emoji")
            yield Input(placeholder="u otro emoji / texto, y Enter")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(str(event.button.label))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class Entrar(ModalScreen[dict | None]):
    """Primera vez: servidor, usuario y contraseña."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialogo entrar"):
            yield Static("Pupila", classes="logo")
            yield Static("Entra a tu servidor de Matrix", classes="titulo")
            yield Input(placeholder="https://servidor:8448", id="servidor")
            yield Input(placeholder="usuario (o @usuario:servidor)", id="usuario")
            yield Input(placeholder="contraseña", password=True, id="clave")
            yield Button("Entrar", variant="primary", id="entrar")
            yield Static("", id="error")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        siguiente = {"servidor": "#usuario", "usuario": "#clave"}.get(event.input.id or "")
        if siguiente:
            self.query_one(siguiente, Input).focus()
        else:
            self.intentar()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.intentar()

    @work(exclusive=True)
    async def intentar(self) -> None:
        from .matrix import ErrorMatrix, Matrix

        servidor = self.query_one("#servidor", Input).value.strip()
        usuario = self.query_one("#usuario", Input).value.strip()
        clave = self.query_one("#clave", Input).value
        error = self.query_one("#error", Static)
        if not (servidor and usuario and clave):
            error.update("Faltan datos.")
            return
        if not servidor.startswith("http"):
            servidor = "https://" + servidor
        error.update("Entrando…")
        mx = Matrix(servidor)
        try:
            import socket
            await mx.entrar(usuario, clave, f"Pupila ({socket.gethostname()})")
        except ErrorMatrix as e:
            error.update(f"No se pudo: {e.error}")
            return
        except Exception as e:  # red, certificado, dirección mal escrita
            error.update(f"No se pudo conectar: {e}")
            return
        finally:
            await mx.cerrar()
        self.dismiss({"homeserver": servidor, "token": mx.token, "user_id": mx.user_id,
                      "device_id": mx.device_id})
