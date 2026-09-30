"""Lo que Pupila sabe del servidor: salas, espacios, miembros y mensajes.

Se alimenta de las respuestas de /sync y /messages; no habla con la red.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MENSAJES = {"m.room.message", "m.sticker"}
MAX_EVENTOS = 400  # por sala; lo más viejo se olvida (se puede volver a pedir)


@dataclass
class Evento:
    event_id: str
    sender: str
    ts: int
    tipo: str
    contenido: dict
    borrado: bool = False
    editado: bool = False
    reacciones: dict[str, dict[str, str]] = field(default_factory=dict)  # clave -> {sender: id reacción}
    pendiente: bool = False  # eco local, aún sin confirmar
    fallido: bool = False

    @property
    def msgtype(self) -> str:
        return self.contenido.get("msgtype", "m.sticker" if self.tipo == "m.sticker" else "")

    @property
    def responde_a(self) -> str | None:
        return (self.contenido.get("m.relates_to") or {}).get("m.in_reply_to", {}).get("event_id")

    @property
    def hilo(self) -> str | None:
        rel = self.contenido.get("m.relates_to") or {}
        return rel.get("event_id") if rel.get("rel_type") == "m.thread" else None

    @property
    def texto(self) -> str:
        """El cuerpo sin la cita de respaldo de las respuestas ('> <@x> ...')."""
        cuerpo = self.contenido.get("body", "")
        if self.responde_a and cuerpo.startswith("> "):
            lineas = cuerpo.split("\n")
            i = 0
            while i < len(lineas) and lineas[i].startswith(">"):
                i += 1
            cuerpo = "\n".join(lineas[i:]).lstrip("\n")
        return cuerpo


@dataclass
class Sala:
    room_id: str
    nombre: str = ""
    tema: str = ""
    alias: str = ""
    es_espacio: bool = False
    hijos: dict[str, dict] = field(default_factory=dict)  # room_id -> contenido de m.space.child
    miembros: dict[str, str] = field(default_factory=dict)  # user_id -> displayname
    heroes: list[str] = field(default_factory=list)
    n_miembros: int = 0
    eventos: list[Evento] = field(default_factory=list)
    indice: dict[str, Evento] = field(default_factory=dict)
    prev_batch: str | None = None
    sin_leer: int = 0
    menciones: int = 0
    escribiendo: set[str] = field(default_factory=set)
    invitacion: bool = False
    invitado_por: str = ""
    ultimo_ts: int = 0
    directo_con: str | None = None

    def agregar(self, ev: Evento, al_final: bool = True) -> bool:
        if ev.event_id in self.indice:
            return False
        self.indice[ev.event_id] = ev
        if al_final:
            self.eventos.append(ev)
        else:
            self.eventos.insert(0, ev)
        if ev.tipo in MENSAJES:
            self.ultimo_ts = max(self.ultimo_ts, ev.ts)
        if len(self.eventos) > MAX_EVENTOS:
            viejo = self.eventos.pop(0)
            self.indice.pop(viejo.event_id, None)
        return True


@dataclass
class Cambios:
    salas: set[str] = field(default_factory=set)       # salas con algo nuevo
    estructura: bool = False                            # salas o espacios que aparecen/desaparecen
    nuevos: list[tuple[str, Evento]] = field(default_factory=list)  # mensajes nuevos (para avisar)
    actualizados: set[str] = field(default_factory=set)  # event_ids editados/borrados/reaccionados
    limitadas: set[str] = field(default_factory=set)     # salas cuyo historial se cortó (hueco)


class Almacen:
    def __init__(self, yo: str):
        self.yo = yo
        self.salas: dict[str, Sala] = {}
        self.directos: dict[str, str] = {}  # room_id -> user_id
        self.nombres: dict[str, str] = {}   # user_id -> displayname conocido en cualquier sala
        self.reacciones: dict[str, tuple[str, str, str]] = {}  # id reacción -> (evento, clave, sender)

    # --- nombres ---

    def nombre_usuario(self, user_id: str, sala: Sala | None = None) -> str:
        if sala and sala.miembros.get(user_id):
            return sala.miembros[user_id]
        if self.nombres.get(user_id):
            return self.nombres[user_id]
        return user_id.split(":")[0].lstrip("@")

    def nombre_sala(self, sala: Sala) -> str:
        if sala.nombre:
            return sala.nombre
        if sala.alias:
            return sala.alias
        otro = sala.directo_con or next((h for h in sala.heroes if h != self.yo), None)
        if otro:
            return self.nombre_usuario(otro, sala)
        otros = [u for u in sala.miembros if u != self.yo]
        if otros:
            return ", ".join(self.nombre_usuario(u, sala) for u in otros[:3])
        return "Sala vacía"

    # --- sync ---

    def aplicar_sync(self, d: dict, inicial: bool = False) -> Cambios:
        c = Cambios()
        for ev in (d.get("account_data") or {}).get("events", []):
            if ev.get("type") == "m.direct":
                self.directos = {r: u for u, salas in (ev.get("content") or {}).items() for r in salas}
                for sid, s in self.salas.items():
                    s.directo_con = self.directos.get(sid)
                c.estructura = True
        rooms = d.get("rooms") or {}
        for sid, r in (rooms.get("join") or {}).items():
            nueva = sid not in self.salas or self.salas[sid].invitacion
            s = self.salas.get(sid) or Sala(sid)
            s.invitacion = False
            self.salas[sid] = s
            s.directo_con = self.directos.get(sid)
            if nueva:
                c.estructura = True
            resumen = r.get("summary") or {}
            if "m.heroes" in resumen:
                s.heroes = resumen["m.heroes"]
            if "m.joined_member_count" in resumen:
                s.n_miembros = resumen["m.joined_member_count"]
            for ev in (r.get("state") or {}).get("events", []):
                if self._estado(s, ev):
                    c.estructura = True
            tl = r.get("timeline") or {}
            if tl.get("limited") and not inicial:
                s.eventos.clear()
                s.indice.clear()
                c.limitadas.add(sid)
            if tl.get("prev_batch") and (not s.prev_batch or tl.get("limited") or inicial):
                s.prev_batch = tl["prev_batch"]
            for ev in tl.get("events", []):
                if "state_key" in ev and self._estado(s, ev):
                    c.estructura = True
                nuevo = self._evento(s, ev, c)
                if nuevo and not inicial and nuevo.sender != self.yo and nuevo.tipo in MENSAJES:
                    c.nuevos.append((sid, nuevo))
            for ev in (r.get("ephemeral") or {}).get("events", []):
                if ev.get("type") == "m.typing":
                    s.escribiendo = set(ev.get("content", {}).get("user_ids", [])) - {self.yo}
                    c.salas.add(sid)
            un = r.get("unread_notifications") or {}
            if un:
                s.sin_leer = un.get("notification_count", 0) or 0
                s.menciones = un.get("highlight_count", 0) or 0
            c.salas.add(sid)
        for sid, r in (rooms.get("invite") or {}).items():
            s = self.salas.get(sid) or Sala(sid, invitacion=True)
            s.invitacion = True
            self.salas[sid] = s
            for ev in (r.get("invite_state") or {}).get("events", []):
                self._estado(s, ev)
                if ev.get("type") == "m.room.member" and ev.get("state_key") == self.yo:
                    s.invitado_por = ev.get("sender", "")
            c.estructura = True
        for sid in (rooms.get("leave") or {}):
            if self.salas.pop(sid, None):
                c.estructura = True
        return c

    def aplicar_historial(self, sid: str, d: dict) -> Cambios:
        """Respuesta de /messages (dir=b): eventos del más nuevo al más viejo."""
        c = Cambios()
        s = self.salas[sid]
        for ev in d.get("state") or []:
            self._estado(s, ev)
        for ev in d.get("chunk") or []:
            if "state_key" in ev:
                self._estado(s, ev)
            self._evento(s, ev, c, al_final=False)
        s.prev_batch = d.get("end") if d.get("chunk") else None
        c.salas.add(sid)
        return c

    # --- internos ---

    def _estado(self, s: Sala, ev: dict) -> bool:
        """Aplica un evento de estado. Devuelve True si cambia la estructura (espacios, nombre)."""
        t, k, cont = ev.get("type"), ev.get("state_key"), ev.get("content") or {}
        if t == "m.room.name":
            cambio = s.nombre != cont.get("name", "")
            s.nombre = cont.get("name", "")
            return cambio
        if t == "m.room.topic":
            s.tema = cont.get("topic", "")
        elif t == "m.room.canonical_alias":
            s.alias = cont.get("alias", "") or ""
        elif t == "m.room.create":
            if cont.get("type") == "m.space" and not s.es_espacio:
                s.es_espacio = True
                return True
        elif t == "m.space.child" and k:
            if cont.get("via"):
                if k not in s.hijos:
                    s.hijos[k] = cont
                    return True
                s.hijos[k] = cont
            elif s.hijos.pop(k, None) is not None:
                return True
        elif t == "m.room.member" and k:
            if cont.get("membership") in ("join", "invite"):
                nombre = cont.get("displayname") or ""
                s.miembros[k] = nombre
                if nombre:
                    self.nombres[k] = nombre
            else:
                s.miembros.pop(k, None)
        return False

    def _evento(self, s: Sala, ev: dict, c: Cambios, al_final: bool = True) -> Evento | None:
        t = ev.get("type", "")
        cont = ev.get("content") or {}
        rel = cont.get("m.relates_to") or {}
        eid = ev.get("event_id", "")
        if t == "m.room.redaction":
            objetivo = cont.get("redacts") or ev.get("redacts")
            self._borrar(s, objetivo, c)
            return None
        if t == "m.reaction" and rel.get("rel_type") == "m.annotation":
            destino = s.indice.get(rel.get("event_id", ""))
            clave = rel.get("key", "")
            self.reacciones[eid] = (rel.get("event_id", ""), clave, ev.get("sender", ""))
            if destino and not (ev.get("unsigned") or {}).get("redacted_because"):
                destino.reacciones.setdefault(clave, {})[ev.get("sender", "")] = eid
                c.actualizados.add(destino.event_id)
                c.salas.add(s.room_id)
            return None
        if rel.get("rel_type") == "m.replace" and "m.new_content" in cont:
            destino = s.indice.get(rel.get("event_id", ""))
            if destino and destino.sender == ev.get("sender"):
                destino.contenido = {**cont["m.new_content"],
                                     "m.relates_to": destino.contenido.get("m.relates_to", {})}
                destino.editado = True
                c.actualizados.add(destino.event_id)
                c.salas.add(s.room_id)
            return None
        if "state_key" in ev or t not in MENSAJES:
            return None
        pendiente = self._confirmar_eco(s, ev)
        if pendiente:
            c.actualizados.add(pendiente.event_id)
            return None
        nuevo = Evento(eid, ev.get("sender", ""), ev.get("origin_server_ts", 0), t, cont,
                       borrado=bool((ev.get("unsigned") or {}).get("redacted_because")) or not cont)
        if s.agregar(nuevo, al_final):
            c.salas.add(s.room_id)
            return nuevo
        return None

    def _confirmar_eco(self, s: Sala, ev: dict) -> Evento | None:
        """Si es un mensaje nuestro que ya estaba como eco local ("~txn"), lo confirma."""
        if ev.get("sender") != self.yo:
            return None
        eid = ev.get("event_id", "")
        txn = (ev.get("unsigned") or {}).get("transaction_id")
        e = s.indice.pop("~" + txn, None) if txn else None
        if e:
            e.event_id = eid
            s.indice[eid] = e
        else:
            e = s.indice.get(eid)
            if not (e and e.pendiente):
                return None
        e.pendiente = False
        e.ts = ev.get("origin_server_ts", e.ts)
        return e

    def _borrar(self, s: Sala, objetivo: str | None, c: Cambios) -> None:
        if not objetivo:
            return
        if objetivo in self.reacciones:
            destino_id, clave, sender = self.reacciones.pop(objetivo)
            destino = s.indice.get(destino_id)
            if destino and clave in destino.reacciones:
                destino.reacciones[clave].pop(sender, None)
                if not destino.reacciones[clave]:
                    del destino.reacciones[clave]
                c.actualizados.add(destino_id)
                c.salas.add(s.room_id)
            return
        e = s.indice.get(objetivo)
        if e:
            e.borrado = True
            e.contenido = {}
            e.reacciones.clear()
            c.actualizados.add(objetivo)
            c.salas.add(s.room_id)

    # --- eco local ---

    def eco(self, sid: str, event_id: str, contenido: dict, ts: int) -> Evento:
        e = Evento(event_id, self.yo, ts, "m.room.message", contenido, pendiente=True)
        self.salas[sid].agregar(e)
        return e

    def cambiar_id(self, sid: str, viejo: str, nuevo: str) -> Evento | None:
        """El eco local recibe su event_id real al terminar el envío."""
        s = self.salas[sid]
        e = s.indice.pop(viejo, None)
        if not e:  # el sync ya lo confirmó y le puso su id
            return s.indice.get(nuevo)
        if nuevo in s.indice:  # el sync lo trajo antes, sin reconocerlo como eco
            s.eventos.remove(e)
            real = s.indice[nuevo]
            real.pendiente = False
            return real
        e.event_id = nuevo
        e.pendiente = False
        s.indice[nuevo] = e
        return e

    # --- árbol de espacios ---

    def raices(self) -> list[Sala]:
        """Espacios de primer nivel (que no son hijos de otro espacio al que pertenezco)."""
        hijos = {h for s in self.salas.values() if s.es_espacio for h in s.hijos}
        return [s for s in self.salas.values() if s.es_espacio and s.room_id not in hijos
                and not s.invitacion]

    def sueltas(self) -> list[Sala]:
        """Salas que no están en ningún espacio."""
        hijos = {h for s in self.salas.values() if s.es_espacio for h in s.hijos}
        return [s for s in self.salas.values() if not s.es_espacio and s.room_id not in hijos
                and not s.invitacion]

    def menciona(self, e: Evento) -> bool:
        local = self.yo.split(":")[0].lstrip("@")
        nombre = self.nombres.get(self.yo, "")
        texto = e.contenido.get("body", "")
        if self.yo in texto or self.yo in e.contenido.get("formatted_body", ""):
            return True
        return any(n and re.search(rf"\b{re.escape(n)}\b", texto, re.I) for n in (local, nombre))
