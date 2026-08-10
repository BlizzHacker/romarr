"""Entry point: `python -m romarr`."""
import json
import logging
import os
from pathlib import Path


def load_ha_options(path: str = "/data/options.json") -> dict[str, str]:
    """Home Assistant's configuration, translated into the environment.

    An HA add-on's options arrive as /data/options.json, not as environment
    variables. Reading it here -- upper-cased keys, scalars only -- makes the
    stock image a working add-on with no shell shim in between, and costs
    nothing anywhere else because the file simply does not exist outside HA.
    The real environment wins over the file: a variable someone set on the
    container is the more deliberate act.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for key, value in raw.items():
        if isinstance(value, (str, int, float, bool)) and str(value) != "":
            name = str(key).upper()
            if name not in os.environ:
                out[name] = str(value).lower() if isinstance(value, bool) else str(value)
    return out


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ha = load_ha_options()
    if ha:
        os.environ.update(ha)
        logging.getLogger(__name__).info(
            "Home Assistant options loaded: %s", ", ".join(sorted(ha)))

    from .app import serve
    serve(port=int(os.environ.get("ROMARR_PORT", "6868")))


if __name__ == "__main__":
    main()
