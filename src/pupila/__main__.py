"""Punto de entrada: `pupila` (o `python -m pupila`)."""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    from . import __version__, config

    ap = argparse.ArgumentParser(prog="pupila", description="Cliente de Matrix para la terminal.")
    ap.add_argument("--version", action="version", version=f"pupila {__version__}")
    ap.add_argument("--salir", action="store_true", help="cierra la sesión guardada y termina")
    args = ap.parse_args()
    if args.salir:
        import asyncio

        from .matrix import Matrix

        s = config.leer_sesion()
        if s:
            async def fuera() -> None:
                mx = Matrix(s["homeserver"], s["token"], s["user_id"])
                try:
                    await mx.salir()
                finally:
                    await mx.cerrar()
            try:
                asyncio.run(fuera())
            except Exception as e:
                print(f"El servidor no respondió ({e}); igual borro la sesión local.")
            config.borrar_sesion()
        print("Sesión cerrada.")
        return 0

    from .app import Pupila  # importa textual-image, que pregunta a la terminal antes de arrancar

    Pupila(config.cargar()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
