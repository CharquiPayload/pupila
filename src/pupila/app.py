"""La aplicación: conecta la pantalla con el servidor."""
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
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.widgets import Footer, Static

from . import config, formato, medios
from .matrix import ErrorMatrix, Matrix, reintentar
from .modelo import Almacen, Cambios, Evento
from .ui import Barra, Confirmar, Entrada, Entrar, Linea, Menu, Mensaje, Reaccion

FABRICA = Path(__file__).with_name("pupila.tcss")
ULTIMA = config.ESTADO_DIR / "ultima_sala"

TEMA = Theme(
    name="pupila", primary="#9b87f5", secondary="#7c6fd6", accent="#c9b8ff",
    foreground="#e6e1f5", background="#16141d", surface="#1d1a26", panel="#2a2536",
    success="#a6da95", warning="#f0c674", error="#f38ba8", dark=True,
)


class Salas(Provider):
    """Ctrl+K: buscar una sala por nombre."""

    async def search(self, query: str) -> Hits:
        app: Pupila = self.app  # type: ignore[assignment]
        if not app.almacen:
            return
        buscar = self.matcher(query)
        for s in app.almacen.salas.values():
            if s.es_espacio:
                continue
            nombre = app.almacen.nombre_sala(s)
            puntos = buscar.match(nombre)
            if puntos > 0:
                yield Hit(puntos, buscar.highlight(nombre), lambda sid=s.room_id: app.abrir(sid),
                          text=nombre, help=app.espacio_de(s.room_id))

    async def discover(self) -> Hits:
        app: Pupila = self.app  # type: ignore[assignment]
        if not app.almacen:
            return
        salas = [s for s in app.almacen.salas.values() if not s.es_espacio and not s.invitacion]
        salas.sort(key=lambda s: (-(s.menciones * 1000 + s.sin_leer), -s.ultimo_ts))
        for s in salas[:20]:
            nombre = app.almacen.nombre_sala(s)
            etiqueta = f"{nombre}  ({s.sin_leer})" if s.sin_leer else nombre
            yield DiscoveryHit(etiqueta, lambda sid=s.room_id: app.abrir(sid), text=nombre,
                               help=app.espacio_de(s.room_id))


