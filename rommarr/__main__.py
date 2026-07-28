"""Entry point: `python -m rommarr`."""
import logging
import os

from .app import serve


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    serve(port=int(os.environ.get("ROMMARR_PORT", "7878")))


if __name__ == "__main__":
    main()
