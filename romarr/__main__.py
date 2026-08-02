"""Entry point: `python -m romarr`."""
import logging
import os

from .app import serve


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    serve(port=int(os.environ.get("ROMARR_PORT", "6868")))


if __name__ == "__main__":
    main()