class Pupila(App):
    TITLE = "Pupila"
    COMMAND_PALETTE_BINDING = "ctrl+k"
    COMMANDS = {Salas}
    BINDINGS = [
        Binding("ctrl+k", "command_palette", "Buscar sala", show=False, priority=True),
        Binding("alt+up", "mover(-1)", "Sala anterior", show=False, priority=True),
        Binding("alt+down", "mover(1)", "Sala siguiente", show=False, priority=True),
        Binding("ctrl+r", "responder_ultimo", "Responder", priority=True),
        Binding("ctrl+b", "barra", "Barra", priority=True),
        Binding("escape", "cancelar", "Cancelar", show=False),
        Binding("ctrl+q", "quit", "Salir", priority=True),
    ]

    def __init__(self, cfg: config.Config) -> None:
        estilos = [FABRICA] + ([config.ESTILO] if config.ESTILO.exists() else [])
        super().__init__(css_path=estilos, watch_css=True)
        self.cfg = cfg
        self.mx: Matrix | None = None
        self.almacen: Almacen | None = None
        self.actual: str | None = None
        self.enfocada = True
        self.conectado = False
        self.imagenes: dict[str, PILImage.Image | None] = {}
        self.animaciones: OrderedDict[str, list] = OrderedDict()  # url -> [(cuadro, segundos)]
        self.respondiendo: Evento | None = None
        self.editando: Evento | None = None
        self._leido: dict[str, str] = {}
        self._escribiendo_hasta = 0.0
        self._barra_pendiente = False

    # ------------------------------------------------------------------ pantalla

    def compose(self) -> ComposeResult:
        with Horizontal(id="cuerpo"):
            yield Barra(id="barra")
            with Vertical(id="centro"):
                yield Static(id="cabecera")
                yield Linea(id="linea")
                yield Static(id="escribiendo")
                yield Static(id="accion")
                yield Entrada(id="entrada")
        yield Footer()

    async def on_mount(self) -> None:
        self.register_theme(TEMA)
        self.theme = "pupila"
        await self.query_one(Linea).mostrar(None)
        self.pintar_cabecera()
        sesion = config.leer_sesion()
        if sesion:
            self.iniciar(sesion)
        else:
            self.push_screen(Entrar(), self._tras_entrar)

    def _tras_entrar(self, sesion: dict | None) -> None:
        if not sesion:
            self.exit()
            return
        config.guardar_sesion(sesion)
        self.iniciar(sesion)

    def iniciar(self, sesion: dict) -> None:
        self.mx = Matrix(sesion["homeserver"], sesion["token"], sesion["user_id"], sesion.get("device_id"))
        self.almacen = Almacen(sesion["user_id"])
        self.query_one(Entrada).focus()
        self.run_worker(self.bucle_sync(), group="sync", exclusive=True)

    def pintar_cabecera(self) -> None:
        cab = self.query_one("#cabecera", Static)
        estado = Text(" ● conectado" if self.conectado else " ○ conectando…",
                      style="#a6da95" if self.conectado else "#f0c674")
        if not (self.almacen and self.actual in self.almacen.salas):
            cab.update(Text.assemble(("Pupila", "bold #b4a7f5"), "  ", estado))
            return
        s = self.almacen.salas[self.actual]
        t = Text.assemble((self.almacen.nombre_sala(s), "bold"))
        espacio = self.espacio_de(s.room_id)
        if espacio:
            t = Text.assemble((espacio + " › ", "dim"), t)
        if s.tema:
            tema = s.tema.split("\n")[0]
            t.append("  " + (tema[:90] + "…" if len(tema) > 90 else tema), style="dim")
        t.append_text(estado)
        cab.update(t)

    def pintar_escribiendo(self) -> None:
        w = self.query_one("#escribiendo", Static)
        s = self.almacen.salas.get(self.actual) if self.almacen and self.actual else None
        if not s or not s.escribiendo:
            w.update("")
            return
        nombres = [self.almacen.nombre_usuario(u, s) for u in sorted(s.escribiendo)]
        texto = nombres[0] if len(nombres) == 1 else ", ".join(nombres[:-1]) + " y " + nombres[-1]
        w.update(Text(f"{texto} {'está' if len(nombres) == 1 else 'están'} escribiendo…",
                      style="italic #b4a7f5"))

    def pintar_accion(self) -> None:
        w = self.query_one("#accion", Static)
        if self.editando:
            w.update(Text.assemble(("✎ Editando tu mensaje", "bold #f0c674"), ("   Esc cancela", "dim")))
        elif self.respondiendo:
            s = self.almacen.salas[self.actual]
            quien = self.almacen.nombre_usuario(self.respondiendo.sender, s)
            resumen = self.respondiendo.texto.split("\n")[0][:70]
            w.update(Text.assemble(("↪ Respondiendo a ", "#b4a7f5"), (quien, "bold"),
                                   (": " + resumen, "dim"), ("   Esc cancela", "dim")))
        else:
            w.update("")
        w.set_class(bool(self.editando or self.respondiendo), "-visible")

    def ancestros(self, sid: str | None) -> set[str]:
        """Los espacios que contienen la sala, hasta arriba (para desplegarlos en la barra)."""
        fuera: set[str] = set()
        pendientes = [sid] if sid else []
        while pendientes and self.almacen:
            actual = pendientes.pop()
            for s in self.almacen.salas.values():
                if s.es_espacio and actual in s.hijos and s.room_id not in fuera:
                    fuera.add(s.room_id)
                    pendientes.append(s.room_id)
        return fuera

    def espacio_de(self, sid: str) -> str:
        if not self.almacen:
            return ""
        for s in self.almacen.salas.values():
            if s.es_espacio and sid in s.hijos:
                return self.almacen.nombre_sala(s)
        return ""

    def pedir_barra(self) -> None:
        """Rehace la barra como mucho cada medio segundo."""
        if self._barra_pendiente:
            return
        self._barra_pendiente = True

        def hacer() -> None:
            self._barra_pendiente = False
            self.query_one(Barra).reconstruir(self)

        self.set_timer(0.5, hacer)

    # ------------------------------------------------------------------ sincronización

    async def bucle_sync(self) -> None:
        desde: str | None = None
        espera = 1.0
        while True:
            try:
                d = await self.mx.sync(desde)
            except ErrorMatrix as e:
                if e.errcode in ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN"):
                    config.borrar_sesion()
                    self.notify("La sesión ya no es válida: vuelve a entrar.", severity="error")
                    self.push_screen(Entrar(), self._tras_entrar)
                    return
                self._desconectado(f"el servidor respondió {e.status}")
                await asyncio.sleep(espera)
                espera = min(espera * 2, 30)
                continue
            except (httpx.TransportError, httpx.TimeoutException) as e:
                self._desconectado(type(e).__name__)
                await asyncio.sleep(espera)
                espera = min(espera * 2, 30)
                continue
            inicial = desde is None
            cambios = self.almacen.aplicar_sync(d, inicial)
            desde = d.get("next_batch", desde)
            espera = 1.0
            if not self.conectado:
                self.conectado = True
                self.pintar_cabecera()
            await self.aplicar(cambios, inicial)

    def _desconectado(self, motivo: str) -> None:
        if self.conectado:
            self.conectado = False
            self.pintar_cabecera()
            self.log(f"sync: {motivo}")

    async def aplicar(self, c: Cambios, inicial: bool) -> None:
        if inicial:
            self.query_one(Barra).reconstruir(self)
            ultima = ULTIMA.read_text().strip() if ULTIMA.exists() else ""
            if ultima not in self.almacen.salas:
                primera = self.query_one(Barra).orden_visual
                ultima = primera[0] if primera else ""
            if ultima:
                self.abrir(ultima)
            return
        if c.estructura or c.salas:
            self.pedir_barra()
        if self.actual in c.salas:
            linea = self.query_one(Linea)
            if self.actual in c.limitadas:
                await linea.mostrar(self.almacen.salas[self.actual])
            else:
                await linea.sincronizar()
                for eid in c.actualizados:
                    await linea.actualizar(eid)
            self.pintar_escribiendo()
            self.marcar_leido()
        for sid, ev in c.nuevos[:3]:
            self.avisar(sid, ev)

    # ------------------------------------------------------------------ salas

    def abrir(self, sid: str) -> None:
        self.run_worker(self._abrir(sid), group="abrir", exclusive=True)

    async def _abrir(self, sid: str) -> None:
        s = self.almacen.salas.get(sid) if self.almacen else None
        if not s:
            return
        if s.invitacion:
            quien = self.almacen.nombre_usuario(s.invitado_por) if s.invitado_por else "alguien"
            if await self.push_screen_wait(Confirmar(
                    f"{quien} te invitó a «{self.almacen.nombre_sala(s)}». ¿Entrar?", "Entrar", "Ahora no")):
                try:
                    await self.mx.unirse(sid)
                except ErrorMatrix as e:
                    self.notify(f"No se pudo entrar: {e.error}", severity="error")
            return
        if s.es_espacio:
            return
        self.actual = sid
        self.respondiendo = self.editando = None
        self.pintar_accion()
        config.ESTADO_DIR.mkdir(parents=True, exist_ok=True)
        ULTIMA.write_text(sid)
        await self.query_one(Linea).mostrar(s)
        self.pintar_cabecera()
        self.pintar_escribiendo()
        self.marcar_leido()
        self.pedir_barra()
        self.query_one(Entrada).focus()

    @on(Barra.NodeSelected)
    def _nodo(self, event: Barra.NodeSelected) -> None:
        sid = event.node.data
        if sid and not sid.startswith("#") and not event.node.allow_expand:
            self.abrir(sid)

    def action_mover(self, paso: int) -> None:
        orden = self.query_one(Barra).orden_visual
        if not orden:
            return
        i = orden.index(self.actual) if self.actual in orden else -1
        self.abrir(orden[(i + paso) % len(orden)])

    def action_barra(self) -> None:
        self.query_one(Barra).toggle_class("-oculta")

    @on(Linea.PedirHistorial)
    async def _historial(self) -> None:
        linea = self.query_one(Linea)
        s = linea.sala
        if not s or not s.prev_batch or linea.cargando:
            return
        linea.cargando = True
        try:
            d = await self.mx.mensajes(s.room_id, s.prev_batch)
            self.almacen.aplicar_historial(s.room_id, d)
            if linea.sala is s:
                alto_antes = linea.virtual_size.height
                await linea.mostrar(s, al_final=False)
                self.call_after_refresh(
                    lambda: linea.scroll_to(y=max(0, linea.virtual_size.height - alto_antes), animate=False))
        except (ErrorMatrix, httpx.HTTPError) as e:
            self.notify(f"No pude traer mensajes anteriores: {e}", severity="warning")
        finally:
            linea.cargando = False

    def marcar_leido(self) -> None:
        if not (self.actual and self.enfocada and self.almacen):
            return
        s = self.almacen.salas.get(self.actual)
        ultimo = next((e.event_id for e in reversed(s.eventos) if not e.event_id.startswith("~")), None) if s else None
        if not ultimo or self._leido.get(self.actual) == ultimo:
            return
        self._leido[self.actual] = ultimo
        s.sin_leer = s.menciones = 0
        self.pedir_barra()
        self.run_worker(self._leido_servidor(self.actual, ultimo), group="leido")

    async def _leido_servidor(self, sid: str, eid: str) -> None:
        try:
            await self.mx.leido(sid, eid)
        except (ErrorMatrix, httpx.HTTPError):
            pass

    def on_app_focus(self) -> None:
        self.enfocada = True
        self.marcar_leido()

    def on_app_blur(self) -> None:
        self.enfocada = False

    # ------------------------------------------------------------------ avisos

    def avisar(self, sid: str, ev: Evento) -> None:
        if not self.cfg.avisos:
            return
        s = self.almacen.salas.get(sid)
        if not s or (sid == self.actual and self.enfocada):
            return
        if not s.sin_leer and not self.almacen.menciona(ev):
            return  # sala silenciada en el servidor
        quien = self.almacen.nombre_usuario(ev.sender, s)
        sala = self.almacen.nombre_sala(s)
        titulo = quien if sala == quien else f"{quien} · {sala}"
        if not self.cfg.avisos_texto:
            texto = "Mensaje nuevo"
        elif ev.msgtype in formato.ICONOS:
            texto = formato.ICONOS[ev.msgtype] + " " + (ev.contenido.get("body") or "archivo")
        else:
            texto = ev.texto
        if shutil.which("notify-send"):
            self.run_worker(self._ejecutar("notify-send", "-a", "Pupila", "-i", "mail-message-new",
                                           titulo, texto[:300]), group="avisos")
        if self.cfg.campana:
            self.bell()

    async def _ejecutar(self, *cmd: str, entrada: bytes | None = None) -> tuple[int, bytes]:
        try:
            p = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE if entrada is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(entrada), 20)
            return p.returncode or 0, out
        except (OSError, asyncio.TimeoutError):
            return 1, b""

    # ------------------------------------------------------------------ escribir y enviar

    @on(Entrada.Changed)
    def _escribe(self, event: Entrada.Changed) -> None:
        if not (self.actual and self.mx):
            return
        ahora = time.monotonic()
        if event.text_area.text and ahora > self._escribiendo_hasta:
            self._escribiendo_hasta = ahora + 4
            self.run_worker(self._typing(self.actual, True), group="typing")
        elif not event.text_area.text and self._escribiendo_hasta:
            self._escribiendo_hasta = 0
            self.run_worker(self._typing(self.actual, False), group="typing")

    async def _typing(self, sid: str, si: bool) -> None:
        try:
            await self.mx.escribiendo(sid, si)
        except (ErrorMatrix, httpx.HTTPError):
            pass

    @on(Entrada.Enviar)
    async def _enviar(self, event: Entrada.Enviar) -> None:
        if not self.actual:
            self.notify("Primero abre una sala.", severity="warning")
            return
        entrada = self.query_one(Entrada)
        texto = event.texto.rstrip()
        entrada.clear()
        if texto.startswith("/") and not texto.startswith("//"):
            await self.comando(texto)
            return
        if texto.startswith("//"):
            texto = texto[1:]
        await self.enviar_texto(self.actual, texto)

    async def enviar_texto(self, sid: str, texto: str, msgtype: str = "m.text") -> None:
        contenido: dict = {"msgtype": msgtype, "body": texto}
        html = formato.a_html(texto)
        if html:
            contenido.update(format="org.matrix.custom.html", formatted_body=html)
        if self.editando:
            ev, self.editando = self.editando, None
            self.pintar_accion()
            try:
                await reintentar(self.mx.editar, sid, ev.event_id, contenido)
            except (ErrorMatrix, httpx.HTTPError) as e:
                self.notify(f"No se pudo editar: {e}", severity="error")
            return
        if self.respondiendo:
            contenido["m.relates_to"] = {"m.in_reply_to": {"event_id": self.respondiendo.event_id}}
            self.respondiendo = None
            self.pintar_accion()
        await self.enviar_contenido(sid, contenido)

    async def enviar_contenido(self, sid: str, contenido: dict) -> None:
        txn = uuid.uuid4().hex
        eco = self.almacen.eco(sid, "~" + txn, contenido, int(time.time() * 1000))
        linea = self.query_one(Linea)
        if sid == self.actual:
            await linea.sincronizar()
            linea.anchor()  # lo que envías siempre se ve
        self._escribiendo_hasta = 0
        self.run_worker(self._typing(sid, False), group="typing")
        try:
            real = await reintentar(self.mx.enviar, sid, contenido, txn=txn)
        except (ErrorMatrix, httpx.HTTPError) as e:
            eco.fallido = True
            await linea.actualizar(eco.event_id)
            self.notify(f"No se pudo enviar: {e}", severity="error")
            return
        e = self.almacen.cambiar_id(sid, "~" + txn, real)
        if sid == self.actual:
            if e is not eco:  # el sync lo trajo antes: sobra el eco
                await linea.quitar("~" + txn)
            linea.reindexar()
            await linea.actualizar(real)
        self.marcar_leido()

    async def comando(self, texto: str) -> None:
        nombre, _, resto = texto[1:].partition(" ")
        resto = resto.strip()
        if nombre == "me" and resto:
            await self.enviar_texto(self.actual, resto, "m.emote")
        elif nombre in ("subir", "upload") and resto:
            await self.subir(Path(resto).expanduser())
        elif nombre in ("salir", "quit"):
            self.exit()
        elif nombre in ("ayuda", "help"):
            self.notify("/me acción · /subir ruta · //texto envía algo que empieza con / · "
                        "Ctrl+V pega imágenes · ↑ edita tu último mensaje · clic en un mensaje: "
                        "responder, reaccionar, borrar…", timeout=12)
        else:
            self.notify(f"No conozco /{nombre}. Prueba /ayuda (o // para enviarlo como texto).",
                        severity="warning")

    def action_cancelar(self) -> None:
        if self.editando or self.respondiendo:
            if self.editando:
                self.query_one(Entrada).clear()
            self.editando = self.respondiendo = None
            self.pintar_accion()
        self.query_one(Entrada).focus()

    @on(Entrada.EditarUltimo)
    def _editar_ultimo(self) -> None:
        s = self.almacen.salas.get(self.actual) if self.almacen and self.actual else None
        if not s:
            return
        mio = next((e for e in reversed(s.eventos) if e.sender == self.almacen.yo and not e.borrado
                    and not e.pendiente and e.msgtype in ("m.text", "m.emote", "m.notice")), None)
        if mio:
            self.editar(mio)

    def editar(self, ev: Evento) -> None:
        self.editando, self.respondiendo = ev, None
        entrada = self.query_one(Entrada)
        entrada.load_text(ev.texto)
        entrada.move_cursor(entrada.document.end)
        entrada.focus()
        self.pintar_accion()

    def action_responder_ultimo(self) -> None:
        s = self.almacen.salas.get(self.actual) if self.almacen and self.actual else None
        if not s:
            return
        ev = next((e for e in reversed(s.eventos) if e.sender != self.almacen.yo and not e.borrado), None)
        if ev:
            self.responder(ev)

    def responder(self, ev: Evento) -> None:
        self.respondiendo, self.editando = ev, None
        self.pintar_accion()
        self.query_one(Entrada).focus()

    # ------------------------------------------------------------------ clic en un mensaje

    @on(Mensaje.Clic)
    @work(exclusive=True, group="menu")
    async def _menu(self, event: Mensaje.Clic) -> None:
        ev = event.mensaje.ev
        if ev.event_id.startswith("~"):
            return
        opciones = []
        if ev.msgtype in formato.ICONOS and ev.contenido.get("url"):
            opciones.append(("abrir", "Abrir"))
        if not ev.borrado:
            opciones += [("responder", "Responder"), ("reaccionar", "Reaccionar")]
            if ev.msgtype not in formato.ICONOS:
                opciones.append(("copiar", "Copiar texto"))
        if ev.sender == self.almacen.yo and not ev.borrado:
            if ev.msgtype in ("m.text", "m.emote"):
                opciones.append(("editar", "Editar"))
            opciones.append(("borrar", "Borrar"))
        if not opciones:
            return
        s = self.almacen.salas[self.actual]
        titulo = f"{self.almacen.nombre_usuario(ev.sender, s)} · {formato.hora(ev.ts)}"
        eleccion = await self.push_screen_wait(Menu(titulo, opciones))
        if eleccion == "abrir":
            await self.abrir_archivo(ev)
        elif eleccion == "responder":
            self.responder(ev)
        elif eleccion == "reaccionar":
            clave = await self.push_screen_wait(Reaccion())
            if clave:
                await self.reaccionar(ev, clave)
        elif eleccion == "copiar":
            await self.copiar(ev.texto)
        elif eleccion == "editar":
            self.editar(ev)
        elif eleccion == "borrar":
            if await self.push_screen_wait(Confirmar("¿Borrar este mensaje? No se puede deshacer.",
                                                     "Borrar", "Cancelar")):
                try:
                    await self.mx.borrar(self.actual, ev.event_id)
                except (ErrorMatrix, httpx.HTTPError) as e:
                    self.notify(f"No se pudo borrar: {e}", severity="error")

    async def reaccionar(self, ev: Evento, clave: str) -> None:
        mia = ev.reacciones.get(clave, {}).get(self.almacen.yo)
        try:
            if mia:
                await self.mx.borrar(self.actual, mia)  # tocar la misma reacción la quita
            else:
                await self.mx.reaccionar(self.actual, ev.event_id, clave)
        except (ErrorMatrix, httpx.HTTPError) as e:
            self.notify(f"No se pudo reaccionar: {e}", severity="error")

    async def copiar(self, texto: str) -> None:
        if shutil.which("wl-copy"):
            await self._ejecutar("wl-copy", entrada=texto.encode())
        elif shutil.which("xclip"):
            await self._ejecutar("xclip", "-selection", "clipboard", entrada=texto.encode())
        else:
            self.copy_to_clipboard(texto)
        self.notify("Copiado.", timeout=2)

    # ------------------------------------------------------------------ archivos

    async def imagen(self, mxc: str) -> PILImage.Image | None:
        if mxc in self.imagenes:
            return self.imagenes[mxc]
        cache = config.CACHE_DIR / "miniaturas" / mxc.removeprefix("mxc://").replace("/", "_")
        try:
            if cache.exists():
                datos = cache.read_bytes()
            else:
                datos = await self.mx.miniatura(mxc)
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(datos)
            img = PILImage.open(io.BytesIO(datos))
            img.load()
        except Exception as e:  # imagen rota, formato raro, red
            self.log(f"imagen {mxc}: {e}")
            img = None
        self.imagenes[mxc] = img
        return img

    async def descargar(self, ev: Evento) -> Path | None:
        """El archivo completo, guardado en la caché (se descarga una sola vez)."""
        mxc = ev.contenido.get("url", "")
        nombre = Path(ev.contenido.get("filename") or ev.contenido.get("body") or "archivo").name
        destino = config.CACHE_DIR / "archivos" / f"{mxc.rsplit('/', 1)[-1]}_{nombre}"
        if not destino.exists():
            try:
                datos = await self.mx.bajar(mxc)
            except (ErrorMatrix, httpx.HTTPError) as e:
                self.log(f"descarga {mxc}: {e}")
                return None
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_bytes(datos)
        return destino

    async def animacion(self, ev: Evento) -> list | None:
        """Los cuadros de un GIF (o de un video-GIF de Discord/WhatsApp) para animarlo en el chat."""
        url = ev.contenido.get("url", "")
        if url in self.animaciones:
            self.animaciones.move_to_end(url)
            return self.animaciones[url]
        if (medios.info(ev).get("size") or 0) > 20 * 1024 * 1024:
            return None
        ruta = await self.descargar(ev)
        if not ruta:
            return None
        datos = ruta.read_bytes()
        try:
            if ev.msgtype == "m.video":
                cuadros = await medios.cuadros_video(datos)
            else:
                cuadros = await asyncio.to_thread(medios.cuadros_gif, datos)
        except Exception as e:  # GIF roto, video raro
            self.log(f"animación {url}: {e}")
            cuadros = []
        if not cuadros:
            return None
        self.animaciones[url] = cuadros
        while len(self.animaciones) > 16:
            self.animaciones.popitem(last=False)
        return cuadros

    async def miniatura_video(self, ev: Evento) -> PILImage.Image | None:
        clave = "video:" + ev.contenido.get("url", "")
        if clave in self.imagenes:
            return self.imagenes[clave]
        img = None
        miniatura = medios.info(ev).get("thumbnail_url")
        if miniatura:
            img = await self.imagen(miniatura)
        elif (medios.info(ev).get("size") or 0) <= 25 * 1024 * 1024:
            ruta = await self.descargar(ev)
            if ruta:
                img = await medios.primer_cuadro(ruta.read_bytes())
        self.imagenes[clave] = img
        return img

    async def abrir_archivo(self, ev: Evento) -> None:
        nombre = ev.contenido.get("filename") or ev.contenido.get("body") or "archivo"
        self.notify(f"Abriendo {nombre}…", timeout=2)
        ruta = await self.descargar(ev)
        if not ruta:
            self.notify("No se pudo descargar.", severity="error")
            return
        es_video = ev.msgtype == "m.video" or medios.es_animado(ev)
        mpv = shutil.which("mpv")
        if es_video and mpv:
            bucle = ["--loop-file=inf"] if medios.es_animado(ev) else []
            if self.cfg.reproductor == "terminal":
                with self.suspend():  # mpv dibuja en la misma terminal; q vuelve a Pupila
                    subprocess.run([mpv, "--vo=tct", "--really-quiet", *bucle, str(ruta)])
                return
            await asyncio.create_subprocess_exec(
                mpv, "--force-window=immediate", "--really-quiet", *bucle, str(ruta),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        elif shutil.which("xdg-open"):
            p = await asyncio.create_subprocess_exec("xdg-open", str(ruta), stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            try:  # si en unos segundos terminó con error, es que no hay programa para ese tipo de archivo
                fallo = await asyncio.wait_for(p.wait(), 4) != 0
            except asyncio.TimeoutError:
                fallo = False
            if fallo:
                consejo = ("Instala mpv (sudo pacman -S mpv) y Pupila lo usará para los videos."
                           if es_video else "Tu sistema no tiene un programa asociado a este tipo de archivo.")
                self.notify(f"No hay con qué abrir {nombre}. {consejo}\nQuedó guardado en {ruta}",
                            severity="warning", timeout=12)
        else:
            self.notify(f"Guardado en {ruta}")

    @on(Mensaje.Abrir)
    @work(group="abrir-archivo")
    async def _abrir_medio(self, event: Mensaje.Abrir) -> None:
        await self.abrir_archivo(event.mensaje.ev)

    async def subir(self, ruta: Path | None = None, datos: bytes | None = None, tipo: str | None = None,
                    nombre: str | None = None) -> None:
        sid = self.actual
        if not sid:
            return
        if ruta is not None:
            if not ruta.is_file():
                self.notify(f"No existe {ruta}", severity="error")
                return
            datos, nombre = ruta.read_bytes(), ruta.name
        assert datos is not None and nombre
        tipo = tipo or mimetypes.guess_type(nombre)[0] or "application/octet-stream"
        info: dict = {"mimetype": tipo, "size": len(datos)}
        msgtype = "m.file"
        if tipo.startswith("image/"):
            msgtype = "m.image"
            try:
                with PILImage.open(io.BytesIO(datos)) as im:
                    info["w"], info["h"] = im.size
            except Exception:
                pass
        elif tipo.startswith("video/"):
            msgtype = "m.video"
        elif tipo.startswith("audio/"):
            msgtype = "m.audio"
        self.notify(f"Subiendo {nombre} ({formato.tamano(len(datos))})…", timeout=3)
        try:
            mxc = await self.mx.subir(datos, tipo, nombre)
        except (ErrorMatrix, httpx.HTTPError) as e:
            self.notify(f"No se pudo subir: {e}", severity="error")
            return
        contenido = {"msgtype": msgtype, "body": nombre, "filename": nombre, "url": mxc, "info": info}
        if msgtype == "m.image":
            try:
                img = PILImage.open(io.BytesIO(datos))
                img.load()
                self.imagenes[mxc] = img
            except Exception:
                pass
        await self.enviar_contenido(sid, contenido)

    @on(Entrada.Pegar)
    @work(exclusive=True, group="pegar")
    async def _pegar(self) -> None:
        entrada = self.query_one(Entrada)
        if shutil.which("wl-paste"):
            _, tipos = await self._ejecutar("wl-paste", "--list-types")
            imagen = next((t for t in tipos.decode(errors="replace").split() if t.startswith("image/")), None)
            if imagen:
                _, datos = await self._ejecutar("wl-paste", "--type", imagen)
                if datos and await self.push_screen_wait(Confirmar(
                        f"¿Enviar la imagen del portapapeles ({formato.tamano(len(datos))})?", "Enviar", "No")):
                    ext = mimetypes.guess_extension(imagen) or ".png"
                    await self.subir(datos=datos, tipo=imagen, nombre=f"imagen{ext}")
                return
            _, texto = await self._ejecutar("wl-paste", "--no-newline")
        elif shutil.which("xclip"):
            _, texto = await self._ejecutar("xclip", "-selection", "clipboard", "-o")
        else:
            self.notify("Para pegar hace falta wl-clipboard (Wayland) o xclip.", severity="warning")
            return
        if texto:
            entrada.insert(texto.decode(errors="replace"))

    @on(Entrada.ArchivoSoltado)
    @work(exclusive=True, group="pegar")
    async def _soltado(self, event: Entrada.ArchivoSoltado) -> None:
        r = event.ruta
        if await self.push_screen_wait(Confirmar(
                f"¿Enviar {r.name} ({formato.tamano(r.stat().st_size)})?", "Enviar", "No")):
            await self.subir(r)
