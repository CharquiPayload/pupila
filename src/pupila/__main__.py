"""Entry point: `pupila` (or `python -m pupila`)."""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    from . import __version__, config

    ap = argparse.ArgumentParser(prog="pupila", description="A Matrix client for the terminal.")
    ap.add_argument("--version", action="version", version=f"pupila {__version__}")
    ap.add_argument("--logout", action="store_true", help="log out of the saved session and exit")
    args = ap.parse_args()
    if args.logout:
        import asyncio

        from .matrix import Matrix

        config.load()  # moves files from older versions into place
        s = config.read_session()
        if s:
            async def out() -> None:
                mx = Matrix(s["homeserver"], s["token"], s["user_id"])
                try:
                    await mx.logout()
                finally:
                    await mx.close()
            try:
                asyncio.run(out())
            except Exception as e:
                print(f"The server didn't answer ({e}); deleting the local session anyway.")
            config.delete_session()
        print("Logged out.")
        return 0

    from .app import Pupila  # imports textual-image, which asks the terminal before starting

    Pupila(config.load()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
