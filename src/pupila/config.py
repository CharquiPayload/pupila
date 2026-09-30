"""Dónde vive cada cosa de Pupila y su configuración.

- ~/.config/pupila/config.toml   preferencias (se crea con valores por defecto)
- ~/.config/pupila/pupila.tcss   estilo propio, encima del de fábrica (opcional)
- ~/.local/state/pupila/sesion.json   token de la sesión (solo lo lee tu usuario)
- ~/.cache/pupila/               imágenes y archivos descargados
"""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _xdg(var: str, defecto: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / defecto) / "pupila"


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config")
ESTADO_DIR = _xdg("XDG_STATE_HOME", ".local/state")
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache")
CONFIG = CONFIG_DIR / "config.toml"
ESTILO = CONFIG_DIR / "pupila.tcss"
SESION = ESTADO_DIR / "sesion.json"

CONFIG_INICIAL = """\
# Pupila: preferencias. Se leen al abrir; los cambios de estilo van en pupila.tcss
# (en esta misma carpeta), que se recarga en vivo mientras Pupila está abierta.

[avisos]
activos = true          # notificaciones de escritorio (notify-send)
con_texto = true        # mostrar el mensaje en la notificación
campana = false         # además, la campana de la terminal

[imagenes]
activas = true          # mostrar imágenes dentro de la terminal
alto = 12               # alto en líneas
estilo = "auto"         # "auto": nítidas si la terminal puede (foot, kitty), bloques si no
                        # "bloques": siempre en bloques de colores, estilo pixel
                        # "texto": con caracteres, lo más retro

[salas]
# Espacios cuyas salas van por nombre (el resto, por actividad reciente).
por_nombre = []

# Colores de personas concretas: "@usuario:servidor" = "#rrggbb"
[colores]
"""


@dataclass
class Config:
    avisos: bool = True
    avisos_texto: bool = True
    campana: bool = False
    imagenes: bool = True
    alto_imagen: int = 12
    estilo_imagen: str = "auto"
    por_nombre: list[str] = field(default_factory=list)
    colores: dict[str, str] = field(default_factory=dict)


def cargar() -> Config:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        CONFIG.write_text(CONFIG_INICIAL)
    try:
        d = tomllib.loads(CONFIG.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        d = {}
    a, i, s = d.get("avisos", {}), d.get("imagenes", {}), d.get("salas", {})
    return Config(
        avisos=a.get("activos", True), avisos_texto=a.get("con_texto", True),
        campana=a.get("campana", False), imagenes=i.get("activas", True),
        alto_imagen=int(i.get("alto", 12)), estilo_imagen=str(i.get("estilo", "auto")),
        por_nombre=list(s.get("por_nombre", [])),
        colores=dict(d.get("colores", {})),
    )


def leer_sesion() -> dict | None:
    try:
        return json.loads(SESION.read_text())
    except (OSError, ValueError):
        return None


def guardar_sesion(d: dict) -> None:
    ESTADO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SESION.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, SESION)


def borrar_sesion() -> None:
    SESION.unlink(missing_ok=True)
